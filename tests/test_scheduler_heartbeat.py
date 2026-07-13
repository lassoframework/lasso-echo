"""
Scheduler process heartbeat + late-draw watchdog (launch hardening Part 1).
The loop's heartbeat kv row updates each cycle and carries the next fire time;
status can read it; a draw >30 minutes past the target hour with no run recorded
fires ONE deduped ops alert; on-time / already-fired days stay silent.
Fully OFFLINE (tmp sqlite).
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import listener, ops_alerts  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    yield


def _wire_alerts(monkeypatch):
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **k: fired.append(m))
    return fired


def _utc(h, m=0, day=14):
    return datetime(2026, 7, day, h, m, tzinfo=timezone.utc)


# ---- heartbeat ------------------------------------------------------------------
def test_heartbeat_updates_each_cycle():
    listener.write_scheduler_heartbeat(_utc(9, 0), 14, "2026-07-13")
    first = listener.read_scheduler_heartbeat()
    assert first["ts"] == _utc(9, 0).isoformat()
    listener.write_scheduler_heartbeat(_utc(9, 1), 14, "2026-07-13")
    second = listener.read_scheduler_heartbeat()
    assert second["ts"] == _utc(9, 1).isoformat()   # refreshed, not stale


def test_heartbeat_next_fire_today_before_window():
    listener.write_scheduler_heartbeat(_utc(9), 14, "2026-07-13")
    hb = listener.read_scheduler_heartbeat()
    assert hb["next_fire"] == _utc(14).isoformat()  # today 14:00 UTC


def test_heartbeat_next_fire_tomorrow_after_fired():
    listener.write_scheduler_heartbeat(_utc(15), 14, "2026-07-14")  # already fired
    hb = listener.read_scheduler_heartbeat()
    assert hb["next_fire"] == _utc(14, day=15).isoformat()  # tomorrow


def test_heartbeat_none_when_never_written():
    assert listener.read_scheduler_heartbeat() is None


# ---- late-draw watchdog -----------------------------------------------------------
def test_late_draw_alert_fires_in_simulation(monkeypatch):
    """14:31 UTC, target 14:00, no run recorded today: the draw is 31 minutes
    late and one ops alert fires."""
    fired = _wire_alerts(monkeypatch)
    out = listener.check_late_draw(_utc(14, 31), "2026-07-13", 14)
    assert out is True
    assert len(fired) == 1
    assert "late" in fired[0] and "run-daily" in fired[0]


def test_late_draw_alert_deduped_per_day(monkeypatch):
    fired = _wire_alerts(monkeypatch)
    assert listener.check_late_draw(_utc(14, 31), "2026-07-13", 14) is True
    assert listener.check_late_draw(_utc(14, 45), "2026-07-13", 14) is False
    assert listener.check_late_draw(_utc(16, 0), "2026-07-13", 14) is False
    assert len(fired) == 1                              # one alert for the day


def test_no_alert_inside_grace_window(monkeypatch):
    fired = _wire_alerts(monkeypatch)
    assert listener.check_late_draw(_utc(14, 29), "2026-07-13", 14) is False
    assert fired == []


def test_no_alert_before_the_window(monkeypatch):
    fired = _wire_alerts(monkeypatch)
    assert listener.check_late_draw(_utc(9, 0), "2026-07-13", 14) is False
    assert fired == []


def test_no_alert_when_already_fired_today(monkeypatch):
    fired = _wire_alerts(monkeypatch)
    assert listener.check_late_draw(_utc(16, 0), "2026-07-14", 14) is False
    assert fired == []


# ---- status surfaces the heartbeat ------------------------------------------------
def test_status_shows_heartbeat(capsys):
    import agent.__main__ as mm
    listener.write_scheduler_heartbeat(_utc(9), 14, "2026-07-13")
    mm.main(["status"])
    out = capsys.readouterr().out
    assert "-- scheduler --" in out
    assert _utc(9).isoformat() in out                    # the heartbeat ts
    assert _utc(14).isoformat() in out                   # the next fire time
