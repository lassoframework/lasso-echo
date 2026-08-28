"""
Story Studio §4: the portal "Create a Story" route surface. The render lane (create
/ deny) is gated per gym (default OFF, pilot allowlist); the sort queue (list /
resolve) is gated by STORY_CLASSIFIER (default ON). Every create stages PENDING or
HOLDS; deny returns segments; the footage picker reuses the gym media pool.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_studio_routes as routes  # noqa: E402
from agent import story_music as sm  # noqa: E402
from agent import story_composer as comp  # noqa: E402


class _FakeStore:
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

    def update_request(self, rid, fields):
        return True


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


def _fake_render(plan, *, output_dir, ask_frame_text="", music_path=""):
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
        "pierce_ig", {"asset_ids": ["a0"], "brief": "A win"},
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
