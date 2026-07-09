"""
Media inbox core tests (Stage 2 Part 5). Offline.

Asserts: a known sender's media stages routed to its tenant; the same bytes
received twice insert nothing (idempotent by content hash, webhook retries
safe); an unknown sender is HELD with one ops alert per sender per day and
never routed; the texted sentence rides as the caption note; flag OFF = inert.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import db, media_inbox, ops_alerts, tenants  # noqa: E402


def _tenant(monkeypatch, tmp_path, key="inboxgym", phone="+13175550301"):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    out = tenants.intake_create({
        "key": key, "name": "Inbox Gym", "avatar": "Families.",
        "voice": {"tone": "Warm."},
        "approver": {"name": "Sam", "phone": phone},
        "sender_phones": [phone], "media_lanes": ["sms"],
    }, base_dir=str(tmp_path))
    assert not out.get("blocked"), out
    return key


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEDIA_INBOX_ENABLED", "true")
    monkeypatch.setenv("AGENT_MEDIA_INBOX_DIR", str(tmp_path / "staging"))


def _payload(sender="+13175550301", data=b"JPGBYTES-A", name="a.jpg",
             text="New squat rack day."):
    return {"provider": "test", "sender": sender, "text": text,
            "media": [{"name": name, "mime": "image/jpeg", "data": data}]}


def _wipe(sha_prefix=None):
    with db._lock, media_inbox._conn() as conn:
        conn.execute("DELETE FROM media_inbox")
        conn.commit()


# ---- routing ---------------------------------------------------------------------------

def test_known_sender_routes_to_tenant(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    out = media_inbox.receive(_payload(data=b"ROUTE-BYTES-1"), base_dir=str(tmp_path))
    assert out["tenant"] == "inboxgym"
    assert out["staged"] == 1 and out["held"] == 0
    row = media_inbox.rows(tenant_key="inboxgym")[0]
    assert row["status"] == "staged"
    assert row["caption_note"] == "New squat rack day."
    assert os.path.isfile(row["staged_path"])


def test_same_bytes_twice_insert_nothing(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    p = _payload(data=b"IDEMPOTENT-BYTES")
    first = media_inbox.receive(p, base_dir=str(tmp_path))
    second = media_inbox.receive(p, base_dir=str(tmp_path))  # webhook retry
    assert first["staged"] == 1
    assert second["staged"] == 0 and second["duplicates"] == 1
    assert len(media_inbox.rows(tenant_key="inboxgym")) == 1


def test_unknown_sender_held_with_one_alert(monkeypatch, tmp_path):
    _wipe()
    _arm(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    # clear the per-day alert stamp for this sender
    import hashlib
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).date().isoformat()
    stamp = (f"inbox_unknown_alerted_"
             f"{hashlib.sha1(b'+19999990000').hexdigest()[:12]}_{day}")
    with db._lock, db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (stamp,))
        conn.commit()

    out = media_inbox.receive(_payload(sender="+19999990000", data=b"HELD-BYTES-1"),
                              base_dir=str(tmp_path))
    assert out["tenant"] == ""
    assert out["held"] == 1 and out["staged"] == 0
    row = media_inbox.rows(status="held")[0]
    assert row["tenant_key"] == ""
    # one alert, masked phone, never the full number
    assert len(fired) == 1
    assert "HELD" in fired[0]
    assert "0000" in fired[0] and "+19999990000" not in fired[0]
    # a second batch from the same sender the same day: held again, NO new alert
    out2 = media_inbox.receive(_payload(sender="+19999990000", data=b"HELD-BYTES-2"),
                               base_dir=str(tmp_path))
    assert out2["held"] == 1
    assert len(fired) == 1
    with db._lock, db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (stamp,))
        conn.commit()


def test_caption_note_rides_every_item_in_batch(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path)
    _arm(monkeypatch, tmp_path)
    out = media_inbox.receive({
        "provider": "test", "sender": "+13175550301", "text": "Grand opening set.",
        "media": [{"name": "a.jpg", "mime": "image/jpeg", "data": b"BATCH-A"},
                  {"name": "b.jpg", "mime": "image/jpeg", "data": b"BATCH-B"}],
    }, base_dir=str(tmp_path))
    assert out["staged"] == 2
    for row in media_inbox.rows(tenant_key="inboxgym"):
        assert row["caption_note"] == "Grand opening set."


def test_flag_off_inert(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MEDIA_INBOX_ENABLED", raising=False)
    assert media_inbox.receive(_payload(), base_dir=str(tmp_path)) is None
