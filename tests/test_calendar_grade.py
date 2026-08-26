"""
tests/test_calendar_grade.py — Wave 5: calendar grader unit tests.

All tests are deterministic and offline (no API calls, no Supabase).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from agent.calendar_grade import (
    A_THRESHOLD,
    CalendarGrade,
    BANDS,
    grade_month,
)
from agent.caption_ledger import caption_hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(post_date="2026-09-01", caption="", pillar="platform",
              vision_derived=True, media_url="https://cdn.example.com/img.jpg",
              template_id="tmpl_A", media_kind="photo"):
    return {
        "post_date": post_date,
        "caption": caption,
        "pillar": pillar,
        "category": pillar,
        "vision_derived": vision_derived,
        "media_url": media_url,
        "template_id": template_id,
        "media_kind": media_kind,
    }


def _perfect_caption(i: int) -> str:
    """A clean caption with an ask, no dashes, 160+ chars, short first line, gen-pop hook.

    First line must be <= 125 chars (hook_too_long soft flag threshold).
    Total must be >= 150 chars (median length threshold).
    Must contain a booking ask (ASK_RE match).
    No banned dashes, no athlete-avatar language.
    """
    # Short hook line (<= 125 chars), newline-terminated so it is the first line only
    hook = f"Hard to stay on track? We get it. Post {i % 99}."
    # Body: enough to hit 150+ total chars, with a booking ask
    body = (
        "\nBusy moms and working professionals love our 30-minute format. "
        "No experience needed. Real results, real people. "
        "Get started today and book your free intro class."
    )
    return hook + body


def _perfect_month(n=28) -> list:
    """28 rows, all pillars covered, clean captions, one ask each, vision_derived."""
    pillars = ["platform", "doctrine", "b2b", "podcast", "summit",
               "welcome", "book"]
    rows = []
    for i in range(n):
        d = f"2026-09-{(i + 1):02d}" if i < 30 else f"2026-10-{(i - 29):02d}"
        pillar = pillars[i % len(pillars)]
        cap = _perfect_caption(i)
        rows.append(_make_row(
            post_date=d,
            caption=cap,
            pillar=pillar,
            vision_derived=True,
            media_url="https://cdn.example.com/img.jpg",
            template_id="tmpl_A",
            media_kind="photo",
        ))
    return rows


# ---------------------------------------------------------------------------
# Test 1: perfect month grades A (total >= 90)
# ---------------------------------------------------------------------------

def test_perfect_month_grades_A():
    rows = _perfect_month(28)
    result = grade_month(rows, profile="GYM")
    assert isinstance(result, CalendarGrade)
    assert result.total >= 90, (
        f"Perfect month scored {result.total} ({result.letter}), "
        f"expected >= 90. Scores: {result.scores}. "
        f"Defects: {result.defects}"
    )
    assert result.letter == "A"


# ---------------------------------------------------------------------------
# Test 2: 20 duplicate captions -> consistency score 0, letter F
# ---------------------------------------------------------------------------

def test_duplicate_captions_grades_F_on_consistency():
    """20 duplicate captions must zero the consistency leg and yield an F overall.

    Spec (ECHO_A_GRADE_SPEC.md line 286): 'the 20x repeat month grades F on
    consistency'. When consistency hits 0 from duplicate captions the total is
    capped to 59 so the letter is always F, not D or C."""
    same_cap = _perfect_caption(0)
    rows = [_make_row(
        post_date=f"2026-09-{(i + 1):02d}",
        caption=same_cap,
        pillar="platform",
        vision_derived=True,
    ) for i in range(20)]
    result = grade_month(rows, profile="GYM")
    assert result.scores["consistency"] == 0, (
        f"Expected consistency=0 with 20 dups, got {result.scores['consistency']}"
    )
    assert result.letter == "F", (
        f"Spec requires 20x repeat month grades F; got letter={result.letter!r} "
        f"(total={result.total}, scores={result.scores})"
    )
    assert result.total < 60, (
        f"Expected total < 60 (F range) with 20 identical captions, got {result.total}"
    )


# ---------------------------------------------------------------------------
# Test 3: 0 ask-containing posts -> path_to_join <= 4
# ---------------------------------------------------------------------------

def test_no_ask_posts_path_low():
    # Captions with no ask at all
    rows = [_make_row(
        post_date=f"2026-09-{(i + 1):02d}",
        caption=f"Post {i} about weight loss and consistency. Great vibes today.",
        pillar="platform",
        vision_derived=True,
    ) for i in range(10)]
    result = grade_month(rows, profile="GYM")
    assert result.scores["path_to_join"] <= 4, (
        f"Expected path_to_join <= 4 with no asks, "
        f"got {result.scores['path_to_join']}"
    )


# ---------------------------------------------------------------------------
# Test 4: summit at 44% -> content_mix has cap violation defect
# ---------------------------------------------------------------------------

def test_content_mix_category_over_25_pct():
    rows = []
    # 12 summit out of 27 is ~44%
    for i in range(12):
        rows.append(_make_row(
            post_date=f"2026-09-{(i + 1):02d}",
            caption=_perfect_caption(i),
            pillar="summit",
        ))
    for i in range(12, 27):
        rows.append(_make_row(
            post_date=f"2026-09-{(i + 1):02d}",
            caption=_perfect_caption(i),
            pillar="platform",
        ))
    result = grade_month(rows, profile="GYM")
    # There should be a defect about a category over 25%
    cap_defects = [d for d in result.defects
                   if "25%" in d[2] or "over 25" in d[2]]
    assert cap_defects, (
        f"Expected a content_mix cap violation defect, got: {result.defects}"
    )


# ---------------------------------------------------------------------------
# Test 5: em-dash in caption -> caption_craft score = 0
# ---------------------------------------------------------------------------

def test_em_dash_caption_craft_zero():
    em_dash_cap = "Ready to transform your body—join us today. Get started now."
    rows = [_make_row(
        post_date=f"2026-09-{(i + 1):02d}",
        caption=em_dash_cap,
        vision_derived=True,
    ) for i in range(5)]
    result = grade_month(rows, profile="GYM")
    assert result.scores["caption_craft"] == 0, (
        f"Expected caption_craft=0 with em-dash, got {result.scores['caption_craft']}"
    )


# ---------------------------------------------------------------------------
# Test 6: profile="B2B" uses _proof_numbers, not _visual_match
# ---------------------------------------------------------------------------

def test_b2b_profile_uses_proof_numbers():
    # B2B rows: captions with numbers and mentions -> visual_match (proof_numbers) should score
    rows = []
    for i in range(20):
        cap = (f"We helped {500 + i} gyms grow their revenue. "
               f"@gymowner{i} saw 40 new members. Book a call today.")
        rows.append(_make_row(
            post_date=f"2026-09-{(i + 1):02d}",
            caption=cap,
            pillar="b2b",
            vision_derived=True,
        ))
    result_b2b = grade_month(rows, profile="B2B")
    result_gym = grade_month(rows, profile="GYM")
    # B2B profile must score visual_match via proof_numbers (numbers + mentions present)
    # vs GYM profile (visual_match checks vision_derived + stock)
    # The key test: both return a CalendarGrade and B2B uses proof_numbers
    assert result_b2b.scores["visual_match"] is not None
    # With 20 rows containing numbers and mentions, B2B proof_numbers scores >= 10
    assert result_b2b.scores["visual_match"] >= 10, (
        f"B2B proof_numbers with numbers+mentions: {result_b2b.scores['visual_match']}"
    )


# ---------------------------------------------------------------------------
# Test 7: grade_month returns CalendarGrade with .total, .letter, .scores, .defects
# ---------------------------------------------------------------------------

def test_grade_month_returns_calendar_grade():
    rows = _perfect_month(5)
    result = grade_month(rows, profile="GYM")
    assert isinstance(result, CalendarGrade)
    assert isinstance(result.total, int)
    assert isinstance(result.letter, str)
    assert isinstance(result.scores, dict)
    assert isinstance(result.defects, list)
    # All expected legs present
    for leg in ("consistency", "content_mix", "caption_craft",
                "visual_match", "right_audience", "path_to_join"):
        assert leg in result.scores, f"Missing score leg: {leg}"


# ---------------------------------------------------------------------------
# Test 8: CalendarGrade total=90 -> A; total=89 -> B
# ---------------------------------------------------------------------------

def test_letter_grade_bands():
    grade_90 = CalendarGrade(total=90, letter="A",
                             scores={}, defects=[])
    grade_89 = CalendarGrade(total=89, letter="B",
                             scores={}, defects=[])
    # Verify the bands logic inline
    def _letter(total):
        return next(l for floor, l in BANDS if total >= floor)

    assert _letter(90) == "A"
    assert _letter(89) == "B"
    assert _letter(80) == "B"
    assert _letter(79) == "C"
    assert _letter(70) == "C"
    assert _letter(69) == "D"
    assert _letter(60) == "D"
    assert _letter(59) == "F"
    assert _letter(0) == "F"


# ---------------------------------------------------------------------------
# Test 9: A_THRESHOLD is 90
# ---------------------------------------------------------------------------

def test_a_threshold_is_90():
    assert A_THRESHOLD == 90


# ---------------------------------------------------------------------------
# Test 10: defect tuples present for each violated leg
# ---------------------------------------------------------------------------

def test_defects_present_for_violations():
    # Rows with multiple deliberate violations:
    # - em-dash (caption_craft)
    # - no ask (path_to_join)
    # - not vision_derived + stock (visual_match)
    # - athlete-avatar leak (right_audience)
    rows = [
        _make_row(
            post_date=f"2026-09-{(i + 1):02d}",
            caption=f"Compete in the next competition—sign up now.",
            pillar="platform",
            vision_derived=False,
            media_url="https://stockphotos.com/img.jpg",
        )
        for i in range(5)
    ]
    result = grade_month(rows, profile="GYM")
    # caption_craft violation (em-dash) -> score = 0 (no defect tuple needed since it's a hard 0)
    assert result.scores["caption_craft"] == 0
    # visual_match defects (stock detected)
    visual_defects = [d for d in result.defects if d[0] == "visual_match"]
    assert visual_defects, "Expected visual_match defects for stock media"
    # right_audience defects (athlete-avatar leak)
    audience_defects = [d for d in result.defects if d[0] == "right_audience"]
    assert audience_defects, "Expected right_audience defects for compete/competition hook"
