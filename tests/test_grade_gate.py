"""
House-style grade gate tests. Offline: no API calls. Asserts programmatic checks
(Q3 single accent, Q4 no banned copy, Q6 feed-stopping) and structural logic
(GradeResult, pass threshold). Vision client None -> vision questions default
to None (pass-through).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.grade_gate import (  # noqa: E402
    GradeResult, PASS_THRESHOLD,
    _q3_single_accent_heuristic,
    _q4_no_banned_copy,
    _q6_feed_stopping_heuristic,
    grade_card,
)


# ---- Q3: single red accent heuristic -------------------------------------------

def test_q3_passes_with_exactly_one_red():
    prompt = "Exactly one element on the entire card uses red (#FF0000). Never a red background."
    assert _q3_single_accent_heuristic(prompt) is True


def test_q3_fails_red_background():
    prompt = "Use a red background for the entire card with exactly one accent."
    assert _q3_single_accent_heuristic(prompt) is False


def test_q3_fails_no_red_at_all():
    prompt = "Navy canvas with white type and sky blue accents."
    assert _q3_single_accent_heuristic(prompt) is False


def test_q3_fails_no_exactly_one():
    prompt = "Use red generously throughout the card to create energy."
    assert _q3_single_accent_heuristic(prompt) is False


# ---- Q4: no banned copy --------------------------------------------------------

def test_q4_passes_clean_headline():
    assert _q4_no_banned_copy("Speed to lead wins") is True


def test_q4_fails_em_dash():
    assert _q4_no_banned_copy("Speed to lead—wins") is False


def test_q4_fails_en_dash():
    assert _q4_no_banned_copy("Speed to lead–wins") is False


def test_q4_fails_hyphen():
    assert _q4_no_banned_copy("Speed-to-lead wins") is False


def test_q4_fails_vendor():
    assert _q4_no_banned_copy("Your vendor solution") is False


def test_q4_passes_empty_headline():
    assert _q4_no_banned_copy("") is True


# ---- Q6: feed-stopping visual anchor -------------------------------------------

def test_q6_passes_with_illustrated_element():
    prompt = "ILLUSTRATED ELEMENT: a gym owner reviewing reports at a desk."
    assert _q6_feed_stopping_heuristic(prompt) is True


def test_q6_passes_with_visual_anchor():
    prompt = "VISUAL ANCHOR: a full-width NAVY color block occupying the top half."
    assert _q6_feed_stopping_heuristic(prompt) is True


def test_q6_passes_with_color_block():
    prompt = "color block in navy fills the upper zone of the card."
    assert _q6_feed_stopping_heuristic(prompt) is True


def test_q6_passes_with_full_width():
    prompt = "full-width photo treatment behind the type."
    assert _q6_feed_stopping_heuristic(prompt) is True


def test_q6_passes_with_duotone():
    prompt = "duotone photo treatment: navy on cream, full bleed."
    assert _q6_feed_stopping_heuristic(prompt) is True


def test_q6_passes_with_magazine_cover():
    prompt = "Set the headline at magazine cover scale."
    assert _q6_feed_stopping_heuristic(prompt) is True


def test_q6_fails_bare_editorial_no_anchor():
    prompt = (
        "Archetype EDITORIAL: typography alone. Eyebrow: OWNER'S ADVANTAGE. "
        "Headline: Built by gym owners. Deck: The system we run on ourselves first."
    )
    assert _q6_feed_stopping_heuristic(prompt) is False


def test_q6_fails_empty_prompt():
    assert _q6_feed_stopping_heuristic("") is False


# ---- GradeResult structural logic ----------------------------------------------

def test_grade_result_zero_fails_passes():
    gr = GradeResult(
        scores={"Q1": True, "Q2": True, "Q3": True, "Q4": True, "Q5": True},
        passed=True,
        failed_questions=[],
    )
    assert gr.passed is True
    assert gr.failed_questions == []


def test_grade_result_one_fail_passes():
    gr = GradeResult(
        scores={"Q1": None, "Q2": True, "Q3": True, "Q4": True, "Q5": False},
        passed=True,
        failed_questions=["Q5"],
    )
    assert gr.passed is True


def test_grade_result_two_fails_fails():
    gr = GradeResult(
        scores={"Q1": False, "Q2": True, "Q3": False, "Q4": True, "Q5": True},
        passed=False,
        failed_questions=["Q1", "Q3"],
    )
    assert gr.passed is False
    assert len(gr.failed_questions) == 2


# ---- grade_card: vision=None treats Q1/Q2/Q5 as None (pass-through) -----------
# Prompts include "ILLUSTRATED ELEMENT" so Q6 passes programmatically.

def test_grade_card_no_vision_client_q3_q4_pass():
    # Good prompt: Q3 passes, Q4 passes, Q6 passes (has illustrated element)
    prompt = (
        "Exactly one element on the entire card uses red (#FF0000). "
        "Never a red background. "
        "ILLUSTRATED ELEMENT: a gym owner reviewing a lead pipeline report."
    )
    result = grade_card(prompt, headline="Speed to lead wins", vision_client=None)
    # Q1, Q2, Q5 are None (vision skipped). Q3=True, Q4=True, Q6=True. 0 hard fails -> passed.
    assert result.passed is True
    assert result.scores["Q1"] is None
    assert result.scores["Q3"] is True
    assert result.scores["Q4"] is True
    assert result.scores["Q6"] is True
    assert "Q3" not in result.failed_questions
    assert "Q4" not in result.failed_questions
    assert "Q6" not in result.failed_questions


def test_grade_card_no_vision_client_q4_fail():
    # Banned headline (hyphen). Q3 passes, Q6 passes (illustrated element), Q4 fails.
    # 1 hard fail -> passed (≤1 fail allowed).
    prompt = (
        "Exactly one element on the entire card uses red. Never a red background. "
        "ILLUSTRATED ELEMENT: a gym owner at a whiteboard."
    )
    result = grade_card(prompt, headline="Speed-to-lead wins", vision_client=None)
    assert result.passed is True
    assert "Q4" in result.failed_questions


def test_grade_card_no_vision_client_q3_and_q4_fail():
    # Both programmatic checks fail -> 2 hard fails -> card fails
    prompt = "Use a red background with cream text."  # no "exactly one", red background
    result = grade_card(prompt, headline="Speed-to-lead wins", vision_client=None)
    # Q3=False (red background), Q4=False (hyphen), Q6=False (no anchor). 3 fails -> failed.
    assert result.passed is False
    assert "Q3" in result.failed_questions
    assert "Q4" in result.failed_questions


def test_grade_card_q6_fails_bare_editorial():
    # Editorial prompt with no visual anchor: Q6 fails.
    # Q3 passes (has "exactly one" + "red"), Q4 passes, Q6 fails.
    # 1 hard fail -> still passes (≤1 fail allowed).
    prompt = (
        "Exactly one element on the entire card uses red. Never a red background. "
        "Archetype EDITORIAL: Eyebrow OWNER'S ADVANTAGE. "
        "Headline: Built by gym owners."
    )
    result = grade_card(prompt, headline="Built by gym owners", vision_client=None)
    assert result.scores["Q6"] is False
    assert "Q6" in result.failed_questions
    # Q3 True, Q4 True, Q6 False, Q1/Q2/Q5 None -> 1 fail -> passes
    assert result.passed is True


def test_grade_card_two_fails_including_q6():
    # Q4 fails (hyphen) AND Q6 fails (no anchor) -> 2 fails -> card fails.
    prompt = "Exactly one element on the entire card uses red. Never a red background."
    result = grade_card(prompt, headline="Speed-to-lead wins", vision_client=None)
    assert result.scores["Q4"] is False
    assert result.scores["Q6"] is False
    assert result.passed is False


def test_pass_threshold_constant():
    assert PASS_THRESHOLD == 5
