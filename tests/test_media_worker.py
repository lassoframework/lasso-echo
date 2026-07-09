"""
Ingest worker tests (Stage 2 Part 6). Offline (hooks injected, fake S3).

Asserts: perceptual dedupe catches near-identical re-shots per tenant; R2 keys
and the filed library are tenant-scoped; the caption gate holds media with no
sentence out of the library with one auto-ask, and attach_caption releases it;
consent refusal rejects; held rows are never processed; flag OFF = inert.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, media_inbox, media_worker, ops_alerts, tenants  # noqa: E402


class FakeS3:
    def __init__(self):
        self.keys = []

    def exists(self, key):
        return False

    def put(self, key, local_path):
        self.keys.append(key)


def _tenant(monkeypatch, tmp_path, key, phone):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    out = tenants.intake_create({
        "key": key, "name": key, "avatar": "Families.",
        "voice": {"tone": "Warm."},
        "approver": {"name": "Sam", "phone": phone},
        "sender_phones": [phone], "media_lanes": ["sms"],
    }, base_dir=str(tmp_path))
    assert not out.get("blocked"), out


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEDIA_INBOX_ENABLED", "true")
    monkeypatch.setenv("AGENT_MEDIA_INBOX_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("AGENT_HOSTING_ENABLED", "true")
    monkeypatch.setattr(config, "S3_PUBLIC_BASE_URL", "https://cdn.echo.test")
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path / "library"))


def _wipe():
    with db._lock, media_inbox._conn() as conn:
        conn.execute("DELETE FROM media_inbox")
        conn.executescript(media_worker._PHASH_SCHEMA)
        conn.execute("DELETE FROM media_phashes")
        conn.commit()


def _receive(tmp_path, sender, data, name="a.jpg", text="A caption."):
    return media_inbox.receive({
        "provider": "test", "sender": sender, "text": text,
        "media": [{"name": name, "mime": "image/jpeg", "data": data}],
    }, base_dir=str(tmp_path))


# hooks: deterministic fakes (no PIL in the test env)
def _phash_const(value):
    return lambda data, name: value


def _no_thumb(data, name, out_path):
    return False


# ---- perceptual dedupe ----------------------------------------------------------------------

def test_perceptual_dedupe_catches_near_identical(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path, "wgym", "+13175550401")
    _arm(monkeypatch, tmp_path)
    _receive(tmp_path, "+13175550401", b"SHOT-ONE", name="one.jpg")
    _receive(tmp_path, "+13175550401", b"SHOT-TWO", name="two.jpg")  # different bytes
    out = media_worker.process(s3_client=FakeS3(), phash=_phash_const("SAMEHASH"),
                               thumbnail=_no_thumb, autotag=lambda p: None,
                               base_dir=str(tmp_path))
    # same perceptual hash: the second is a duplicate, only one filed
    assert out["processed"] == 1 and out["duplicates"] == 1
    lib = os.path.join(str(tmp_path / "library"), "wgym")
    assert os.path.isfile(os.path.join(lib, "one.jpg"))
    assert not os.path.exists(os.path.join(lib, "two.jpg"))


def test_phash_scoped_per_tenant(monkeypatch, tmp_path):
    """The same perceptual hash for two DIFFERENT tenants is not a duplicate."""
    _wipe()
    _tenant(monkeypatch, tmp_path, "gym_a", "+13175550402")
    _tenant(monkeypatch, tmp_path, "gym_b", "+13175550403")
    _arm(monkeypatch, tmp_path)
    _receive(tmp_path, "+13175550402", b"TENANT-A-BYTES", name="a.jpg")
    _receive(tmp_path, "+13175550403", b"TENANT-B-BYTES", name="b.jpg")
    out = media_worker.process(s3_client=FakeS3(), phash=_phash_const("SHAREDHASH"),
                               thumbnail=_no_thumb, autotag=lambda p: None,
                               base_dir=str(tmp_path))
    assert out["processed"] == 2 and out["duplicates"] == 0


# ---- tenant scoping --------------------------------------------------------------------------

def test_r2_keys_and_library_are_tenant_scoped(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path, "scopegym", "+13175550404")
    _arm(monkeypatch, tmp_path)
    _receive(tmp_path, "+13175550404", b"SCOPE-BYTES", name="rack.jpg",
             text="The new rack.")
    s3 = FakeS3()
    out = media_worker.process(s3_client=s3, phash=lambda d, n: None,
                               thumbnail=_no_thumb, autotag=lambda p: None,
                               base_dir=str(tmp_path))
    assert out["processed"] == 1
    # R2 key rides the existing media_host tenant isolation
    assert s3.keys and s3.keys[0].startswith("echo/scopegym/")
    # the filed copy + caption sidecar live under the tenant's library folder
    lib = os.path.join(str(tmp_path / "library"), "scopegym")
    assert os.path.isfile(os.path.join(lib, "rack.jpg"))
    note = open(os.path.join(lib, "rack.txt"), encoding="utf-8").read()
    assert note == "The new rack."


# ---- caption gate ----------------------------------------------------------------------------

def test_missing_caption_blocks_and_auto_asks_once(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path, "quietgym", "+13175550405")
    _arm(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    _receive(tmp_path, "+13175550405", b"NO-CAPTION-BYTES", name="mystery.jpg",
             text="")
    out = media_worker.process(s3_client=FakeS3(), phash=lambda d, n: None,
                               thumbnail=_no_thumb, autotag=lambda p: None,
                               base_dir=str(tmp_path))
    assert out["awaiting_caption"] == 1 and out["processed"] == 0
    # not in the library = nothing can draft from it
    lib = os.path.join(str(tmp_path / "library"), "quietgym")
    assert not os.path.exists(os.path.join(lib, "mystery.jpg"))
    assert len(fired) == 1 and "no sentence" in fired[0]
    # a second pass does NOT re-ask
    out2 = media_worker.process(s3_client=FakeS3(), phash=lambda d, n: None,
                                thumbnail=_no_thumb, autotag=lambda p: None,
                                base_dir=str(tmp_path))
    assert out2["awaiting_caption"] == 0    # row is awaiting, not staged
    assert len(fired) == 1
    # cleanup kv stamps
    for row in media_inbox.rows(status="awaiting_caption"):
        with db._lock, db.connect() as conn:
            conn.execute("DELETE FROM kv WHERE key=?", (f"caption_ask_{row['id']}",))
            conn.commit()


def test_attach_caption_releases_the_row(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path, "latergym", "+13175550406")
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: None)
    _receive(tmp_path, "+13175550406", b"LATE-CAPTION-BYTES", name="late.jpg", text="")
    media_worker.process(s3_client=FakeS3(), phash=lambda d, n: None,
                         thumbnail=_no_thumb, autotag=lambda p: None,
                         base_dir=str(tmp_path))
    row = media_inbox.rows(status="awaiting_caption")[0]
    assert media_worker.attach_caption(row["id"], "") is False   # empty refused
    assert media_worker.attach_caption(row["id"], "Team photo after the 6pm class.")
    out = media_worker.process(s3_client=FakeS3(), phash=lambda d, n: None,
                               thumbnail=_no_thumb, autotag=lambda p: None,
                               base_dir=str(tmp_path))
    assert out["processed"] == 1
    lib = os.path.join(str(tmp_path / "library"), "latergym")
    note = open(os.path.join(lib, "late.txt"), encoding="utf-8").read()
    assert note == "Team photo after the 6pm class."
    with db._lock, db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (f"caption_ask_{row['id']}",))
        conn.commit()


# ---- consent + held + flag off ----------------------------------------------------------------

def test_consent_refusal_rejects(monkeypatch, tmp_path):
    _wipe()
    _tenant(monkeypatch, tmp_path, "consentgym", "+13175550407")
    _arm(monkeypatch, tmp_path)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    _receive(tmp_path, "+13175550407", b"CONSENT-BYTES", name="people.jpg")
    out = media_worker.process(
        s3_client=FakeS3(), phash=lambda d, n: None, thumbnail=_no_thumb,
        autotag=lambda p: None,
        consent=lambda d, n, t: (False, "unknown people, consent not recorded"),
        base_dir=str(tmp_path))
    assert out["rejected"] == 1 and out["processed"] == 0
    assert any("consent" in m for m in fired)
    lib = os.path.join(str(tmp_path / "library"), "consentgym")
    assert not os.path.exists(os.path.join(lib, "people.jpg"))


def test_held_rows_never_processed(monkeypatch, tmp_path):
    _wipe()
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: None)
    _receive(tmp_path, "+19998887777", b"HELD-WORKER-BYTES", name="held.jpg")
    out = media_worker.process(s3_client=FakeS3(), phash=lambda d, n: None,
                               thumbnail=_no_thumb, autotag=lambda p: None,
                               base_dir=str(tmp_path))
    assert out == {"processed": 0, "duplicates": 0, "rejected": 0,
                   "awaiting_caption": 0}
    assert media_inbox.rows(status="held")   # still held, untouched


def test_flag_off_inert(monkeypatch):
    monkeypatch.delenv("AGENT_MEDIA_INBOX_ENABLED", raising=False)
    assert media_worker.process() is None
