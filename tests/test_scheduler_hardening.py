"""
Scheduler hardening tests (loud no-card alerts + persisted run date). Fully OFFLINE:
recording posters, spy run functions, tmp state dir. Asserts: one ops alert on every
non-drafted branch (exception, disabled, no_voice, zero drafts on a posting day); no
alert on success or on an expected skip-day zero; the fire date round-trips through
the state file; and a fresh process that reads a persisted "today" does not re-fire.
Default flag behavior unchanged (alerts stay dormant while OFF).
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import listener, ops_alerts  # noqa: E402


class RecordingPoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _wire(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    return rec


def _alerts(rec):
    return [n for n in rec.notices if "scheduled draft run produced no cards" in n]


THURSDAY = "2026-07-02"   # posting day
SATURDAY = "2026-07-04"   # manually configured skip day


# ---- 1. alert fires on each non-drafted branch --------------------------------
def test_alert_on_exception(monkeypatch):
    rec = _wire(monkeypatch)

    def boom(store=None):
        raise RuntimeError("store exploded")

    out = listener._fire_daily(store=None, today=THURSDAY, run=boom)
    assert out is None
    assert len(_alerts(rec)) == 1
    assert "RuntimeError" in _alerts(rec)[0]


def test_alert_on_disabled_and_no_voice(monkeypatch):
    for status in ("disabled", "no_voice"):
        rec = _wire(monkeypatch)
        listener._fire_daily(store=None, today=THURSDAY,
                             run=lambda store=None: {"status": status, "drafts": []})
        assert len(_alerts(rec)) == 1, status
        assert status in _alerts(rec)[0]


def test_alert_on_zero_drafts_on_posting_day(monkeypatch):
    rec = _wire(monkeypatch)
    listener._fire_daily(store=None, today=THURSDAY,
                         run=lambda store=None: {"status": "drafted", "drafts": []})
    assert len(_alerts(rec)) == 1
    assert "0 drafts" in _alerts(rec)[0]


# ---- 2. no alert on success, and no alert on an expected skip-day zero --------
def test_no_alert_on_successful_drafted_run(monkeypatch):
    rec = _wire(monkeypatch)
    listener._fire_daily(store=None, today=THURSDAY,
                         run=lambda store=None: {"status": "drafted", "drafts": ["d1", "d2"]})
    assert _alerts(rec) == []


def test_no_alert_on_skip_day_zero_drafts(monkeypatch):
    from agent import config as _config
    rec = _wire(monkeypatch)
    monkeypatch.setattr(_config, "POSTING_SKIP_DAYS", ["sat"])
    listener._fire_daily(store=None, today=SATURDAY,
                         run=lambda store=None: {"status": "drafted", "drafts": []})
    assert _alerts(rec) == []


def test_alerts_stay_dormant_while_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_ALERTS_ENABLED", raising=False)
    rec = RecordingPoster()
    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: rec)
    listener._fire_daily(store=None, today=THURSDAY,
                         run=lambda store=None: {"status": "disabled", "drafts": []})
    assert rec.notices == []   # default behavior unchanged: silent while OFF


# ---- 3. persisted run date ----------------------------------------------------
def test_last_run_date_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    assert listener._read_last_run_date() is None      # empty state
    listener._write_last_run_date(THURSDAY)
    assert listener._read_last_run_date() == THURSDAY  # survives a fresh read


def test_unavailable_state_dir_falls_back_in_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path / "missing" / "deeper"))
    assert listener._read_last_run_date() is None      # read: quiet None
    listener._write_last_run_date(THURSDAY)            # write: never raises


# ---- 4. fresh process with persisted "today" does not re-fire ------------------
class _FakeDatetime:
    """now() pinned inside the fire window (hour 14 UTC) on the given day."""
    def __init__(self, day):
        self._day = day

    def now(self, tz=None):
        y, m, d = (int(x) for x in self._day.split("-"))
        return datetime(y, m, d, 14, 30, tzinfo=timezone.utc)


class _StopLoop(Exception):
    pass


def _run_one_loop_pass(monkeypatch, tmp_path, day):
    """Run _daily_scheduler through exactly one pass (sleep breaks the loop) with a
    pinned clock inside the fire window; return the list of _fire_daily calls."""
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(listener, "datetime", _FakeDatetime(day))
    fired = []
    monkeypatch.setattr(listener, "_fire_daily", lambda store, today: fired.append(today))

    def stop(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(listener.time, "sleep", stop)
    try:
        listener._daily_scheduler(store=None)
    except _StopLoop:
        pass
    return fired


def test_fresh_process_with_persisted_today_does_not_fire(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    listener._write_last_run_date(THURSDAY)            # a prior process fired today
    fired = _run_one_loop_pass(monkeypatch, tmp_path, THURSDAY)
    assert fired == []                                  # redeploy in-window: no double fire


def test_fresh_process_with_stale_date_fires_and_persists(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_DIR", str(tmp_path))
    listener._write_last_run_date("2026-07-01")        # yesterday
    fired = _run_one_loop_pass(monkeypatch, tmp_path, THURSDAY)
    assert fired == [THURSDAY]                          # fires once for today
    assert listener._read_last_run_date() == THURSDAY   # and persists the new date
