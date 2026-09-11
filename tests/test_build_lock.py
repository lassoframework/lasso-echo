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
"""

import os
import sys

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
