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


# ---- 7. Portal-deny asset rollback (24h-refill-gap fix) ------------------------
#
# _handle_action_supabase's deny branch writes status='denied' straight to
# content_calendar; it never ran through approvals.handle_action's deny branch
# (gym_media_selector.on_draft_denied / podcast_selector.on_draft_denied), so a
# denied asset sat out the once-a-day observe_denials sweep (agent.runner.run_daily)
# for up to 24h before it could refill the day it just freed. These tests exercise
# the real deny action against the REAL gym_media_selector / podcast_selector
# rollback_use (not a mock), so the type-routing, date/asset scoping, and
# idempotency are proven against production code, not a test double.

from agent import portal_calendar_store as _pcs_mod  # noqa: E402
from agent import gym_media_selector as _gms  # noqa: E402
from agent import podcast_selector as _pods  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset as _make_gym_asset  # noqa: E402
from tests.podcast_fakes import FakeStore as _FakePodcastStore, make_asset as _make_pod_asset  # noqa: E402


class _DenyCalendarStore:
    """Stands in for SupabaseCalendarStore for the deny-rollback tests only
    (mirrors _Store in test_portal_actions_finality.py)."""

    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.writes = []

    def get_row(self, account_key, row_id):
        r = self._rows.get(row_id)
        if r is None or r.get("gym_id") != account_key:
            return None
        return dict(r)

    def set_status(self, account_key, row_id, new_status):
        self.writes.append(("status", row_id, new_status))
        return dict(self._rows[row_id], status=new_status)


def _deny_env(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc-key-secret")


def _patch_deny_calendar_store(monkeypatch, store):
    monkeypatch.setattr(_pcs_mod, "SupabaseCalendarStore", lambda *a, **k: store)
    monkeypatch.setattr(portal_routes._pcs, "SupabaseCalendarStore",
                        lambda *a, **k: store)


def _row(row_id="r1", account_key="eng", post_date="2026-08-27",
        draft_type=None, pillar=None, asset_id=None, status="pending"):
    return {"id": row_id, "gym_id": account_key, "post_date": post_date,
            "account": "instagram", "status": status, "caption": "text",
            "draft_type": draft_type, "pillar": pillar,
            "source_media_asset_id": asset_id, "image_url": "https://r2/x.jpg"}


def test_deny_gym_media_rolls_back_asset_scoped_to_date(monkeypatch):
    """A portal deny of a gym-media row (draft_type == 'gym_media') returns its
    photo to the pool SYNCHRONOUSLY, scoped to this row's own post_date + asset."""
    _deny_env(monkeypatch)
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    media_store = FakeMediaStore(assets=[
        _make_gym_asset("a1", gym_id="eng", used_count=0, last_used_at=None)])
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: media_store)
    from datetime import datetime, timezone
    asset = media_store.get_asset("a1")
    _gms.stamp_use(asset, "eng", "2026-08-27", store=media_store,
                   now=datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert media_store.assets["a1"]["used_count"] == 1  # staged, awaiting approval

    store = _DenyCalendarStore([_row(draft_type="gym_media", asset_id="a1")])
    _patch_deny_calendar_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action("deny", "eng", "r1", "actor")

    assert status == 200
    assert body["status"] == "denied"
    assert media_store.assets["a1"]["used_count"] == 0
    assert media_store.assets["a1"]["last_used_at"] is None


def test_deny_rolls_back_a_LIVE_SHAPED_row_that_has_no_draft_type(monkeypatch):
    """THE ROUTING BUG. content_calendar has NO draft_type column: measured against
    the live table 2026-08-30, it reads None on all 229 ENG rows. Keying the rollback
    on draft_type == 'gym_media' therefore made the whole fix a silent no-op for gym
    media, the main case it exists for. The real signal is a non-empty
    source_media_asset_id, present on exactly the photo pillars (faces/community/
    results, 172 of 229) and absent on every generated pillar. This row is shaped like
    a real one: draft_type absent entirely."""
    _deny_env(monkeypatch)
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    media_store = FakeMediaStore(assets=[
        _make_gym_asset("a1", gym_id="eng", used_count=0, last_used_at=None)])
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: media_store)
    from datetime import datetime, timezone
    _gms.stamp_use(media_store.get_asset("a1"), "eng", "2026-08-27",
                   store=media_store, now=datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert media_store.assets["a1"]["used_count"] == 1

    row = _row(asset_id="a1", pillar="faces")
    row.pop("draft_type", None)                    # exactly as the live table returns
    store = _DenyCalendarStore([row])
    _patch_deny_calendar_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action("deny", "eng", "r1", "actor")

    assert status == 200 and body["status"] == "denied"
    assert media_store.assets["a1"]["used_count"] == 0, "live-shaped row did not roll back"


def test_deny_podcast_rolls_back_clip(monkeypatch):
    """A portal deny of a podcast row (pillar == 'podcast') returns its clip too --
    the same synchronous rollback, routed to the OTHER selector."""
    _deny_env(monkeypatch)
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    pod_store = _FakePodcastStore(assets=[
        _make_pod_asset("clip1", used_count=0, last_used_at=None)])
    monkeypatch.setattr("agent.podcast_index.default_store", lambda: pod_store)
    from datetime import datetime, timezone
    asset = pod_store.assets["clip1"]
    _pods.stamp_use(asset, "eng", "2026-08-27", store=pod_store,
                    now=datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert pod_store.assets["clip1"]["used_count"] == 1

    store = _DenyCalendarStore([_row(pillar="podcast")])
    _patch_deny_calendar_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action("deny", "eng", "r1", "actor")

    assert status == 200
    assert body["status"] == "denied"
    assert pod_store.assets["clip1"]["used_count"] == 0
    assert pod_store.assets["clip1"]["last_used_at"] is None


def test_deny_unrecognized_type_does_not_guess(monkeypatch):
    """A row that is neither draft_type=='gym_media' nor pillar=='podcast' (a plain
    post) is left ALONE -- a wrong guess would re-pool the WRONG asset, which is
    worse than the 24h delay this fix exists to close."""
    _deny_env(monkeypatch)
    calls = []
    monkeypatch.setattr(_gms, "rollback_use",
                        lambda *a, **k: calls.append(("gym", a, k)))
    monkeypatch.setattr(_pods, "rollback_use",
                        lambda *a, **k: calls.append(("pod", a, k)))

    store = _DenyCalendarStore([_row(draft_type="post", pillar=None)])
    _patch_deny_calendar_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action("deny", "eng", "r1", "actor")

    assert status == 200
    assert body["status"] == "denied"
    assert calls == [], "neither selector may be touched for an unrecognized type"


def test_deny_rollback_exception_does_not_break_deny_response(monkeypatch):
    """A rollback failure (e.g. the media store is down) must never fail the deny
    itself -- the client still gets its normal 200/denied response."""
    _deny_env(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("media store unavailable")

    monkeypatch.setattr(_gms, "rollback_use", _boom)
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda msg, **k: alerts.append(msg))

    store = _DenyCalendarStore([_row(draft_type="gym_media", asset_id="a1")])
    _patch_deny_calendar_store(monkeypatch, store)
    status, body = portal_routes.handle_portal_action("deny", "eng", "r1", "actor")

    assert status == 200
    assert body["status"] == "denied"
    assert alerts and "r1" in alerts[0]


def test_deny_twice_does_not_double_rollback(monkeypatch):
    """Denying the same row twice (or the nightly observe_denials sweep running
    after the synchronous call) must never double-rollback or corrupt the ledger.
    Exercised against the REAL rollback_use, whose idempotency is the actual
    protection -- not a test double standing in for it."""
    _deny_env(monkeypatch)
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    media_store = FakeMediaStore(assets=[
        _make_gym_asset("a1", gym_id="eng", used_count=0, last_used_at=None)])
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: media_store)
    from datetime import datetime, timezone
    asset = media_store.get_asset("a1")
    _gms.stamp_use(asset, "eng", "2026-08-27", store=media_store,
                   now=datetime(2026, 8, 27, tzinfo=timezone.utc))

    store = _DenyCalendarStore([_row(draft_type="gym_media", asset_id="a1")])
    _patch_deny_calendar_store(monkeypatch, store)

    status1, _ = portal_routes.handle_portal_action("deny", "eng", "r1", "actor")
    assert status1 == 200
    assert media_store.assets["a1"]["used_count"] == 0

    status2, _ = portal_routes.handle_portal_action("deny", "eng", "r1", "actor")
    assert status2 == 200
    # A second rollback of an already-rolled-back record must be a no-op: the
    # counter must NOT go negative (the real bug a double-rollback would cause).
    assert media_store.assets["a1"]["used_count"] == 0
    assert media_store.assets["a1"]["last_used_at"] is None
