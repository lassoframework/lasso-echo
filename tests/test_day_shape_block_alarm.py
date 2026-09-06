"""
DAY SHAPE BLOCK ALARM: a gym blocked by day_shape.py must show its remaining
runway and escalate to SOCIAL once it has been blocked 3+ consecutive days.

Fully offline. AGENT_DB_PATH is per-test (conftest), so kv is isolated.
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.jobs.day_shape_block_alarm import (  # noqa: E402
    consecutive_blocked_days,
    record_and_maybe_escalate,
    record_block,
    runway_days,
)

DAY1 = date(2026, 9, 4)
DAY2 = date(2026, 9, 5)
DAY3 = date(2026, 9, 6)


class _Violation:
    def __init__(self, msg):
        self._msg = msg

    def message(self):
        return self._msg


def _row(post_date, status="pending"):
    return {"post_date": post_date, "status": status}


# ---------------------------------------------------------------------------
# consecutive_blocked_days / record_block (pure, injected kv)
# ---------------------------------------------------------------------------

def test_no_blocks_recorded_is_zero_streak():
    assert consecutive_blocked_days("gym-1", asof=DAY3, kv_get=lambda k, d: "") == 0


def test_single_day_block_is_streak_one(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    record_block("gym-1", today=DAY3)
    assert consecutive_blocked_days("gym-1", asof=DAY3) == 1


def test_three_consecutive_blocks_is_streak_three(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    record_block("gym-1", today=DAY1)
    record_block("gym-1", today=DAY2)
    record_block("gym-1", today=DAY3)
    assert consecutive_blocked_days("gym-1", asof=DAY3) == 3


def test_a_gap_day_resets_the_streak(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    # Blocked DAY1, clean DAY2 (no stamp), blocked DAY3 -- streak as of DAY3 is 1.
    record_block("gym-1", today=DAY1)
    record_block("gym-1", today=DAY3)
    assert consecutive_blocked_days("gym-1", asof=DAY3) == 1


def test_streaks_are_scoped_per_gym(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    record_block("gym-1", today=DAY3)
    assert consecutive_blocked_days("gym-2", asof=DAY3) == 0


def test_recording_the_same_day_twice_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    record_block("gym-1", today=DAY3)
    record_block("gym-1", today=DAY3)
    assert consecutive_blocked_days("gym-1", asof=DAY3) == 1


# ---------------------------------------------------------------------------
# runway_days (pure)
# ---------------------------------------------------------------------------

def test_runway_counts_days_to_last_live_row():
    rows = [_row("2026-09-06"), _row("2026-09-10", status="approved")]
    assert runway_days(rows, today=date(2026, 9, 6)) == 4


def test_runway_ignores_denied_and_killed_rows():
    rows = [_row("2026-09-20", status="denied"), _row("2026-09-08", status="approved")]
    assert runway_days(rows, today=date(2026, 9, 6)) == 2


def test_runway_zero_when_nothing_live():
    rows = [_row("2026-09-06", status="denied")]
    assert runway_days(rows, today=date(2026, 9, 6)) == 0


def test_runway_zero_when_last_live_row_is_today():
    rows = [_row("2026-09-06", status="pending")]
    assert runway_days(rows, today=date(2026, 9, 6)) == 0


def test_runway_ignores_malformed_rows():
    rows = ["not-a-dict", None, {}, _row("2026-09-10", status="published")]
    assert runway_days(rows, today=date(2026, 9, 6)) == 4


# ---------------------------------------------------------------------------
# record_and_maybe_escalate -- the I/O wrapper: escalates once per streak
# ---------------------------------------------------------------------------

def test_no_escalation_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    violations = [_Violation("gym-1 2026-09-06 instagram: 2 posts share the same caption")]
    result = record_and_maybe_escalate(
        "gym-1", violations, [], today=DAY1, enabled=True, alert=alerts.append)
    assert result["streak"] == 1
    assert result["escalated"] is False
    assert alerts == []


def test_escalates_exactly_on_the_day_threshold_is_reached(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    violations = [_Violation("collision")]
    record_and_maybe_escalate("gym-1", violations, [], today=DAY1, enabled=True,
                              alert=alerts.append)
    record_and_maybe_escalate("gym-1", violations, [], today=DAY2, enabled=True,
                              alert=alerts.append)
    assert alerts == []  # streak 1, 2 -- below threshold 3
    result = record_and_maybe_escalate("gym-1", violations, [], today=DAY3,
                                       enabled=True, alert=alerts.append)
    assert result["streak"] == 3
    assert result["escalated"] is True
    assert len(alerts) == 1
    assert "gym-1" in alerts[0]
    assert "SOCIAL" in alerts[0]


def test_does_not_re_escalate_the_day_after_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    violations = [_Violation("collision")]
    for d in (DAY1, DAY2, DAY3):
        record_and_maybe_escalate("gym-1", violations, [], today=d, enabled=True,
                                  alert=alerts.append)
    assert len(alerts) == 1  # escalated once, at DAY3
    day4 = DAY3 + timedelta(days=1)
    record_and_maybe_escalate("gym-1", violations, [], today=day4, enabled=True,
                              alert=alerts.append)
    assert len(alerts) == 1  # still just once -- the re-fire is ops_alerts.alert's job


def test_re_escalates_after_a_clean_day_and_a_new_streak(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    violations = [_Violation("collision")]
    for d in (DAY1, DAY2, DAY3):
        record_and_maybe_escalate("gym-1", violations, [], today=d, enabled=True,
                                  alert=alerts.append)
    assert len(alerts) == 1
    # A clean day (no block recorded) breaks the streak.
    clean_day = DAY3 + timedelta(days=1)
    new_streak_days = [clean_day + timedelta(days=i) for i in range(1, 4)]
    for d in new_streak_days:
        record_and_maybe_escalate("gym-1", violations, [], today=d, enabled=True,
                                  alert=alerts.append)
    assert len(alerts) == 2  # a NEW streak reaching threshold escalates again


def test_escape_hatch_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    violations = [_Violation("collision")]
    result = record_and_maybe_escalate("gym-1", violations, [], today=DAY1,
                                       enabled=False, alert=alerts.append)
    assert result == {"streak": 0, "escalated": False}
    assert alerts == []
    assert consecutive_blocked_days("gym-1", asof=DAY1) == 0  # never even stamped


def test_alert_failure_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    violations = [_Violation("collision")]

    def flaky_alert(msg):
        raise RuntimeError("slack down")

    for d in (DAY1, DAY2, DAY3):
        result = record_and_maybe_escalate("gym-1", violations, [], today=d,
                                           enabled=True, alert=flaky_alert)
    assert result["streak"] == 3
    assert result["escalated"] is False  # alert raised, so it did not succeed


def test_message_includes_runway(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    violations = [_Violation("collision")]
    rows = [_row("2026-09-12", status="approved")]
    for d in (DAY1, DAY2, DAY3):
        record_and_maybe_escalate("gym-1", violations, rows, today=d, enabled=True,
                                  alert=alerts.append)
    assert len(alerts) == 1
    assert "6 day" in alerts[0]  # 2026-09-12 minus DAY3 (2026-09-06) = 6 days


# ---------------------------------------------------------------------------
# config flags: default ON / 3, escape hatches work
# ---------------------------------------------------------------------------

def test_config_alarm_default_on(monkeypatch):
    monkeypatch.delenv("AGENT_DAY_SHAPE_BLOCK_ALARM", raising=False)
    assert config.day_shape_block_alarm_enabled() is True


def test_config_alarm_escape_hatch(monkeypatch):
    monkeypatch.setenv("AGENT_DAY_SHAPE_BLOCK_ALARM", "false")
    assert config.day_shape_block_alarm_enabled() is False


def test_config_escalate_days_default(monkeypatch):
    monkeypatch.delenv("DAY_SHAPE_ESCALATE_DAYS", raising=False)
    assert config.day_shape_escalate_days() == 3


def test_config_escalate_days_override(monkeypatch):
    monkeypatch.setenv("DAY_SHAPE_ESCALATE_DAYS", "5")
    assert config.day_shape_escalate_days() == 5


def test_config_escalate_days_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("DAY_SHAPE_ESCALATE_DAYS", "nope")
    assert config.day_shape_escalate_days() == 3


# ---------------------------------------------------------------------------
# VERIFY BY DELETION: prove the gap-resets-streak rule is load-bearing
# ---------------------------------------------------------------------------

def test_deletion_proof_gap_reset_is_load_bearing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    # If the walk-backward loop did not stop at the first missing day, an old
    # streak from long ago would silently inflate today's count. Confirm a gap
    # actually stops the count instead of being skipped over.
    far_past = DAY3 - timedelta(days=20)
    record_block("gym-1", today=far_past)
    record_block("gym-1", today=DAY3)
    assert consecutive_blocked_days("gym-1", asof=DAY3) == 1
