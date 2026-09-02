"""
listener_watch tests (agent/listener_watch.py), fully offline.

2026-09-02: scout-listener, the desktop process that picks Echo's support tickets and
ops-fix requests out of #echosupport, crash-looped 47 times on a MODULE_NOT_FOUND and
nobody was told. Client tickets sat untriaged for hours and the only evidence was a stderr
file no human reads. Echo alerts loudly when a GYM's calendar breaks; nothing alerted when
the thing that READS those alerts was face down.

A dead process cannot report its own death and a sleeping Mac cannot alert anyone, so the
ping goes inward and ABSENCE is the signal. These tests pin the three-state behavior (never
seen / healthy / stale), alert-once-per-episode, the recovery announcement, and the HMAC
that stops a forged ping from masking a real outage.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import listener_watch as lw  # noqa: E402

SRC = "scout-listener"
KEY = "xoxb-echo-bot-token-value"
T0 = datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)


class _FakeDb:
    def __init__(self, kv=None):
        self.kv = dict(kv or {})

    def kv_get(self, key, default=""):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


def _seen(dt):
    return {f"listener_seen_{SRC}": dt.isoformat()}


# ---- record / last_seen ----------------------------------------------------------------

def test_record_stamps_and_last_seen_reads_it_back():
    db = _FakeDb()
    assert lw.record(SRC, db=db, now=T0) is True
    assert lw.last_seen(SRC, db=db) == T0


def test_record_refuses_an_empty_source():
    db = _FakeDb()
    assert lw.record("", db=db) is False
    assert db.kv == {}


def test_last_seen_is_none_when_never_seen_or_corrupt():
    assert lw.last_seen(SRC, db=_FakeDb()) is None
    assert lw.last_seen(SRC, db=_FakeDb({f"listener_seen_{SRC}": "not-a-date"})) is None


def test_a_naive_stamp_is_read_as_utc_not_crashed_on():
    db = _FakeDb({f"listener_seen_{SRC}": "2026-09-02T18:00:00"})
    got = lw.last_seen(SRC, db=db)
    assert got is not None and got.tzinfo is not None


# ---- the HMAC: a forged ping must not be able to mask an outage -----------------------

def test_a_correctly_signed_fresh_ping_verifies():
    ts = str(T0.timestamp())
    assert lw.verify(SRC, ts, lw.sign(SRC, ts, KEY), KEY, now=T0) is True


def test_a_wrong_key_is_refused():
    ts = str(T0.timestamp())
    assert lw.verify(SRC, ts, lw.sign(SRC, ts, "someone-elses-token"), KEY, now=T0) is False


def test_a_tampered_source_is_refused():
    ts = str(T0.timestamp())
    sig = lw.sign(SRC, ts, KEY)
    assert lw.verify("some-other-service", ts, sig, KEY, now=T0) is False


def test_a_replayed_old_ping_is_refused():
    """The whole point: a captured heartbeat must not be replayable tomorrow to keep a
    dead listener looking alive."""
    old = T0 - timedelta(hours=6)
    ts = str(old.timestamp())
    sig = lw.sign(SRC, ts, KEY)          # a genuinely valid signature, just stale
    assert lw.verify(SRC, ts, sig, KEY, now=T0) is False


def test_a_future_dated_ping_is_refused():
    future = T0 + timedelta(hours=6)
    ts = str(future.timestamp())
    assert lw.verify(SRC, ts, lw.sign(SRC, ts, KEY), KEY, now=T0) is False


def test_missing_key_or_signature_or_garbage_ts_is_refused():
    ts = str(T0.timestamp())
    assert lw.verify(SRC, ts, lw.sign(SRC, ts, KEY), "", now=T0) is False
    assert lw.verify(SRC, ts, "", KEY, now=T0) is False
    assert lw.verify(SRC, "not-a-number", "deadbeef", KEY, now=T0) is False
    assert lw.sign(SRC, ts, "") == ""


# ---- sweep: three states, alert once, announce recovery -------------------------------

def test_a_source_never_seen_is_silent():
    """It may simply not be deployed yet. A watch that screams on day one gets muted, and
    a muted watch is worse than no watch."""
    alerts = []
    out = lw.sweep(db=_FakeDb(), alert=alerts.append, now=T0, logger=lambda m: None)
    assert out["never_seen"] == 1 and out["down"] == 0
    assert alerts == []


def test_a_recent_heartbeat_is_healthy_and_silent():
    alerts = []
    db = _FakeDb(_seen(T0 - timedelta(minutes=3)))
    out = lw.sweep(db=db, alert=alerts.append, now=T0, logger=lambda m: None)
    assert out["healthy"] == 1 and out["down"] == 0 and alerts == []


def test_a_stale_heartbeat_alerts_with_what_is_actually_broken():
    alerts = []
    db = _FakeDb(_seen(T0 - timedelta(hours=2)))
    out = lw.sweep(db=db, alert=alerts.append, now=T0, logger=lambda m: None)
    assert out["down"] == 1
    assert len(alerts) == 1
    msg = alerts[0]
    assert "scout-listener" in msg
    # it must say what the CONSEQUENCE is, not just that a process is down
    assert "#echosupport" in msg and "untriaged" in msg
    # and give the operator the actual next command
    assert "launchctl" in msg


def test_it_alerts_once_per_episode_not_every_pass():
    """A Mac off for a weekend must not produce hundreds of alerts."""
    alerts = []
    db = _FakeDb(_seen(T0 - timedelta(hours=2)))
    for _ in range(5):
        lw.sweep(db=db, alert=alerts.append, now=T0, logger=lambda m: None)
    assert len(alerts) == 1


def test_recovery_is_announced_once_and_then_silent():
    alerts = []
    db = _FakeDb(_seen(T0 - timedelta(hours=2)))
    lw.sweep(db=db, alert=alerts.append, now=T0, logger=lambda m: None)   # down
    assert len(alerts) == 1
    # it checks in again
    lw.record(SRC, db=db, now=T0 + timedelta(minutes=1))
    out = lw.sweep(db=db, alert=alerts.append, now=T0 + timedelta(minutes=2),
                   logger=lambda m: None)
    assert out["recovered"] == 1
    assert len(alerts) == 2 and "is back" in alerts[1]
    # and stays quiet afterwards
    lw.sweep(db=db, alert=alerts.append, now=T0 + timedelta(minutes=3),
             logger=lambda m: None)
    assert len(alerts) == 2


def test_a_second_outage_after_a_recovery_alerts_again():
    alerts = []
    db = _FakeDb(_seen(T0 - timedelta(hours=2)))
    lw.sweep(db=db, alert=alerts.append, now=T0, logger=lambda m: None)
    lw.record(SRC, db=db, now=T0 + timedelta(minutes=1))
    lw.sweep(db=db, alert=alerts.append, now=T0 + timedelta(minutes=2),
             logger=lambda m: None)                                  # recovered
    later = T0 + timedelta(hours=5)
    out = lw.sweep(db=db, alert=alerts.append, now=later, logger=lambda m: None)
    assert out["down"] == 1
    assert len(alerts) == 3, "a fresh outage must be reported, not swallowed by the old one"


def test_the_staleness_threshold_is_four_missed_pings_not_one():
    """The listener pings every 5 minutes. One missed ping (a restart, a reconnect, a lid)
    must never page anyone."""
    alerts = []
    db = _FakeDb(_seen(T0 - timedelta(minutes=6)))
    lw.sweep(db=db, alert=alerts.append, now=T0, logger=lambda m: None)
    assert alerts == []
    assert lw.DEFAULT_STALE_AFTER >= 4 * 5 * 60
