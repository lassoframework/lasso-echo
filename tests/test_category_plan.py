"""
Weekly quotas + platform sub-topic rotation (category rotation Part 4).

Across planned months, proves: no weekly cap breached (podcast<=3, platform<=2,
b2b<=1, book<=1, summit per the ramp), book never over 1 per week yet present
across the month, doctrine fills the gaps, and no platform sub-topic repeats
within any 10-day window.
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import category_plan  # noqa: E402
from agent.content_categories import CATEGORIES, PLATFORM_SUBTOPICS  # noqa: E402

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
