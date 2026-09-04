"""Router mount for the Story Studio "Create a Story" lane
(ECHO_STORY_STUDIO_BUILD §4). These drive the REAL do_GET / do_POST handlers
(reached via build_server) with a stubbed socket, mirroring
test_intake_web_media_guard.py, and assert:

  * GET  /portal/<token>/studio/sort-queue          resolves token -> account_key
        and dispatches to story_studio_routes.handle_list_sort_queue.
  * POST /portal/<token>/studio/story               carries the CSRF/Origin rail +
        per-token rate limiter every other portal write route has, and dispatches to
        handle_create_story with the resolved account_key.
  * POST /portal/<token>/studio/story/<id>/deny     dispatches to handle_deny_story.
  * POST /portal/<token>/studio/sort-queue/<id>/resolve dispatches to
        handle_resolve_sort_item with the lane.
  * A revoked token 404s before reaching any handler.

The per-gym render gate (STORY_STUDIO_RENDER / pilot allowlist) lives INSIDE the
handlers; these tests verify the ROUTER wiring (token resolution, origin, rate
limit, revoked guard, dispatch), not the render gate (covered in
test_story_studio_routes.py).
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

    inst._deny = _deny
    inst._send_json = _send_json
    return inst, captured


@pytest.fixture
def _arm(monkeypatch):
    # A legacy env token authenticates to gym 'gritx'; arm approvals + the render
    # pilot allowlist for gritx so the render lane is live for this gym.
    monkeypatch.setenv("AGENT_INTAKE_TOKEN_GRITX", "gritxtoken12345")
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", "gritx")
    intake_web._token_hits.clear()
    return "gritxtoken12345"


# ---- GET sort-queue ---------------------------------------------------------
def test_get_sort_queue_dispatches(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake_list(account_key):
        called["account_key"] = account_key
        return 200, {"items": [{"asset_id": "amb1", "reasons": [], "thumb_url": "x"}]}

    monkeypatch.setattr("agent.story_studio_routes.handle_list_sort_queue", _fake_list)
    inst, cap = _make(f"/portal/{token}/studio/sort-queue",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert called.get("account_key") == "gritx"
    assert cap.get("json", (None,))[0] == 200


def test_get_sort_queue_revoked_404(_arm, monkeypatch):
    token = _arm
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda c, r2=None: True)
    inst, cap = _make(f"/portal/{token}/studio/sort-queue",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert cap.get("deny", (None,))[0] == 404
    assert "json" not in cap


# ---- POST create-story: origin + rate rails --------------------------------
def test_create_story_rejects_cross_origin(_arm, monkeypatch):
    token = _arm
    inst, cap = _make(
        f"/portal/{token}/studio/story",
        {"Origin": "https://evil.example", "Host": "portal.lassoframework.com",
         "Content-Length": "2"},
        body=b"{}")
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 403
    assert "json" not in cap                # never reached the handler


def test_create_story_rate_limited_per_token(_arm, monkeypatch):
    token = _arm
    hp = intake_web._token_hash_prefix(token)
    for _ in range(intake_web._TOKEN_RATE_PER_MINUTE):
        assert intake_web.allow_token_request(hp) is True
    assert intake_web.allow_token_request(hp) is False   # window full
    inst, cap = _make(
        f"/portal/{token}/studio/story",
        {"Host": "portal.lassoframework.com", "Content-Length": "2"},
        body=b"{}")
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 429


def test_create_story_same_origin_within_limit_dispatches(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake_create(account_key, body, actor_id="", **kw):
        called["args"] = (account_key, body.get("asset_ids"), actor_id)
        return 200, {"ok": True, "status": "staged", "request_id": "r1"}

    monkeypatch.setattr("agent.story_studio_routes.handle_create_story", _fake_create)
    body = json.dumps({"asset_ids": ["a0", "a1"], "brief": "A win",
                       "actor_id": "coach1"}).encode()
    inst, cap = _make(
        f"/portal/{token}/studio/story",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert called.get("args") == ("gritx", ["a0", "a1"], "coach1")
    assert cap.get("json", (None,))[0] == 200


def test_create_story_revoked_404(_arm, monkeypatch):
    token = _arm
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda c, r2=None: True)
    body = json.dumps({"asset_ids": ["a0"]}).encode()
    inst, cap = _make(
        f"/portal/{token}/studio/story",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert cap.get("deny", (None,))[0] == 404
    assert "json" not in cap


# ---- POST deny-story --------------------------------------------------------
def test_deny_story_dispatches(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake_deny(account_key, request_id, reason="", **kw):
        called["args"] = (account_key, request_id, reason)
        return 200, {"ok": True, "returned": True}

    monkeypatch.setattr("agent.story_studio_routes.handle_deny_story", _fake_deny)
    body = json.dumps({"reason": "off brand"}).encode()
    inst, cap = _make(
        f"/portal/{token}/studio/story/req_abc/deny",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert called.get("args") == ("gritx", "req_abc", "off brand")
    assert cap.get("json", (None,))[0] == 200


# ---- POST resolve-sort-item -------------------------------------------------
def test_resolve_sort_item_dispatches(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake_resolve(account_key, asset_id, lane, actor_id="", **kw):
        called["args"] = (account_key, asset_id, lane, actor_id)
        return 200, {"ok": True, "lane": lane}

    monkeypatch.setattr("agent.story_studio_routes.handle_resolve_sort_item",
                        _fake_resolve)
    body = json.dumps({"lane": "raw", "actor_id": "coach2"}).encode()
    inst, cap = _make(
        f"/portal/{token}/studio/sort-queue/amb_9/resolve",
        {"Host": "portal.lassoframework.com", "Content-Length": str(len(body))},
        body=body)
    inst.do_POST()
    assert called.get("args") == ("gritx", "amb_9", "raw", "coach2")
    assert cap.get("json", (None,))[0] == 200


# ---- GET the story READ lane (2026-09-04) ----------------------------------
# create-story POSTs the SAME /studio/story path, so these pin that a GET reaches the
# READ handlers and never the create handler.
def test_get_story_list_dispatches(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake_list(account_key, **_k):
        called["list"] = account_key
        return 200, {"ok": True, "stories": [], "clip_bounds": {"min_clips": 2}}

    def _boom_create(*_a, **_k):            # a GET must NEVER reach create
        raise AssertionError("a GET on /studio/story reached handle_create_story")

    monkeypatch.setattr("agent.story_studio_routes.handle_list_stories", _fake_list)
    monkeypatch.setattr("agent.story_studio_routes.handle_create_story", _boom_create)
    inst, cap = _make(f"/portal/{token}/studio/story",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert called.get("list") == "gritx"
    assert cap.get("json", (None,))[0] == 200


def test_get_one_story_dispatches_with_the_request_id(_arm, monkeypatch):
    token = _arm
    called = {}

    def _fake_get(account_key, request_id, **_k):
        called["args"] = (account_key, request_id)
        return 200, {"ok": True, "story": {"request_id": request_id}}

    monkeypatch.setattr("agent.story_studio_routes.handle_get_story", _fake_get)
    inst, cap = _make(f"/portal/{token}/studio/story/sr_abc-1.2",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert called.get("args") == ("gritx", "sr_abc-1.2")
    assert cap.get("json", (None,))[0] == 200


def test_get_story_revoked_404(_arm, monkeypatch):
    token = _arm
    monkeypatch.setattr("agent.intake_web.is_revoked", lambda c, r2=None: True)
    inst, cap = _make(f"/portal/{token}/studio/story/sr_1",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert cap.get("deny", (None,))[0] == 404
    assert "json" not in cap                # never reached a handler


def test_get_story_unknown_token_404(_arm, monkeypatch):
    inst, cap = _make("/portal/notarealtoken123/studio/story",
                      {"Host": "portal.lassoframework.com"})
    inst.do_GET()
    assert cap.get("deny", (None,))[0] == 404
    assert "json" not in cap


def test_deny_path_still_wins_over_the_get_story_pattern(_arm, monkeypatch):
    """/studio/story/<id>/deny must keep routing to deny, not read as a story id."""
    token = _arm
    called = {}

    def _fake_deny(account_key, request_id, reason="", **_k):
        called["args"] = (account_key, request_id)
        return 200, {"ok": True, "returned": True}

    monkeypatch.setattr("agent.story_studio_routes.handle_deny_story", _fake_deny)
    inst, cap = _make(f"/portal/{token}/studio/story/sr_9/deny",
                      {"Host": "portal.lassoframework.com", "Origin":
                       "https://portal.lassoframework.com", "Content-Length": "2"},
                      body=b"{}")
    inst.do_POST()
    assert called.get("args") == ("gritx", "sr_9")
