"""
Tests for the self-serve portal onboard endpoint (POST /portal/onboard).

Drives the REAL request handler via build_server(0).RequestHandlerClass and
Handler.__new__ (skips socket init), mirroring test_intake_web_portal.py. All
tests are OFFLINE: a tmp cwd for scaffolded files, a tmp AGENT_DB_PATH, a fixed
signing secret, and a fake rfile/wfile so no live socket is ever touched.

Auth env var: AGENT_PORTAL_ONBOARD_KEY (X-Portal-Key header, constant-time).
mint() is DETERMINISTIC (HMAC-SHA256 of the lowercased key under the shared
secret), so onboarding the same gym twice returns the SAME live token.
"""

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402


SIGNING_SECRET = "test-signing-secret-abc123"
PORTAL_KEY = "portal-shared-key-xyz789"


# ---- harness -------------------------------------------------------------------

def _make_handler():
    """The real request handler class, reached via the build_server() factory.
    Bind port 0 (never serve) and read RequestHandlerClass off the server."""
    server = intake_web.build_server(0)
    try:
        return server.RequestHandlerClass
    finally:
        server.server_close()


class _FakeWFile:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, data):
        self.buffer.write(data)


def _post_onboard(body_dict=None, portal_key=PORTAL_KEY, raw_override=None):
    """Drive do_POST for /portal/onboard. Returns (status_code, response_json_or_text)."""
    Handler = _make_handler()
    inst = Handler.__new__(Handler)  # skip __init__: it wants a live socket
    inst.path = "/portal/onboard"
    inst.command = "POST"

    if raw_override is not None:
        raw = raw_override
    else:
        raw = json.dumps(body_dict or {}).encode("utf-8")

    headers = {"Content-Length": str(len(raw))}
    if portal_key is not None:
        headers["X-Portal-Key"] = portal_key
    inst.headers = headers
    inst.rfile = io.BytesIO(raw)

    # Capture the response written by _send_json / _deny.
    captured = {}

    def _send_response(code):
        captured["status"] = code

    def _send_header(k, v):
        pass

    def _end_headers():
        pass

    wfile = _FakeWFile()
    inst.send_response = _send_response
    inst.send_header = _send_header
    inst.end_headers = _end_headers
    inst.wfile = wfile

    inst.do_POST()

    raw_out = wfile.buffer.getvalue()
    try:
        payload = json.loads(raw_out.decode("utf-8"))
    except Exception:
        payload = raw_out.decode("utf-8", "replace")
    return captured.get("status"), payload


def _base_env(monkeypatch, tmp_path):
    """Set up an isolated, offline env: tmp cwd (for scaffold files), tmp DB,
    the signing secret, and the shared portal key. Callers still toggle the
    APPROVALS gate per test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_INTAKE_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("AGENT_PORTAL_ONBOARD_KEY", PORTAL_KEY)


# ---- 1. Missing / wrong X-Portal-Key -> 401 ------------------------------------

def test_missing_key_returns_401(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"}, portal_key=None
    )
    assert status == 401
    assert "unauthorized" in body["error"]


def test_wrong_key_returns_401(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"},
        portal_key="the-wrong-key",
    )
    assert status == 401
    assert "unauthorized" in body["error"]


def test_unset_shared_key_fails_closed_401(monkeypatch, tmp_path):
    """No AGENT_PORTAL_ONBOARD_KEY configured => every request 401s (fail CLOSED)."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_PORTAL_ONBOARD_KEY", raising=False)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"}, portal_key="anything"
    )
    assert status == 401


# ---- 2. AGENT_PORTAL_APPROVALS off -> 403 --------------------------------------

def test_approvals_flag_off_returns_403(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "Gym Key"}
    )
    assert status == 403
    assert "disabled" in body["error"]


# ---- 3. Valid key + body -> 200, resolvable token ------------------------------

def test_valid_onboard_returns_resolvable_token(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")  # so client_for_token verifies

    status, body = _post_onboard(
        {"account_key": "newgym", "display_name": "New Gym Fitness"}
    )
    assert status == 200, body
    assert body["account_key"] == "newgym"
    assert body["publish_off"] is True
    assert body["onboarded"] is True
    token = body["raw_token"]
    assert isinstance(token, str) and token
    # The raw token resolves back to the account_key via the real verifier.
    assert intake_web.client_for_token(token) == "newgym"


# ---- 4. Idempotent: same account twice -> same live token ----------------------

def test_idempotent_same_token(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")

    s1, b1 = _post_onboard({"account_key": "repeatgym", "display_name": "Repeat Gym"})
    s2, b2 = _post_onboard({"account_key": "repeatgym", "display_name": "Repeat Gym"})
    assert s1 == 200 and s2 == 200, (b1, b2)
    # Both calls succeed and both tokens resolve to the same account.
    assert intake_web.client_for_token(b1["raw_token"]) == "repeatgym"
    assert intake_web.client_for_token(b2["raw_token"]) == "repeatgym"
    # mint() is deterministic under HMAC signing, so the token is identical and
    # a live token is never rotated on re-onboard.
    assert b1["raw_token"] == b2["raw_token"]


# ---- 5. Bad account_key slug -> 400 --------------------------------------------

def test_bad_slug_uppercase_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "BadGym", "display_name": "Bad Gym"}
    )
    assert status == 400
    assert "account_key" in body["error"]


def test_bad_slug_special_chars_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    for bad in ("gym-key", "gym key", "gym.key", "gym_key", "gym/key", ""):
        status, body = _post_onboard(
            {"account_key": bad, "display_name": "Gym"}
        )
        assert status == 400, f"{bad!r} should be rejected, got {status}"


def test_missing_display_name_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(
        {"account_key": "gymkey", "display_name": "   "}
    )
    assert status == 400
    assert "display_name" in body["error"]


def test_invalid_json_body_returns_400(monkeypatch, tmp_path):
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = _post_onboard(raw_override=b"{not json")
    assert status == 400


# ---- 6. Pure-function idempotency (handle_portal_onboard direct) ---------------

def test_handle_portal_onboard_pure_idempotent(monkeypatch, tmp_path):
    """The module-level handler is deterministic and never rotates a live token."""
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    s1, r1 = intake_web.handle_portal_onboard(
        {"account_key": "puregym", "display_name": "Pure Gym"}
    )
    s2, r2 = intake_web.handle_portal_onboard(
        {"account_key": "puregym", "display_name": "Pure Gym"}
    )
    assert s1 == 200 and s2 == 200
    assert r1["raw_token"] == r2["raw_token"]
    assert intake_web.client_for_token(r1["raw_token"]) == "puregym"
