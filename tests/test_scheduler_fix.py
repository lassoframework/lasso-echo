"""
Scheduler reliability fix tests (2026-07-16).

Tests the >= fire condition, run-daily CLI idempotency, and _next_fire accuracy
after a late restart. Fully offline: tmp state dir, pinned clocks, spy functions.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import listener  # noqa: E402


THURSDAY = "2026-07-02"
YESTERDAY = "2026-07-15"
TODAY = "2026-07-16"


class _StopLoop(Exception):
    pass


def _pin_clock(monkeypatch, year, month, day, hour, minute=0):
    class _Pinned:
        def now(self, tz=None):
            return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    monkeypatch.setattr(listener, "datetime", _Pinned())


def _run_one_pass(monkeypatch, tmp_path):
    fired = []
    monkeypatch.setattr(listener, "_fire_daily", lambda store, today: fired.append(today))
    monkeypatch.setattr(listener.time, "sleep", lambda _: (_ for _ in ()).throw(_StopLoop()))
    try:
        listener._daily_scheduler(store=None)
    except _StopLoop:
        pass
    return fired


# ---- 1. >= fire condition: late restart still fires today ---------------------

def test_fire_condition_catches_late_restart(monkeypatch, tmp_path):
    """A restart at 16:44 UTC (target=14) with yesterday's last_run_date must fire.
    The old == condition would silently skip; >= fires immediately."""
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DAILY_HOUR_UTC", "14")
    listener._write_last_run_date(YESTERDAY)
    _pin_clock(monkeypatch, 2026, 7, 16, 16, 44)  # 16:44 UTC, well past target=14
    fired = _run_one_pass(monkeypatch, tmp_path)
    assert fired == [TODAY], f"expected ['{TODAY}'], got {fired}"


def test_fire_condition_still_fires_on_the_hour(monkeypatch, tmp_path):
    """Normal case: restart exactly at target hour still fires."""
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DAILY_HOUR_UTC", "14")
    listener._write_last_run_date(YESTERDAY)
    _pin_clock(monkeypatch, 2026, 7, 16, 14, 0)  # exactly 14:00 UTC
    fired = _run_one_pass(monkeypatch, tmp_path)
    assert fired == [TODAY]


def test_fire_condition_no_fire_before_target_hour(monkeypatch, tmp_path):
    """Before the target hour, no draw fires."""
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DAILY_HOUR_UTC", "14")
    listener._write_last_run_date(YESTERDAY)
    _pin_clock(monkeypatch, 2026, 7, 16, 9, 0)  # 09:00 UTC, before target=14
    fired = _run_one_pass(monkeypatch, tmp_path)
    assert fired == []


def test_fire_condition_no_double_fire_same_day(monkeypatch, tmp_path):
    """A restart at 16:44 with today's last_run_date must NOT re-fire."""
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DAILY_HOUR_UTC", "14")
    listener._write_last_run_date(TODAY)
    _pin_clock(monkeypatch, 2026, 7, 16, 16, 44)
    fired = _run_one_pass(monkeypatch, tmp_path)
    assert fired == []


# ---- 2. _next_fire accuracy after late restart --------------------------------

def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_next_fire_late_restart_shows_today():
    """16:44 UTC restart, target=14, today not yet run -> next_fire = today 14:00."""
    now = _utc(2026, 7, 16, 16, 44)
    nf = listener._next_fire(now, 14, YESTERDAY)
    assert nf == _utc(2026, 7, 16, 14, 0)


def test_next_fire_before_window_shows_today():
    """09:00 UTC, today not yet run -> next_fire = today 14:00."""
    now = _utc(2026, 7, 16, 9, 0)
    nf = listener._next_fire(now, 14, YESTERDAY)
    assert nf == _utc(2026, 7, 16, 14, 0)


def test_next_fire_after_run_shows_tomorrow():
    """16:00 UTC, today already ran -> next_fire = tomorrow 14:00."""
    now = _utc(2026, 7, 16, 16, 0)
    nf = listener._next_fire(now, 14, TODAY)
    assert nf == _utc(2026, 7, 17, 14, 0)


# ---- 3. run-daily CLI idempotency --------------------------------------------

def test_run_daily_cli_noop_when_already_ran(monkeypatch, tmp_path, capsys):
    """run-daily exits clean when today's draw is already recorded.
    We write the actual today date so the check matches whatever date the test runs on."""
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    import datetime as _dt
    real_today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    listener._write_last_run_date(real_today)

    import agent.__main__ as mm
    ran = []
    monkeypatch.setattr(mm, "run_daily", lambda **kw: ran.append(1) or {"status": "drafted", "drafts": []})

    with pytest.raises(SystemExit) as exc:
        mm.main(["run-daily"])
    assert exc.value.code == 0
    assert ran == [], "run_daily must NOT be called when today already ran"
    out = capsys.readouterr().out
    assert "No-op" in out


def test_run_daily_cli_runs_and_writes_date(monkeypatch, tmp_path):
    """run-daily calls run_daily and persists today's date when today has not run."""
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    import datetime as _dt
    real_today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()

    import agent.__main__ as mm
    ran = []
    monkeypatch.setattr(mm, "run_daily", lambda **kw: ran.append(1) or {"status": "drafted", "drafts": []})

    mm.main(["run-daily"])
    assert ran == [1], "run_daily must be called when today has not run"
    assert listener._read_last_run_date() == real_today
