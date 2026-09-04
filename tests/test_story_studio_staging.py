"""
Story Studio Wave 6: staging. Behind STORY_STUDIO_RENDER (default OFF). Every render
lands PENDING; a HELD outcome stages nothing; deny returns segments to the pool;
track_id + license_ref are stored on the render; the content_hash is recorded in the
re-ingest ledger.
"""
import os
import sys

import pytest

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

    def update_request(self, rid, fields, gym_id=None):
        for r in self.requests:
            if r.get("id") == rid:
                r.update(fields)
        return True

    def update_render(self, rid, fields, gym_id=None):
        for r in self.renders:
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


def _fake_render(plan, *, output_dir, ask_frame_text="", music_path="", **_k):
    from agent import story_composer as comp
    return comp.ComposeResult(plan=plan, output_path=f"{output_dir}/final.mp4")


# The approval-queue row Story Studio writes. Patched in for every test in this file
# so the REAL _stage_calendar_row path runs (row build + insert), fully offline.
_STAGED_ROWS = []


class _FakeCalStore:
    def insert_rows(self, gym_id, rows):
        out = []
        for i, r in enumerate(rows or []):
            row = dict(r)
            row["id"] = f"cal-{len(_STAGED_ROWS) + i}"
            _STAGED_ROWS.append((gym_id, row))
            out.append(row)
        return out


@pytest.fixture(autouse=True)
def _cal(monkeypatch):
    _STAGED_ROWS.clear()
    monkeypatch.setattr("agent.config.portal_calendar_supabase_enabled", lambda: True)
    monkeypatch.setattr("agent.portal_calendar_store.SupabaseCalendarStore",
                        lambda *a, **k: _FakeCalStore())
    yield


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


def test_create_story_hands_the_validated_overlay_to_the_renderer(monkeypatch, tmp_path):
    """THE call-site guard for page 4's bug. story_composer has a test proving it
    burns overlay_frames when it receives them, and _default_end_frame refuses to
    drop a requested ask -- but NOTHING pinned the hand-off itself. Delete
    `overlay_frames=overlay.frames, ask_frame_lines=overlay.ask_frame` from
    story_studio.create_story and every other test still passed while every Story
    rendered with no hook, no anchor and no ask: exactly the shipped bug. This test
    asserts the validated OverlaySpec actually reaches the renderer."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"ID3fake")
    seen = {}

    def _capture_render(plan, *, output_dir, ask_frame_text="", ask_frame_lines=None,
                        overlay_frames=None, music_path="", **_k):
        from agent import story_composer as comp
        seen.update(ask_frame_text=ask_frame_text, ask_frame_lines=ask_frame_lines,
                    overlay_frames=overlay_frames)
        return comp.ComposeResult(plan=plan, output_path=f"{output_dir}/final.mp4")

    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0", "a1"],
         "brief": "Kitchener mornings hit different at Pierce Fitness",
         "identity_tokens": ["Pierce Fitness", "Kitchener"], "requested_by": "coach1"},
        candidates=_cands("pierce"), assets_by_id={},
        store=_FakeStore(), music_library=_RealPathLibrary(str(audio)),
        render_fn=_capture_render, output_dir=str(tmp_path))

    assert res["status"] == "staged"
    assert seen["overlay_frames"], \
        "create_story staged a Story without handing the hook frames to the renderer"
    assert seen["ask_frame_lines"], \
        "create_story staged a Story without handing the validated ask frame over"
    flat = " ".join(ln for fr in seen["overlay_frames"] for ln in fr)
    assert "KITCHENER" in flat, "the identity anchor never reached the burn"
    assert all(ln == ln.upper() for fr in seen["overlay_frames"] for ln in fr)
    assert seen["ask_frame_text"], "the ask text never reached the renderer"


def test_render_stores_track_id_and_license_ref(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"x")
    store = _FakeStore()
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "Big lifts today",
         "identity_tokens": ["Pierce"]},
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
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
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
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=_FakeStore(),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert "music" in res["reason"].lower()


def test_empty_dir_library_still_holds(monkeypatch, tmp_path):
    # AGENT_STORY_MUSIC_DIR armed but the manifest declares no tracks (the shipped
    # /data/story-music template): behavior is EXACTLY today's — the render HOLDS on
    # the missing licensed audio, never posts silently, never crashes.
    _arm(monkeypatch)
    music_dir = tmp_path / "story-music"
    music_dir.mkdir()
    (music_dir / "manifest.json").write_text('{"tracks": []}', encoding="utf-8")
    monkeypatch.setenv("AGENT_STORY_MUSIC_DIR", str(music_dir))
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=_FakeStore(),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert "music" in res["reason"].lower()


def test_avatar_breach_holds(monkeypatch, tmp_path):
    # The avatar rail is OFF by default since Blake's 2026-09-01 ruling (CrossFit,
    # hyrox and competitive athletics are allowed). This test describes the rail's
    # behavior WHEN ARMED, so it arms it explicitly.
    monkeypatch.setenv("AGENT_AVATAR_ATHLETE_RAIL", "true")
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "HYROX prep starts now",
         "identity_tokens": ["Pierce"]},
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
        {"gym_id": "pierce", "asset_ids": ["a0", "a1"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
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


def test_deny_also_denies_the_render_row(monkeypatch, tmp_path):
    """story_render.status is constrained to exactly ('pending','denied') and deny()
    never wrote 'denied' — so every denied render's row still read PENDING forever
    while its request and its calendar card both said denied. Found by querying live
    Supabase after 10 verification renders: 12 phantom pending story_render rows."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    store = _FakeStore()
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0", "a1"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=store,
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    rid = res["request_id"]
    assert [r["status"] for r in store.renders] == [ss.STATUS_PENDING]

    ss.deny(rid, "pierce", reason="off brand", store=store)
    assert [r["status"] for r in store.renders] == [ss.STATUS_DENIED], \
        "deny() left the story_render row claiming pending"
    assert [r["status"] for r in store.requests] == [ss.STATUS_DENIED]


# ---- the approval row is REALLY written (the lane used to only return a Draft) -----
def test_staged_story_writes_a_pending_approval_row(monkeypatch, tmp_path):
    """REGRESSION: create_story built a Draft, returned it, and called that "staged"
    while NO content_calendar row was ever written — the coach got a success response
    and no approval card ever appeared. A staged story must leave a real PENDING row."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "staged"
    assert len(_STAGED_ROWS) == 1, "no approval row reached the calendar"
    gym_id, row = _STAGED_ROWS[0]
    assert gym_id == "pierce"
    assert row["status"] == "pending", "the approval gate must be intact"
    assert row["format"] == "story"
    assert row.get("image_url"), "the row must carry the rendered story media"
    assert res["calendar_row_id"] == row["id"]
    assert res["story_render"]["calendar_row_id"] == row["id"]


def test_calendar_insert_failure_holds_instead_of_claiming_staged(monkeypatch, tmp_path):
    """If the row cannot be written the coach must NOT be told the story is staged."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")

    class _Boom:
        def insert_rows(self, gym_id, rows):
            raise RuntimeError("supabase down")

    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path), cal_store=_Boom())
    assert res["status"] == "held"
    assert "approval queue" in res["reason"]
    assert not _STAGED_ROWS


def test_unconfigured_calendar_store_holds(monkeypatch, tmp_path):
    """An unconfigured store must HOLD, never report a staged story nobody can approve."""
    _arm(monkeypatch)
    monkeypatch.setattr("agent.config.portal_calendar_supabase_enabled", lambda: False)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win"},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert not _STAGED_ROWS


def test_staged_story_row_targets_instagram_not_the_gym_key(monkeypatch, tmp_path):
    """content_calendar.account must be a real PLATFORM. It defaulted to gym_id, and
    calendar_autopublish._account_for skips anything that is not instagram/facebook —
    so the row would have sat pending forever, unpublishable and unexplained."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "staged"
    _gym, row = _STAGED_ROWS[0]
    assert row["account"] == "instagram", f"unpublishable account: {row['account']!r}"
    # The publisher routes on this column and skips anything that is not a platform.
    from agent import calendar_autopublish as cap
    assert cap._account_for(row, gym_id="eng") is not None, \
        "the publisher cannot route this row"


def test_story_row_with_no_publish_target_is_refused(monkeypatch):
    """The guard itself: a row whose account is not a platform must never be staged."""
    from agent.drafter import Draft, DraftStatus
    bad = Draft(draft_id="story_x", account_key="pierce", platform="pierce",
                caption="", hashtags=[], creative_path="/tmp/x.mp4",
                creative_public_url="https://cdn/x.mp4", scheduled_for="",
                status=DraftStatus.PENDING, is_story=True, day_key="2026-09-05",
                draft_type="story_studio", category="hype_montage")
    row_id, err = ss._stage_calendar_row("pierce", bad, cal_store=_FakeCalStore())
    assert row_id is None and "publish target" in err


def test_deny_also_denies_the_approval_row_it_created(monkeypatch, tmp_path):
    """Now that create_story writes a real PENDING row, denying only the story_request
    would leave that card in the approval queue — still approvable by anyone using the
    normal calendar UI, and pointing at segments the deny just recycled."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=_FakeStore(),
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    row_id = res["calendar_row_id"]
    assert row_id

    denied = []

    class _CalStore:
        def deny_with_reason(self, gym, rid, reason):
            denied.append((gym, rid, reason))
            return {"id": rid}

    ss.deny(res["request_id"], "pierce", reason="not on brand",
            store=_FakeStore(), cal_store=_CalStore())
    assert denied and denied[0][1] == row_id, "the approval row was left orphaned"
    assert denied[0][0] == "pierce"
    assert "not on brand" in denied[0][2]
