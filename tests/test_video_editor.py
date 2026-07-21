"""
Video editor tests (Option A: Echo directs, Higgsfield renders).

Stage-isolated. ffmpeg-dependent tests skip when ffmpeg is absent; planning,
cost, cache, and gate tests run everywhere.
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import video_editor as ve  # noqa: E402
from agent import config  # noqa: E402
from agent.clipper import Moment  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _moment(start=10.0, end=70.0, **kw):
    base = dict(start_ts=start, end_ts=end, duration=end - start,
                hook="Most gyms ignore follow up", rationale="r", bucket="lead_speed",
                score=88, transcript_text="t")
    base.update(kw)
    return Moment(**base)


def _transcript(start=10.0, end=70.0, step=1.0):
    """Word-level transcript spanning [start, end] with concrete gym words."""
    vocab = ["most", "gyms", "ignore", "follow", "leads", "slipping", "through",
             "cracks", "booking", "calendar", "revenue", "members", "showed",
             "speed", "phone", "coach", "front", "desk", "missed", "calls"]
    words = []
    t = start
    i = 0
    while t < end:
        words.append({"word": vocab[i % len(vocab)], "start": t, "end": t + 0.4})
        t += step
        i += 1
    return {"words": words, "segments": []}


# ---- aspect / dims ----------------------------------------------------------

def test_dims_known_aspects():
    assert ve._dims("9:16") == (1080, 1920)
    assert ve._dims("1:1") == (1080, 1080)


def test_dims_unknown_raises():
    with pytest.raises(ve.VideoEditorError):
        ve._dims("4:3")


# ---- Higgsfield prompt / house style ----------------------------------------

def test_prompt_has_no_dashes_and_bans_text():
    p = ve.build_higgsfield_prompt("a coach on the phone with a lead — following up")
    # em/en dashes from the input scene are stripped (no dashes reach the render)
    assert "—" not in p and "–" not in p
    # the scene tail (what varies per beat) has no hyphens either
    scene = p.split("Scene: ", 1)[1]
    assert "-" not in scene
    # the prompt forbids on-image text and clip-art
    assert "no text" in p.lower()
    assert "clip art" in p.lower() or "not clip art" in p.lower()


# ---- manifest planning ------------------------------------------------------

def test_plan_fallback_is_grounded_and_capped(monkeypatch):
    monkeypatch.setenv("AGENT_VIDEO_BROLL_CAP", "3")
    m = _moment(10, 130)  # 2 minute clip
    tr = _transcript(10, 130)
    manifest = ve.plan_broll_manifest(m, tr, llm=None)
    assert manifest["kind"] in ("video", "image")
    assert len(manifest["beats"]) <= 3
    assert manifest["beats"], "fallback should produce at least one beat"
    clip_tokens = ve._tokens(ve._clip_text(tr, 10, 130))
    for b in manifest["beats"]:
        # every beat grounded in the transcript
        assert ve._fabrication_ok(b["concept"], b.get("source_span", ""), clip_tokens)
        assert "prompt" in b
        assert b["offset"] >= ve._BROLL_MIN_OFFSET


def test_fabrication_gate_rejects_invented_numbers():
    # transcript speaks "retention" but never "70" -> an invented stat is blocked
    clip_tokens = ve._tokens("we lose members on retention every month booking")
    assert ve._fabrication_ok("retention", "retention rate", clip_tokens) is True
    # invented number "70" not in transcript -> rejected even though words are grounded
    assert ve._fabrication_ok("70 percent retention", "70 percent", clip_tokens) is False
    # a number that IS spoken passes
    ct2 = ve._tokens("if i have 10 in my group i designate 3 people")
    assert ve._fabrication_ok("10 in group", "10 group", ct2) is True


def test_planner_llm_drops_ungrounded_beats():
    m = _moment(0, 60)
    tr = _transcript(0, 60)

    def fake_llm(system, user):
        return json.dumps([
            {"offset": 8, "duration": 4, "concept": "leads slipping through",
             "source_span": "leads slipping through cracks", "visual": "a lead list"},
            {"offset": 30, "duration": 4, "concept": "quantum blockchain synergy",
             "source_span": "quantum blockchain", "visual": "a server room"},
        ])

    manifest = ve.plan_broll_manifest(m, tr, llm=fake_llm, cap=6)
    concepts = [b["concept"] for b in manifest["beats"]]
    assert "leads slipping through" in concepts
    assert "quantum blockchain synergy" not in concepts  # ungrounded, dropped


def test_planner_respects_cap(monkeypatch):
    m = _moment(0, 200)
    tr = _transcript(0, 200)

    def fake_llm(system, user):
        # 6 grounded beats, well spaced
        return json.dumps([
            {"offset": 10 + i * 20, "duration": 4, "concept": "booking calendar",
             "source_span": "booking calendar", "visual": "a calendar"}
            for i in range(6)
        ])

    manifest = ve.plan_broll_manifest(m, tr, llm=fake_llm, cap=2)
    assert len(manifest["beats"]) == 2
    assert manifest["dropped_for_cap"] >= 1


# ---- cost projection --------------------------------------------------------

def test_cost_projection_video_vs_image(monkeypatch):
    monkeypatch.setenv("AGENT_VIDEO_BROLL_CAP", "6")
    m = _moment(0, 120)
    tr = _transcript(0, 120)

    monkeypatch.setenv("AGENT_VIDEO_BROLL_KIND", "video")
    vman = ve.plan_broll_manifest(m, tr, llm=None)
    assert vman["cost_per_overlay"] == 7.5
    assert vman["projected_cost"] == round(len(vman["beats"]) * 7.5, 2)

    monkeypatch.setenv("AGENT_VIDEO_BROLL_KIND", "image")
    iman = ve.plan_broll_manifest(m, tr, llm=None)
    assert iman["cost_per_overlay"] == 2.0


def test_project_episode_cost_sums():
    ms = [{"projected_cost": 15.0}, {"projected_cost": 7.5}, {"projected_cost": 0}]
    assert ve.project_episode_cost(ms) == 22.5


# ---- cache + renderer + cost cap --------------------------------------------

def test_cache_key_stable_and_prompt_sensitive():
    b1 = {"duration": 4, "prompt": "scene A"}
    b2 = {"duration": 4, "prompt": "scene A"}
    b3 = {"duration": 4, "prompt": "scene B"}
    assert ve.overlay_cache_key(b1, "video") == ve.overlay_cache_key(b2, "video")
    assert ve.overlay_cache_key(b1, "video") != ve.overlay_cache_key(b3, "video")
    # kind changes the extension
    assert ve.overlay_cache_path("/x", b1, "video").endswith(".mp4")
    assert ve.overlay_cache_path("/x", b1, "image").endswith(".png")


def test_render_overlays_reuses_cache_never_repays(tmp_path):
    cache = str(tmp_path / "ov")
    manifest = {"kind": "video", "beats": [
        {"offset": 5, "duration": 4, "concept": "c1", "prompt": "p1"},
    ]}
    calls = {"n": 0}

    def renderer(beat, out_path, kind):
        calls["n"] += 1
        with open(out_path, "wb") as fh:
            fh.write(b"x" * 100)

    # first run: one render
    ov1 = ve.render_overlays(manifest, renderer=renderer, cache_dir=cache)
    assert len(ov1) == 1 and calls["n"] == 1 and ov1[0]["cached"] is False
    # second run: cache hit, renderer NOT called again
    ov2 = ve.render_overlays(manifest, renderer=renderer, cache_dir=cache)
    assert len(ov2) == 1 and calls["n"] == 1 and ov2[0]["cached"] is True


def test_render_overlays_cost_cap_stops(tmp_path):
    cache = str(tmp_path / "ov")
    manifest = {"kind": "video", "beats": [
        {"offset": 5 + i, "duration": 3, "concept": f"c{i}", "prompt": f"p{i}"}
        for i in range(5)
    ]}

    def renderer(beat, out_path, kind):
        with open(out_path, "wb") as fh:
            fh.write(b"x" * 100)

    with pytest.raises(ve.VideoEditorError, match="cost cap"):
        ve.render_overlays(manifest, renderer=renderer, cache_dir=cache, cap=2)


def test_episode_budget_caps_across_clips(tmp_path):
    """The episode budget is shared across manifests so total NEW renders never
    exceed the cap, no matter how many clips."""
    cache = str(tmp_path / "ov")
    budget = ve.RenderBudget(3)

    def renderer(beat, out_path, kind):
        with open(out_path, "wb") as fh:
            fh.write(b"x" * 100)

    total = 0
    # 4 clips, 2 fresh beats each = 8 desired; budget of 3 must stop at 3
    for c in range(4):
        manifest = {"kind": "video", "beats": [
            {"offset": 5, "duration": 3, "concept": f"c{c}a", "prompt": f"p{c}a"},
            {"offset": 15, "duration": 3, "concept": f"c{c}b", "prompt": f"p{c}b"},
        ]}
        ov = ve.render_overlays(manifest, renderer=renderer, cache_dir=cache,
                                budget=budget)
        total += sum(1 for o in ov if not o["cached"])
    assert budget.used == 3
    assert total == 3  # never spent past the episode cap


def test_render_overlays_no_renderer_skips_uncached(tmp_path):
    cache = str(tmp_path / "ov")
    manifest = {"kind": "video", "beats": [
        {"offset": 5, "duration": 4, "concept": "c1", "prompt": "p1"},
    ]}
    ov = ve.render_overlays(manifest, renderer=None, cache_dir=cache)
    assert ov == []  # nothing cached, no renderer, no spend


# ---- flags default OFF ------------------------------------------------------

def test_flags_default_off(monkeypatch):
    for env in ("AGENT_VIDEO_EDITOR_ENABLED", "AGENT_VIDEO_BROLL_ENABLED",
                "AGENT_VIDEO_RENDER"):
        monkeypatch.delenv(env, raising=False)
    assert config.video_editor_enabled() is False
    assert config.video_broll_enabled() is False
    assert config.video_render_enabled() is False


def test_edit_episode_off_returns_none(monkeypatch):
    monkeypatch.delenv("AGENT_VIDEO_EDITOR_ENABLED", raising=False)
    assert ve.edit_episode("x") is None


# ---- assembly (ffmpeg) ------------------------------------------------------

# ---- v1: routing, stills, treatment, captions -------------------------------

def test_route_classifier_stats_go_still_scenes_go_motion():
    assert ve._classify_route("close rate 80 percent", "eighty percent close") == "still"
    assert ve._classify_route("the three step framework", "three step") == "still"
    assert ve._classify_route("a coach on the phone", "calling a lead") == "motion"


def test_planner_routes_and_enforces_separate_caps(monkeypatch):
    monkeypatch.setenv("AGENT_VIDEO_BROLL_CAP", "1")   # motion cap
    monkeypatch.setenv("AGENT_VIDEO_STILLS_CAP", "1")  # stills cap
    m = _moment(0, 200)
    tr = _transcript(0, 200)

    def fake_llm(system, user):
        return json.dumps([
            {"offset": 10, "duration": 4, "concept": "coach calling a lead",
             "source_span": "coach phone", "visual": "a coach on the phone",
             "route": "motion"},
            {"offset": 30, "duration": 4, "concept": "front desk missed calls",
             "source_span": "missed calls", "visual": "a busy front desk",
             "route": "motion"},
            {"offset": 60, "duration": 4, "concept": "booking calendar revenue",
             "source_span": "booking calendar", "visual": "n/a", "route": "still"},
            {"offset": 90, "duration": 4, "concept": "members showed booking",
             "source_span": "members showed", "visual": "n/a", "route": "still"},
        ])

    man = ve.plan_broll_manifest(m, tr, llm=fake_llm)
    assert man["motion_count"] == 1
    assert man["still_count"] == 1
    assert man["dropped_for_cap"] == 2
    # still beat carries card_text + image kind
    still = [b for b in man["beats"] if b["route"] == "still"][0]
    assert still["kind"] == "image"
    assert still["card_text"]
    # projected cost mixes motion (video) + still (nano)
    assert man["projected_cost"] == round(1 * man["cost_per_overlay"]
                                          + 1 * man["cost_per_still"], 2)


def test_still_prompt_has_grounded_text_no_dashes():
    beat = {"concept": "twenty-five percent", "card_text": "TWENTY FIVE PERCENT"}
    p = ve._build_still_prompt(beat)
    assert "TWENTY FIVE PERCENT" in p
    # the only-rendered-words clause carries the grounded text, dash-free
    only = p.split("exactly:")[1]
    assert "-" not in only.split("No other text")[0]
    # professional art-direction, not a plain text slide
    assert "NOT a plain centered text slide" in p


def test_still_prompt_framework_layout():
    beat = {"card_text": "CLIENT SALESPERSON OVERSEER", "still_layout": "framework"}
    p = ve._build_still_prompt(beat)
    assert "FRAMEWORK diagram" in p


def test_still_card_renderer_needs_pipeline(monkeypatch):
    # with creative_studio disabled, the still renderer raises (never silent spend)
    monkeypatch.delenv("AGENT_CREATIVE_STUDIO_ENABLED", raising=False)
    with pytest.raises(ve.VideoEditorError, match="creative_studio"):
        ve.still_card_renderer({"prompt": "x"}, "/tmp/none.png", "image")


def test_render_overlays_routes_still_and_motion(tmp_path):
    cache = str(tmp_path / "ov")
    manifest = {"kind": "video", "beats": [
        {"offset": 5, "duration": 4, "concept": "m", "prompt": "pm", "route": "motion"},
        {"offset": 20, "duration": 4, "concept": "s", "prompt": "ps", "route": "still",
         "kind": "image"},
    ]}
    hits = {"motion": 0, "still": 0}

    def motion_r(beat, out, kind):
        hits["motion"] += 1
        open(out, "wb").write(b"m" * 50)

    def still_r(beat, out, kind):
        hits["still"] += 1
        open(out, "wb").write(b"s" * 50)

    ov = ve.render_overlays(manifest, renderer=motion_r, still_renderer=still_r,
                            cache_dir=cache, budget=ve.RenderBudget(6),
                            still_budget=ve.RenderBudget(6))
    assert hits == {"motion": 1, "still": 1}
    routes = sorted(o["route"] for o in ov)
    assert routes == ["motion", "still"]
    # still asset is a .png, motion a .mp4
    kinds = {o["route"]: o["asset_path"].rsplit(".", 1)[1] for o in ov}
    assert kinds["still"] == "png" and kinds["motion"] == "mp4"


def test_word_highlight_ass_one_red_word_no_ghost(tmp_path):
    tr = _transcript(0, 6, step=0.5)
    ass = str(tmp_path / "wh.ass")
    ve._make_word_highlight_ass(tr, 0, 6, ass, 1080, 1920, margin_v=500)
    content = open(ass).read()
    assert "Anton" in content
    events = [l for l in content.splitlines() if l.startswith("Dialogue:")]
    assert events
    for e in events:
        # exactly ONE active red word per event (one red color tag), rest white
        assert e.count("&H002A2AFF&") == 1, e
    # no hyphen reaches on-screen text
    body = "\n".join(events)
    assert "-" not in body


def test_snap_to_word_boundaries():
    tr = {"words": [
        {"word": "most", "start": 10.0, "end": 10.4},
        {"word": "gyms", "start": 10.4, "end": 10.9},
        {"word": "ignore", "start": 60.2, "end": 60.8},
        {"word": "leads", "start": 60.8, "end": 61.5},
    ], "segments": []}
    m = _moment(10.3, 61.2)  # slightly mid-word on both ends
    ve.snap_to_word_boundaries(m, tr)
    assert m.start_ts == 10.0   # snapped to nearest word start
    assert m.end_ts == 61.5     # snapped to nearest word end
    assert m.duration == round(61.5 - 10.0, 2)


def test_snap_reverts_degenerate_span():
    # two boundaries so close they'd collapse the clip -> revert to original
    tr = {"words": [
        {"word": "a", "start": 10.0, "end": 10.2},
        {"word": "b", "start": 10.25, "end": 10.4},
    ], "segments": []}
    m = _moment(10.1, 10.3)  # both inside adjacent tiny words
    ve.snap_to_word_boundaries(m, tr)
    # snapped span would be < 1s -> guard reverts to original
    assert m.start_ts == 10.1 and m.end_ts == 10.3


def test_snap_is_noop_when_no_word_in_window():
    tr = {"words": [{"word": "x", "start": 500.0, "end": 500.5}], "segments": []}
    m = _moment(10.0, 70.0)
    ve.snap_to_word_boundaries(m, tr)
    assert m.start_ts == 10.0 and m.end_ts == 70.0  # unchanged


def test_plan_keep_intervals_removes_dead_air():
    # words at 0-1, then a 3s gap, then 4-5, then 5-6 (no gap)
    words = [(0.0, 1.0), (4.0, 5.0), (5.0, 6.0)]
    intervals, time_map, total = ve.plan_keep_intervals(words, 6.0, gap=0.45, keep=0.1)
    # the 3s gap (1.0 -> 4.0) is removed, leaving ~0.1s
    assert total < 6.0
    assert total == pytest.approx(6.0 - (3.0 - 0.1), abs=0.05)
    # time before the gap maps ~1:1; time after the gap is shifted earlier
    assert time_map(0.5) == pytest.approx(0.5, abs=0.01)
    assert time_map(4.5) < 4.5


def test_plan_keep_intervals_noop_when_tight():
    words = [(0.0, 1.0), (1.1, 2.0), (2.1, 3.0)]  # no gap over 0.45
    intervals, time_map, total = ve.plan_keep_intervals(words, 3.0, gap=0.45, keep=0.1)
    assert total == pytest.approx(3.0, abs=0.01)
    assert len(intervals) == 1


def test_remap_transcript_shifts_words_to_tightened_timeline():
    tr = {"words": [
        {"word": "a", "start": 100.0, "end": 100.5},
        {"word": "b", "start": 105.0, "end": 105.5},
    ], "segments": []}
    # a linear map that halves time (stub)
    rt = ve._remap_transcript(tr, 100.0, 106.0, lambda t: t / 2.0)
    assert [w["word"] for w in rt["words"]] == ["a", "b"]
    assert rt["words"][0]["start"] == pytest.approx(0.0)   # (100-100)/2
    assert rt["words"][1]["start"] == pytest.approx(2.5)   # (105-100)/2


def test_flags_default_off_v1(monkeypatch):
    monkeypatch.delenv("AGENT_VIDEO_STILLS_ENABLED", raising=False)
    assert config.video_stills_enabled() is False


pytestmark_ff = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not found")


@pytestmark_ff
def test_face_detect_graceful_on_no_face(tmp_path):
    # a synthetic solid-color video has no face -> returns None, never raises
    src = str(tmp_path / "noface.mp4")
    _make_src(src, duration=3)
    assert ve.video_assets.detect_face_bottom_frac(src) is None


@pytestmark_ff
def test_bottom_treatment_produces_correct_dims(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")
    src = str(tmp_path / "framed.mp4")
    _make_src(src, duration=3, w=1080, h=1920)
    out = str(tmp_path / "branded.mp4")
    ve.apply_bottom_treatment(src, out, 1080, 1920, str(tmp_path / "work"))
    assert _probe_dims(out) == (1080, 1920)


@pytestmark_ff
def test_word_highlight_burns_output(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")
    src = str(tmp_path / "framed.mp4")
    _make_src(src, duration=4, w=1080, h=1920)
    out = str(tmp_path / "wh.mp4")
    tr = _transcript(0, 4, step=0.4)
    ve.burn_word_highlight(src, out, tr, 0, 4, 1080, 1920, face_bottom_frac=0.4)
    assert os.path.isfile(out) and os.path.getsize(out) > 0
    assert _probe_dims(out) == (1080, 1920)


def _make_src(path, duration=12, w=640, h=360):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=blue:s={w}x{h}:r=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(duration), "-c:v", "libx264", "-c:a", "aac", path,
    ], capture_output=True, check=True)


def _probe_dims(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_streams", path], capture_output=True, text=True)
    for s in json.loads(r.stdout).get("streams", []):
        if s.get("codec_type") == "video":
            return s.get("width"), s.get("height")
    return None, None


@pytestmark_ff
def test_assemble_916_and_11_dims(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")
    src = str(tmp_path / "src.mp4")
    _make_src(src, duration=12)
    out_dir = str(tmp_path / "out")
    m = _moment(1, 9)
    tr = _transcript(1, 9)

    p916 = ve.assemble_clip(m, src, tr, [], out_dir, "clip", aspect="9:16",
                            captioned=True)
    assert _probe_dims(p916) == (1080, 1920)

    p11 = ve.assemble_clip(m, src, tr, [], out_dir, "clip", aspect="1:1",
                           captioned=True)
    assert _probe_dims(p11) == (1080, 1080)


@pytestmark_ff
def test_ad_cut_differs_from_captioned(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")
    src = str(tmp_path / "src.mp4")
    _make_src(src, duration=12)
    out_dir = str(tmp_path / "out")
    m = _moment(1, 9)
    tr = _transcript(1, 9)

    cap = ve.assemble_clip(m, src, tr, [], out_dir, "clip", aspect="9:16",
                           captioned=True)
    ad = ve.assemble_clip(m, src, tr, [], out_dir, "clip", aspect="9:16",
                          captioned=False)
    assert os.path.isfile(cap) and os.path.isfile(ad)
    assert cap != ad
    # captioned render has burned-in text, so it should not be byte-identical
    assert os.path.getsize(cap) != os.path.getsize(ad)


@pytestmark_ff
def test_polish_and_jumpcuts_end_to_end(monkeypatch, tmp_path):
    """Full polish=ON + jumpcuts=ON assemble path: bookended, correct dims, synced."""
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")
    monkeypatch.setenv("AGENT_VIDEO_POLISH", "true")
    monkeypatch.setenv("AGENT_VIDEO_JUMPCUTS", "true")
    src = str(tmp_path / "src.mp4")
    _make_src(src, duration=12)
    # words 0-4, a 3s dead-air gap, then 7-10 -> jumpcuts should fire
    tr = {"words": (
        [{"word": w, "start": 0.5 * i, "end": 0.5 * i + 0.4} for i, w in
         enumerate(["most", "gyms", "ignore", "the", "follow", "up", "calls"])]
        + [{"word": w, "start": 7.0 + 0.5 * i, "end": 7.0 + 0.5 * i + 0.4}
           for i, w in enumerate(["speed", "to", "lead", "wins", "members"])]
    ), "segments": []}
    m = _moment(0, 10, hook="most gyms ignore follow up")
    p = ve.assemble_clip(m, src, tr, [], str(tmp_path / "out"), "clip",
                         aspect="9:16", captioned=True)
    assert os.path.isfile(p)
    assert p.endswith("_final.mp4")            # bookended
    assert _probe_dims(p) == (1080, 1920)
    # video and audio durations stay within 0.3s of each other (in sync)
    import subprocess as _sp
    r = _sp.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", p], capture_output=True, text=True)
    durs = [float(s["duration"]) for s in json.loads(r.stdout)["streams"]
            if "duration" in s]
    assert max(durs) - min(durs) < 0.3


@pytestmark_ff
def test_concat_av_joins_mixed_inputs(tmp_path):
    """_concat_av normalizes and joins clips with different fps/audio (bookend fix)."""
    a = str(tmp_path / "a.mp4")
    b = str(tmp_path / "b.mp4")
    _make_src(a, duration=2)
    _make_src(b, duration=3)
    out = str(tmp_path / "joined.mp4")
    ve._concat_av([a, b], out, 1080, 1920)
    assert os.path.isfile(out)
    assert _probe_dims(out) == (1080, 1920)
    import subprocess as _sp
    r = _sp.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", out], capture_output=True, text=True)
    assert float(r.stdout.strip()) > 4.0   # ~5s joined


@pytestmark_ff
def test_assemble_composites_overlay(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_VIDEO_EDITOR_ENABLED", "true")
    src = str(tmp_path / "src.mp4")
    _make_src(src, duration=12)
    # build a red still overlay asset
    overlay_png = str(tmp_path / "ov.png")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=400x400", "-frames:v", "1", overlay_png],
                   capture_output=True, check=True)
    out_dir = str(tmp_path / "out")
    m = _moment(1, 9)
    tr = _transcript(1, 9)
    overlays = [{"offset": 2.0, "duration": 2.0, "asset_path": overlay_png,
                 "kind": "image"}]
    out = ve.assemble_clip(m, src, tr, overlays, out_dir, "clip", aspect="9:16",
                           captioned=False)
    assert os.path.isfile(out)
    assert _probe_dims(out) == (1080, 1920)
