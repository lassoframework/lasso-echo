"""
Story Studio §4: the portal "Create a Story" route surface. The render lane (create
/ deny) is gated per gym (default OFF, pilot allowlist); the sort queue (list /
resolve) is gated by STORY_CLASSIFIER (default ON). Every create stages PENDING or
HOLDS; deny returns segments; the footage picker reuses the gym media pool.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_studio_routes as routes  # noqa: E402
from agent import story_music as sm  # noqa: E402
from agent import story_composer as comp  # noqa: E402


# Story Studio now writes a real PENDING approval row; give these offline tests a
# calendar store so the genuine _stage_calendar_row path runs.
_SS_ROWS = []


class _SSFakeCalStore:
    def insert_rows(self, gym_id, rows):
        out = []
        for i, r in enumerate(rows or []):
            row = dict(r)
            row["id"] = f"cal-{len(_SS_ROWS) + i}"
            _SS_ROWS.append((gym_id, row))
            out.append(row)
        return out


@pytest.fixture(autouse=True)
def _ss_cal(monkeypatch):
    _SS_ROWS.clear()
    monkeypatch.setattr("agent.config.portal_calendar_supabase_enabled", lambda: True)
    monkeypatch.setattr("agent.portal_calendar_store.SupabaseCalendarStore",
                        lambda *a, **k: _SSFakeCalStore())
    yield




class _FakeStore:
    """Mirrors SupabaseStoryStudioStore, INCLUDING its gym scoping: every read filters
    on gym_id, so a test that forgets to scope fails here the way it would in prod."""

    def __init__(self):
        self.requests = []
        self.renders = []

    def available(self):
        return True

    def insert_request(self, row):
        self.requests.append(dict(row))
        return dict(row)

    def insert_render(self, row):
        self.renders.append(dict(row))
        return dict(row)

    def update_request(self, rid, fields, gym_id=None):
        return True

    def update_render(self, rid, fields, gym_id=None):
        return True

    def get_request(self, request_id, gym_id=None):
        assert gym_id, "get_request must be gym-scoped"
        for r in self.requests:
            if str(r.get("id")) == str(request_id) and r.get("gym_id") == gym_id:
                return dict(r)
        return None

    def render_for_request(self, request_id, gym_id):
        assert gym_id, "render_for_request must be gym-scoped"
        for r in self.renders:
            if str(r.get("request_id")) == str(request_id) and r.get("gym_id") == gym_id:
                return dict(r)
        return None

    def list_renders(self, gym_id, status=None):
        assert gym_id, "list_renders must be gym-scoped"
        return [dict(r) for r in self.renders if r.get("gym_id") == gym_id
                and (status is None or r.get("status") == status)]

    def list_requests(self, gym_id, status=None):
        assert gym_id, "list_requests must be gym-scoped"
        return [dict(r) for r in self.requests if r.get("gym_id") == gym_id
                and (status is None or r.get("status") == status)]


class _RealPathLibrary(sm.StubMusicLibrary):
    def __init__(self, audio_path):
        super().__init__(tracks=[sm.Track(
            track_id="hype_test", license_ref="lasso-lib:LIC-TEST",
            shelf=sm.SHELF_HYPE, title="Test Hype", path=audio_path)])
        self._audio = audio_path

    def resolve_path(self, track):
        return self._audio


def _cands(gym, n=6, seg=10.0):
    return [{"asset_id": f"a{i}", "gym_id": gym, "start_ts": 0,
             "end_ts": seg, "score": 90 - i} for i in range(n)]


def _fake_render(plan, *, output_dir, ask_frame_text="", music_path="", **_k):
    return comp.ComposeResult(plan=plan, output_path=f"{output_dir}/final.mp4")


def _arm(monkeypatch, gym="pierce"):
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", gym)
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    monkeypatch.setattr("agent.story_studio._host", lambda p, g: "https://r2/s.mp4")


# ---- render lane flag gate --------------------------------------------------
def test_create_lane_off_returns_403(monkeypatch):
    monkeypatch.delenv("STORY_STUDIO_RENDER", raising=False)
    monkeypatch.delenv("STORY_STUDIO_RENDER_GYMS", raising=False)
    status, body = routes.handle_create_story("pierce_ig", {"asset_ids": ["a0"]})
    assert status == 403
    assert body["ok"] is False


def test_create_requires_asset_ids(monkeypatch):
    _arm(monkeypatch)
    status, body = routes.handle_create_story("pierce_ig", {"asset_ids": []})
    assert status == 400


# ---- happy path stages PENDING ----------------------------------------------
def test_create_stages_pending(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"x")
    status, body = routes.handle_create_story(
        "pierce_ig", {"asset_ids": ["a0", "a1"], "brief": "Members crushed today",
                      "identity_tokens": ["Pierce"]},
        actor_id="coach1", candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)), render_fn=_fake_render)
    assert status == 200
    assert body["status"] == "staged"
    assert body["draft_id"]
    assert body["music"]["track_id"] == "hype_test"
    assert body["music"]["license_ref"] == "lasso-lib:LIC-TEST"


def test_create_hold_is_a_normal_200(monkeypatch, tmp_path):
    # a missing music ops asset HOLDS: nothing staged, honest reason, still a 200.
    _arm(monkeypatch)
    status, body = routes.handle_create_story(
        "pierce_ig", {"asset_ids": ["a0"], "brief": "A win",
                      "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=_FakeStore(), render_fn=_fake_render)
    assert status == 200
    assert body["status"] == "held"
    assert "music" in body["hold_reason"].lower()


# ---- deny returns segments --------------------------------------------------
def test_deny_off_returns_403(monkeypatch):
    monkeypatch.delenv("STORY_STUDIO_RENDER", raising=False)
    monkeypatch.delenv("STORY_STUDIO_RENDER_GYMS", raising=False)
    status, body = routes.handle_deny_story("pierce_ig", "req1", reason="off brand")
    assert status == 403


def test_deny_returns_ok(monkeypatch):
    _arm(monkeypatch)
    status, body = routes.handle_deny_story("pierce_ig", "req_unknown",
                                            reason="off brand")
    assert status == 200
    assert body["ok"] is True


# ---- sort queue (classifier flag) -------------------------------------------
def test_sort_queue_lists_pending(monkeypatch):
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    from agent import story_sort_queue as _q
    _q.enqueue("northgate", "amb_route_1", reasons=["9:16 22s no text"])
    status, body = routes.handle_list_sort_queue("northgate_ig")
    assert status == 200
    assert any(i["asset_id"] == "amb_route_1" for i in body["items"])


def test_sort_queue_off_returns_empty(monkeypatch):
    monkeypatch.setenv("STORY_CLASSIFIER", "false")
    status, body = routes.handle_list_sort_queue("northgate_ig")
    assert status == 200
    assert body["items"] == []


def test_resolve_sort_item(monkeypatch):
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    from agent import story_sort_queue as _q
    _q.enqueue("westgate", "amb_route_2", reasons=["borderline"])
    status, body = routes.handle_resolve_sort_item("westgate_ig", "amb_route_2",
                                                   "raw", actor_id="coach1")
    assert status == 200
    assert body["lane"] == "raw"


def test_resolve_bad_lane_is_400(monkeypatch):
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    status, body = routes.handle_resolve_sort_item("westgate_ig", "x", "banana")
    assert status == 400


# ---- the READ lane (2026-09-04) ---------------------------------------------
# Before this, story_studio_store's get/list readers had NO callers: a story's music
# and overlay were visible only in the create response, so reopening an older approval
# lost them. These tests pin that the lane reads, stays read-only, and stays scoped.
def _staged_story(monkeypatch, tmp_path, gym="pierce", account="pierce_ig"):
    """Create one real staged story through the normal handler and hand back the store
    it persisted into, so the read tests read genuinely-written rows."""
    _arm(monkeypatch, gym=gym)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"x")
    store = _FakeStore()
    status, body = routes.handle_create_story(
        account, {"asset_ids": ["a0", "a1"], "brief": "Members crushed today",
                  "identity_tokens": [gym]},
        actor_id="coach1", candidates=_cands(gym), store=store,
        music_library=_RealPathLibrary(str(audio)), render_fn=_fake_render)
    assert status == 200 and body["status"] == "staged", body
    return store, body


def test_get_story_returns_the_persisted_music_and_overlay(monkeypatch, tmp_path):
    store, created = _staged_story(monkeypatch, tmp_path)
    status, body = routes.handle_get_story("pierce_ig", created["request_id"],
                                           store=store)
    assert status == 200
    story = body["story"]
    # the SAME evidence the create response carried, now readable after the fact.
    assert story["overlay"] == created["overlay"]
    assert story["music"]["track_id"] == "hype_test"
    assert story["music"]["license_ref"] == "lasso-lib:LIC-TEST"
    assert story["segments"], "the segment plan must survive the round trip"
    assert story["clip_count"] == len(story["segments"])
    assert story["brief"] == "Members crushed today"
    assert story["calendar_row_id"]


def test_get_story_404s_for_another_gyms_request_id(monkeypatch, tmp_path):
    """Tenant isolation at the read boundary: holding a real request id from ANOTHER
    gym must read as absent, never as content."""
    store, created = _staged_story(monkeypatch, tmp_path)
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", "pierce,northgate")
    status, body = routes.handle_get_story("northgate_ig", created["request_id"],
                                           store=store)
    assert status == 404
    assert body["ok"] is False


def test_get_story_404s_on_an_unknown_id(monkeypatch, tmp_path):
    store, _created = _staged_story(monkeypatch, tmp_path)
    status, _body = routes.handle_get_story("pierce_ig", "sr_nope", store=store)
    assert status == 404


def test_list_stories_returns_history_newest_first_with_bounds(monkeypatch, tmp_path):
    store, created = _staged_story(monkeypatch, tmp_path)
    status, body = routes.handle_list_stories("pierce_ig", store=store)
    assert status == 200
    assert [s["request_id"] for s in body["stories"]] == [created["request_id"]]
    # the picker's bounds come from Echo, not a number retyped in the client.
    b = body["clip_bounds"]
    assert b["min_clips"] == 2
    assert b["max_used_clips"] >= 10
    assert b["seg_min_sec"] == 3.0


def test_list_stories_never_shows_another_gyms_renders(monkeypatch, tmp_path):
    store, _created = _staged_story(monkeypatch, tmp_path)
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", "pierce,northgate")
    status, body = routes.handle_list_stories("northgate_ig", store=store)
    assert status == 200
    assert body["stories"] == []


def test_read_lane_is_off_when_the_gym_is_not_armed(monkeypatch):
    monkeypatch.delenv("STORY_STUDIO_RENDER", raising=False)
    monkeypatch.delenv("STORY_STUDIO_RENDER_GYMS", raising=False)
    assert routes.handle_list_stories("pierce_ig")[0] == 403
    assert routes.handle_get_story("pierce_ig", "sr_1")[0] == 403


def test_list_stories_answers_bounds_even_with_no_store(monkeypatch):
    """The picker asks for bounds on mount. An environment with no story store must
    still answer them (empty history), not break the picker."""
    _arm(monkeypatch)

    class _Unavailable:
        def available(self):
            return False

    status, body = routes.handle_list_stories("pierce_ig", store=_Unavailable())
    assert status == 200
    assert body["stories"] == []
    assert body["clip_bounds"]["max_used_clips"] >= 10


def test_a_store_read_failure_is_a_502_not_a_crash(monkeypatch):
    _arm(monkeypatch)

    class _Broken:
        def available(self):
            return True

        def list_renders(self, gym_id, status=None):
            raise RuntimeError("postgrest down")

    status, body = routes.handle_list_stories("pierce_ig", store=_Broken())
    assert status == 502
    assert body["ok"] is False
    assert "RuntimeError" in body["error"]
