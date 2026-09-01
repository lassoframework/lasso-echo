"""
Story Studio Wave 4: the Roxx overlay standard. §6 overlay rules: >8 words/line
rejected + re-wrapped; <=2 lines/frame (3rd -> next frame); safe zones enforced;
contrast fail -> scrim; copy_gate on overlay (dashes banned on-image too); the
per-gym avatar rail on overlay copy; identity anchor; stat/event cards.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from agent import story_overlay as ov  # noqa: E402


# ---- wrapping + framing ----------------------------------------------------
def test_over_8_words_per_line_is_rewrapped():
    line = "one two three four five six seven eight nine ten eleven"
    frames = ov.layout_overlay(line)
    for fr in frames:
        for ln in fr:
            assert len(ln.split()) <= ov.MAX_WORDS_PER_LINE
    # every frame is valid under the rules.
    for fr in frames:
        assert ov.line_violations(fr) == []


def test_third_line_rolls_to_next_frame():
    # three wrapped lines -> two frames (2 + 1), never 3 lines on one frame.
    lines = ["LINE ONE HERE", "LINE TWO HERE", "LINE THREE HERE"]
    frames = ov.frame_lines(lines)
    assert len(frames) == 2
    assert len(frames[0]) == 2
    assert len(frames[1]) == 1


def test_line_violations_flags_bad_frames():
    assert ov.line_violations(["a b c d e f g h i"])          # 9 words
    assert ov.line_violations(["one", "two", "three"])         # 3 lines
    assert ov.line_violations(["ONE TWO", "THREE FOUR"]) == []


def test_layout_is_all_caps():
    frames = ov.layout_overlay("start in august")
    assert frames[0][0] == "START IN AUGUST"


# ---- safe zones ------------------------------------------------------------
def test_safe_zone_rejects_top_and_bottom_bands():
    assert ov.safe_zone_ok((300, 500)) is True
    assert ov.safe_zone_ok((100, 300)) is False               # into top 250px
    assert ov.safe_zone_ok((1500, 1650)) is False             # into bottom 310px
    lo, hi = ov.safe_zone_bounds()
    assert (lo, hi) == (250, 1610)


# ---- contrast / scrim ------------------------------------------------------
def test_white_text_on_white_needs_scrim():
    assert ov.needs_scrim((255, 255, 255), (250, 250, 250)) is True
    a = ov.scrim_alpha_for((255, 255, 255), (250, 250, 250))
    assert a > 0                                              # a scrim is added


def test_white_text_on_dark_needs_no_scrim():
    assert ov.needs_scrim((255, 255, 255), (10, 15, 30)) is False
    assert ov.scrim_alpha_for((255, 255, 255), (10, 15, 30)) == 0


def test_contrast_ratio_symmetric():
    r = ov.contrast_ratio((0, 0, 0), (255, 255, 255))
    assert round(r, 1) == 21.0


# ---- identity anchor -------------------------------------------------------
def test_identity_anchor_added_when_missing():
    frames = ov.build_overlay("we start monday", identity_tokens=["Birmingham"]).frames
    joined = " ".join(ln for fr in frames for ln in fr)
    assert "BIRMINGHAM" in joined


def test_identity_anchor_present_not_duplicated():
    spec = ov.build_overlay("BIRMINGHAM starts monday", identity_tokens=["Birmingham"])
    joined = " ".join(ln for fr in spec.frames for ln in fr)
    assert joined.count("BIRMINGHAM") == 1


# ---- copy_gate + avatar rail on overlay copy -------------------------------
def test_overlay_copy_scrubs_dashes():
    # an en dash in the source is scrubbed, never burned on-image.
    spec = ov.build_overlay("start now — no excuses", identity_tokens=["City"])
    joined = " ".join(ln for fr in spec.frames for ln in fr)
    assert "—" not in joined
    assert "-" not in joined  # on-image text carries no ascii hyphen either


def test_overlay_avatar_rail_blocks_hyrox_by_default(monkeypatch):
    # The avatar rail is OFF by default since Blake's 2026-09-01 ruling (CrossFit,
    # hyrox and competitive athletics are allowed). This test describes the rail's
    # behavior WHEN ARMED, so it arms it explicitly.
    monkeypatch.setenv("AGENT_AVATAR_ATHLETE_RAIL", "true")
    with pytest.raises(ov.OverlayRejected):
        ov.build_overlay("HYROX PREP STARTS NOW", identity_tokens=["City"], gym="pierce")


def test_overlay_avatar_rail_allows_hyrox_for_allowlisted_gym(monkeypatch):
    monkeypatch.setenv("STORY_HYROX_AVATAR_GYMS", "birmingham")
    spec = ov.build_overlay("BIRMINGHAM HYROX SEASON",
                            identity_tokens=["Birmingham"], gym="birmingham")
    joined = " ".join(ln for fr in spec.frames for ln in fr)
    assert "HYROX" in joined


# ---- identity anchor is a hard invariant on a REAL render (spec §1) --------
def test_enforce_ask_refuses_a_render_with_no_identity_tokens():
    """story_studio.py always calls build_overlay(enforce_ask=True) for a real
    render. There is no server-side default for identity_tokens (the frontend must
    supply the gym's city/name), so a render that would ship with no anchor at all
    must HOLD, never silently post without one."""
    with pytest.raises(ov.OverlayRejected):
        ov.build_overlay("BOOK YOUR FREE INTRO", identity_tokens=(), gym="pierce",
                         enforce_ask=True)


def test_enforce_ask_false_is_the_card_builder_unit_path():
    """enforce_ask defaults False (card-builder / unit use, where no render is
    happening) — this path may build a spec with no identity_tokens; it is
    story_studio's job, not build_overlay's, to refuse an actual render."""
    spec = ov.build_overlay("BOOK YOUR FREE INTRO", identity_tokens=(), gym="pierce")
    assert spec.frames  # no raise


# ---- low confidence -> generic safe + flag ---------------------------------
def test_low_confidence_ships_generic_safe_with_flag():
    spec = ov.build_overlay("", identity_tokens=["City"], low_confidence=True)
    assert spec.grounded_from == "generic_safe"
    assert spec.flags
    joined = " ".join(ln for fr in spec.frames for ln in fr)
    assert ov.GENERIC_SAFE_HOOK in joined


# ---- exactly one ask frame (spec §1) ---------------------------------------
def test_count_asks():
    assert ov.count_asks("BOOK YOUR FREE INTRO") == 1
    assert ov.count_asks("START THIS WEEK") == 1
    assert ov.count_asks("YOUR NEXT REP STARTS HERE") == 0        # a hook, not an ask
    assert ov.count_asks("BOOK NOW AND SIGN UP TODAY") >= 2       # two asks


def test_enforce_ask_builds_single_ask_frame():
    spec = ov.build_overlay("BIRMINGHAM STRONG", identity_tokens=["Birmingham"],
                            ask="BOOK YOUR FREE INTRO", enforce_ask=True)
    assert spec.ask_frame                                          # one end-frame exists
    assert ov.count_asks(" ".join(spec.ask_frame)) == 1


def test_enforce_ask_rejects_ask_in_body():
    # the body must carry ZERO asks; the ask lives only on the end-frame.
    with pytest.raises(ov.OverlayRejected):
        ov.build_overlay("BOOK YOUR SPOT NOW", identity_tokens=["City"],
                         ask="START THIS WEEK", enforce_ask=True)


def test_enforce_ask_rejects_missing_ask():
    with pytest.raises(ov.OverlayRejected):
        ov.build_overlay("BIRMINGHAM STRONG", identity_tokens=["Birmingham"],
                         ask="", enforce_ask=True)


def test_enforce_ask_rejects_double_ask():
    with pytest.raises(ov.OverlayRejected):
        ov.build_overlay("BIRMINGHAM STRONG", identity_tokens=["Birmingham"],
                         ask="BOOK NOW AND SIGN UP TODAY", enforce_ask=True)


def test_no_enforce_ask_leaves_ask_as_field_only():
    # default (card-builder / unit use): the ask is stored, no invariant enforced.
    spec = ov.build_overlay("SOME COPY", identity_tokens=["City"], ask="")
    assert spec.ask_frame == []


# ---- cards -----------------------------------------------------------------
def test_stat_card_name_number_place():
    s = ov.stat_card("Mike Collins", "1:04:24", "Stockholm")
    assert s == "MIKE COLLINS\n1:04:24\nSTOCKHOLM"


def test_event_card_what_when_ask():
    s = ov.event_card("1 Year Party", "Saturday September 12", "Save your spot")
    assert "1 YEAR PARTY" in s and "SATURDAY SEPTEMBER 12" in s
