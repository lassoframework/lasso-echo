"""
Tests for portal_routes.py — calendar, library, and draft-action endpoints.

All tests are offline: injectable stores, monkeypatched accounts, no live db/network.

Key invariants:
  1. All routes return 403 when AGENT_PORTAL_APPROVALS is OFF.
  2. Calendar only returns drafts for the requesting account_key (token isolation).
  3. Library resolves via account.library_path; empty path = empty list.
  4. Actions delegate to portal_approvals (approve/edit/deny/kill).
  5. Unknown token = 404 from the HTTP layer (not leaking account existence).
"""

import os
import sys
import json

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_routes


# ---- minimal fake Draft --------------------------------------------------------

from agent.drafter import DraftStatus


class _FakeDraft:
    def __init__(self, draft_id, account_key, day_key, status=DraftStatus.PENDING,
                 platform="instagram", caption="test caption",
                 creative_public_url=None, scheduled_for=None,
                 blocked_reason=None, draft_type="post"):
        self.draft_id = draft_id
        self.account_key = account_key
        self.day_key = day_key
        self.status = status
        self.platform = platform
        self.caption = caption
        self.creative_public_url = creative_public_url
        self.scheduled_for = scheduled_for
        self.blocked_reason = blocked_reason
        self.draft_type = draft_type


class _FakeStore:
    def __init__(self, drafts=()):
        self._drafts = list(drafts)

    def list_pending(self):
        return [d for d in self._drafts if d.status == DraftStatus.PENDING]

    def get(self, draft_id):
        for d in self._drafts:
            if d.draft_id == draft_id:
                return d
        return None


# ---- 1. Flag-off gate ----------------------------------------------------------

def test_calendar_flag_off_returns_403(monkeypatch):
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    status, body = portal_routes.handle_portal_calendar("gymA", "2026-07")
    assert status == 403
    assert "OFF" in body["error"]


def test_library_flag_off_returns_403(monkeypatch):
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    status, body = portal_routes.handle_portal_library("gymA")
    assert status == 403
    assert "OFF" in body["error"]


def test_action_flag_off_returns_403(monkeypatch):
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    status, body = portal_routes.handle_portal_action(
        "approve", "gymA", "draft-001", "actor-1"
    )
    assert status == 403
    assert "OFF" in body["error"]


# ---- 2. Calendar lists only requesting gym's drafts (TOKEN ISOLATION) ----------

def test_calendar_returns_only_own_gym_drafts(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    store = _FakeStore([
        _FakeDraft("d1", "gymA", "2026-07-10"),
        _FakeDraft("d2", "gymA", "2026-07-15"),
        _FakeDraft("d3", "gymB", "2026-07-12"),  # different gym — must NOT appear
    ])
    status, body = portal_routes.handle_portal_calendar("gymA", "2026-07", store=store)
    assert status == 200
    ids = [d["draft_id"] for d in body["drafts"]]
    assert "d1" in ids
    assert "d2" in ids
    assert "d3" not in ids, "gymB draft must NOT appear in gymA calendar"


def test_calendar_token_isolation_reversed(monkeypatch):
    """gymB token cannot read gymA drafts."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    store = _FakeStore([
        _FakeDraft("d1", "gymA", "2026-07-10"),
    ])
    status, body = portal_routes.handle_portal_calendar("gymB", "2026-07", store=store)
    assert status == 200
    assert body["drafts"] == []


# ---- 3. Calendar month filter --------------------------------------------------

def test_calendar_filters_by_month(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    store = _FakeStore([
        _FakeDraft("jul-d1", "gymA", "2026-07-01"),
        _FakeDraft("aug-d1", "gymA", "2026-08-01"),
    ])
    status, body = portal_routes.handle_portal_calendar("gymA", "2026-07", store=store)
    assert status == 200
    ids = [d["draft_id"] for d in body["drafts"]]
    assert "jul-d1" in ids
    assert "aug-d1" not in ids, "August draft must not appear in July calendar"


def test_calendar_bad_month_returns_400(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = portal_routes.handle_portal_calendar("gymA", "not-a-month")
    assert status == 400
    assert "month" in body["error"]


def test_calendar_empty_month_returns_400(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = portal_routes.handle_portal_calendar("gymA", "")
    assert status == 400


def test_calendar_response_shape(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    store = _FakeStore([
        _FakeDraft("d1", "gymA", "2026-07-05", creative_public_url="https://cdn/img.jpg"),
    ])
    status, body = portal_routes.handle_portal_calendar("gymA", "2026-07", store=store)
    assert status == 200
    assert body["account_key"] == "gymA"
    assert body["month"] == "2026-07"
    d = body["drafts"][0]
    for key in ("draft_id", "day_key", "status", "platform", "caption",
                "creative_public_url", "scheduled_for", "blocked_reason"):
        assert key in d, f"missing key: {key}"
    assert d["creative_public_url"] == "https://cdn/img.jpg"


# ---- 4. Library ----------------------------------------------------------------

def test_library_unknown_account_returns_404(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr("agent.portal_routes.get_account", lambda k: None)
    status, body = portal_routes.handle_portal_library("unknown-gym")
    assert status == 404


def test_library_no_path_returns_empty_list(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    class _FakeAccount:
        library_path = None

    monkeypatch.setattr("agent.portal_routes.get_account", lambda k: _FakeAccount())
    status, body = portal_routes.handle_portal_library("gymA")
    assert status == 200
    assert body["creatives"] == []
    assert body["account_key"] == "gymA"


def test_library_returns_creatives_list(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

    class _FakeAccount:
        library_path = str(tmp_path)

    monkeypatch.setattr("agent.portal_routes.get_account", lambda k: _FakeAccount())
    status, body = portal_routes.handle_portal_library("gymA")
    assert status == 200
    assert len(body["creatives"]) == 1
    c = body["creatives"][0]
    assert c["stem"] == "photo"
    assert c["media_type"] == "image"
    for key in ("path", "media_type", "public_url", "client_note"):
        assert key in c


# ---- 5. Action delegation to portal_approvals ----------------------------------

def test_action_approve_delegates_to_portal_approvals(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    calls = []

    def _fake_approve(account_key, draft_id, actor_id, store=None, **kw):
        calls.append(("approve", account_key, draft_id, actor_id))
        return {"ok": True, "action": "approve", "draft_id": draft_id, "detail": "approved"}

    monkeypatch.setattr("agent.portal_routes._pa.approve", _fake_approve)
    status, body = portal_routes.handle_portal_action("approve", "gymA", "d1", "user-99")
    assert status == 200
    assert body["ok"] is True
    assert calls == [("approve", "gymA", "d1", "user-99")]


def test_action_edit_passes_note(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    calls = []

    def _fake_edit(account_key, draft_id, actor_id, note="", store=None, **kw):
        calls.append(("edit", note))
        return {"ok": True, "action": "edit", "draft_id": draft_id, "detail": "edited"}

    monkeypatch.setattr("agent.portal_routes._pa.edit", _fake_edit)
    status, body = portal_routes.handle_portal_action(
        "edit", "gymA", "d1", "user-99", note="please shorten the caption"
    )
    assert status == 200
    assert calls[0] == ("edit", "please shorten the caption")


def test_action_returns_403_on_unauthorized(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")

    def _fake_approve(account_key, draft_id, actor_id, store=None, **kw):
        return {"ok": False, "action": "approve", "draft_id": draft_id,
                "detail": "Denied: not authorized"}

    monkeypatch.setattr("agent.portal_routes._pa.approve", _fake_approve)
    status, body = portal_routes.handle_portal_action("approve", "gymA", "d1", "bad-actor")
    assert status == 403
    assert body["ok"] is False


def test_action_unknown_action_returns_400(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = portal_routes.handle_portal_action("publish", "gymA", "d1", "actor")
    assert status == 400
    assert "unknown action" in body["error"]


def test_action_missing_draft_id_returns_400(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = portal_routes.handle_portal_action("approve", "gymA", "", "actor")
    assert status == 400


def test_action_missing_actor_id_returns_400(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    status, body = portal_routes.handle_portal_action("approve", "gymA", "d1", "")
    assert status == 400


# ---- 6. HTTP routing: token resolves and unknown token → 404 -------------------

def test_http_calendar_unknown_token_returns_404(monkeypatch):
    """Unknown portal token must return 404 (not leak account existence)."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr("agent.intake_web.client_for_token", lambda t: None)

    from agent.intake_web import build_server
    import io, urllib.request

    server = build_server(0)
    port = server.server_address[1]
    import threading
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/portal/validtoken123/calendar?month=2026-07"
            )
        assert exc_info.value.code == 404
    finally:
        server.shutdown()


def test_http_calendar_valid_token_returns_json(monkeypatch):
    """Valid token + flag ON returns 200 JSON."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setattr("agent.intake_web.client_for_token", lambda t: "gymA")

    patched_called = []

    def _fake_calendar(account_key, month, store=None):
        patched_called.append((account_key, month))
        return 200, {"account_key": account_key, "month": month, "drafts": []}

    monkeypatch.setattr("agent.intake_web._pr.handle_portal_calendar", _fake_calendar)

    from agent.intake_web import build_server
    import threading, urllib.request

    server = build_server(0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/portal/validtoken123/calendar?month=2026-07"
        )
        body = json.loads(resp.read())
        assert body["account_key"] == "gymA"
        assert patched_called == [("gymA", "2026-07")]
    finally:
        server.shutdown()


def test_http_action_flag_off_returns_403(monkeypatch):
    """POST to action route returns 403 when flag OFF."""
    monkeypatch.delenv("AGENT_PORTAL_APPROVALS", raising=False)
    monkeypatch.setattr("agent.intake_web.client_for_token", lambda t: "gymA")

    from agent.intake_web import build_server
    import threading, urllib.request, urllib.error

    server = build_server(0)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        payload = json.dumps({"draft_id": "d1", "actor_id": "u1"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/portal/validtoken123/approve",
            data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 403
    finally:
        server.shutdown()
