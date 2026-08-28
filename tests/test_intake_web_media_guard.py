"""audit #6: the Connect Google Drive media WRITE routes must carry the SAME
CSRF/Origin rail + per-token rate limiter every other portal write route has.

Before the fix the media POST branch (check-connection / sources / disconnect / hide)
called neither _origin_ok() nor allow_token_request(), so a foreign page could drive
it with the gym's token and check-connection could hammer Google Drive unthrottled.

These drive the REAL do_POST handler (reached via build_server) with a stubbed
socket, asserting a cross-origin POST is 403'd and an over-limit token is 429'd, and
that a same-origin request within the limit is dispatched to gym_media_routes.
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
        # case-insensitive header lookup like http.client.HTTPMessage
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

    inst._deny = _deny
    inst._send_json = _send_json
    return inst, captured


@pytest.fixture
def _arm(monkeypatch):
    # A legacy env token authenticates to gym 'gritx'; arm the lane + approvals.
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GRITX", "gritxtoken12345")
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr("agent.config.gym_drive_connect_active_for", lambda g: True)
    # Reset the per-token limiter between tests.
    intake_web._token_hits.clear()
    return "gritxtoken12345"


def test_media_write_rejects_cross_origin(_arm, monkeypatch):
    token = _arm
    inst, cap = _make(
        f"/portal/{token}/media/check-connection",
        {"Origin": "https://evil.example", "Host": "portal.lassoframework.com",
         "Content-Length": "2"},
        body=b"{}")
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 403
    assert "json" not in cap                # never reached the handler


def test_media_write_rate_limited_per_token(_arm, monkeypatch):
    token = _arm
    # Exhaust the per-token budget in the CURRENT monotonic window (do_POST's own
    # limiter call uses time.monotonic()), so the next media POST is 429'd. Same-origin
    # (no Origin header) so the origin rail passes and the limiter is what bites.
    hp = intake_web._token_hash_prefix(token)
    for _ in range(intake_web._TOKEN_RATE_PER_MINUTE):
        assert intake_web.allow_token_request(hp) is True
    assert intake_web.allow_token_request(hp) is False   # confirm window is full
    # Refill to the cap exactly (the confirm call above did not add since it was over).
    inst, cap = _make(
        f"/portal/{token}/media/check-connection",
        {"Host": "portal.lassoframework.com", "Content-Length": "2"},
        body=b"{}")
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 429


def test_media_write_same_origin_within_limit_dispatches(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake_check(account_key, folder_url):
        called["args"] = (account_key, folder_url)
        return 200, {"ok": True}

    monkeypatch.setattr("agent.gym_media_routes.handle_check_connection", _fake_check)
    body = json.dumps({"folder_url": "https://drive/x"}).encode()
    inst, cap = _make(
        f"/portal/{token}/media/check-connection",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    # dispatched to the handler with the resolved account + folder url
    assert called.get("args") == ("gritx", "https://drive/x")
    assert cap.get("json", (None,))[0] == 200
