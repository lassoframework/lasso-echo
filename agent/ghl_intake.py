"""
GHL (GoHighLevel) intake adapter (Stage 2 Part 7).

Dormant behind AGENT_GHL_INTAKE_ENABLED (default OFF: handle_webhook refuses,
nothing is verified, fetched, staged, or replied). Armed, one inbound GHL
message webhook flows:

  1. SIGNATURE FIRST: the X-GHL-Signature header is an Ed25519 signature over
     the raw body, verified against the GHL public key (env AGENT_GHL_PUBLIC_KEY,
     PEM; read lazily, never logged). No signature or a bad signature = the
     payload is REFUSED before a byte of it is parsed. The verifier is
     injectable for tests; the default needs the `cryptography` package.
  2. PHOTOS ARE CAPTURED IMMEDIATELY: carrier attachment URLs expire, so every
     image attachment is downloaded in the webhook handler and handed to the
     media inbox queue (Part 5) as bytes. Routing, hold rules, hash idempotency
     and the caption note all ride the queue.
  3. VIDEO IS NOT PULLED THROUGH THE CARRIER: a video MIME triggers ONE
     auto-reply (replier hook) carrying the tenant's tokenized upload link, so
     the original lands at full quality through the Part 9 endpoint instead of
     a recompressed MMS. Unknown senders never get texted; their event raises
     the inbox's unknown-sender hold/alert path instead.

Nothing here publishes or drafts. The reply hook is an injected callable
(replier(phone, text)); wiring it to a real GHL send is a by-hand step.
"""

import base64
import os

from . import config, intake_tokens, media_inbox, ops_alerts, tenants

_UPLOAD_TOKEN_ENV_PREFIX = "AGENT_INTAKE_TOKEN_"


def _verify_default(signature_b64, body_bytes):
    """Ed25519 verification against AGENT_GHL_PUBLIC_KEY (PEM). False on ANY
    problem: missing key, bad base64, bad signature. Never raises, never logs
    key material."""
    pem = os.environ.get("AGENT_GHL_PUBLIC_KEY", "")
    if not pem or not signature_b64:
        return False
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key  # lazy
        key = load_pem_public_key(pem.encode("utf-8"))
        key.verify(base64.b64decode(signature_b64), body_bytes)
        return True
    except Exception:
        return False


def upload_link_for(tenant_key):
    """The tenant's tokenized upload link, or '' when it cannot be built (the reply
    then says the link is coming rather than sending a dead URL). The link is
    SIGNED with the shared secret, so no per-tenant env var is needed. A legacy
    AGENT_INTAKE_TOKEN_<KEY> value, if still set, is honored as a pinned override
    so a tenant already on the old link keeps it during the cutover."""
    base = os.environ.get("AGENT_UPLOAD_BASE_URL", "").rstrip("/")
    if not base:
        return ""
    legacy = os.environ.get(f"{_UPLOAD_TOKEN_ENV_PREFIX}{tenant_key.upper()}", "")
    if legacy:
        return f"{base}/u/{legacy}"
    try:
        return f"{base}/u/{intake_tokens.mint(tenant_key)}"
    except ValueError:
        return ""   # no signing secret set yet


def _video_reply(tenant_key):
    link = upload_link_for(tenant_key)
    if link:
        return ("Got your video. Texted video gets crushed by the carrier, so "
                f"please drop the original here and it lands at full quality: {link}")
    return ("Got your video. Texted video gets crushed by the carrier; we will "
            "send you a private upload link for the original.")


def handle_webhook(headers, body, verifier=None, fetch=None, replier=None,
                   base_dir=None):
    """
    One GHL webhook. Returns a summary dict, or None while the flag is OFF.
      {"ok": False, "reason": ...}                       refused (signature)
      {"ok": True, "photos": n, "videos": n, "inbox": {...}|None}
    body is the RAW bytes the signature covers; parsing happens only after the
    signature verifies.
    """
    if not config.ghl_intake_enabled():
        return None
    verifier = verifier or _verify_default
    sig = (headers or {}).get("X-GHL-Signature", "") or (headers or {}).get(
        "x-ghl-signature", "")
    raw = body if isinstance(body, bytes) else str(body or "").encode("utf-8")
    if not verifier(sig, raw):
        ops_alerts.alert("ghl intake: webhook REFUSED, X-GHL-Signature missing "
                         "or invalid. Payload not parsed.")
        return {"ok": False, "reason": "signature refused"}

    import json
    try:
        event = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"ok": False, "reason": "body is not JSON"}

    sender = str(event.get("phone", "") or event.get("from", "") or "")
    text = str(event.get("body", "") or event.get("message", "") or "").strip()
    attachments = event.get("attachments") or []

    photos, videos, media_items = 0, 0, []
    video_seen = False
    for att in attachments:
        url = str(att.get("url", "") or "")
        mime = str(att.get("mime", "") or att.get("contentType", "") or "").lower()
        name = os.path.basename(str(att.get("name", "") or url.split("?")[0] or "media"))
        if mime.startswith("video/"):
            videos += 1
            video_seen = True
            continue  # never pulled through the carrier; the upload link handles it
        if not mime.startswith("image/"):
            continue  # documents etc. are out of scope for the media lanes
        if fetch is None:
            continue  # no fetcher wired (tests or dry probes): nothing captured
        try:
            data = fetch(url)  # IMMEDIATE: carrier URLs expire
        except Exception as e:
            ops_alerts.alert(f"ghl intake: photo download failed ({name}): "
                             f"{type(e).__name__}: {e}. The carrier URL may have "
                             "expired; ask the client to resend.")
            continue
        if data:
            media_items.append({"name": name, "mime": mime, "data": data})
            photos += 1

    inbox_result = None
    if media_items:
        inbox_result = media_inbox.receive(
            {"provider": "ghl", "sender": sender, "text": text,
             "media": media_items}, base_dir=base_dir)

    if video_seen:
        tenant_key = tenants.tenant_for_sender(sender, base_dir=base_dir)
        if tenant_key and replier is not None:
            replier(sender, _video_reply(tenant_key))
        elif not tenant_key:
            # unknown senders are never texted back; hold + alert instead
            media_inbox.receive({"provider": "ghl", "sender": sender,
                                 "text": text, "media": []}, base_dir=base_dir)
            ops_alerts.alert("ghl intake: video from an unmapped sender; no "
                             "reply sent (unknown numbers are never texted). "
                             "Map the phone to a tenant first.")

    return {"ok": True, "photos": photos, "videos": videos, "inbox": inbox_result}
