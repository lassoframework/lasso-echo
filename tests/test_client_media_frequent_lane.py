"""
FIX 2: the listener's FREQUENT client-media lane. A client gym's upload is picked up
PROMPTLY (within minutes) and its DRAFT calendar auto-built, instead of waiting up to
24h for the once/day run_daily pass. Fully OFFLINE: the scan is injected.

Asserts on listener.run_client_media_lane + _client_media_scan_due:
  * flag ON, due  -> scan runs, returns the new monotonic marker
  * flag OFF       -> scan NOT called, marker unchanged (byte-for-byte dormant)
  * throttled      -> a second call inside the interval does NOT run again
  * try/except     -> a scan that raises never propagates (loop-safe), marker advances
  * nothing-new    -> a cheap no-op scan still counts as a run (idempotent by design)
  * startup lane line announces armed/dormant
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import listener  # noqa: E402


def test_scan_due_predicate_pure():
    # first tick: last=0.0 and a real (large) monotonic now -> always due.
    assert listener._client_media_scan_due(10_000.0, 0.0, 300) is True
    # after a run at t=10_000, throttled until the interval elapses.
    assert listener._client_media_scan_due(10_100.0, 10_000.0, 300) is False
    assert listener._client_media_scan_due(10_300.0, 10_000.0, 300) is True
    assert listener._client_media_scan_due(10_650.0, 10_350.0, 300) is True


def test_lane_runs_when_flag_on_and_due(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    calls = {"n": 0}
    new_last = listener.run_client_media_lane(
        now_mono=1000.0, last_mono=0.0, interval_secs=300,
        scan=lambda: calls.__setitem__("n", calls["n"] + 1))
    assert calls["n"] == 1
    assert new_last == 1000.0            # marker advanced to now


def test_lane_skipped_when_flag_off(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "false")
    calls = {"n": 0}
    new_last = listener.run_client_media_lane(
        now_mono=1000.0, last_mono=0.0, interval_secs=300,
        scan=lambda: calls.__setitem__("n", calls["n"] + 1))
    assert calls["n"] == 0               # never scanned
    assert new_last == 0.0               # marker unchanged (dormant)


def test_lane_throttled_within_interval(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    calls = {"n": 0}
    scan = lambda: calls.__setitem__("n", calls["n"] + 1)  # noqa: E731
    # first call (last=0.0, real monotonic now) runs
    last = listener.run_client_media_lane(now_mono=10_000.0, last_mono=0.0,
                                          interval_secs=300, scan=scan)
    assert calls["n"] == 1 and last == 10_000.0
    # a call 100s later is INSIDE the 300s throttle -> no run, marker unchanged
    last = listener.run_client_media_lane(now_mono=10_100.0, last_mono=last,
                                          interval_secs=300, scan=scan)
    assert calls["n"] == 1 and last == 10_000.0
    # a call 300s after the last run is due again
    last = listener.run_client_media_lane(now_mono=10_300.0, last_mono=last,
                                          interval_secs=300, scan=scan)
    assert calls["n"] == 2 and last == 10_300.0


def test_lane_isolated_scan_error_never_propagates(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    # ops_alerts.alert must not require Slack in a test; stub it.
    monkeypatch.setattr(listener.ops_alerts, "alert", lambda *a, **k: None)

    def _boom():
        raise RuntimeError("R2 exploded")

    # does NOT raise; marker still advances so the lane does not hot-loop on failure
    new_last = listener.run_client_media_lane(
        now_mono=500.0, last_mono=0.0, interval_secs=300, scan=_boom)
    assert new_last == 500.0


def test_lane_nothing_new_is_a_cheap_run(monkeypatch):
    """A scan with nothing new still 'runs' (scan_and_generate itself no-ops per gym
    whose media count == existing feeds); the lane just marks the run and moves on."""
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    ran = {"n": 0}
    new_last = listener.run_client_media_lane(
        now_mono=9_000.0, last_mono=0.0, interval_secs=300,
        scan=lambda: ran.__setitem__("n", ran["n"] + 1))
    assert ran["n"] == 1 and new_last == 9_000.0


def test_startup_lane_announces_state(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "true")
    listener._print_scheduled_lanes()
    out = capsys.readouterr().out
    assert "[scheduler] client media sync (frequent): ARMED" in out
    monkeypatch.setenv("AGENT_CLIENT_MEDIA_SYNC", "false")
    listener._print_scheduled_lanes()
    out = capsys.readouterr().out
    assert "client media sync (frequent): dormant (AGENT_CLIENT_MEDIA_SYNC off)" in out
