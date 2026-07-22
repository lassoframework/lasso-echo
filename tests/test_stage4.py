"""
Stage 4 feature tests:
  1. GHL /ghl/inbound route in intake_web (404/403/200)
  2. dam.set_consent sidecar + audit log
  3. _moderate_default flag off/on, video bypass, fail-open
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ---- helpers -----------------------------------------------------------------------

def _post(srv, path, body=b"", headers=None):
    port = srv.server_address[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _live_server(monkeypatch, *, ghl_enabled=True):
    if ghl_enabled:
        monkeypatch.setenv("AGENT_GHL_INTAKE_ENABLED", "true")
    else:
        monkeypatch.delenv("AGENT_GHL_INTAKE_ENABLED", raising=False)
    from agent.intake_web import build_server
    srv = build_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ---- 1. GHL /ghl/inbound route ------------------------------------------------------

def test_ghl_route_404_when_flag_off(monkeypatch):
    srv = _live_server(monkeypatch, ghl_enabled=False)
    try:
        status, _ = _post(srv, "/ghl/inbound", b"{}")
        assert status == 404
    finally:
        srv.shutdown()


def test_ghl_route_403_on_missing_signature(monkeypatch):
    srv = _live_server(monkeypatch, ghl_enabled=True)
    try:
        body = json.dumps({"phone": "+15551234567", "body": "hi",
                           "attachments": []}).encode()
        status, _ = _post(srv, "/ghl/inbound", body,
                          {"Content-Type": "application/json"})
        assert status == 403
    finally:
        srv.shutdown()


def test_ghl_route_200_on_verified_webhook(monkeypatch):
    import agent.ghl_intake as ghl
    monkeypatch.setattr(ghl, "_verify_default", lambda sig, b: True)
    srv = _live_server(monkeypatch, ghl_enabled=True)
    try:
        body = json.dumps({"phone": "+15551234567", "body": "hello",
                           "attachments": []}).encode()
        status, resp_body = _post(srv, "/ghl/inbound", body,
                                  {"Content-Type": "application/json"})
        assert status == 200
        assert json.loads(resp_body)["ok"] is True
    finally:
        srv.shutdown()


# ---- 2. dam consent audit -----------------------------------------------------------

def test_set_consent_writes_sidecar_and_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"fake jpg")

    from agent import dam
    dam.set_consent(str(img), "granted", member_ref="member_001",
                    granted_by="coach_blake", note="signed release on file")

    sidecar = dam.read_sidecar(str(img))
    assert sidecar["consent"] == "granted"
    assert sidecar["member_ref"] == "member_001"

    entries = dam.consent_log_entries(str(img))
    assert len(entries) == 1
    assert entries[0]["action"] == "granted"
    assert entries[0]["granted_by"] == "coach_blake"
    assert "release" in entries[0]["note"]


def test_consent_log_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"fake jpg")

    from agent import dam
    dam.set_consent(str(img), "pending")
    dam.set_consent(str(img), "granted", granted_by="blake")

    entries = dam.consent_log_entries(str(img))
    assert len(entries) == 2
    assert entries[0]["action"] == "granted"   # newest first
    assert entries[1]["action"] == "pending"


def test_consent_log_empty_for_unknown_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    from agent import dam
    entries = dam.consent_log_entries("/nonexistent/photo.jpg")
    assert entries == []


# ---- 3. content moderation ----------------------------------------------------------

def test_moderate_passes_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_CONTENT_MODERATION_ENABLED", raising=False)
    from agent.intake_ingest import _moderate_default
    ok, reason = _moderate_default(b"fake image data", "photo.jpg")
    assert ok is True
    assert reason == ""


def test_moderate_video_always_passes(monkeypatch):
    monkeypatch.setenv("AGENT_CONTENT_MODERATION_ENABLED", "true")
    from agent.intake_ingest import _moderate_default
    for ext in (".mp4", ".mov", ".avi"):
        ok, _ = _moderate_default(b"fake", f"clip{ext}")
        assert ok is True


def test_moderate_gemini_flags_content(monkeypatch):
    monkeypatch.setenv("AGENT_CONTENT_MODERATION_ENABLED", "true")
    import agent.intake_ingest as ii
    monkeypatch.setattr(ii, "_gemini_moderate", lambda data: (False, "nudity"))
    ok, reason = ii._moderate_default(b"fake", "photo.jpg")
    assert ok is False
    assert reason == "nudity"


def test_moderate_fails_open_on_exception(monkeypatch):
    monkeypatch.setenv("AGENT_CONTENT_MODERATION_ENABLED", "true")
    import agent.intake_ingest as ii

    def _boom(data):
        raise RuntimeError("Gemini API unavailable")

    monkeypatch.setattr(ii, "_gemini_moderate", _boom)
    ok, _ = ii._moderate_default(b"fake", "photo.jpg")
    assert ok is True   # fail open so uploads never stall permanently
