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

pytestmark_ff = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not found")


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
