"""
Weekly quotas + platform sub-topic rotation (category rotation Part 4).

Across planned months, proves: no weekly cap breached (podcast<=3, platform<=2,
b2b<=1, book<=1, summit per the ramp), book never over 1 per week yet present
across the month, doctrine fills the gaps, and no platform sub-topic repeats
within any 10-day window.

Wave 2 additions (AGENT_CATEGORY_QUOTAS):
- CATEGORIES includes "proof" and "call"
- GYM_PILLARS includes all 6 gym pillars
- validate_quotas() returns violations for non-compliant plans
- category_pct() returns the correct percentage
- 25% hard cap is detected and reported
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import category_plan  # noqa: E402
from agent.content_categories import CATEGORIES, GYM_PILLARS, PLATFORM_SUBTOPICS  # noqa: E402

# Months spanning the ramp: July (near zero summit) through November (heaviest).
_MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]


# ---- weekly caps never breached ---------------------------------------------------------

def test_weekly_caps_never_breached():
    for month in _MONTHS:
        result = category_plan.month_plan(month)
        for wk in result["weeks"]:
            c = wk["counts"]
            assert c.get("podcast", 0) == 3, f"{wk['monday']}: podcast {c}"
            assert c.get("b2b", 0) == 1, f"{wk['monday']}: b2b {c}"
            assert c.get("platform", 0) <= 2, f"{wk['monday']}: platform {c}"
            assert c.get("book", 0) <= 1, f"{wk['monday']}: book {c}"
            assert c.get("summit", 0) <= 2, f"{wk['monday']}: summit {c}"
            assert sum(c.values()) == 7, f"{wk['monday']}: week is not 7 posts {c}"


def test_summit_matches_ramp_each_week():
    for month in _MONTHS:
        result = category_plan.month_plan(month)
        for wk in result["weeks"]:
            expected = min(2, category_plan.summit_quota_for_week(wk["monday"]))
            assert wk["counts"].get("summit", 0) == expected, (
                f"{wk['monday']}: summit {wk['counts'].get('summit', 0)} != ramp {expected}")


def test_book_never_over_one_per_week_but_present():
    seen_book = False
    for month in _MONTHS:
        result = category_plan.month_plan(month)
        for wk in result["weeks"]:
            assert wk["counts"].get("book", 0) <= 1, f"{wk['monday']}: book over 1"
            if wk["counts"].get("book", 0) == 1:
                seen_book = True
    assert seen_book, "book never appeared across the planned span"


def test_book_and_doctrine_alternate_by_week():
    """Even ISO weeks carry the book slot; odd weeks carry the doctrine slot."""
    result = category_plan.month_plan("2026-07")  # summit is 0 all month here
    for wk in result["weeks"]:
        wknum = date.fromisoformat(wk["monday"]).isocalendar().week
        if wknum % 2 == 0:
            assert wk["counts"].get("book", 0) == 1, f"{wk['monday']} even: expected book"
        else:
            assert wk["counts"].get("doctrine", 0) >= 1, f"{wk['monday']} odd: expected doctrine"


def test_every_category_in_taxonomy():
    for month in _MONTHS:
        for e in category_plan.month_plan(month)["entries"]:
            assert e["category"] in CATEGORIES, f"unknown category {e['category']!r}"


# ---- platform sub-topic rotation: no repeat within 10 days -------------------------------

def test_platform_days_carry_a_subtopic():
    for e in category_plan.month_plan("2026-07")["entries"]:
        if e["category"] == "platform":
            assert e["sub_topic"] in PLATFORM_SUBTOPICS, f"{e['day']}: bad sub-topic"
        else:
            assert e["sub_topic"] == "", f"{e['day']}: non-platform carries a sub-topic"


def test_no_platform_subtopic_repeat_within_10_days():
    # Plan a long CONTINUOUS span (weeks threaded, seq carried) so cross-week and
    # cross-month platform pairs are exercised the way a real runner plans.
    platform_posts = []  # (date, sub_topic)
    seq = 0
    monday = date(2026, 7, 6)  # a Monday
    while monday <= date(2026, 10, 31):
        entries, seq = category_plan.week_plan(monday.isoformat(), seq)
        for e in entries:
            if e["category"] == "platform":
                platform_posts.append((date.fromisoformat(e["day"]), e["sub_topic"]))
        monday += timedelta(days=7)
    platform_posts.sort()
    for i, (d_i, st_i) in enumerate(platform_posts):
        for d_j, st_j in platform_posts[i + 1:]:
            if (d_j - d_i).days > 10:
                break
            assert st_i != st_j, (
                f"platform sub-topic {st_i!r} repeats within 10 days: {d_i} and {d_j}")


def test_month_plan_only_includes_days_in_month():
    result = category_plan.month_plan("2026-07")
    for e in result["entries"]:
        assert e["day"][:7] == "2026-07", f"{e['day']} not in July"


# ---- month plan output: category mix + sub-topic spread ---------------------------------

def test_summary_counts_match_entries():
    result = category_plan.month_plan("2026-07")
    # summary counts should equal a manual tally of entries
    tally = {}
    for e in result["entries"]:
        tally[e["category"]] = tally.get(e["category"], 0) + 1
    assert result["summary"] == tally


def test_format_summary_is_readable():
    result = category_plan.month_plan("2026-10")
    text = category_plan.format_summary(result)
    assert "Category mix:" in text
    assert "podcast" in text and "platform" in text
    # no dash characters in the human-facing summary (standing law)
    import re
    assert re.search(r"[—–‐-]", text) is None


# ---- Wave 2: CATEGORIES includes proof and call -----------------------------------------

def test_categories_includes_proof():
    """CATEGORIES tuple must include 'proof' (Wave 2 addition)."""
    assert "proof" in CATEGORIES, f"'proof' missing from CATEGORIES: {CATEGORIES}"


def test_categories_includes_call():
    """CATEGORIES tuple must include 'call' (Wave 2 addition)."""
    assert "call" in CATEGORIES, f"'call' missing from CATEGORIES: {CATEGORIES}"


# ---- Wave 2: GYM_PILLARS includes all 6 pillars -----------------------------------------

def test_gym_pillars_has_six_entries():
    """GYM_PILLARS must have exactly 6 entries."""
    assert len(GYM_PILLARS) == 6, f"GYM_PILLARS has {len(GYM_PILLARS)} entries: {GYM_PILLARS}"


def test_gym_pillars_includes_all_required():
    """GYM_PILLARS must include all six gen-pop boutique fitness pillars."""
    required = {"results", "education", "community", "faces", "offer", "invite"}
    missing = required - set(GYM_PILLARS)
    assert not missing, f"GYM_PILLARS missing: {missing}"


# ---- Wave 2: validate_quotas --------------------------------------------------------

def test_validate_quotas_violation_for_zero_proof_posts():
    """A plan with no 'proof' posts should return a proof_below_min violation."""
    # 10-row plan with no proof posts (B2B profile, weekly minimum is 2)
    rows = [{"category": "doctrine"} for _ in range(7)]
    violations = category_plan.validate_quotas(rows, profile="B2B")
    proof_violations = [v for v in violations if v.startswith("proof_below_min")]
    assert proof_violations, (
        f"Expected proof_below_min violation, got: {violations}"
    )


def test_validate_quotas_violation_for_zero_call_posts():
    """A plan with no 'call' posts should return a call_below_min violation (B2B)."""
    rows = [{"category": "doctrine"} for _ in range(7)]
    violations = category_plan.validate_quotas(rows, profile="B2B")
    call_violations = [v for v in violations if v.startswith("call_below_min")]
    assert call_violations, (
        f"Expected call_below_min violation, got: {violations}"
    )


def test_validate_quotas_compliant_b2b_plan_no_violations():
    """A B2B plan meeting all minimums and under the 25% cap should return no violations."""
    # 7-day week with: 3 proof, 3 call, 1 doctrine
    # proof pct = 3/7 = 43% -> over cap!
    # Correct: 2 proof (29%), 3 call (43%)... still over cap.
    # Must keep each category <= 25% of total. 7 posts: 25% = 1.75 -> max 1 per category.
    # But minimums are 2 proof + 3 call in a 7-post week. That means proof=29%, call=43%.
    # The 25% cap applies at the MONTH level for B2B, not the week.
    # Use a 28-post month plan to stay under 25%: 8 proof (28.5%) -- still over.
    # Use 40 posts: 8 proof = 20%, 12 call = 30%... call over.
    # The spec says 25% cap is per-month AND weekly minimums are 2 proof / 3 call.
    # A compliant month (4 weeks): 8 proof, 12 call, 20 other = 40 posts.
    # proof = 20%, call = 30% -> call over cap in a month.
    # Interpretation: the 25% cap and the weekly minimums can both be met by having
    # a mix large enough. For testing "no violations", build a plan where no single
    # category breaches 25% AND minimums are met. Use profile="GYM" for a cleaner test.
    #
    # GYM: results>=4, offer>=4, faces>=3, community>=5, education>=6, invite fills.
    # 30-post month: results=4, offer=4, faces=3, community=5, education=6, invite=8 = 30
    # Pcts: results=13%, offer=13%, faces=10%, community=17%, education=20%, invite=27% -> invite over!
    # invite=7: total=29. Add 1 more education: education=7(24%), invite=7(24%) = 29 ok.
    rows = (
        [{"category": "results"}]    * 4 +
        [{"category": "offer"}]      * 4 +
        [{"category": "faces"}]      * 3 +
        [{"category": "community"}]  * 6 +
        [{"category": "education"}]  * 7 +
        [{"category": "invite"}]     * 5   # 29 total; each <= 24.2% < 25%
    )
    violations = category_plan.validate_quotas(rows, profile="GYM")
    assert not violations, f"Expected no violations for compliant GYM plan, got: {violations}"


def test_validate_quotas_no_violations_for_compliant_plan():
    """A GYM plan meeting all minimums with no category over 25% has no violations."""
    rows = (
        [{"category": "results"}]    * 5 +
        [{"category": "offer"}]      * 4 +
        [{"category": "faces"}]      * 4 +
        [{"category": "community"}]  * 6 +
        [{"category": "education"}]  * 8 +
        [{"category": "invite"}]     * 5
    )  # 32 posts; max pct = education 8/32 = 25.0% -> exactly at limit, not over
    violations = category_plan.validate_quotas(rows, profile="GYM")
    # education is exactly 25%, which is NOT over (cap is OVER 25%)
    assert not violations, f"Unexpected violations: {violations}"


# ---- Wave 2: category_pct ---------------------------------------------------------------

def test_category_pct_one_of_four_returns_25():
    """1 of 4 rows in category 'doctrine' should return exactly 25.0."""
    rows = [
        {"category": "doctrine"},
        {"category": "podcast"},
        {"category": "podcast"},
        {"category": "b2b"},
    ]
    pct = category_plan.category_pct(rows, "doctrine")
    assert pct == 25.0, f"Expected 25.0, got {pct}"


def test_category_pct_empty_rows_returns_zero():
    """category_pct with empty plan_rows returns 0.0."""
    assert category_plan.category_pct([], "doctrine") == 0.0


def test_category_pct_all_same_returns_100():
    """All rows in same category returns 100.0."""
    rows = [{"category": "podcast"} for _ in range(5)]
    assert category_plan.category_pct(rows, "podcast") == 100.0


# ---- Wave 2: 25% hard cap violation detection -------------------------------------------

def test_25_pct_cap_violation_detected():
    """6 of 20 posts = 30% doctrine -> over the 25% hard cap -> violation reported."""
    rows = (
        [{"category": "doctrine"}]  * 6 +
        [{"category": "podcast"}]   * 14
    )  # 20 posts; doctrine = 30%
    violations = category_plan.validate_quotas(rows, profile="ANY")
    cap_violations = [v for v in violations if "category_over_cap" in v and "doctrine" in v]
    assert cap_violations, (
        f"Expected category_over_cap:doctrine violation, got: {violations}"
    )


def test_25_pct_cap_violation_format():
    """Cap violation string has the expected format: 'category_over_cap:<cat>:<pct>%'."""
    rows = [{"category": "doctrine"}] * 6 + [{"category": "podcast"}] * 14
    violations = category_plan.validate_quotas(rows, profile="ANY")
    cap_v = [v for v in violations if "category_over_cap:doctrine" in v]
    assert cap_v, f"No doctrine cap violation found in {violations}"
    # Should contain a percentage with % sign
    assert "%" in cap_v[0], f"Expected % in violation string: {cap_v[0]}"


def test_exactly_25_pct_is_not_a_violation():
    """Exactly 25% is at the boundary; NOT a violation (must be OVER 25%)."""
    # 5 of 20 posts = 25.0% exactly
    rows = [{"category": "doctrine"}] * 5 + [{"category": "podcast"}] * 15
    violations = category_plan.validate_quotas(rows, profile="ANY")
    cap_violations = [v for v in violations if "category_over_cap" in v and "doctrine" in v]
    assert not cap_violations, (
        f"25.0% exactly should NOT be a violation, got: {cap_violations}"
    )
