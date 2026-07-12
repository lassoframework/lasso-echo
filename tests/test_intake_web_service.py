"""
Intake web service deployability: the app boots with minimal env (just PORT
semantics — port 0 here), /healthz answers 200 whether the intake flag is on
or off (Railway's health check must not kill a dark service), and every other
route stays 404 while dark.

Real HTTP against a ThreadingHTTPServer bound to an ephemeral port; offline.
"""

import json
import os
import sys
import threading
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.intake_web import build_server


@pytest.fixture
def server(monkeypatch):
    """A live server on an ephemeral port with NO intake env set."""
    for name in list(os.environ):
        if name.startswith("AGENT_INTAKE_TOKEN_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENT_INTAKE_ENABLED", raising=False)
    srv = build_server(port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _get(srv, path):
    port = srv.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_boots_with_minimal_env(server):
    """build_server needs nothing but a port — no R2, Slack, or Meta env."""
    assert server.server_address[1] > 0


def test_healthz_answers_while_dark(server):
    status, body = _get(server, "/healthz")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["intake_enabled"] is False


def test_healthz_reports_armed_flag(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    status, body = _get(server, "/healthz")
    assert status == 200
    assert json.loads(body)["intake_enabled"] is True


def test_everything_else_is_404_while_dark(server):
    for path in ("/", "/u/sometoken123", "/healthz/extra", "/admin"):
        status, _ = _get(server, path)
        assert status == 404, f"{path} must be 404 while dark, got {status}"


def test_upload_page_serves_when_armed_with_token(server, monkeypatch):
    monkeypatch.setenv("AGENT_INTAKE_ENABLED", "true")
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GYM_ALPHA", "tok_gym_alpha_1")
    status, body = _get(server, "/u/tok_gym_alpha_1")
    assert status == 200
    assert b"<html" in body.lower() or b"<!doctype" in body.lower() or body

    # wrong token stays indistinguishable from off
    status, _ = _get(server, "/u/wrongtoken00")
    assert status == 404
