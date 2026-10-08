"""
Intake-web inventory mutation guard tests. Fully OFFLINE: a fake R2 client, a
fake mutation authority (no PG), real tmp library/SQLite/journal paths.

Covers the begin/complete receipt fence around tenant-bound R2 writes in
agent/intake_web.py:
  - guarded upload: media bytes + upload manifest write under the protocol and
    complete with a receipt,
  - pending-on-uncertain: an uncertain begin/complete never returns success and
    retains the exact mutation pending in the local journal,
  - byte identity: stored bytes are the exact original client bytes (digests
    bound in the mutation request; no re-encoding or digest drift),
  - fail closed: incomplete inventory (missing epoch/library, or an unsettled
    prior pending mutation) holds BEFORE any byte is written.
"""

import hashlib
import json
import os
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import config, intake_web  # noqa: E402
from agent import local_inventory_mutation as mutation  # noqa: E402


class FakeR2:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (data, content_type)

    def put_bytes_if_absent(self, key, data, content_type="application/octet-stream"):
        if key in self.objects:
            raise RuntimeError("PreconditionFailed")
        self.objects[key] = (data, content_type)

    def get_bytes(self, key):
        value = self.objects.get(key)
        return value[0] if value is not None else None


class Authority:
    """Fake mutation authority; fail='begin'/'complete' simulates a lost ack."""

    def __init__(self, fail=None):
        self.fail = fail
        self.pending = None

    def begin(self, request):
        self.pending = dict(request, state="pending", generation=3,
                            result_digest=None,
                            begun_at="2026-10-08T00:00:00+00:00",
                            completed_at=None)
        if self.fail == "begin":
            raise mutation.MutationHold("ack_lost")
        return dict(self.pending)

    def complete(self, request, result_digest):
        if self.fail == "complete":
            raise mutation.MutationHold("ack_lost")
        self.pending.update(state="complete", result_digest=result_digest,
                            completed_at="2026-10-08T00:00:01+00:00")
        return dict(self.pending)

    def close(self):
        pass


JPG = ("photo.jpg", "image/jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF raw \x00\x01 bytes")
VID = ("clip.mov", "video/quicktime", bytes(range(256)) * 4)


@pytest.fixture
def fenced(tmp_path, monkeypatch):
    """Intake flag on + mutation fence on with real tmp durable paths."""
    root = tmp_path.resolve()
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMA", "tok-gyma-12345678")
    monkeypatch.setenv("AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_INVENTORY_MUTATION_EPOCH", str(uuid.uuid4()))
    database = root / "echo.db"
    sqlite3.connect(database).close()
    monkeypatch.setenv("AGENT_DB_PATH", str(database))
    monkeypatch.setattr(config, "LIBRARY_PATH", str(root))
    (root / "gyma").mkdir()
    intake_web._hits.clear()
    return root


def _install_authority(monkeypatch, authority):
    monkeypatch.setattr(mutation.MutationAuthority, "from_environment",
                        staticmethod(lambda: authority))


def _journals(root):
    directory = root / "inventory-mutation-receipts"
    return [json.loads(p.read_text()) for p in directory.glob("*.json")] \
        if directory.exists() else []


# ---- guarded R2 upload: writes settle under begin/complete -------------------
def test_guarded_upload_completes_with_receipt(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 200 and body["stored"] == 1
    records = _journals(fenced)
    assert len(records) == 1
    record = records[0]
    assert record["local_state"] == "complete"
    assert record["gym_id"] == "gyma" and record["kind"] == "intake_upload"
    assert record["begin_receipt"]["state"] == "pending"
    assert record["complete_receipt"]["state"] == "complete"
    assert record["complete_receipt"]["result_digest"] == record["result_digest"]


# ---- byte identity: stored bytes are exactly what the client sent ------------
def test_guarded_upload_preserves_exact_bytes(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    status, _ = intake_web.handle_upload("tok-gyma-12345678", [JPG, VID], r2=r2)
    assert status == 200
    media = {k: v for k, v in r2.objects.items() if not k.endswith(".json")}
    assert len(media) == 2
    stored = sorted(data for data, _ct in media.values())
    originals = sorted([JPG[2], VID[2]])
    assert stored == originals  # no re-encoding, no truncation
    for data, _ct in media.values():
        assert hashlib.sha256(data).hexdigest() in (
            hashlib.sha256(JPG[2]).hexdigest(), hashlib.sha256(VID[2]).hexdigest())


# ---- pending-on-uncertain: never fake success --------------------------------
def test_uncertain_begin_writes_nothing_and_reports_pending(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority(fail="begin"))
    r2 = FakeR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503
    assert body["pending"] is True and "ok" not in body
    assert r2.objects == {}  # begin never settled: no byte was written
    (record,) = _journals(fenced)
    assert record["local_state"] == "prepared"  # exact identity retained pending


def test_uncertain_complete_keeps_pending_not_success(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority(fail="complete"))
    r2 = FakeR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503
    assert body["pending"] is True and "ok" not in body
    (record,) = _journals(fenced)
    # local effects happened but PG completion is uncertain: pending forever,
    # never certified complete, and a retry is blocked by assert_settled.
    assert record["local_state"] == "local_committed"
    assert "complete_receipt" not in record


# ---- fail closed when inventory is incomplete --------------------------------
def test_missing_epoch_holds_before_any_write(fenced, monkeypatch):
    monkeypatch.delenv("LOCAL_INVENTORY_MUTATION_EPOCH")
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503
    assert body["pending"] is True
    assert r2.objects == {}
    assert _journals(fenced) == []  # held before the mutation was even prepared


def test_missing_gym_library_holds_before_any_write(fenced, monkeypatch):
    os.rmdir(fenced / "gyma")
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503
    assert body["pending"] is True
    assert r2.objects == {}


def test_unsettled_prior_mutation_blocks_new_writes(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority(fail="begin"))
    r2 = FakeR2()
    status, _ = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503  # leaves a pending journal behind
    # A retry (even with a healthy authority) fails closed until reconciliation.
    _install_authority(monkeypatch, Authority())
    r2b = FakeR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2b)
    assert status == 503
    assert body["code"] == "local_mutation_reconciliation_required"
    assert r2b.objects == {}


# ---- guarded intake manifest (form path) --------------------------------------
def test_guarded_intake_form_manifest_completes(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    fields = {"gym_name": "Gym A", "voice": "loud"}
    status, body = intake_web.handle_intake_form("tok-gyma-12345678", fields, r2=r2)
    assert status == 200 and body["ok"] is True
    (key, (data, ctype)), = r2.objects.items()
    assert key.endswith("_intake.json") and ctype == "application/json"
    archived = json.loads(data)
    assert archived["client"] == "gyma" and archived["answers"]["gym_name"] == "Gym A"
    (record,) = _journals(fenced)
    assert record["local_state"] == "complete" and record["kind"] == "intake_manifest"


def test_intake_form_fails_closed_when_inventory_incomplete(fenced, monkeypatch):
    monkeypatch.delenv("LOCAL_INVENTORY_MUTATION_EPOCH")
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    fields = {"gym_name": "Gym A", "voice": "loud"}
    status, body = intake_web.handle_intake_form("tok-gyma-12345678", fields, r2=r2)
    assert status == 503
    assert body["pending"] is True and "ok" not in body
    assert r2.objects == {}


def test_guarded_portal_intake_manifest_completes(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    body = {"gym": {"name": "Gym A"}, "voice": {"vibe": "loud"}}
    status, resp = intake_web.handle_portal_intake("tok-gyma-12345678", body, r2=r2)
    assert status == 200 and resp["status"] == "received"
    assert any(k.endswith("_intake.json") for k in r2.objects)
    (record,) = _journals(fenced)
    assert record["local_state"] == "complete" and record["kind"] == "intake_manifest"


def test_portal_intake_uncertain_complete_is_pending_not_received(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority(fail="complete"))
    r2 = FakeR2()
    body = {"gym": {"name": "Gym A"}, "voice": {"vibe": "loud"}}
    status, resp = intake_web.handle_portal_intake("tok-gyma-12345678", body, r2=r2)
    assert status == 503  # the portal keeps echo_forwarded=false and re-forwards
    assert resp["pending"] is True and resp.get("status") != "received"
    (record,) = _journals(fenced)
    assert record["local_state"] == "local_committed"


# ---- fence OFF: legacy behavior is byte-for-byte unchanged --------------------
def test_fence_off_upload_writes_without_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMA", "tok-gyma-12345678")
    monkeypatch.delenv("AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED", raising=False)
    monkeypatch.setattr(config, "LIBRARY_PATH", str(tmp_path.resolve()))
    intake_web._hits.clear()
    r2 = FakeR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 200 and body == {"ok": True, "stored": 1}
    assert not (tmp_path / "inventory-mutation-receipts").exists()


@pytest.mark.parametrize("failure", ["corrupt", "truncate", "missing", "read_error"])
@pytest.mark.parametrize("target", ["media", "manifest", "form", "portal"])
def test_remote_identity_failure_never_completes(fenced, monkeypatch, failure, target):
    authority = Authority()
    _install_authority(monkeypatch, authority)

    class BadReadback(FakeR2):
        def get_bytes(self, key):
            data = super().get_bytes(key)
            match = ((target == "media" and key.endswith(".jpg")) or
                     (target != "media" and key.endswith(".json")))
            if data is not None and match:
                if failure == "read_error":
                    raise RuntimeError("read failed")
                if failure == "missing":
                    return None
                if failure == "truncate":
                    return data[:-1]
                return bytes([data[0] ^ 1]) + data[1:]
            return data

    r2 = BadReadback()
    if target == "form":
        status, body = intake_web.handle_intake_form(
            "tok-gyma-12345678", {"gym_name": "Gym A", "voice": "loud"}, r2=r2)
    elif target == "portal":
        status, body = intake_web.handle_portal_intake(
            "tok-gyma-12345678", {"gym": {"name": "Gym A"},
                                    "voice": {"vibe": "loud"}}, r2=r2)
    else:
        status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503 and body["pending"] is True
    (record,) = _journals(fenced)
    assert record["local_state"] == "pending"
    assert "complete_receipt" not in record
    assert authority.pending["state"] == "pending"


@pytest.mark.parametrize("names", [("photo.jpg", "photo.jpg"),
                                  ("my photo.jpg", "my?photo.jpg")])
def test_duplicate_sanitized_keys_rejected_before_begin(fenced, monkeypatch, names):
    authority = Authority()
    _install_authority(monkeypatch, authority)
    r2 = FakeR2()
    files = [(name, JPG[1], JPG[2]) for name in names]
    status, body = intake_web.handle_upload("tok-gyma-12345678", files, r2=r2)
    assert status == 503 and body["code"] == "r2_duplicate_planned_key"
    assert r2.objects == {} and authority.pending is None
    assert _journals(fenced) == []


@pytest.mark.parametrize("existing", ["photo.jpg", "upload.json"])
def test_existing_keys_rejected_before_any_write(fenced, monkeypatch, existing):
    from datetime import datetime, timezone
    authority = Authority()
    _install_authority(monkeypatch, authority)
    r2 = FakeR2()
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    key = "intake/gyma/incoming/20261008T000000Z_" + existing
    r2.objects[key] = (b"original", "application/octet-stream")
    before = dict(r2.objects)
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2, now=now)
    assert status == 503 and body["code"] == "r2_key_collision"
    assert r2.objects == before and authority.pending is None


def test_racing_key_is_never_clobbered(fenced, monkeypatch):
    _install_authority(monkeypatch, Authority())

    class RacingR2(FakeR2):
        def put_bytes_if_absent(self, key, data, content_type="application/octet-stream"):
            self.objects[key] = (b"other request", content_type)
            super().put_bytes_if_absent(key, data, content_type)

    r2 = RacingR2()
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503 and body["pending"] is True
    assert all(data == b"other request" for data, _ctype in r2.objects.values())
    (record,) = _journals(fenced)
    assert record["local_state"] == "pending" and "complete_receipt" not in record


def test_missing_atomic_create_holds_before_begin(fenced, monkeypatch):
    authority = Authority()
    _install_authority(monkeypatch, authority)
    r2 = FakeR2()
    r2.put_bytes_if_absent = None
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503 and body["code"] == "r2_conditional_create_unavailable"
    assert r2.objects == {} and authority.pending is None


def test_preflight_read_failure_holds_before_begin(fenced, monkeypatch):
    authority = Authority()
    _install_authority(monkeypatch, authority)
    r2 = FakeR2()
    def fail(key):
        raise RuntimeError("unreadable")
    r2.get_bytes = fail
    status, body = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=r2)
    assert status == 503 and body["code"] == "r2_collision_read_failed"
    assert r2.objects == {} and authority.pending is None


def test_upload_begin_binds_full_manifest_identity(fenced, monkeypatch):
    payloads = []
    real_run = mutation.run
    def capture(cfg, authority, kind, payload, apply):
        payloads.append(payload)
        return real_run(cfg, authority, kind, payload, apply)
    monkeypatch.setattr(mutation, "run", capture)
    _install_authority(monkeypatch, Authority())
    r2 = FakeR2()
    status, _ = intake_web.handle_upload(
        "tok-gyma-12345678", [JPG], note="batch note", captions=["caption"],
        client_contexts=["context"], consents=[True], r2=r2)
    assert status == 200
    (payload,) = payloads
    identity = payload["manifest"]
    manifest = r2.get_bytes(identity["key"])
    assert identity["bytes"] == len(manifest)
    assert identity["sha256"] == hashlib.sha256(manifest).hexdigest()
    sidecar = json.loads(manifest)
    assert sidecar["note"] == "batch note"
    assert list(sidecar["captions"].values()) == ["caption"]
    assert list(sidecar["client_context"].values()) == ["context"]
    assert list(sidecar["consent"].values()) == [True]
    (record,) = _journals(fenced)
    assert record["request_digest"] == mutation.digest(payload)


def test_r2_wrapper_uses_atomic_conditional_put():
    class S3:
        def put_object(self, **kwargs):
            self.kwargs = kwargs
    s3 = S3()
    r2 = intake_web._R2(s3, "bucket")
    r2.put_bytes_if_absent("exact-key", b"raw", content_type="video/mp4")
    assert s3.kwargs == {"Bucket": "bucket", "Key": "exact-key", "Body": b"raw",
                         "ContentType": "video/mp4", "IfNoneMatch": "*"}


@pytest.mark.parametrize("path", ["upload", "form", "portal"])
def test_same_second_request_preserves_first_manifest(fenced, monkeypatch, path):
    from datetime import datetime, timezone
    authority = Authority()
    _install_authority(monkeypatch, authority)
    r2 = FakeR2()
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    def submit(note):
        if path == "form":
            return intake_web.handle_intake_form(
                "tok-gyma-12345678", {"gym_name": "Gym A", "voice": note},
                r2=r2, now=now)
        if path == "portal":
            return intake_web.handle_portal_intake(
                "tok-gyma-12345678", {"gym": {"name": "Gym A"},
                                        "voice": {"vibe": note}}, r2=r2, now=now)
        return intake_web.handle_upload("tok-gyma-12345678", [JPG],
                                        note=note, r2=r2, now=now)
    assert submit("first")[0] == 200
    before = dict(r2.objects)
    status, body = submit("second")
    assert status == 503 and body["code"] == "r2_key_collision"
    assert r2.objects == before
    assert len(_journals(fenced)) == 1
