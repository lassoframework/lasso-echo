"""
DENY STREAK ALARM: three consecutive coach denials on one account is a content
alarm, escalated to SOCIAL. Tough Temple denied for a week and nobody read it
-- this module is the detection that was missing, not a content fix.

Fully offline. AGENT_DB_PATH is per-test (conftest), so kv dedup is isolated.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db  # noqa: E402
from agent.jobs.deny_streak_alarm import deny_streak_violations, run  # noqa: E402


def _row(gym_id="toughtemple", account="toughtemple_ig", post_date="2026-09-01",
         status="denied", row_id=None):
    return {"gym_id": gym_id, "account": account, "post_date": post_date,
            "status": status, "id": row_id or f"{gym_id}-{post_date}"}


# ---------------------------------------------------------------------------
# deny_streak_violations (pure)
# ---------------------------------------------------------------------------

def test_three_consecutive_denials_trip_the_alarm():
    rows = [_row(post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
    found = deny_streak_violations(rows, threshold=3)
    assert len(found) == 1
    v = found[0]
    assert v.gym_id == "toughtemple"
    assert v.streak_len == 3
    assert v.first_date == "2026-09-01"
    assert v.last_date == "2026-09-03"


def test_two_denials_do_not_trip_the_alarm():
    rows = [_row(post_date=f"2026-09-0{i}") for i in (1, 2)]
    assert deny_streak_violations(rows, threshold=3) == []


def test_an_approval_between_denials_breaks_the_streak():
    rows = [
        _row(post_date="2026-09-01", status="denied"),
        _row(post_date="2026-09-02", status="approved"),
        _row(post_date="2026-09-03", status="denied"),
        _row(post_date="2026-09-04", status="denied"),
    ]
    # Only 2 denials after the approval -- not a trailing streak of 3.
    assert deny_streak_violations(rows, threshold=3) == []


def test_only_the_trailing_streak_counts():
    # An OLDER run of 3 denials, then the coach approved, then one new denial.
    # The coach was heard and answered -- this is not still-unread signal.
    rows = [
        _row(post_date="2026-09-01", status="denied"),
        _row(post_date="2026-09-02", status="denied"),
        _row(post_date="2026-09-03", status="denied"),
        _row(post_date="2026-09-04", status="approved"),
        _row(post_date="2026-09-05", status="denied"),
    ]
    assert deny_streak_violations(rows, threshold=3) == []


def test_published_also_breaks_the_streak():
    rows = [
        _row(post_date="2026-09-01", status="denied"),
        _row(post_date="2026-09-02", status="published"),
        _row(post_date="2026-09-03", status="denied"),
        _row(post_date="2026-09-04", status="denied"),
    ]
    assert deny_streak_violations(rows, threshold=3) == []


def test_a_pending_row_neither_extends_nor_breaks_the_streak():
    rows = [
        _row(post_date="2026-09-01", status="denied"),
        _row(post_date="2026-09-02", status="pending"),
        _row(post_date="2026-09-03", status="denied"),
        _row(post_date="2026-09-04", status="denied"),
    ]
    found = deny_streak_violations(rows, threshold=3)
    assert len(found) == 1
    # Only the two DENIED rows plus... wait: pending is skipped, so the
    # trailing DENIED-only count is 3 (01, 03, 04), the pending row does not
    # count toward streak_len.
    assert found[0].streak_len == 3


def test_streaks_are_scoped_per_gym_and_account():
    rows = (
        [_row(gym_id="toughtemple", account="toughtemple_ig", post_date=f"2026-09-0{i}")
         for i in (1, 2, 3)]
        + [_row(gym_id="toughtemple", account="toughtemple_fb", post_date="2026-09-01",
                status="denied")]
    )
    found = deny_streak_violations(rows, threshold=3)
    # FB has only 1 denial -- must not be pooled with IG's 3.
    assert len(found) == 1
    assert found[0].account == "toughtemple_ig"


def test_different_gyms_never_pool_together():
    rows = (
        [_row(gym_id="toughtemple", post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
        + [_row(gym_id="chateau", account="chateau_ig", post_date="2026-09-01")]
    )
    found = deny_streak_violations(rows, threshold=3)
    assert len(found) == 1
    assert found[0].gym_id == "toughtemple"


def test_default_threshold_is_three():
    rows = [_row(post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
    assert len(deny_streak_violations(rows)) == 1


def test_ignores_rows_missing_post_date_or_status():
    rows = [
        _row(post_date="", status="denied"),
        _row(post_date="2026-09-01", status=""),
        "not-a-dict",
        None,
    ]
    assert deny_streak_violations(rows, threshold=3) == []


def test_message_names_the_gym_and_says_escalate_to_social():
    rows = [_row(post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
    v = deny_streak_violations(rows, threshold=3)[0]
    assert "toughtemple" in v.message()
    assert "SOCIAL" in v.message()


# ---------------------------------------------------------------------------
# run() -- the I/O wrapper: dedup, re-fire on extension, escape hatch
# ---------------------------------------------------------------------------

def test_run_alarms_and_dedupes_same_streak(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    rows = [_row(post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
    r1 = run(rows=rows, enabled=True, alert=alerts.append)
    assert len(r1["alarmed"]) == 1
    assert len(alerts) == 1
    r2 = run(rows=rows, enabled=True, alert=alerts.append)
    assert r2["alarmed"] == []
    assert len(alerts) == 1  # not re-alerted for the SAME streak


def test_run_re_fires_when_streak_extends(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    rows3 = [_row(post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
    run(rows=rows3, enabled=True, alert=alerts.append)
    rows4 = rows3 + [_row(post_date="2026-09-04")]
    r2 = run(rows=rows4, enabled=True, alert=alerts.append)
    assert len(r2["alarmed"]) == 1  # new last_date -> re-fires per doctrine
    assert len(alerts) == 2


def test_run_escape_hatch_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    alerts = []
    rows = [_row(post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
    result = run(rows=rows, enabled=False, alert=alerts.append)
    assert result == {"ok": True, "alarmed": [], "reason": "disabled"}
    assert alerts == []


def test_run_uses_fetch_rows_lazily_only_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    calls = []

    def fetch():
        calls.append(1)
        return [_row(post_date=f"2026-09-0{i}") for i in (1, 2, 3)]

    run(fetch_rows=fetch, enabled=False, alert=lambda m: None)
    assert calls == []  # disabled: never touches the store

    run(fetch_rows=fetch, enabled=True, alert=lambda m: None)
    assert calls == [1]


def test_run_no_rows_source_reports_reason():
    result = run(enabled=True, alert=lambda m: None)
    assert result == {"ok": False, "alarmed": [], "reason": "no_rows_source"}


def test_run_one_gym_alert_failure_never_sinks_the_rest(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    rows = (
        [_row(gym_id="toughtemple", account="toughtemple_ig",
              post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
        + [_row(gym_id="chateau", account="chateau_ig",
                post_date=f"2026-09-0{i}") for i in (1, 2, 3)]
    )

    def flaky_alert(msg):
        if "toughtemple" in msg:
            raise RuntimeError("slack down")

    result = run(rows=rows, enabled=True, alert=flaky_alert)
    assert result["alarmed"] == [
        {"gym_id": "chateau", "account": "chateau_ig", "streak_len": 3,
         "last_date": "2026-09-03"}
    ]


# ---------------------------------------------------------------------------
# config flags: default ON / 3, escape hatches work
# ---------------------------------------------------------------------------

def test_config_alarm_default_on(monkeypatch):
    monkeypatch.delenv("AGENT_DENY_STREAK_ALARM", raising=False)
    assert config.deny_streak_alarm_enabled() is True


def test_config_alarm_escape_hatch(monkeypatch):
    monkeypatch.setenv("AGENT_DENY_STREAK_ALARM", "false")
    assert config.deny_streak_alarm_enabled() is False


def test_config_threshold_default(monkeypatch):
    monkeypatch.delenv("DENY_STREAK_THRESHOLD", raising=False)
    assert config.deny_streak_threshold() == 3


def test_config_threshold_override(monkeypatch):
    monkeypatch.setenv("DENY_STREAK_THRESHOLD", "5")
    assert config.deny_streak_threshold() == 5


def test_config_threshold_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("DENY_STREAK_THRESHOLD", "nope")
    assert config.deny_streak_threshold() == 3


# ---------------------------------------------------------------------------
# VERIFY BY DELETION: prove the breaker set is load-bearing
# ---------------------------------------------------------------------------

def test_deletion_proof_breaker_check_is_load_bearing():
    # If "approved" were removed from the breaker set, this would wrongly fire
    # -- the coach approved right before the newest denial pair, so this is a
    # fresh disagreement of 2, not an unread streak of 3.
    rows = [
        _row(post_date="2026-09-01", status="denied"),
        _row(post_date="2026-09-02", status="denied"),
        _row(post_date="2026-09-03", status="approved"),
        _row(post_date="2026-09-04", status="denied"),
        _row(post_date="2026-09-05", status="denied"),
    ]
    assert deny_streak_violations(rows, threshold=3) == []
