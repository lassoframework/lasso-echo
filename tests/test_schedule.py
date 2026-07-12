"""
Posting-schedule tests (2026 cadence). Pure timing: 7 days a week (no skip days by
default), Tue-Thu are priority, the primary slot is 18:30 New York (with correct
EDT/EST offsets), the morning slot is 07:30, and a config/env override retunes the
time or re-adds skip days. No publishing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, schedule  # noqa: E402

# Reference week of 2026: Mon 06-29, Tue 06-30, Wed 07-01, Thu 07-02, Fri 07-03,
# Sat 07-04, Sun 07-05.


def test_weekday_abbr():
    assert schedule.weekday_abbr("2026-07-01") == "wed"
    assert schedule.weekday_abbr("2026-07-04") == "sat"
    assert schedule.weekday_abbr("2026-07-01T18:30:00-04:00") == "wed"  # ISO prefix ok


def test_all_seven_days_post_by_default():
    """Default cadence: 7 days a week, no skip days. Saturday is a posting day."""
    assert schedule.should_post_on("2026-07-04") is True    # Saturday — posts now
    assert schedule.should_post_on("2026-07-01") is True    # Wednesday
    assert schedule.should_post_on("2026-07-05") is True    # Sunday
    assert schedule.should_post_on("2026-07-03") is True    # Friday


def test_skip_days_env_override(monkeypatch):
    """AGENT_POSTING_SKIP_DAYS=sat re-enables the old Saturday-skip behavior."""
    monkeypatch.setattr(config, "POSTING_SKIP_DAYS", ["sat"])
    assert schedule.should_post_on("2026-07-04") is False   # Saturday now skipped
    assert schedule.should_post_on("2026-07-01") is True    # Wednesday unaffected


def test_priority_days_tue_wed_thu():
    for d, expect in [("2026-06-30", True),   # Tue
                      ("2026-07-01", True),    # Wed
                      ("2026-07-02", True),    # Thu
                      ("2026-06-29", False),   # Mon
                      ("2026-07-03", False),   # Fri
                      ("2026-07-04", False)]:  # Sat
        assert schedule.is_priority_day(d) is expect, d


def test_primary_slot_1830_with_edt_and_est_offsets():
    assert schedule.slot_time("primary") == "18:30"
    assert schedule.scheduled_for("2026-07-01") == "2026-07-01T18:30:00-04:00"        # EDT
    assert schedule.scheduled_for("2026-01-15") == "2026-01-15T18:30:00-05:00"        # EST
    assert schedule.scheduled_for("2026-07-01", slot="primary") == "2026-07-01T18:30:00-04:00"


def test_morning_slot_0730():
    assert schedule.slot_time("morning") == "07:30"
    assert schedule.scheduled_for("2026-07-01", slot="morning") == "2026-07-01T07:30:00-04:00"


def test_env_override_retunes_time(monkeypatch):
    # AGENT_POSTING_PRIMARY_TIME override (read live from config at call time).
    monkeypatch.setattr(config, "POSTING_PRIMARY_TIME", "12:00")
    assert schedule.slot_time("primary") == "12:00"
    assert schedule.scheduled_for("2026-07-01") == "2026-07-01T12:00:00-04:00"
