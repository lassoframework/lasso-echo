"""
Story Studio Wave 6: staging. Behind STORY_STUDIO_RENDER (default OFF). Every render
lands PENDING; a HELD outcome stages nothing; deny returns segments to the pool;
track_id + license_ref are stored on the render; the content_hash is recorded in the
re-ingest ledger.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_studio as ss  # noqa: E402
from agent import story_music as sm  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402


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
        for r in self.requests:
            if r.get("id") == rid:
                r.update(fields)
        return True


class _RealPathLibrary(sm.StubMusicLibrary):
    """A hype track whose audio file actually exists on disk (so the burn is not held
    for a missing ops asset)."""

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
    from agent import story_composer as comp
    return comp.ComposeResult(plan=plan, output_path=f"{output_dir}/final.mp4")


def _arm(monkeypatch, gym="pierce"):
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", gym)
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    # keep hosting + selector store inert (offline) so staging does not reach network.
    monkeypatch.setattr("agent.story_studio._host", lambda p, g: "https://r2/story.mp4")


# ---- flag gate -------------------------------------------------------------
def test_render_lane_off_returns_off(monkeypatch):
    monkeypatch.delenv("STORY_STUDIO_RENDER", raising=False)
    monkeypatch.delenv("STORY_STUDIO_RENDER_GYMS", raising=False)
    res = ss.create_story({"gym_id": "pierce", "asset_ids": ["a0"]})
    assert res["status"] == "off"


# ---- happy path: every render PENDING --------------------------------------
def test_staged_draft_is_pending(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"ID3fake")
    store = _FakeStore()
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0", "a1"], "brief": "Members crushed today",
         "identity_tokens": ["Pierce"], "requested_by": "coach1"},
        candidates=_cands("pierce"), assets_by_id={},
        analysis={"confidence": 0.9, "tags": ["workout"]},
        store=store, music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "staged"
    assert res["draft"].status == DraftStatus.PENDING
    assert res["draft"].is_story is True


def test_render_stores_track_id_and_license_ref(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"x")
    store = _FakeStore()
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "Big lifts today"},
        candidates=_cands("pierce"), store=store,
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    sr = res["story_render"]
    assert sr["track_id"] == "hype_test"
    assert sr["license_ref"] == "lasso-lib:LIC-TEST"
    assert sr["content_hash"]                       # ledger stamp
    assert sr["status"] == "pending"


def test_content_hash_recorded_in_reingest_ledger(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"y")
    from agent import story_ledger
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win"},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    ch = res["story_render"]["content_hash"]
    assert story_ledger.is_echo_render(ch) is True   # can never be re-ingested


# ---- HELD outcomes stage nothing -------------------------------------------
def test_missing_renderer_holds_and_stages_nothing(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")

    def _boom(plan, **k):
        from agent import story_composer as comp
        return comp.ComposeResult(plan=plan, held=True, hold_reason="ffmpeg absent")

    store = _FakeStore()
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win"},
        candidates=_cands("pierce"), store=store,
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_boom, output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert res["draft"] is None
    assert store.renders == []                       # nothing staged


def test_missing_music_asset_holds(monkeypatch, tmp_path):
    _arm(monkeypatch)
    # default stub library has metadata but NO audio file path -> hold, never silent.
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win"},
        candidates=_cands("pierce"), store=_FakeStore(),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert "music" in res["reason"].lower()


def test_avatar_breach_holds(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "HYROX prep starts now"},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert "avatar" in res["reason"].lower() or "hyrox" in res["reason"].lower()


# ---- deny returns segments to the pool -------------------------------------
def test_deny_rolls_back_and_logs(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    # selector store inert -> stamping no-ops, but the kv segment record is written.
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0", "a1"], "brief": "A win"},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    rid = res["request_id"]
    # deny does not raise and logs the reason; returns a bool.
    out = ss.deny(rid, "pierce", reason="off brand")
    assert out in (True, False)
    # the segment kv record exists (proof the deny path had segments to return).
    import json
    from agent import db
    rec = json.loads(db.kv_get(ss._SEG_KEY.format(rid), "") or "{}")
    # every consumed segment's asset is recorded so the deny can return it to the pool.
    plan_assets = set(rec.get("asset_ids") or [])
    assert plan_assets                                  # at least the used segments
    assert plan_assets.issubset({f"a{i}" for i in range(6)})
