"""
Intake upload page tests. Fully OFFLINE: a fake R2 client only, pure handler
functions (no HTTP server started). Asserts: 404 unless the flag is on AND the
token maps to a client; content-type allowlist; per-file and per-request size caps;
rate limit; files + sidecar land under intake/<client>/incoming/; the raw token is
never stored (fingerprint only); unknown paths have no handler (no listing).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402


class FakeR2:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = (data, content_type)


def _arm(monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMA", "tok-gyma-12345678")
    intake_web._hits.clear()


JPG = ("photo.jpg", "image/jpeg", b"\xff\xd8\xff fake jpg")


# ---- gate: 404 when off or token unknown ---------------------------------------
def test_404_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMA", "tok-gyma-12345678")
    status, _ = intake_web.handle_upload("tok-gyma-12345678", [JPG], r2=FakeR2())
    assert status == 404


def test_404_when_token_unknown(monkeypatch):
    _arm(monkeypatch)
    status, _ = intake_web.handle_upload("wrong-token-000", [JPG], r2=FakeR2())
    assert status == 404


# ---- guardrails ------------------------------------------------------------------
def test_content_type_allowlist(monkeypatch):
    _arm(monkeypatch)
    status, body = intake_web.handle_upload(
        "tok-gyma-12345678", [("evil.exe", "application/x-msdownload", b"MZ")], r2=FakeR2())
    assert status == 400
    assert "not allowed" in body["error"]


def test_per_file_size_cap(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_INTAKE_MAX_FILE_MB", "1")
    big = ("big.jpg", "image/jpeg", b"x" * (2 * 1024 * 1024))
    status, body = intake_web.handle_upload("tok-gyma-12345678", [big], r2=FakeR2())
    assert status == 400
    assert "too large" in body["error"]


def test_per_request_size_cap(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_INTAKE_MAX_FILE_MB", "2")
    monkeypatch.setenv("AGENT_INTAKE_MAX_REQUEST_MB", "3")
    f = ("a.jpg", "image/jpeg", b"x" * (1536 * 1024))  # 1.5MB each
    status, body = intake_web.handle_upload(
        "tok-gyma-12345678", [f, f, f], r2=FakeR2())    # 4.5MB total
    assert status == 400
    assert "upload too large" in body["error"]


def test_rate_limit(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_INTAKE_RATE_PER_MINUTE", "3")
    assert all(intake_web.allow_request("1.2.3.4", now=100.0 + i) for i in range(3))
    assert intake_web.allow_request("1.2.3.4", now=103.5) is False   # 4th inside 60s
    assert intake_web.allow_request("5.6.7.8", now=103.5) is True    # other IP fine
    assert intake_web.allow_request("1.2.3.4", now=161.0) is True    # window rolled


# ---- happy path: files + sidecar under intake/<client>/incoming/ ---------------
def test_upload_stores_files_and_sidecar_never_raw_token(monkeypatch):
    _arm(monkeypatch)
    r2 = FakeR2()
    vid = ("clip.mp4", "video/mp4", b"fake mp4 bytes")
    status, body = intake_web.handle_upload(
        "tok-gyma-12345678", [JPG, vid], note="Saturday open house crowd", r2=r2)
    assert status == 200 and body["stored"] == 2

    keys = sorted(r2.objects)
    assert all(k.startswith("intake/gyma/incoming/") for k in keys)
    media = [k for k in keys if not k.endswith(".json")]
    sidecars = [k for k in keys if k.endswith("_upload.json")]
    assert len(media) == 2 and len(sidecars) == 1

    sidecar = json.loads(r2.objects[sidecars[0]][0])
    assert sidecar["note"] == "Saturday open house crowd"
    assert sidecar["client"] == "gyma"
    assert len(sidecar["filenames"]) == 2
    # the raw token is NEVER stored; only a sha256 fingerprint
    raw = json.dumps(sidecar)
    assert "tok-gyma-12345678" not in raw
    assert len(sidecar["token_sha256"]) == 64


def test_client_isolation_by_token(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYMB", "tok-gymb-87654321")
    r2 = FakeR2()
    intake_web.handle_upload("tok-gymb-87654321", [JPG], r2=r2)
    assert all(k.startswith("intake/gymb/incoming/") for k in r2.objects)
    assert not any("gyma" in k for k in r2.objects)   # never another client's prefix
