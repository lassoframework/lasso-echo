"""
Summit ramp (category rotation Part 6).

Summit weekly quota is date-based: near zero in July, rising through September
and October, heaviest (2) the two weeks before Nov 7, then zero once the summit
is past. The three-date lock the spec calls for is here, plus the tier and
auto-stop boundaries.
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import category_plan  # noqa: E402


# ---- the three-date lock ----------------------------------------------------------------

def test_ramp_july_near_zero():
    assert category_plan.summit_quota_for_week("2026-07-06") == 0


def test_ramp_mid_october_rising():
    assert category_plan.summit_quota_for_week("2026-10-12") == 1


def test_ramp_early_november_heaviest():
    assert category_plan.summit_quota_for_week("2026-11-02") == 2


# ---- the shape: monotonic-ish ramp up, then hard stop -----------------------------------

def test_ramp_is_zero_all_summer():
    monday = date(2026, 7, 6)
    while monday < date(2026, 8, 31):
        assert category_plan.summit_quota_for_week(monday.isoformat()) == 0, monday
        monday += timedelta(days=7)


def test_ramp_heaviest_is_exactly_two_weeks_before():
    # The two weeks whose Monday is within 14 days of Nov 7 carry 2; the week
    # before that (Oct 19) is still 1.
    assert category_plan.summit_quota_for_week("2026-10-19") == 1
    assert category_plan.summit_quota_for_week("2026-10-26") == 2
    assert category_plan.summit_quota_for_week("2026-11-02") == 2


def test_ramp_auto_stops_after_the_summit():
    # Nov 7 is the summit; the week after carries nothing, forever.
    assert category_plan.summit_quota_for_week("2026-11-09") == 0
    assert category_plan.summit_quota_for_week("2026-12-07") == 0
    assert category_plan.summit_quota_for_week("2027-01-04") == 0


def test_ramp_never_exceeds_two():
    monday = date(2026, 1, 5)
    while monday < date(2027, 1, 1):
        q = category_plan.summit_quota_for_week(monday.isoformat())
        assert 0 <= q <= 2, f"{monday}: quota {q} out of range"
        monday += timedelta(days=7)


def test_ramp_accepts_any_weekday_in_the_week():
    # Passing a Thursday resolves to the same week's Monday quota.
    assert (category_plan.summit_quota_for_week("2026-11-05")
            == category_plan.summit_quota_for_week("2026-11-02"))
