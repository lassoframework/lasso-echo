"""
WhatsApp (WABA) intake adapter (Stage 2 Part 8).

Dormant behind AGENT_WHATSAPP_INTAKE_ENABLED (default OFF: handle_webhook
refuses, nothing is verified, downloaded, or staged).

NOTE BEFORE ARMING: receiving client media over WhatsApp Business requires the
`whatsapp_business_messaging` permission via Meta App Review IN ADDITION to the
existing app permissions. Do NOT arm this flag until that review is granted;
until then the webhook simply never fires in production.

Armed, one inbound WABA webhook flows:
  1. SIGNATURE FIRST: X-Hub-Signature-256 is an HMAC-SHA256 of the raw body
     keyed with the app secret (env name AGENT_WHATSAPP_APP_SECRET, read
     lazily, never logged). Constant-time compare; no match = REFUSED before
     parsing.
  2. Media messages resolve their WABA media id to bytes via the injectable
     fetch_media hook (the default calls the Graph media endpoint with the
     token named by AGENT_WHATSAPP_TOKEN_ENV; the token never lands in a log).
     Downloads are CAPPED AT 16MB (the WABA media ceiling): anything larger is
     refused with one alert, never truncated.
  3. Everything lands through the SAME Part 5 queue (provider "whatsapp"):
     sender routing, unknown-sender hold, hash idempotency, and the caption
     note (the message caption, else the text body) all ride the queue.

Nothing here publishes or drafts.
"""

import hashlib
import hmac
import os

from . import config, media_inbox, ops_alerts

MAX_MEDIA_BYTES = 16 * 1024 * 1024  # the WABA media ceiling; never truncated

WHATSAPP_TOKEN_ENV = "AGENT_WHATSAPP_TOKEN"  # env var NAME, never the value


def verify_signature(header_value, body_bytes, secret=None):
    """
    True when X-Hub-Signature-256 ('sha256=<hex>') matches HMAC-SHA256(body,
    app secret). Constant-time compare. False on ANY problem: missing header,
    missing secret, wrong format, no match. Never raises, never logs the secret.
    """
    secret = secret if secret is not None else os.environ.get(
        "AGENT_WHATSAPP_APP_SECRET", "")
    if not secret or not header_value or not str(header_value).startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"),
                        body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(header_value)[len("sha256="):], expected)


def _default_fetch_media(media_id):
    """(bytes, mime, name) for one WABA media id via the Graph endpoint. The
    token is read lazily by env NAME and passed only into the request headers."""
    token = os.environ.get(WHATSAPP_TOKEN_ENV, "")
    if not token:
        raise RuntimeError("WABA token env is not set; cannot fetch media")
    import requests  # lazy
    headers = {"Authorization": f"Bearer {token}"}
    meta = requests.get(f"{config.GRAPH_API_BASE}/{media_id}",
                        headers=headers, timeout=30)
    meta.raise_for_status()
    info = meta.json() or {}
    url, mime = info.get("url", ""), info.get("mime_type", "")
    blob = requests.get(url, headers=headers, timeout=120)
    blob.raise_for_status()
    ext = (mime.split("/") + [""])[1].split(";")[0] or "bin"
    return blob.content, mime, f"wa_{media_id}.{ext}"


def _messages(event):
    """Every message object in a WABA webhook event, flattened."""
    out = []
    for entry in (event.get("entry") or []):
        for change in (entry.get("changes") or []):
            out.extend((change.get("value") or {}).get("messages") or [])
    return out


def handle_webhook(headers, body, fetch_media=None, base_dir=None, secret=None,
                   http=None):
    """
    One WABA webhook. Returns a summary dict, or None while the flag is OFF.
      {"ok": False, "reason": ...}                 refused (signature / not JSON)
      {"ok": True, "media": n, "oversize": n, "inbox": {...}|None}
    body is the RAW bytes the signature covers; parsing happens only after the
    signature verifies.
    http is the injectable HTTP client for send_receipt (default: requests).
    """
    if not config.whatsapp_intake_enabled():
        return None
    raw = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
    sig = (headers or {}).get("X-Hub-Signature-256", "") or (headers or {}).get(
        "x-hub-signature-256", "")
    if not verify_signature(sig, raw, secret=secret):
        ops_alerts.alert("whatsapp intake: webhook REFUSED, X-Hub-Signature-256 "
                         "missing or invalid. Payload not parsed.")
        return {"ok": False, "reason": "signature refused"}

    import json
    try:
        event = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"ok": False, "reason": "body is not JSON"}

    fetch_media = fetch_media or _default_fetch_media
    _http = http  # injectable for send_receipt; None = real requests
    counted, oversize, batches = 0, 0, {}
    for msg in _messages(event):
        sender = str(msg.get("from", "") or "")
        mtype = str(msg.get("type", "") or "")
        if mtype not in ("image", "video"):
            continue
        media = msg.get(mtype) or {}
        media_id = str(media.get("id", "") or "")
        caption = str(media.get("caption", "") or
                      (msg.get("text") or {}).get("body", "") or "").strip()
        if not media_id:
            continue
        try:
            data, mime, name = fetch_media(media_id)
        except Exception as e:
            ops_alerts.alert(f"whatsapp intake: media fetch failed for id "
                             f"{media_id}: {type(e).__name__}: {e}")
            continue
        if len(data) > MAX_MEDIA_BYTES:
            oversize += 1
            ops_alerts.alert(f"whatsapp intake: media {name} is over the 16MB "
                             "WABA ceiling; refused, never truncated. Ask for "
                             "the original via the upload link.")
            continue
        key = (sender, caption)
        batches.setdefault(key, []).append(
            {"name": name, "mime": mime, "data": data})
        counted += 1

    sent_receipt_to = set()
    inbox_result = None
    for (sender, caption), items in batches.items():
        inbox_result = media_inbox.receive(
            {"provider": "whatsapp", "sender": f"+{sender.lstrip('+')}",
             "text": caption, "media": items}, base_dir=base_dir)
        if sender not in sent_receipt_to:
            send_receipt(sender, http=_http)
            sent_receipt_to.add(sender)
    return {"ok": True, "media": counted, "oversize": oversize,
            "inbox": inbox_result}


def send_receipt(to_number, http=None):
    """Send a single templated receipt reply to the sender after media staging.
    Called at most once per unique sender per webhook. Only fires after successful
    media ingest; never auto-replies to non-media messages. Token and phone
    number id are read lazily from env; if either is missing, one ops alert fires
    and the function returns None without raising.
    """
    token = os.environ.get("AGENT_WHATSAPP_TOKEN", "")
    phone_number_id = os.environ.get("AGENT_WHATSAPP_PHONE_NUMBER_ID", "")
    if not token or not phone_number_id:
        ops_alerts.alert("whatsapp receipt: token or phone id not set")
        return None
    import json as _json
    payload = _json.dumps({
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {
            "body": (
                "Got it! Your media is in review. "
                "We will reach out if we need anything else."
            )
        },
    }).encode("utf-8")
    url = f"{config.GRAPH_API_BASE}/{phone_number_id}/messages"
    headers_out = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    http = http or _default_http()
    try:
        resp = http.post(url, data=payload, headers=headers_out, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        ops_alerts.alert(f"whatsapp receipt: send failed to {to_number}: "
                         f"{type(e).__name__}: {e}")
        return None
    return True


def _default_http():
    import requests
    return requests
