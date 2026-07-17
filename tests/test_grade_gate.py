"""
House-style grade gate tests. Offline: no API calls. Asserts programmatic checks
(Q3 single accent, Q4 no banned copy) and structural logic (GradeResult, pass
threshold). Vision client None -> vision questions default to None (pass-through).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.grade_gate import (  # noqa: E402
    GradeResult, PASS_THRESHOLD,
    _q3_single_accent_heuristic,
    _q4_no_banned_copy,
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

def test_grade_card_no_vision_client_q3_q4_pass():
    # Good prompt: has "exactly one" + "red" + no "red background"; clean headline
    prompt = ("Exactly one element on the entire card uses red (#FF0000). "
              "Never a red background.")
    result = grade_card(prompt, headline="Speed to lead wins", vision_client=None)
    # Q1, Q2, Q5 are None (vision skipped). Q3=True, Q4=True. 0 hard fails -> passed.
    assert result.passed is True
    assert result.scores["Q1"] is None
    assert result.scores["Q3"] is True
    assert result.scores["Q4"] is True
    assert "Q3" not in result.failed_questions
    assert "Q4" not in result.failed_questions


def test_grade_card_no_vision_client_q4_fail():
    # Banned headline: contains a hyphen
    prompt = "Exactly one element on the entire card uses red. Never a red background."
    result = grade_card(prompt, headline="Speed-to-lead wins", vision_client=None)
    # Q4 fails; Q1/Q2/Q5 None; Q3 True. 1 hard fail -> passed (≤1 fail allowed).
    assert result.passed is True
    assert "Q4" in result.failed_questions


def test_grade_card_no_vision_client_q3_and_q4_fail():
    # Both programmatic checks fail -> 2 hard fails -> card fails
    prompt = "Use a red background with cream text."  # no "exactly one", red background
    result = grade_card(prompt, headline="Speed-to-lead wins", vision_client=None)
    # Q3=False (red background), Q4=False (hyphen). 2 fails -> failed.
    assert result.passed is False
    assert "Q3" in result.failed_questions
    assert "Q4" in result.failed_questions


def test_pass_threshold_constant():
    assert PASS_THRESHOLD == 4
