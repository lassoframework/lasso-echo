"""
WhatsApp intake adapter tests (Stage 2 Part 8). Offline; signatures are REAL
HMAC-SHA256 values computed in the tests (stdlib), fetch_media injected.

Asserts: a correctly signed webhook downloads media and stages it through the
same inbox queue; a tampered body refuses before parsing; media over the 16MB
WABA ceiling is refused (never truncated) with one alert; the caption rides;
flag OFF = inert.
"""

import hashlib
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, media_inbox, ops_alerts, tenants, whatsapp_intake  # noqa: E402

SECRET = "test_app_secret_wa"


def _sign(body):
    mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={mac}"}


def _tenant(monkeypatch, tmp_path, key="wagym", phone="+13175550601"):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    out = tenants.intake_create({
        "key": key, "name": key, "avatar": "Families.",
        "voice": {"tone": "Warm."},
        "approver": {"name": "Sam", "phone": phone},
        "sender_phones": [phone], "media_lanes": ["whatsapp"],
    }, base_dir=str(tmp_path))
    assert not out.get("blocked"), out


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WHATSAPP_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_MEDIA_INBOX_ENABLED", "true")
    monkeypatch.setenv("AGENT_MEDIA_INBOX_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("AGENT_WHATSAPP_APP_SECRET", SECRET)


def _wipe():
    with db._lock, media_inbox._conn() as conn:
        conn.execute("DELETE FROM media_inbox")
        conn.commit()


def _event(sender="13175550601", media_id="m123", caption="Class photo."):
    return json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "type": "image",
         "image": {"id": media_id, "caption": caption}}]}}]}]}).encode()


def _fetch(data=b"WA-MEDIA-BYTES"):
    return lambda media_id: (data, "image/jpeg", f"wa_{media_id}.jpg")


# ---- signature ---------------------------------------------------------------------------

def test_valid_signature_processes_and_stages(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    body = _event()
    out = whatsapp_intake.handle_webhook(_sign(body), body,
                                         fetch_media=_fetch(b"WA-STAGE-BYTES"),
                                         base_dir=str(tmp_path))
    assert out["ok"] is True and out["media"] == 1
    rows = media_inbox.rows(tenant_key="wagym")
    assert len(rows) == 1
    assert rows[0]["provider"] == "whatsapp"
    assert rows[0]["caption_note"] == "Class photo."


def test_tampered_body_refused_before_parsing(monkeypatch, tmp_path):
    _wipe()
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: None)
    body = _event()
    headers = _sign(body)
    tampered = body.replace(b"m123", b"m666")
    fetched = []
    out = whatsapp_intake.handle_webhook(
        headers, tampered,
        fetch_media=lambda mid: fetched.append(mid) or (b"X", "image/jpeg", "x.jpg"),
        base_dir=str(tmp_path))
    assert out == {"ok": False, "reason": "signature refused"}
    assert fetched == []
    assert media_inbox.rows() == []


def test_missing_secret_refuses(monkeypatch, tmp_path):
    _wipe()
    monkeypatch.setenv("AGENT_WHATSAPP_INTAKE_ENABLED", "true")
    monkeypatch.delenv("AGENT_WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: None)
    body = _event()
    out = whatsapp_intake.handle_webhook(_sign(body), body,
                                         fetch_media=_fetch(),
                                         base_dir=str(tmp_path))
    assert out["ok"] is False


# ---- the 16MB ceiling ----------------------------------------------------------------------

def test_oversize_media_refused_never_truncated(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    big = b"X" * (whatsapp_intake.MAX_MEDIA_BYTES + 1)
    body = _event()
    out = whatsapp_intake.handle_webhook(_sign(body), body,
                                         fetch_media=_fetch(big),
                                         base_dir=str(tmp_path))
    assert out["oversize"] == 1 and out["media"] == 0
    assert media_inbox.rows(tenant_key="wagym") == []
    assert any("16MB" in m for m in fired)


# ---- same queue: routing + idempotency ride along -------------------------------------------

def test_retry_of_same_media_inserts_nothing(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    body = _event(media_id="m777")
    fetch = _fetch(b"WA-RETRY-BYTES")
    first = whatsapp_intake.handle_webhook(_sign(body), body, fetch_media=fetch,
                                           base_dir=str(tmp_path))
    second = whatsapp_intake.handle_webhook(_sign(body), body, fetch_media=fetch,
                                            base_dir=str(tmp_path))
    assert first["inbox"]["staged"] == 1
    assert second["inbox"]["duplicates"] == 1 and second["inbox"]["staged"] == 0
    assert len(media_inbox.rows(tenant_key="wagym")) == 1


# ---- flag off --------------------------------------------------------------------------------

def test_flag_off_inert(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_WHATSAPP_INTAKE_ENABLED", raising=False)
    body = _event()
    assert whatsapp_intake.handle_webhook(_sign(body), body,
                                          fetch_media=_fetch(),
                                          base_dir=str(tmp_path)) is None
