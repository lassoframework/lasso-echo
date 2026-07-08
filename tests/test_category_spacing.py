"""
No-repeat spacing (category rotation Part 5).

Two laws:
  1. The same bucket never falls on two consecutive days, EXCEPT the podcast
     touches (intentionally Mon/Thu/Sun and crossing Sun -> Mon).
  2. The same specific concept never repeats within 21 days.
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import category_plan  # noqa: E402

_MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]


# ---- law 1: no same bucket two days running (podcast exempt) -----------------------------

def test_no_consecutive_same_bucket_across_months():
    for month in _MONTHS:
        entries = category_plan.month_plan(month)["entries"]
        violations = category_plan.consecutive_bucket_violations(entries)
        assert violations == [], f"{month}: consecutive same-bucket days {violations}"


def test_no_consecutive_same_bucket_continuous_span():
    # Thread whole weeks continuously (catches cross-week Sat -> next-Mon pairs).
    entries = []
    monday = date(2026, 7, 6)
    seq = 0
    while monday <= date(2026, 11, 30):
        wk, seq = category_plan.week_plan(monday.isoformat(), seq)
        entries.extend(wk)
        monday += timedelta(days=7)
    violations = category_plan.consecutive_bucket_violations(entries)
    assert violations == [], f"consecutive same-bucket days in span: {violations}"


def test_heaviest_summit_week_summit_not_consecutive():
    # The two-summit weeks must not place summit on adjacent Fri/Sat.
    wk, _ = category_plan.week_plan("2026-11-02")
    summit_days = [date.fromisoformat(e["day"]) for e in wk if e["category"] == "summit"]
    assert len(summit_days) == 2
    gaps = [(b - a).days for a, b in zip(sorted(summit_days), sorted(summit_days)[1:])]
    assert all(g > 1 for g in gaps), f"summit posts are consecutive: {summit_days}"


def test_podcast_consecutive_is_allowed():
    # A Sun podcast followed by the next Mon podcast must NOT be flagged.
    entries = [
        {"day": "2026-07-12", "category": "podcast", "sub_topic": ""},  # Sun
        {"day": "2026-07-13", "category": "podcast", "sub_topic": ""},  # Mon
    ]
    assert category_plan.consecutive_bucket_violations(entries) == []


def test_consecutive_validator_flags_non_podcast():
    entries = [
        {"day": "2026-07-07", "category": "platform", "sub_topic": "ads"},
        {"day": "2026-07-08", "category": "platform", "sub_topic": "google"},
    ]
    violations = category_plan.consecutive_bucket_violations(entries)
    assert len(violations) == 1
    assert violations[0][2] == "platform"


# ---- law 2: same concept never repeats within 21 days -----------------------------------

def test_concept_spacing_flags_repeat_within_21_days():
    assignments = [
        {"day": "2026-07-01", "concept": "case_study_smith"},
        {"day": "2026-07-15", "concept": "case_study_smith"},  # 14 days: too soon
    ]
    violations = category_plan.concept_spacing_violations(assignments)
    assert len(violations) == 1
    assert violations[0][2] == "case_study_smith"


def test_concept_spacing_passes_at_21_days():
    assignments = [
        {"day": "2026-07-01", "concept": "case_study_smith"},
        {"day": "2026-07-22", "concept": "case_study_smith"},  # exactly 21 days: ok
    ]
    assert category_plan.concept_spacing_violations(assignments) == []


def test_concept_spacing_ignores_empty_concepts():
    assignments = [
        {"day": "2026-07-01", "concept": ""},
        {"day": "2026-07-02", "concept": ""},
    ]
    assert category_plan.concept_spacing_violations(assignments) == []


def test_concept_spacing_three_occurrences():
    # 0, +20 (flag), +21 from the second (ok vs second but the middle one flags)
    assignments = [
        {"day": "2026-07-01", "concept": "x"},
        {"day": "2026-07-21", "concept": "x"},  # 20 days from first: flag
        {"day": "2026-08-11", "concept": "x"},  # 21 days from second: ok
    ]
    violations = category_plan.concept_spacing_violations(assignments)
    assert len(violations) == 1
    assert violations[0][0] == "2026-07-01" and violations[0][1] == "2026-07-21"


# ---- the plan itself respects concept spacing for platform sub-topics --------------------

def test_platform_subtopics_respect_21_day_spacing():
    # Build a continuous plan, tag each platform day with its plan-level concept
    # key, and assert no concept repeats within 21 days.
    assignments = []
    monday = date(2026, 7, 6)
    seq = 0
    while monday <= date(2026, 10, 31):
        wk, seq = category_plan.week_plan(monday.isoformat(), seq)
        for e in wk:
            key = category_plan.entry_concept_key(e)
            if key:
                assignments.append({"day": e["day"], "concept": key})
        monday += timedelta(days=7)
    assert category_plan.concept_spacing_violations(assignments) == []
