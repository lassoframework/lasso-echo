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
    # GYM legs (Blake 2026-08-27: image quality is NOT graded for clients, so
    # visual_match is absent and the remaining five legs renormalize to 0-100)
    for leg in ("consistency", "content_mix", "caption_craft",
                "right_audience", "path_to_join"):
        assert leg in result.scores, f"Missing score leg: {leg}"
    assert "visual_match" not in result.scores, (
        "GYM profile must not grade the visual_match leg (client-owned media)"
    )
    # B2B keeps the visual_match (proof_numbers) leg, unchanged
    b2b = grade_month(rows, profile="B2B")
    assert "visual_match" in b2b.scores


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
# GYM rubric renormalization (Blake 2026-08-27: clients upload their own
# media, so image quality is never graded; the five remaining legs scale
# 100/85 to keep the total 0-100)
# ---------------------------------------------------------------------------

def test_gym_a_achievable_with_client_photos_of_any_quality():
    """A GYM month with stock-looking, non-vision-derived uploads but clean
    captions/mix must still grade A: Echo controls captions and mix only."""
    rows = []
    pillars = ["community", "results", "education", "coach", "invite",
               "story", "faq"]
    for i in range(28):
        rows.append(_make_row(
            post_date=f"2026-09-{(i + 1):02d}" if i < 30 else "",
            caption=_perfect_caption(i),
            pillar=pillars[i % len(pillars)],
            vision_derived=False,
            media_url="https://stockphotos.com/upload.jpg",
            template_id=f"tmpl_{i % 3}",       # mixed templates: also not graded
            media_kind="photo",
        ))
    result = grade_month(rows, profile="GYM")
    assert result.total >= 90, (
        f"Client-photo month must reach A regardless of image quality; got "
        f"{result.total} ({result.letter}), scores={result.scores}, "
        f"defects={result.defects}"
    )
    assert result.letter == "A"


def test_gym_renormalization_math():
    """Perfect GYM month: raw 85/85 -> 100. One 1-day gap: raw 81 -> 95
    (int(81 * 100 / 85 + 0.5))."""
    rows = _perfect_month(28)
    perfect = grade_month(rows, profile="GYM")
    assert perfect.total == 100, (
        f"Perfect month should renormalize to 100, got {perfect.total} "
        f"({perfect.scores})"
    )
    # Introduce exactly one 1-day gap (-4 on consistency): drop 2026-09-02.
    gapped = [r for r in rows if r["post_date"] != "2026-09-02"]
    g = grade_month(gapped, profile="GYM")
    assert g.scores["consistency"] == 16, g.scores
    assert g.total == 95, (
        f"raw 81 must renormalize to 95, got {g.total} ({g.scores})"
    )


def test_same_date_cross_post_and_story_are_one_post_not_dups():
    """A feed cross-posted to IG + FB with its paired story shares ONE caption
    on ONE date by design: no consistency dup defect, consistency stays 20."""
    rows = []
    for i in range(10):
        d = f"2026-09-{(i + 1):02d}"
        cap = _perfect_caption(i)
        rows.append(_make_row(post_date=d, caption=cap, pillar="platform"))
        fb = _make_row(post_date=d, caption=cap, pillar="platform")
        fb["account"] = "facebook"
        rows.append(fb)
        story = _make_row(post_date=d, caption=cap, pillar="platform")
        story["format"] = "story"
        rows.append(story)
    result = grade_month(rows, profile="GYM")
    dup_defects = [d for d in result.defects
                   if d[0] == "consistency" and "repeated" in d[2]]
    assert not dup_defects, (
        f"Same-date cross-post/story mirrors must not count as dups: {dup_defects}"
    )
    assert result.scores["consistency"] == 20


def test_cross_date_repeat_still_counts_as_dup():
    """The SAME caption on two different dates is a true repeat: -8."""
    cap = _perfect_caption(1)
    rows = [
        _make_row(post_date="2026-09-01", caption=cap),
        _make_row(post_date="2026-09-02", caption=cap),
    ]
    result = grade_month(rows, profile="GYM")
    dup_defects = [d for d in result.defects
                   if d[0] == "consistency" and "repeated 2 times" in d[2]]
    assert dup_defects, f"Cross-date repeat must be a dup defect: {result.defects}"
    assert result.scores["consistency"] <= 12


# ---------------------------------------------------------------------------
# Test 10: defect tuples present for each violated leg
# ---------------------------------------------------------------------------

def test_defects_present_for_violations():
    # Rows with multiple deliberate violations:
    # - em-dash (caption_craft)
    # - no ask (path_to_join)
    # - athlete-avatar leak (right_audience)
    # Stock / not-vision-derived media is deliberately included: the GYM
    # profile no longer grades image quality (client-owned media), so it must
    # produce NO visual_match defects.
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
    # GYM never emits visual_match defects (image quality is not graded)
    visual_defects = [d for d in result.defects if d[0] == "visual_match"]
    assert not visual_defects, (
        f"GYM profile must not grade visuals, got: {visual_defects}"
    )
    # right_audience defects (athlete-avatar leak)
    audience_defects = [d for d in result.defects if d[0] == "right_audience"]
    assert audience_defects, "Expected right_audience defects for compete/competition hook"


def test_content_mix_sprint_summit_is_exempt():
    """Blake's 10-day sprint ruling: summit concentration inside a sprint window is
    intended, not a content_mix defect. Summit dated on a sprint day must not trip
    the 25% cap, while every other category (and off-sprint summit) still does."""
    from agent import calendar_grade as cg
    from agent import summit_queue as sq
    sprint = sorted(sq.sprint_days())
    assert sprint, "need sprint days for this test"
    rows = [{"pillar": "summit", "post_date": d, "caption": "x", "format": "feed"}
            for d in sprint[:24]]
    # a handful of genuinely balanced off-sprint content
    rows += [{"pillar": "doctrine", "post_date": "2026-12-01", "caption": "x", "format": "feed"},
             {"pillar": "platform", "post_date": "2026-12-02", "caption": "x", "format": "feed"},
             {"pillar": "podcast", "post_date": "2026-12-03", "caption": "x", "format": "feed"},
             {"pillar": "b2b", "post_date": "2026-12-04", "caption": "x", "format": "feed"}]
    defects = []
    cg._content_mix(rows, "B2B", None, defects)
    summit_defects = [d for d in defects if d[1] == "summit"]
    assert not summit_defects, f"sprint summit must be exempt, got {summit_defects}"


def test_content_mix_off_sprint_summit_still_capped():
    """Summit OUTSIDE any sprint window is capped exactly as before (regression)."""
    from agent import calendar_grade as cg
    # Month-sized and on DISTINCT dates: the cap is only measured at or above
    # _MIX_CAP_MIN_POSTS posts, and same-date same-caption rows are ONE post.
    rows = [{"pillar": "summit", "post_date": f"2026-12-{d:02d}",
             "caption": f"x{d}", "format": "feed"} for d in range(10, 20)]
    rows += [{"pillar": "doctrine", "post_date": f"2026-12-{d:02d}",
              "caption": f"y{d}", "format": "feed"} for d in range(20, 30)]
    defects = []
    cg._content_mix(rows, "B2B", None, defects)
    assert any(d[1] == "summit" for d in defects), "off-sprint summit must still cap"


def test_empty_and_story_captions_never_count_as_duplicates():
    """2026-08-31: hash('') zeroed LASSO's consistency ('repeated 30 times') over its
    captionless story book + GBP photos, and stories share their paired feed's caption
    BY DESIGN. Neither may ever count as a duplicate."""
    from agent.calendar_grade import grade_month
    rows = []
    for d in range(1, 11):
        day = f"2026-09-{d:02d}"
        rows.append({"post_date": day, "account": "instagram", "format": "feed",
                     "caption": f"Unique caption number {d} with plenty of words to "
                                "clear the craft floor. Book your intro session.",
                     "pillar": "service" if d % 2 else "about"})
        rows.append({"post_date": day, "account": "instagram", "format": "story",
                     "caption": "", "pillar": "service"})          # captionless story
    g = grade_month(rows, profile="GYM")
    assert not any("repeated" in str(d[2]) for d in g.defects), \
        "captionless stories must never register as duplicate captions"


def test_proof_slot_backing_judged_from_real_row_shape():
    """2026-08-31: vision_derived/media_kind are NOT content_calendar columns, so every
    proof/results row flagged 'unbacked' forever (a phantom that held ENG at F). A proof
    slot with real media on the row is BACKED; only a media-less one is a defect."""
    from agent.calendar_grade import grade_month
    base = {"account": "instagram", "format": "feed",
            "caption": "Real member results and the coaching that earned them. Ask us "
                       "how to start your own twelve week block today."}
    rows = [
        dict(base, post_date="2026-09-01", pillar="proof",
             image_url="https://r2/real_member_photo.jpg"),          # backed
        dict(base, post_date="2026-09-02", pillar="results",
             image_url="", source_media_asset_id="drive123"),        # backed (Drive)
        dict(base, post_date="2026-09-03", pillar="proof", image_url=""),   # unbacked
    ]
    g = grade_month(rows, profile="GYM")
    unbacked = [d for d in g.defects if "unbacked" in str(d[2])]
    assert len(unbacked) == 1 and unbacked[0][1] == "2026-09-03"


# ---------------------------------------------------------------------------
# The 25% mix cap is only measurable on a month-sized book
# ---------------------------------------------------------------------------

def test_mix_cap_exempt_on_a_book_too_small_to_satisfy_it():
    """On a 7-post book the cap allows floor(0.25 * 7) = 1 post per pillar, so
    satisfying it needs SEVEN pillars. sunnyside and topfuel were marked down
    for arithmetic no repair could clear. Exempt, and COUNTED so the digest
    can say so."""
    rows = [_make_row(post_date=f"2026-09-{(i + 1):02d}",
                      caption=_perfect_caption(i),
                      pillar=["service", "about", "service"][i % 3])
            for i in range(7)]
    g = grade_month(rows, profile="GYM")
    assert not [d for d in g.defects if "over 25%" in d[2]], g.defects
    assert any("25% cap not measurable" in k for k in g.exempt), g.exempt
    assert g.scores["content_mix"] == 20


def test_mix_cap_still_applies_to_a_month_sized_book():
    """Regression: a real month is still capped exactly as before."""
    rows = [_make_row(post_date=f"2026-09-{(i + 1):02d}",
                      caption=_perfect_caption(i),
                      pillar="service" if i < 8 else "about")
            for i in range(16)]
    g = grade_month(rows, profile="GYM")
    assert [d for d in g.defects if "over 25%" in d[2]], g.defects
    assert g.scores["content_mix"] < 20
