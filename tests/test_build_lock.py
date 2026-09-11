"""
build_lock.py: the per-gym advisory lock around build_client_month.

Ticket 4941e162-2923-495f-8efb-d2554dea5aec (CrossFit Reverb / Dean Holcomb,
2026-09-11): content_calendar showed five separate insert timestamps within
two hours, several landing near-duplicate captions on the SAME post_date as
PENDING rows side by side -- the signature of two overlapping build_client_month
calls for the same gym. These tests assert the lock actually prevents that:
a second acquire for a gym already held (fresh) is refused; a different gym is
never blocked by another gym's lock; release clears it; a stale lock (older
than STALE_SECONDS) self-heals; and release by the WRONG holder never clears
someone else's live lock (the stale-run-recovery-killed-the-wrong-process case).

FOLLOW-UP AUDIT (2026-09-11, same day): the original static STALE_SECONDS
(15 minutes) was never checked against the actual worst-case LEGITIMATE
build runtime, which (transcodes + up to ~248 caption LLM calls + uncapped
per-candidate vision calls -- see the module docstring) can plausibly exceed
it. The tests below cover the heartbeat fix: a build that legitimately runs
longer than the OLD 15-minute window must not lose its lock (test_heartbeat_
keeps_a_long_legitimate_build_from_going_stale); a genuinely crashed/silent
holder must still be reclaimed within the new SHORT HEARTBEAT_STALE_SECONDS
window (test_a_holder_that_stops_heartbeating_is_reclaimed_quickly); and a
heartbeat write failure must never crash the build or release the lock early
(test_heartbeat_write_failure_never_raises_and_never_releases_early).
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import build_lock  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    yield


def test_second_acquire_for_same_gym_is_refused():
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-a") is True
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-b") is False


def test_different_gym_is_never_blocked():
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-a") is True
    assert build_lock.acquire("pierce", holder="run-b") is True


def test_same_holder_reacquiring_is_not_refused():
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-a") is True
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-a") is True


def test_release_clears_the_lock_for_the_next_run():
    build_lock.acquire("crossfitreverb30b5b2", holder="run-a")
    build_lock.release("crossfitreverb30b5b2", holder="run-a")
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-b") is True


def test_stale_lock_self_heals(monkeypatch):
    build_lock.acquire("crossfitreverb30b5b2", holder="run-a")
    # simulate run-a having died 20 minutes ago without releasing
    real_time = build_lock.time.time

    def _later():
        return real_time() + build_lock.STALE_SECONDS + 60

    monkeypatch.setattr(build_lock.time, "time", _later)
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-b") is True


def test_release_by_wrong_holder_never_clears_a_live_lock():
    build_lock.acquire("crossfitreverb30b5b2", holder="run-a")
    build_lock.release("crossfitreverb30b5b2", holder="run-b")   # not run-a's to clear
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-c") is False


def test_is_locked_reports_without_acquiring():
    assert build_lock.is_locked("crossfitreverb30b5b2") is False
    build_lock.acquire("crossfitreverb30b5b2", holder="run-a")
    assert build_lock.is_locked("crossfitreverb30b5b2") is True


def test_empty_gym_id_never_locks_anything():
    assert build_lock.acquire("", holder="run-a") is False
    assert build_lock.is_locked("") is False


def test_unreadable_store_fails_closed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kv store down")

    from agent import db
    monkeypatch.setattr(db, "kv_get", _boom)
    assert build_lock.acquire("crossfitreverb30b5b2", holder="run-a") is False


# ---------------------------------------------------------------------------
# Heartbeat regression tests (2026-09-11 follow-up audit).
# ---------------------------------------------------------------------------

def test_heartbeat_keeps_a_long_legitimate_build_from_going_stale(monkeypatch):
    """A build that legitimately keeps heartbeating for well over the OLD
    static 15-minute timeout must never lose its lock, and a second caller
    for the same gym must stay refused the whole time."""
    gym = "crossfitreverb30b5b2"
    assert build_lock.acquire(gym, holder="run-a") is True

    real_time = build_lock.time.time
    elapsed = {"t": 0.0}

    def _fake_time():
        return real_time() + elapsed["t"]

    monkeypatch.setattr(build_lock.time, "time", _fake_time)

    # Simulate a build running for 40 minutes (well past the OLD 15-minute
    # static STALE_SECONDS) that heartbeats every 45s, exactly like
    # HeartbeatHandle's background thread would.
    old_static_timeout = 15 * 60
    step = build_lock.HEARTBEAT_INTERVAL_SECONDS
    total = old_static_timeout + 25 * 60  # 40 minutes of simulated build time
    t = 0.0
    while t < total:
        t += step
        elapsed["t"] = t
        assert build_lock.heartbeat(gym, holder="run-a") is True
        # A second caller must be refused at every single check along the way,
        # not just at the end.
        assert build_lock.acquire(gym, holder="run-b") is False
        assert build_lock.is_locked(gym) is True


def test_a_holder_that_stops_heartbeating_is_reclaimed_quickly(monkeypatch):
    """A holder that goes silent (crashes) is reclaimed within the SHORT
    HEARTBEAT_STALE_SECONDS window, not the old long static one."""
    gym = "crossfitreverb30b5b2"
    assert build_lock.acquire(gym, holder="run-a") is True

    real_time = build_lock.time.time

    def _later():
        return real_time() + build_lock.HEARTBEAT_STALE_SECONDS + 30

    monkeypatch.setattr(build_lock.time, "time", _later)
    # No heartbeat was ever called after acquisition: run-a is presumed dead.
    assert build_lock.is_locked(gym) is False
    assert build_lock.acquire(gym, holder="run-b") is True
    # The new detection window is short: it must be smaller than the OLD
    # static 15-minute timeout this replaces (the whole point of the fix).
    assert build_lock.HEARTBEAT_STALE_SECONDS < 15 * 60


def test_heartbeat_write_failure_never_raises_and_never_releases_early(monkeypatch):
    """A kv hiccup during a heartbeat must not crash the build and must not
    cause the lock to be lost/raced against early -- it just fails to renew
    THAT one time; the lock is still fresh from the original acquire()."""
    gym = "crossfitreverb30b5b2"
    assert build_lock.acquire(gym, holder="run-a") is True

    from agent import db
    real_kv_set = db.kv_set

    def _boom(*a, **k):
        raise RuntimeError("kv store hiccup")

    monkeypatch.setattr(db, "kv_set", _boom)
    # Must not raise, and reports the renewal did not happen.
    assert build_lock.heartbeat(gym, holder="run-a") is False

    # Restore the store (NOT monkeypatch.undo() -- this test's autouse
    # AGENT_DB_PATH fixture shares the same MonkeyPatch instance, and a full
    # undo() would also revert that, silently pointing kv reads/writes at a
    # different db). The lock is still held by run-a (the failed heartbeat
    # did not release or corrupt it) and still fresh.
    monkeypatch.setattr(db, "kv_set", real_kv_set)
    assert build_lock.is_locked(gym) is True
    assert build_lock.acquire(gym, holder="run-b") is False
    assert build_lock.acquire(gym, holder="run-a") is True  # still ours


def test_heartbeat_never_raises_when_gym_id_is_empty():
    assert build_lock.heartbeat("", holder="run-a") is False


def test_heartbeat_refuses_once_another_holder_has_legitimately_taken_over(monkeypatch):
    """If this holder went quiet long enough to be reclaimed by someone else,
    a late heartbeat from the OLD holder must not clobber the new owner."""
    gym = "crossfitreverb30b5b2"
    assert build_lock.acquire(gym, holder="run-a") is True

    real_time = build_lock.time.time
    monkeypatch.setattr(
        build_lock.time, "time",
        lambda: real_time() + build_lock.HEARTBEAT_STALE_SECONDS + 30)
    assert build_lock.acquire(gym, holder="run-b") is True  # legitimate takeover

    # Restore real time (NOT monkeypatch.undo() -- see the write-failure test
    # above for why a full undo() is unsafe here).
    monkeypatch.setattr(build_lock.time, "time", real_time)
    # The old holder's late heartbeat must be refused, not silently accepted.
    assert build_lock.heartbeat(gym, holder="run-a") is False
    assert build_lock.is_locked(gym) is True  # run-b's lock stands


def test_start_heartbeat_thread_renews_the_lock_while_running(monkeypatch):
    """HeartbeatHandle's background thread must actually call heartbeat() on
    its interval while running, and stop cleanly (no further renewals) once
    .stop() is called."""
    gym = "crossfitreverb30b5b2"
    assert build_lock.acquire(gym, holder="run-a") is True

    calls = []
    real_heartbeat = build_lock.heartbeat

    def _tracking_heartbeat(base_key, *, holder=""):
        calls.append(1)
        return real_heartbeat(base_key, holder=holder)

    monkeypatch.setattr(build_lock, "heartbeat", _tracking_heartbeat)

    handle = build_lock.start_heartbeat(gym, holder="run-a", interval=0.05)
    try:
        time.sleep(0.3)
        assert len(calls) >= 2  # several renewals fired while it ran
    finally:
        handle.stop()

    seen_at_stop = len(calls)
    time.sleep(0.2)
    # No further renewals after stop() -- the thread actually exited.
    assert len(calls) == seen_at_stop
