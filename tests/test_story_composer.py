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


def test_plan_routes_whole_request_to_opus_when_source_too_long():
    t = tmpl.get("hype_montage")
    cands = _cands("pierce", 4)
    assets = {"a0": {"duration_sec": 400, "size_bytes": 10}}  # one source over cap
    plan = comp.plan_compose(cands, "pierce", t, assets_by_id=assets)
    assert plan.route == comp.ROUTE_OPUS
    assert plan.held is True


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
