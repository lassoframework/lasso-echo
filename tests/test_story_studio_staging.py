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


def test_brief_containing_an_email_holds_never_burns_it_onto_the_video(monkeypatch, tmp_path):
    """REAL DATA EXPOSURE (Zanshin Fitness / Pete Mongeau, 2026-09-07 live incident): the
    Story Studio brief is client-typed free text that story_grounding takes VERBATIM as the
    overlay copy (source=brief). Pete typed his own email, pete@zanshin.fit, into the
    one-line "what is this story about?" field and it was rendered ALL CAPS onto a video and
    staged into the approval queue -- nothing on the path from text box to burned pixels ever
    checked for PII. copy_gate.violations now hard-blocks any email address in client-facing
    text (the same gate every caption/overlay/report already passes through), so this must
    HOLD honestly instead of rendering."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    store = _FakeStore()
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "pete@zanshin.fit",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=store,
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert "copy_gate" in res["reason"] or "email" in res["reason"].lower()
    # Nothing reached the render/persist steps at all: no story_render row, no approval card.
    assert store.renders == []
    assert not _STAGED_ROWS


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

    store = _FakeStore()
    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=store,
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path), cal_store=_Boom())
    assert res["status"] == "held"
    assert "approval queue" in res["reason"]
    assert not _STAGED_ROWS
    # REGRESSION (found live 2026-09-08, Zanshin Fitness / Pete Mongeau): step 8 already wrote
    # story_render PENDING before this calendar attempt ran, and nothing used to clean it up
    # on a HELD outcome — an orphaned "pending" render, with whatever overlay copy it burned,
    # sat there forever even though the coach was correctly told nothing was staged.
    assert [r["status"] for r in store.renders] == [ss.STATUS_DENIED], (
        "a HELD outcome must not leave story_render claiming pending"
    )


class _UniqueIdStore:
    """Schema-faithful fake: story_request/story_render both have `id` as a PRIMARY
    KEY in the real Supabase tables, so a second INSERT with the same id fails (a 409
    in production, via story_studio_store.SupabaseStoryStudioStore.insert_request /
    insert_render). _FakeStore above does not enforce this -- it just appends -- which
    is why the calendar-insert-failure tests passed even though the same held path
    left a real Zanshin row stuck at status=pending in production (2026-09-07). This
    fake catches what a permissive fake hides: verify by making failure possible, not
    by asserting the happy path never fails."""

    def __init__(self):
        self.requests = {}
        self.renders = {}

    def available(self):
        return True

    def insert_request(self, row):
        rid = row.get("id")
        if rid in self.requests:
            raise RuntimeError("duplicate key value violates unique constraint")
        self.requests[rid] = dict(row)
        return dict(row)

    def insert_render(self, row):
        rid = row.get("id")
        if rid in self.renders:
            raise RuntimeError("duplicate key value violates unique constraint")
        self.renders[rid] = dict(row)
        return dict(row)

    def update_request(self, rid, fields, gym_id=None):
        if rid in self.requests:
            self.requests[rid].update(fields)
        return True

    def update_render(self, rid, fields, gym_id=None):
        # story_render.status is CHECK-constrained in production to exactly
        # ('pending', 'denied') -- there is no 'held'. A fake that accepted any string
        # here would hide the exact mistake this fix almost shipped: writing 'held' to
        # story_render and having it silently swallowed by the caller's try/except.
        if "status" in fields and fields["status"] not in ("pending", "denied"):
            raise RuntimeError(
                f"new row for relation \"story_render\" violates check constraint "
                f"\"story_render_status_check\": {fields['status']!r}")
        if rid in self.renders:
            self.renders[rid].update(fields)
        return True


def test_calendar_insert_failure_corrects_the_persisted_rows_to_held(monkeypatch, tmp_path):
    """Reproduces the real Zanshin bug (story_request/story_render 8e1b4bdf-...,
    2026-09-07): _persist() already INSERTed both rows as PENDING before the
    calendar-staging step ran. When that step then fails, _held() must UPDATE those
    same rows to status=held with the reason -- not attempt a second INSERT, which a
    real unique-id constraint rejects (silently swallowed by _held()'s own
    try/except), leaving both rows stuck at status=pending forever with no card in
    the approval queue and no honest reason anywhere a human or Pete could see."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"z")
    store = _UniqueIdStore()

    class _Boom:
        def insert_rows(self, gym_id, rows):
            raise RuntimeError("supabase down")

    res = ss.create_story(
        {"gym_id": "pierce", "asset_ids": ["a0"], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        candidates=_cands("pierce"), store=store,
        music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path), cal_store=_Boom())

    assert res["status"] == "held"
    assert len(store.requests) == 1, "must correct the existing row, never insert a second"
    assert len(store.renders) == 1
    req_row = next(iter(store.requests.values()))
    render_row = next(iter(store.renders.values()))
    assert req_row["status"] == "held", "orphaned pending story_request, exactly the Zanshin bug"
    assert req_row.get("hold_reason"), "the hold reason must be recorded, not silently dropped"
    # story_render.status only allows ('pending', 'denied') in production (see deny()'s
    # own comment) -- there is no 'held' value for it, so DENIED is the correct terminal
    # state for a render that will never be used, matching deny()'s own precedent.
    assert render_row["status"] == "denied", "orphaned pending story_render, exactly the Zanshin bug"


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


# ---- the identity anchor on a SERVER-BUILT request (2026-09-04) -------------
# gym_event.story_studio_create_request builds a request with NO identity_tokens key --
# it has no frontend to supply one -- so story_overlay refused it and the event story
# offer HELD every single time. create_story now reads the anchor off the gym's own row
# when the request omits it. The refusal rail is untouched: a gym with no name still
# resolves to nothing and still HOLDS.
def _run_story(monkeypatch, tmp_path, request):
    """Drive create_story the way the other tests here do, with the render armed and
    everything network-bound inert."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"
    audio.write_bytes(b"ID3fake")
    return ss.create_story(
        request, candidates=_cands("pierce"), assets_by_id={},
        analysis={"confidence": 0.9, "tags": ["workout"]},
        store=_FakeStore(), music_library=_RealPathLibrary(str(audio)),
        render_fn=_fake_render, output_dir=str(tmp_path))


def _event_shaped_request():
    """EXACTLY the shape gym_event.story_studio_create_request returns: note the
    complete absence of an identity_tokens key."""
    return {
        "gym_id": "pierce", "account_key": "pierce_ig",
        "asset_ids": ["a0", "a1", "a2"],
        "brief": "Summer Shred kickoff saturday come train with us",
        "template": None, "music_mood": None, "requested_by": "event:one-tap",
    }


def test_a_server_built_request_stages_using_the_gyms_own_name(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.gym_identity.tokens_for",
                        lambda base, **k: ["Pierce Fitness", "Carmel"])
    res = _run_story(monkeypatch, tmp_path, _event_shaped_request())
    assert res["status"] == "staged", res.get("reason")
    burned = (res.get("story_render") or {}).get("overlay_text_final") or ""
    assert "SUMMER SHRED" in burned


def test_a_gym_with_no_resolvable_name_still_HOLDS(monkeypatch, tmp_path):
    """The rail is untouched. Echo never burns an unbranded Story just because the
    lookup came back empty."""
    monkeypatch.setattr("agent.gym_identity.tokens_for", lambda base, **k: [])
    res = _run_story(monkeypatch, tmp_path, _event_shaped_request())
    assert res["status"] == "held"
    assert "identity" in (res.get("reason") or "").lower()


def test_a_request_that_DOES_carry_tokens_is_not_overridden(monkeypatch, tmp_path):
    """A coach tap supplies its own anchor (the portal resolves it). The fallback must
    not second-guess a caller that already answered."""
    called = {"n": 0}

    def _never(base, **k):
        called["n"] += 1
        return ["WRONG"]

    monkeypatch.setattr("agent.gym_identity.tokens_for", _never)
    req = _event_shaped_request()
    req["identity_tokens"] = ["Pierce Fitness"]
    res = _run_story(monkeypatch, tmp_path, req)
    assert res["status"] == "staged", res.get("reason")
    assert called["n"] == 0, "the gym row was queried despite the request answering"
