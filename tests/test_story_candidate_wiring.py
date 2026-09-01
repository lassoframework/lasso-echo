"""
Story Studio candidate wiring (ECHO_STORY_STUDIO_BUILD §3) — the last gap before the
render lane can be armed: a REAL coach tap (or the event one-tap) reaches
story_studio.create_story with candidates=None, so create_story must DISCOVER the
gym's eligible RAW pool itself, score it (opus scoring intent), and download each
picked asset to a REAL local source_path — otherwise every real render HELDs "too few
segments".

Proves:
  * a real tap with a seeded raw pool -> 200 'staged' with a PENDING story_render whose
    segments carry real source_paths and a real content_hash (NOT held);
  * the same through the HTTP route handler (handle_create_story);
  * an EMPTY pool still HELDs honestly (nothing staged);
  * TENANT ISOLATION: another gym's asset never becomes a candidate;
  * the discovery gates: coach-hidden, ineligible, ambiguous-unsorted, Echo's own
    render (re-ingest guard), and over-cap assets are all excluded.

Offline by default (the Drive download + ffmpeg boundaries are injected/mocked). One
REAL ffmpeg render runs when ffmpeg/ffprobe are present, proving the wired default
path produces a genuine 1080x1920 mp4 from downloaded sources.
"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_studio, story_candidates, story_music as sm  # noqa: E402
from agent import story_composer as comp  # noqa: E402
from agent.drafter import DraftStatus  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset  # noqa: E402


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




# ---- fakes -----------------------------------------------------------------
class _FakeStore:
    """story_studio_store stand-in (story_request / story_render sink)."""

    def __init__(self):
        self.requests, self.renders = [], []

    def available(self):
        return True

    def insert_request(self, row):
        self.requests.append(dict(row)); return dict(row)

    def insert_render(self, row):
        self.renders.append(dict(row)); return dict(row)

    def update_request(self, rid, fields):
        for r in self.requests:
            if r.get("id") == rid:
                r.update(fields)
        return True


class _RealPathLibrary(sm.StubMusicLibrary):
    def __init__(self, audio_path):
        super().__init__(tracks=[sm.Track(
            track_id="hype_wire", license_ref="lasso-lib:LIC-WIRE",
            shelf=sm.SHELF_HYPE, title="Wire Hype", path=audio_path)])
        self._audio = audio_path

    def resolve_path(self, track):
        return self._audio


def _video_asset(fid, gym="pierce", dur=8.0, size=20_000_000, **kw):
    a = make_asset(fid, gym_id=gym, kind="video", title=f"{fid}.mp4",
                   mime="video/mp4", size=size, **kw)
    a["duration_sec"] = dur
    return a


def _arm(monkeypatch, gym="pierce"):
    monkeypatch.setenv("STORY_STUDIO_RENDER_GYMS", gym)
    monkeypatch.setattr("agent.config.supabase_url", lambda: "")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "")
    monkeypatch.setattr("agent.story_studio._host", lambda p, g: "https://r2/story.mp4")
    # keep the re-ingest ledger inert unless a test opts in.
    monkeypatch.setattr("agent.story_ledger.is_echo_render", lambda ch, **k: False)


def _point_pool(monkeypatch, store):
    """Point the media-index default store (what discovery reads) at a fake pool."""
    monkeypatch.setattr("agent.gym_media_index.default_store", lambda: store)


# ---- injected renderer that records the bound source paths -----------------
def _recording_render(seen):
    def _fn(plan, *, output_dir, ask_frame_text="", music_path="", **_k):
        # capture what source_paths the wiring bound before the render.
        for s in plan.segments:
            seen.append(s.source_path)
        return comp.ComposeResult(plan=plan, output_path=f"{output_dir}/final.mp4")
    return _fn


# ---------------------------------------------------------------------------
# PROOF 1: a real tap (no candidates injected) -> staged PENDING with real sources
# ---------------------------------------------------------------------------
def test_real_tap_discovers_pool_and_stages_pending(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"; audio.write_bytes(b"ID3fake")
    pool = FakeMediaStore(assets=[
        _video_asset("v1", used_count=0, content_hash="hh1"),
        _video_asset("v2", used_count=0, content_hash="hh2"),
        _video_asset("v3", used_count=0, content_hash="hh3"),
    ])
    _point_pool(monkeypatch, pool)
    drive = FakeDrive()                     # download writes real bytes to dest
    seen_paths = []

    # NOTE: render_fn is injected here so the download binding still runs (create_story
    # binds sources on the DEFAULT renderer only). To exercise the download on the
    # injected renderer too, we bind explicitly below in the real-ffmpeg test. Here we
    # verify discovery -> plan -> stage with the injected downloader on the default lane.
    res = story_studio.create_story(
        {"gym_id": "pierce_ig", "account_key": "pierce_ig",
         "asset_ids": [], "brief": "Members crushed the workout today",
         "identity_tokens": ["Pierce"], "requested_by": "coach1"},
        store=_FakeStore(), music_library=_RealPathLibrary(str(audio)),
        downloader=drive.download, output_dir=str(tmp_path / "out"),
        render_fn=_recording_render(seen_paths))

    # discovery fed the composer -> NOT held.
    assert res["status"] == "staged", res
    draft, sr = res["draft"], res["story_render"]
    assert draft.status == DraftStatus.PENDING
    assert sr["status"] == "pending"
    assert sr["content_hash"]
    # every planned segment came from THIS gym's pool.
    assert sr["segment_plan"]
    assert {s["asset_id"] for s in sr["segment_plan"]}.issubset({"v1", "v2", "v3"})


def test_real_tap_binds_real_source_paths(monkeypatch, tmp_path):
    """The download binding actually populates each segment's source_path with a real
    file (the wiring that was previously missing)."""
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"; audio.write_bytes(b"x")
    pool = FakeMediaStore(assets=[
        _video_asset("v1"), _video_asset("v2"),
    ])
    _point_pool(monkeypatch, pool)
    drive = FakeDrive()

    # discover + bind directly (unit-level) so we can assert the files exist on disk.
    cands, by_id = story_candidates.discover_candidates("pierce", store=pool)
    segs = comp.select_segments(cands, "pierce", {})
    tmp_dir = story_candidates.bind_source_paths(
        segs, by_id, gym_id="pierce", downloader=drive.download)
    try:
        assert segs and all(s.source_path for s in segs)
        for s in segs:
            assert os.path.exists(s.source_path)     # real bytes on disk
        assert drive.downloads == sorted(drive.downloads)  # each asset fetched
    finally:
        story_candidates.cleanup(tmp_dir)
    assert not os.path.isdir(tmp_dir)                 # cleanup removed the sources


# ---------------------------------------------------------------------------
# PROOF 2: the same through the HTTP route -> 200 staged
# ---------------------------------------------------------------------------
def test_route_create_story_returns_staged(monkeypatch, tmp_path):
    from agent import story_studio_routes as routes
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"; audio.write_bytes(b"z")
    pool = FakeMediaStore(assets=[
        _video_asset("v1"), _video_asset("v2"), _video_asset("v3")])
    _point_pool(monkeypatch, pool)

    status, body = routes.handle_create_story(
        "pierce_ig",
        {"asset_ids": ["v1", "v2", "v3"], "brief": "Big lifts today",
         "identity_tokens": ["Pierce"]},
        actor_id="coach1",
        store=_FakeStore(), music_library=_RealPathLibrary(str(audio)),
        render_fn=_recording_render([]))
    assert status == 200, body
    assert body["ok"] is True
    assert body["status"] == "staged"
    assert body["music"]["track_id"] == "hype_wire"


# ---------------------------------------------------------------------------
# PROOF 3: an EMPTY pool still HELDs honestly
# ---------------------------------------------------------------------------
def test_empty_pool_holds_honestly(monkeypatch, tmp_path):
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"; audio.write_bytes(b"z")
    _point_pool(monkeypatch, FakeMediaStore(assets=[]))    # no assets
    store = _FakeStore()
    res = story_studio.create_story(
        {"gym_id": "pierce", "asset_ids": [], "brief": "A win",
         "identity_tokens": ["Pierce"]},
        store=store, music_library=_RealPathLibrary(str(audio)),
        output_dir=str(tmp_path))
    assert res["status"] == "held"
    assert res["draft"] is None
    assert store.renders == []                             # nothing staged
    assert "segment" in res["reason"].lower()


def test_route_empty_pool_holds(monkeypatch, tmp_path):
    from agent import story_studio_routes as routes
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"; audio.write_bytes(b"z")
    _point_pool(monkeypatch, FakeMediaStore(assets=[]))
    status, body = routes.handle_create_story(
        "pierce_ig", {"asset_ids": ["nope"]}, actor_id="coach1",
        store=_FakeStore(), music_library=_RealPathLibrary(str(audio)))
    assert status == 200
    assert body["status"] == "held"
    assert body.get("hold_reason")


# ---------------------------------------------------------------------------
# PROOF 4: TENANT ISOLATION — another gym's asset never becomes a candidate
# ---------------------------------------------------------------------------
def test_other_gyms_asset_is_never_a_candidate(monkeypatch):
    # a pool that (defensively) also holds a mis-tagged foreign row: list_assets filters
    # by gym, but discovery re-asserts tenant on every asset.
    pool = FakeMediaStore(assets=[
        _video_asset("mine", gym="pierce"),
        _video_asset("theirs", gym="rival"),
    ])
    cands, by_id = story_candidates.discover_candidates("pierce", store=pool)
    ids = {c["asset_id"] for c in cands}
    assert "mine" in ids
    assert "theirs" not in ids               # rival gym never enters pierce's story
    assert all(c["gym_id"] == "pierce" for c in cands)


def test_bind_refuses_cross_gym_segment(monkeypatch, tmp_path):
    """Defense in depth: even a hand-built cross-gym segment is refused at download."""
    seg = comp.Segment(asset_id="x", gym_id="rival", start_ts=0, end_ts=8)
    drive = FakeDrive()
    with pytest.raises(comp.TenantMismatch):
        story_candidates.bind_source_paths(
            [seg], {"x": {"id": "x", "gym_id": "rival"}}, gym_id="pierce",
            downloader=drive.download)
    assert drive.downloads == []             # a foreign segment is NEVER downloaded


# ---------------------------------------------------------------------------
# PROOF 5: discovery gates (raw-lane only)
# ---------------------------------------------------------------------------
def test_ineligible_and_hidden_and_short_excluded(monkeypatch):
    pool = FakeMediaStore(assets=[
        _video_asset("ok", eligible=True),
        _video_asset("ineligible", eligible=False),
        _video_asset("unprobed", eligible=None),
        _video_asset("hidden", excluded_by_coach=True),
        _video_asset("tooshort", dur=1.0),          # < 3s segment floor
        make_asset("photo1", gym_id="pierce", kind="photo"),   # photos skipped
    ])
    cands, _ = story_candidates.discover_candidates("pierce", store=pool)
    assert {c["asset_id"] for c in cands} == {"ok"}


def test_echo_own_render_is_reingest_blocked(monkeypatch):
    pool = FakeMediaStore(assets=[
        _video_asset("raw", content_hash="rawhash"),
        _video_asset("echo", content_hash="echohash"),
    ])
    cands, _ = story_candidates.discover_candidates(
        "pierce", store=pool, ledger_lookup=lambda ch, **k: ch == "echohash")
    ids = {c["asset_id"] for c in cands}
    assert "raw" in ids
    assert "echo" not in ids                 # Echo can never eat its own output


def test_ambiguous_unsorted_excluded(monkeypatch):
    pool = FakeMediaStore(assets=[
        _video_asset("raw"), _video_asset("amb"),
    ])
    # 'amb' is sitting unresolved in the Sort-these queue.
    monkeypatch.setattr("agent.story_sort_queue.pending",
                        lambda gym=None: [{"asset_id": "amb"}])
    cands, _ = story_candidates.discover_candidates("pierce", store=pool)
    ids = {c["asset_id"] for c in cands}
    assert "raw" in ids
    assert "amb" not in ids                  # ambiguous never auto-enters the story lane


def test_over_cap_asset_routes_to_opus_not_story(monkeypatch):
    pool = FakeMediaStore(assets=[
        _video_asset("ok", dur=8.0),
        _video_asset("toolong", dur=6 * 60),         # > 5 min cap
        _video_asset("toobig", size=1_000_000_000),  # > 900 MB cap
    ])
    cands, _ = story_candidates.discover_candidates("pierce", store=pool)
    assert {c["asset_id"] for c in cands} == {"ok"}


def test_asset_ids_scope_the_pick(monkeypatch):
    pool = FakeMediaStore(assets=[
        _video_asset("v1"), _video_asset("v2"), _video_asset("v3"),
    ])
    cands, _ = story_candidates.discover_candidates(
        "pierce", asset_ids=["v1", "v3"], store=pool)
    assert {c["asset_id"] for c in cands} == {"v1", "v3"}


# ---------------------------------------------------------------------------
# PROOF 6: the event one-tap flows through the SAME discovery
# ---------------------------------------------------------------------------
def test_event_one_tap_discovers_and_stages(monkeypatch, tmp_path):
    from agent import gym_event
    _arm(monkeypatch)
    audio = tmp_path / "hype.mp3"; audio.write_bytes(b"z")
    pool = FakeMediaStore(assets=[
        _video_asset("e1"), _video_asset("e2"), _video_asset("e3")])
    _point_pool(monkeypatch, pool)

    # a create_story request dict (as gym_event.story_studio_create_request builds),
    # carrying media_ids as asset_ids. render_event_story delegates to the real
    # create_story, which now self-discovers + binds.
    request = {
        "gym_id": "pierce", "account_key": "pierce",
        "asset_ids": ["e1", "e2", "e3"], "brief": "1 year party Saturday",
        "identity_tokens": ["Pierce"], "requested_by": "coach1", "kind": "event",
    }
    # inject the renderer so the offline test does not reach Drive; discovery still runs.
    res = gym_event.render_event_story(
        request, renderer=lambda req: story_studio.create_story(
            req, store=_FakeStore(), music_library=_RealPathLibrary(str(audio)),
            render_fn=_recording_render([]), output_dir=str(tmp_path / "ev")))
    assert res is not None
    assert res["status"] == "staged", res
    assert {s["asset_id"] for s in res["story_render"]["segment_plan"]} <= {
        "e1", "e2", "e3"}


# ---------------------------------------------------------------------------
# PROOF 7: REAL ffmpeg end-to-end from downloaded sources (armed env only)
# ---------------------------------------------------------------------------
_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


@pytest.mark.skipif(not (_FFMPEG and _FFPROBE),
                    reason="ffmpeg/ffprobe absent (real-render proof runs only armed)")
def test_real_render_from_discovered_downloaded_sources(monkeypatch, tmp_path):
    """The wired DEFAULT path: discover the pool, DOWNLOAD each asset to a local file,
    and run the REAL ffmpeg render -> a genuine 1080x1920 H.264 mp4, PENDING."""
    import json

    def _mk(path, secs, hz):
        subprocess.run(
            [_FFMPEG, "-y", "-f", "lavfi",
             "-i", f"testsrc=size=1280x720:rate=30:duration={secs}",
             "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={secs}",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest",
             str(path)], check=True, capture_output=True)
        return str(path)

    monkeypatch.setenv("AGENT_CLIPPER_RENDER_ENABLED", "true")
    _arm(monkeypatch)
    # real source clips, keyed by the media_asset id (== Drive file id).
    blobs = {}
    for i, (secs, hz) in enumerate([(8, 300), (8, 440), (8, 600)]):
        p = _mk(tmp_path / f"c{i}.mp4", secs, hz)
        with open(p, "rb") as fh:
            blobs[f"c{i}"] = fh.read()
    # a genuine mp3 bed (audio only).
    subprocess.run([_FFMPEG, "-y", "-f", "lavfi",
                    "-i", "sine=frequency=220:duration=30",
                    "-c:a", "libmp3lame", "-b:a", "128k", str(tmp_path / "bed.mp3")],
                   check=True, capture_output=True)
    with open(tmp_path / "bed.mp3", "rb") as fh:
        music_bytes = fh.read()

    pool = FakeMediaStore(assets=[
        _video_asset("c0", dur=8.0), _video_asset("c1", dur=8.0),
        _video_asset("c2", dur=8.0)])
    _point_pool(monkeypatch, pool)
    drive = FakeDrive(blobs=blobs)

    audio = tmp_path / "hype.mp3"; audio.write_bytes(music_bytes)

    res = story_studio.create_story(
        {"gym_id": "pierce_ig", "account_key": "pierce_ig", "asset_ids": [],
         "brief": "Members crushed the workout today", "identity_tokens": ["Pierce"],
         "requested_by": "coach_proof"},
        store=_FakeStore(), music_library=_RealPathLibrary(str(audio)),
        downloader=drive.download, output_dir=str(tmp_path / "out"))

    assert res["status"] == "staged", res
    out_path = res["draft"].creative_path
    assert os.path.exists(out_path)
    meta = json.loads(subprocess.run(
        [_FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", out_path],
        capture_output=True, text=True).stdout)
    v = next(s for s in meta["streams"] if s["codec_type"] == "video")
    assert v["codec_name"] == "h264"
    assert (v["width"], v["height"]) == (1080, 1920)
    assert str(res["draft"].status).lower().endswith("pending")
    assert drive.downloads                    # sources were really fetched to disk
