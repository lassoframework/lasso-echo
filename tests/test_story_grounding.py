"""
Story Studio Wave 4: copy grounding. Brief wins over vision; no brief + low vision
confidence -> generic-safe + flag; Echo never fabricates.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import story_grounding as g  # noqa: E402


def test_brief_wins_over_vision():
    analysis = {"confidence": 0.9, "subjects": ["a crowd"], "setting": "indoor"}
    gr = g.ground_copy(brief="Member hit their first pull-up", analysis=analysis)
    assert gr.source == g.SOURCE_BRIEF
    assert "pull-up" in gr.text
    assert gr.low_confidence is False


def test_vision_grounds_when_no_brief_and_confident():
    analysis = {"confidence": 0.8, "subjects": ["two members"], "setting": "indoor",
                "mood": "energetic"}
    gr = g.ground_copy(brief=None, analysis=analysis)
    assert gr.source == g.SOURCE_VISION
    assert gr.text
    assert gr.low_confidence is False


def test_no_brief_low_vision_confidence_is_generic_safe():
    analysis = {"confidence": 0.2, "subjects": [], "setting": ""}
    gr = g.ground_copy(brief=None, analysis=analysis)
    assert gr.source == g.SOURCE_GENERIC
    assert gr.low_confidence is True
    assert gr.flags
    assert gr.text == ""  # nothing fabricated


def test_no_brief_no_analysis_is_generic_safe():
    gr = g.ground_copy(brief=None, analysis=None)
    assert gr.source == g.SOURCE_GENERIC
    assert gr.low_confidence is True


def test_blank_brief_falls_through_to_vision_or_generic():
    gr = g.ground_copy(brief="  ", analysis={"confidence": 0.9,
                                             "subjects": ["a lifter"]})
    assert gr.source == g.SOURCE_VISION
