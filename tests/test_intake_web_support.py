"""Router mount for the gym-facing SUPPORT inbox (POST/GET /portal/<token>/support).
These drive the REAL do_GET / do_POST handlers (reached via build_server) with a
stubbed socket, mirroring test_intake_web_studio.py, and assert:

  * POST /portal/<token>/support {message}  resolves token -> account_key and
        dispatches to support_inbox.submit_support_message; 200 {ok:true} on delivery.
  * empty message -> 400; the disabled flag -> 403; unknown/revoked token -> 404;
        cross-origin -> 403; rate-limit -> 429.
  * a Slack failure surfaces as 502 {ok:false}, never a 500 stack.
  * GET /portal/<token>/support renders the form for a valid token; 403 when disabled.

The Slack transport and gym-identity resolution live in support_inbox (unit-tested
in test_support_inbox.py); these tests verify the ROUTER wiring (flag gate, token
resolution, origin, rate limit, revoked guard, dispatch, status mapping).
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import intake_web  # noqa: E402


def _handler():
    server = intake_web.build_server(0)
    try:
        return server.RequestHandlerClass
    finally:
        server.server_close()


class _Headers(dict):
    def get(self, k, default=None):
        for key, val in self.items():
            if key.lower() == k.lower():
                return val
        return default


def _make(path, headers, body=b"{}"):
    Handler = _handler()
    inst = Handler.__new__(Handler)          # skip __init__ (wants a live socket)
    inst.path = path
    inst.headers = _Headers(headers)
    inst.client_address = ("10.0.0.1", 5555)
    inst.rfile = io.BytesIO(body)
    inst.wfile = io.BytesIO()
    captured = {}

    def _deny(code=404, msg="not found"):
        captured["deny"] = (code, msg)

    def _send_json(obj, status=200, cors_origin=""):
        captured["json"] = (status, obj)

    def _send_html(body_str, status=200):
        captured["html"] = (status, body_str)

    inst._deny = _deny
    inst._send_json = _send_json
    inst._send_html = _send_html
    return inst, captured


@pytest.fixture
def _arm(monkeypatch):
    # A legacy env token authenticates to gym 'gritx'; arm the support inbox flag.
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GRITX", "gritxtoken12345")
    monkeypatch.setenv("AGENT_SUPPORT_INBOX", "true")
    monkeypatch.setenv("AGENT_SUPPORT_CHANNEL_ID", "C0BTDAE1GLW")
    intake_web._token_hits.clear()
    return "gritxtoken12345"


# ---- POST: happy path ----------------------------------------------------------
def test_post_dispatches_and_returns_200(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake(account_key, message, **kw):
        called["args"] = (account_key, message)
        return {"ok": True, "delivered": True, "reason": ""}

    monkeypatch.setattr("agent.support_inbox.submit_support_message", _fake)
    body = json.dumps({"message": "My calendar looks empty"}).encode()
    inst, cap = _make(
        f"/portal/{token}/support",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert called.get("args") == ("gritx", "My calendar looks empty")
    assert cap.get("json") == (200, {"ok": True})


# ---- POST: empty message -> 400 ------------------------------------------------
def test_post_empty_message_400(_arm, monkeypatch):
    token = _arm
    called = {}
    monkeypatch.setattr("agent.support_inbox.submit_support_message",
                        lambda *a, **k: called.setdefault("hit", True))
    body = json.dumps({"message": "   "}).encode()
    inst, cap = _make(
        f"/portal/{token}/support",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert cap.get("json", (None,))[0] == 400
    assert "hit" not in called                # never reached the poster


# ---- POST: disabled flag -> 403 ------------------------------------------------
def test_post_disabled_flag_403(_arm, monkeypatch):
    token = _arm
    monkeypatch.delenv("AGENT_SUPPORT_INBOX", raising=False)   # flag OFF
    body = json.dumps({"message": "hi"}).encode()
    inst, cap = _make(
        f"/portal/{token}/support",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert cap.get("json", (None,))[0] == 403
    assert "deny" not in cap


# ---- POST: cross-origin -> 403 -------------------------------------------------
def test_post_rejects_cross_origin(_arm, monkeypatch):
    token = _arm
    body = json.dumps({"message": "hi"}).encode()
    inst, cap = _make(
        f"/portal/{token}/support",
        {"Origin": "https://evil.example", "Host": "portal.lassoframework.com",
         "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 403
    assert "json" not in cap


# ---- POST: rate limit -> 429 ---------------------------------------------------
def test_post_rate_limited(_arm, monkeypatch):
    token = _arm
    hp = intake_web._token_hash_prefix(token)
    for _ in range(intake_web._TOKEN_RATE_PER_MINUTE):
        assert intake_web.allow_token_request(hp) is True
    assert intake_web.allow_token_request(hp) is False       # window full
    body = json.dumps({"message": "hi"}).encode()
    inst, cap = _make(
        f"/portal/{token}/support",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 429


# ---- POST: unknown/revoked token -> 404 ----------------------------------------
def test_post_revoked_token_404(_arm, monkeypatch):
    token = _arm
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda c, r2=None: True)
    body = json.dumps({"message": "hi"}).encode()
    inst, cap = _make(
        f"/portal/{token}/support",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 404
    assert "json" not in cap


# ---- POST: Slack failure -> 502 (graceful, never a 500 stack) ------------------
def test_post_slack_failure_502(_arm, monkeypatch):
    token = _arm
    monkeypatch.setattr("agent.support_inbox.submit_support_message",
                        lambda *a, **k: {"ok": False, "delivered": False,
                                         "reason": "slack_failed"})
    body = json.dumps({"message": "hi"}).encode()
    inst, cap = _make(
        f"/portal/{token}/support",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    status, obj = cap.get("json")
    assert status == 502
    assert obj.get("ok") is False


# ---- GET: renders the form for a valid token -----------------------------------
def test_get_renders_support_form(_arm, monkeypatch):
    token = _arm
    inst, cap = _make(f"/portal/{token}/support",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    status, html = cap.get("html", (None, ""))
    assert status == 200
    assert "Send to LASSO" in html
    assert "/support" in html


def test_get_disabled_flag_403(_arm, monkeypatch):
    token = _arm
    monkeypatch.delenv("AGENT_SUPPORT_INBOX", raising=False)
    inst, cap = _make(f"/portal/{token}/support",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert cap.get("deny", (None,))[0] == 403


def test_get_revoked_token_404(_arm, monkeypatch):
    token = _arm
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda c, r2=None: True)
    inst, cap = _make(f"/portal/{token}/support",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert cap.get("deny", (None,))[0] == 404
    assert "html" not in cap
