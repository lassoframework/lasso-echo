"""
Story Studio Wave 3: the multi-clip composer. Input caps route long/big raw to the
Opus lane; segments come from ONE gym only (tenant assertion); a missing renderer
HOLDS (never crashes, never fabricates); the plan honors the 2..6 / 15..60s windows.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import story_composer as comp  # noqa: E402
from agent import story_templates as tmpl  # noqa: E402


def _cands(gym, n=4, seg=10.0):
    return [{"asset_id": f"a{i}", "gym_id": gym, "start_ts": 0,
             "end_ts": seg, "score": 90 - i} for i in range(n)]


# ---- input caps ------------------------------------------------------------
def test_long_raw_routes_to_opus_lane():
    assert comp.route_asset({"duration_sec": 400})[0] == comp.ROUTE_OPUS
    assert comp.route_asset({"size_bytes": 950_000_000})[0] == comp.ROUTE_OPUS


def test_short_small_raw_enters_story_lane():
    assert comp.route_asset({"duration_sec": 120, "size_bytes": 50_000_000})[0] == \
        comp.ROUTE_STORY


def test_one_over_cap_source_is_skipped_and_the_rest_still_build():
    """Blake 2026-09-04: one long clip must not kill the whole reel now that a coach
    can pick a dozen. The over-cap clip is dropped WITH its reason (never silently)
    and the montage is built from the rest."""
    t = tmpl.get("hype_montage")
    cands = _cands("pierce", 4)
    assets = {"a0": {"duration_sec": 400, "size_bytes": 10}}  # one source over cap
    plan = comp.plan_compose(cands, "pierce", t, assets_by_id=assets)
    assert plan.held is False
    assert plan.route == comp.ROUTE_STORY
    assert [s.asset_id for s in plan.segments] == ["a1", "a2", "a3"]
    assert [k["asset_id"] for k in plan.skipped] == ["a0"]
    assert "opus" in plan.skipped[0]["reason"].lower()


def test_plan_routes_whole_request_to_opus_when_every_source_is_too_long():
    """The Opus lane still owns a request whose sources are ALL over cap — that is
    what it is for. Only the mixed case changed."""
    t = tmpl.get("hype_montage")
    cands = _cands("pierce", 3)
    assets = {f"a{i}": {"duration_sec": 400, "size_bytes": 10} for i in range(3)}
    plan = comp.plan_compose(cands, "pierce", t, assets_by_id=assets)
    assert plan.route == comp.ROUTE_OPUS
    assert plan.held is True
    assert len(plan.skipped) == 3


# ---- tenant isolation ------------------------------------------------------
def test_cross_gym_segment_blocks_the_compose():
    t = tmpl.get("hype_montage")
    cands = _cands("pierce", 3) + [{"asset_id": "x", "gym_id": "northgate",
                                    "start_ts": 0, "end_ts": 10, "score": 99}]
    plan = comp.plan_compose(cands, "pierce", t)
    assert plan.held is True
    assert "gym" in plan.hold_reason.lower()


def test_assert_segment_tenant_raises_on_mismatch():
    seg = comp.Segment(asset_id="a", gym_id="northgate", start_ts=0, end_ts=5)
    with pytest.raises(comp.TenantMismatch):
        comp.assert_segment_tenant(seg, "pierce")


# ---- selection windows -----------------------------------------------------
def test_selects_2_to_6_segments_in_total_window():
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 6, seg=10.0), "pierce", t)
    assert plan.held is False
    assert 3 <= len(plan.segments) <= 6            # hype_montage min 3
    assert comp.TOTAL_MIN_SEC <= plan.total_sec <= comp.TOTAL_MAX_SEC
    for s in plan.segments:
        assert comp.SEG_MIN_SEC <= s.duration <= comp.SEG_MAX_SEC


def test_out_of_window_segments_are_dropped():
    # a 2s and a 30s clip are outside 3..15s and are not selectable.
    cands = [{"asset_id": "short", "gym_id": "p", "start_ts": 0, "end_ts": 2, "score": 99},
             {"asset_id": "long", "gym_id": "p", "start_ts": 0, "end_ts": 30, "score": 99}]
    segs = comp.select_segments(cands, "p", {"max_segments": 6, "min_segments": 2,
                                             "total_min_sec": 15, "total_max_sec": 60})
    assert segs == []


def test_too_few_segments_holds():
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 1, seg=10.0), "pierce", t)
    assert plan.held is True
    assert "segment" in plan.hold_reason.lower()


# ---- render degrades gracefully --------------------------------------------
def test_render_holds_when_ffmpeg_absent(tmp_path):
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 6, seg=10.0), "pierce", t)

    def _boom(*a, **k):
        raise RuntimeError("ffmpeg not found")

    res = comp.render_compose(plan, output_dir=str(tmp_path), reframe_fn=_boom)
    assert res.held is True
    assert res.output_path == ""                   # never a fabricated path
    assert "held" in res.hold_reason.lower()


def test_render_succeeds_with_injected_primitives(tmp_path):
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 6, seg=10.0), "pierce", t)
    steps = []
    res = comp.render_compose(
        plan, output_dir=str(tmp_path), ask_frame_text="BOOK NOW",
        reframe_fn=lambda seg, d: (steps.append("reframe"), f"{d}/{seg.asset_id}.mp4")[1],
        normalize_fn=lambda ps: (steps.append("norm"), ps)[1],
        assemble_fn=lambda ps, d: (steps.append("assemble"), f"{d}/asm.mp4")[1],
        end_frame_fn=lambda p, d, a: (steps.append("end"), f"{d}/final.mp4")[1])
    assert res.held is False
    assert res.output_path.endswith("final.mp4")
    assert "reframe" in steps and "assemble" in steps and "end" in steps


def test_render_calls_the_overlay_burn_when_frames_are_given(tmp_path):
    """REGRESSION GUARD (spec §1, page 4): the validated hook/ask overlay was
    computed, stored, and then silently thrown away before the video was ever
    touched. If a future change disconnects render_compose from overlay_fn again,
    THIS must fail loudly — a passing render with overlay_frames given but overlay_fn
    never invoked is exactly the bug that shipped."""
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 6, seg=10.0), "pierce", t)
    calls = []
    res = comp.render_compose(
        plan, output_dir=str(tmp_path), ask_frame_text="BOOK NOW",
        ask_frame_lines=[["BOOK NOW"]],
        overlay_frames=[["HOOK LINE ONE"], ["HOOK LINE TWO"]],
        reframe_fn=lambda seg, d: f"{d}/{seg.asset_id}.mp4",
        normalize_fn=lambda ps: ps,
        assemble_fn=lambda ps, d: f"{d}/asm.mp4",
        overlay_fn=lambda p, d, frames: (calls.append(("overlay", frames)), f"{d}/ov.mp4")[1],
        end_frame_fn=lambda p, d, a: (calls.append(("end", a)), f"{d}/final.mp4")[1])
    assert res.held is False
    assert calls and calls[0][0] == "overlay", \
        "overlay_frames was provided but the burn step was never called"
    assert calls[0][1] == [["HOOK LINE ONE"], ["HOOK LINE TWO"]], \
        "the ALREADY-VALIDATED frames must reach the burn unmodified, never re-derived"


def test_render_never_calls_overlay_burn_when_no_frames_exist(tmp_path):
    """The inverse guard: a caller that never built overlay data (a bare unit test,
    or a lane with no overlay) must see byte-identical behavior to before overlay
    wiring existed — no accidental burn call on empty input."""
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 6, seg=10.0), "pierce", t)
    calls = []
    comp.render_compose(
        plan, output_dir=str(tmp_path), ask_frame_text="BOOK NOW",
        reframe_fn=lambda seg, d: f"{d}/{seg.asset_id}.mp4",
        normalize_fn=lambda ps: ps,
        assemble_fn=lambda ps, d: f"{d}/asm.mp4",
        overlay_fn=lambda p, d, frames: (calls.append("overlay"), p)[1],
        end_frame_fn=lambda p, d, a: f"{d}/final.mp4")
    assert "overlay" not in calls


def test_default_end_frame_refuses_to_drop_a_requested_ask(tmp_path):
    """THE loud-failure guard for page 4's centerpiece bug: an ask was requested
    (ask_frame_text set) but the validated ask_frame_lines never made it through —
    this is EXACTLY the class of bug that shipped a blank overlay while the database
    said everything was fine. It must raise, not silently render without the ask."""
    from agent.story_composer import _default_end_frame
    with pytest.raises(RuntimeError, match="refusing to burn"):
        _default_end_frame("/tmp/in.mp4", str(tmp_path), "BOOK NOW", ask_lines=None)


def test_normalize_pins_one_profile_for_the_concat_demuxer(monkeypatch, tmp_path):
    """REGRESSION GUARD: phone clips arrive at 29.98 / 29.99 / 30.09 fps with
    timebases like 1/71400 and 1/11856. The concat demuxer adopts the FIRST input's
    profile and reinterprets later segments in it, so unpinned segments produced
    non-monotonic timestamps and ffmpeg DROPPED most of segments 2..n: a real 3-clip
    montage muxed 29.6s of audio over a 13.4s video track while the container still
    reported 29.6s. _default_normalize's whole job is to hand concat ONE profile, so
    fps, pixel format, mp4 timescale and the audio rate/layout must all be pinned."""
    from agent import clipper_render
    cmds = []
    monkeypatch.setattr(clipper_render, "_require_render", lambda: None)
    monkeypatch.setattr(clipper_render, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(clipper_render, "_run", lambda cmd, label="": cmds.append(cmd))

    out = comp._default_normalize([f"{tmp_path}/a.mp4", f"{tmp_path}/b.mp4"])
    assert len(out) == 2 and all(p.endswith("_norm.mp4") for p in out)
    for cmd in cmds:
        vf = cmd[cmd.index("-vf") + 1]
        assert f"fps={comp.CONCAT_FPS}" in vf, "frame rate not pinned before concat"
        assert "format=yuv420p" in vf, "pixel format not pinned before concat"
        assert "-video_track_timescale" in cmd, "mp4 timebase not pinned before concat"
        assert cmd[cmd.index("-video_track_timescale") + 1] == str(comp.CONCAT_TIMESCALE)
        assert "-ar" in cmd and "-ac" in cmd, "audio rate/layout not pinned"


def test_extract_frame_png_raises_when_ffmpeg_writes_no_frame(monkeypatch, tmp_path):
    """ffmpeg EXITS 0 and writes NOTHING when a seek lands past the last video frame,
    so _run cannot catch it and the failure surfaced as a bare PIL FileNotFoundError
    three frames up the stack. It must be a loud, honest RenderError instead."""
    from agent import clipper_render
    monkeypatch.setattr(clipper_render, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(clipper_render, "_run", lambda cmd, label="": None)
    with pytest.raises(clipper_render.RenderError, match="no video frame exists"):
        clipper_render._extract_frame_png("/tmp/in.mp4", 14.65,
                                          str(tmp_path / "missing.png"))


def test_render_burns_music_when_provided(tmp_path):
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 6, seg=10.0), "pierce", t)
    burned = []
    res = comp.render_compose(
        plan, output_dir=str(tmp_path), music_path="/lib/hype001.mp3",
        reframe_fn=lambda seg, d: f"{d}/x.mp4",
        normalize_fn=lambda ps: ps,
        assemble_fn=lambda ps, d: f"{d}/asm.mp4",
        end_frame_fn=lambda p, d, a: f"{d}/final.mp4",
        music_burn_fn=lambda p, m, d: (burned.append(m), f"{d}/withmusic.mp4")[1])
    assert res.output_path.endswith("withmusic.mp4")
    assert burned == ["/lib/hype001.mp3"]


# ---- most cuts first (Blake 2026-09-04) ------------------------------------
def test_ten_clips_become_ten_cuts_not_four_long_ones():
    """The whole point of lifting the portal's 3-clip cap: hand Echo ten clips and ten
    of them land in the reel, sharing the 60s window as 6s cuts, instead of four
    15s cuts and six clips ignored."""
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 10, seg=15.0), "pierce", t)
    assert plan.held is False
    assert len(plan.segments) == 10
    assert plan.total_sec <= t.segment_plan.total_max_sec
    for s in plan.segments:
        assert comp.SEG_MIN_SEC <= s.duration <= comp.SEG_MAX_SEC


def test_a_share_under_the_floor_drops_to_fewer_cuts():
    """25 clips cannot all land in a 60s reel and clear the 3s floor, so the count
    walks down until it fits — here to the template's own ceiling (10 cuts of 6s),
    never to 25 cuts of 2.4s."""
    t = tmpl.get("hype_montage")
    plan = comp.plan_compose(_cands("pierce", 25, seg=15.0), "pierce", t)
    assert plan.held is False
    assert len(plan.segments) == t.segment_plan.max_segments
    for s in plan.segments:
        assert s.duration >= comp.SEG_MIN_SEC


def test_the_highest_scoring_clips_are_the_ones_kept():
    """Trimming shares the window; it never reorders the pick. _cands scores 90, 89,
    88... so a 3-cut fit keeps a0/a1/a2."""
    t = tmpl.get("athlete_stat")           # min 2, max 8, total 15..40
    segs = comp.select_segments(_cands("pierce", 5, seg=15.0), "pierce",
                                {"min_segments": 2, "max_segments": 3,
                                 "total_min_sec": 15, "total_max_sec": 40})
    assert [s.asset_id for s in segs] == ["a0", "a1", "a2"]
    assert round(sum(s.duration for s in segs), 2) <= 40


def test_a_short_pool_still_holds_with_an_honest_count():
    """When nothing fits the total floor the plan still reports how much footage was
    usable (the fallback pick), so the coach gets a real reason, not '0 segments'."""
    t = tmpl.get("hype_montage")           # total_min 20
    plan = comp.plan_compose(_cands("pierce", 2, seg=4.0), "pierce", t)
    assert plan.held is True
    assert "2 usable segment(s) totaling 8s" in plan.hold_reason


# ---- the music bed mix (Blake 2026-09-04: "make sure the music is A+") ------
# Measured against real ffmpeg before these were written: the OLD burn turned a 12s
# reel into a 4s file when the bed was 4s (amix duration=shortest + -shortest cut the
# VIDEO to the bed), hard-FAILED on a source with no audio stream ("Stream specifier
# ':a' matches no streams"), and came out 3.2dB QUIETER than the source audio alone
# because amix divides every input by the input count.
def test_music_filter_pins_the_mix_to_the_video_not_the_bed():
    graph, label = comp._music_filter(60.0, True)
    assert "duration=first" in graph, "the VIDEO is the length of record, not the bed"
    assert "duration=shortest" not in graph
    assert label == "[a]"


def test_music_filter_mixes_at_unity_so_the_reel_is_not_quieter():
    graph, _ = comp._music_filter(60.0, True)
    assert "normalize=0" in graph, \
        "plain amix divides each input by the input count: the room drops ~6dB"


def test_music_filter_ducks_the_bed_under_the_room():
    graph, _ = comp._music_filter(60.0, True)
    assert "sidechaincompress" in graph
    assert "asplit=2[room][duckkey]" in graph, \
        "the room feeds both the mix and the duck key"
    assert f"ratio={comp.MUSIC_DUCK_RATIO}" in graph


def test_music_filter_fades_in_and_out_instead_of_hard_cutting():
    graph, _ = comp._music_filter(60.0, True)
    assert "afade=t=in:st=0" in graph
    # the fade-out has to LAND before the end, on the closing ask frame.
    assert f"afade=t=out:st={60.0 - comp.MUSIC_FADE_OUT_SEC:.2f}" in graph


def test_music_filter_never_schedules_a_fade_out_before_zero():
    """A reel shorter than the fade itself must not produce a negative start time
    (ffmpeg accepts it silently and the fade never fires)."""
    graph, _ = comp._music_filter(0.5, True)
    assert "afade=t=out:st=0.00" in graph


def test_music_filter_falls_back_to_bed_only_on_a_silent_source():
    """A muted phone clip has NO audio stream; referencing [0:a] is an ffmpeg error,
    not an empty stream, so the old graph failed the whole render."""
    graph, label = comp._music_filter(60.0, False)
    assert "[0:a]" not in graph, "a silent source has no [0:a] to reference"
    assert "sidechaincompress" not in graph, "nothing to duck against"
    assert "amix" not in graph, "the bed IS the audio; there is no second input"
    # and it is carried louder, since it is the only thing the viewer hears.
    assert f"I={comp.MUSIC_ONLY_LUFS}" in graph
    assert label == "[a]"


def test_music_filter_normalizes_the_bed_against_the_library_spread():
    """The live library measured -7.4 to -18.2 LUFS on 2026-09-04 (an 11dB spread), so
    an un-normalized bed made one reel deafening and the next inaudible."""
    graph, _ = comp._music_filter(60.0, True)
    assert f"loudnorm=I={comp.MUSIC_BED_LUFS}" in graph


def test_music_burn_loops_the_bed_and_never_shortens_the_video(monkeypatch, tmp_path):
    """The command itself: the bed loops (a short track repeats instead of ending the
    reel), the output is pinned to the probed VIDEO duration, and -shortest is gone."""
    from agent import clipper_render
    captured = {}
    monkeypatch.setattr(clipper_render, "_require_render", lambda: None)
    monkeypatch.setattr(clipper_render, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(clipper_render, "probe_duration", lambda p: 47.5)
    monkeypatch.setattr(comp, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(clipper_render, "_run",
                        lambda cmd, label="": captured.setdefault("cmd", list(cmd)))
    comp._default_music_burn(str(tmp_path / "v.mp4"), str(tmp_path / "b.mp3"),
                             str(tmp_path / "out"))
    cmd = captured["cmd"]
    assert "-stream_loop" in cmd and cmd[cmd.index("-stream_loop") + 1] == "-1"
    assert "-shortest" not in cmd, "-shortest let a short bed truncate the reel"
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "47.50"
    assert cmd[cmd.index("-map") + 1] == "0:v", "the video track is copied through"


def test_music_burn_probes_for_room_audio_and_switches_graph(monkeypatch, tmp_path):
    from agent import clipper_render
    seen = {}
    monkeypatch.setattr(clipper_render, "_require_render", lambda: None)
    monkeypatch.setattr(clipper_render, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(clipper_render, "probe_duration", lambda p: 30.0)
    monkeypatch.setattr(comp, "_has_audio_stream", lambda p: False)
    monkeypatch.setattr(clipper_render, "_run",
                        lambda cmd, label="": seen.setdefault("cmd", list(cmd)))
    comp._default_music_burn(str(tmp_path / "v.mp4"), str(tmp_path / "b.mp3"),
                             str(tmp_path / "out"))
    graph = seen["cmd"][seen["cmd"].index("-filter_complex") + 1]
    assert "[0:a]" not in graph, "a silent source must never reach the ducking graph"


def test_has_audio_stream_reads_false_when_ffprobe_is_absent(monkeypatch):
    """No probe = bed-only, which still produces a reel. Failing the other way would
    build the ducking graph and hard-fail on a silent source."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert comp._has_audio_stream("/nope.mp4") is False


def test_the_finished_mix_is_delivered_at_the_platform_target():
    """Instagram normalizes reel playback to roughly -14 LUFS. Handing over a reel at
    -20 lets the PLATFORM apply that +6dB, which lifts room noise with it; landing it
    ourselves keeps the gain decision where the bed is still separable from the room."""
    graph, _ = comp._music_filter(60.0, True)
    assert f"loudnorm=I={comp.MIX_DELIVERY_LUFS}" in graph
    # ...and it is the LAST stage, after the balance is set, so it lifts both together.
    assert graph.rstrip().endswith(
        f"[mixed]loudnorm=I={comp.MIX_DELIVERY_LUFS}:TP=-1.5:LRA=11[a]")


def test_the_bed_only_mix_is_delivered_at_the_platform_target_too():
    graph, _ = comp._music_filter(60.0, False)
    assert f"loudnorm=I={comp.MIX_DELIVERY_LUFS}" in graph
