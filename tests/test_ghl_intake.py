"""
GHL intake adapter tests (Stage 2 Part 7). Offline (verifier + fetch + replier
injected).

Asserts: signature verifies BEFORE parsing (a bad signature refuses the payload
and nothing is fetched or staged); photos are downloaded immediately and ride
the inbox queue; a video MIME auto-replies with the tenant's tokenized upload
link and is never downloaded; unknown senders are never texted; the default
verifier refuses when the public key env is absent; flag OFF = inert.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, ghl_intake, media_inbox, ops_alerts, tenants  # noqa: E402

_OK = lambda sig, body: True     # noqa: E731
_BAD = lambda sig, body: False   # noqa: E731


def _tenant(monkeypatch, tmp_path, key="ghlgym", phone="+13175550501"):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    out = tenants.intake_create({
        "key": key, "name": key, "avatar": "Families.",
        "voice": {"tone": "Warm."},
        "approver": {"name": "Sam", "phone": phone},
        "sender_phones": [phone], "media_lanes": ["sms"],
    }, base_dir=str(tmp_path))
    assert not out.get("blocked"), out


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_GHL_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_MEDIA_INBOX_ENABLED", "true")
    monkeypatch.setenv("AGENT_MEDIA_INBOX_DIR", str(tmp_path / "staging"))


def _wipe():
    with db._lock, media_inbox._conn() as conn:
        conn.execute("DELETE FROM media_inbox")
        conn.commit()


def _event(sender="+13175550501", text="New rack.", attachments=None):
    return json.dumps({"phone": sender, "body": text,
                       "attachments": attachments or []}).encode("utf-8")


# ---- signature first ------------------------------------------------------------------------

def test_bad_signature_refuses_before_anything(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: None)
    fetched = []
    out = ghl_intake.handle_webhook(
        {"X-GHL-Signature": "forged"}, _event(
            attachments=[{"url": "http://x/a.jpg", "mime": "image/jpeg"}]),
        verifier=_BAD, fetch=lambda u: fetched.append(u) or b"X",
        base_dir=str(tmp_path))
    assert out == {"ok": False, "reason": "signature refused"}
    assert fetched == []                      # nothing downloaded
    assert media_inbox.rows() == []           # nothing staged


def test_default_verifier_refuses_without_public_key(monkeypatch):
    monkeypatch.delenv("AGENT_GHL_PUBLIC_KEY", raising=False)
    assert ghl_intake._verify_default("c2ln", b"body") is False
    assert ghl_intake._verify_default("", b"body") is False


# ---- photos captured immediately -------------------------------------------------------------

def test_photo_fetched_immediately_and_staged(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return b"PHOTO-BYTES-GHL"

    out = ghl_intake.handle_webhook(
        {"X-GHL-Signature": "good"}, _event(
            text="Front desk redo.",
            attachments=[{"url": "http://cdn.carrier/x.jpg?sig=1",
                          "mime": "image/jpeg", "name": "desk.jpg"}]),
        verifier=_OK, fetch=fetch, base_dir=str(tmp_path))
    assert out["ok"] is True and out["photos"] == 1
    assert fetched == ["http://cdn.carrier/x.jpg?sig=1"]
    rows = media_inbox.rows(tenant_key="ghlgym")
    assert len(rows) == 1
    assert rows[0]["provider"] == "ghl"
    assert rows[0]["caption_note"] == "Front desk redo."


def test_expired_photo_url_alerts_and_continues(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))

    def fetch(url):
        if "dead" in url:
            raise RuntimeError("410 gone")
        return b"ALIVE-BYTES-GHL"

    out = ghl_intake.handle_webhook(
        {"X-GHL-Signature": "good"}, _event(attachments=[
            {"url": "http://cdn/dead.jpg", "mime": "image/jpeg", "name": "dead.jpg"},
            {"url": "http://cdn/alive.jpg", "mime": "image/jpeg", "name": "alive.jpg"},
        ]),
        verifier=_OK, fetch=fetch, base_dir=str(tmp_path))
    assert out["photos"] == 1                       # the live one still landed
    assert any("expired" in m or "resend" in m for m in fired)


# ---- video: auto-reply with the tokenized link, never downloaded ------------------------------

def test_video_auto_replies_with_upload_link(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_UPLOAD_BASE_URL", "https://up.echo.test")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GHLGYM", "tok_ghlgym_secret")
    fetched, replies = [], []
    out = ghl_intake.handle_webhook(
        {"X-GHL-Signature": "good"}, _event(attachments=[
            {"url": "http://cdn/clip.mp4", "mime": "video/mp4", "name": "clip.mp4"}]),
        verifier=_OK, fetch=lambda u: fetched.append(u) or b"X",
        replier=lambda phone, text: replies.append((phone, text)),
        base_dir=str(tmp_path))
    assert out["videos"] == 1 and out["photos"] == 0
    assert fetched == []                            # video never pulled via carrier
    assert len(replies) == 1
    phone, text = replies[0]
    assert phone == "+13175550501"
    assert "https://up.echo.test/u/tok_ghlgym_secret" in text


def test_video_from_unknown_sender_never_texted(monkeypatch, tmp_path):
    _wipe()
    _arm(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    replies = []
    out = ghl_intake.handle_webhook(
        {"X-GHL-Signature": "good"},
        _event(sender="+19998880000", attachments=[
            {"url": "http://cdn/clip.mp4", "mime": "video/mp4"}]),
        verifier=_OK, replier=lambda p, t: replies.append((p, t)),
        base_dir=str(tmp_path))
    assert out["videos"] == 1
    assert replies == []                             # unknown numbers never texted
    assert any("unmapped" in m for m in fired)


# ---- flag off ---------------------------------------------------------------------------------

def test_flag_off_inert(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_GHL_INTAKE_ENABLED", raising=False)
    assert ghl_intake.handle_webhook({}, b"{}", verifier=_OK,
                                     base_dir=str(tmp_path)) is None
