"""LASSO cadence recovery lane: gates, kwargs, isolation, threads, flocks."""

import inspect
import json
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent import config, db, listener
from agent.jobs import lasso_cadence_recovery as lane
from agent.jobs import lasso_daily_paired_stories, lasso_held_media_repair
from agent import calendar_autopublish

NY = ZoneInfo(config.POSTING_TIMEZONE)
NOW = datetime(2026, 10, 8, 13, 30, tzinfo=NY)  # future weekday morning in NY


class _AutonomyStore:
    def __init__(self, autonomous):
        self._autonomous = autonomous

    def gym_autonomy(self, slug):
        assert slug == "lasso"
        return self._autonomous


@pytest.fixture
def armed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_LASSO_CADENCE_RECOVERY", "true")
    for name in ("AGENT_PUBLISH_ENABLED", "AGENT_CALENDAR_AUTOPUBLISH",
                 "AGENT_LASSO_3X_ENABLED", "AGENT_LASSO_VIA_ZERNIO",
                 "AGENT_ZERNIO_PUBLISH"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setattr(lane, "_record", lambda *a, **k: None)


@pytest.fixture
def jobs(monkeypatch):
    calls = {"repair": [], "stories": [], "publish": []}
    monkeypatch.setattr(
        lasso_held_media_repair, "run",
        lambda **kw: calls["repair"].append(kw) or
        {"ok": True, "attempted": 1, "generated": 0, "reused": 1,
         "repaired": 1, "skipped": 0, "errors": 0})
    monkeypatch.setattr(
        lasso_daily_paired_stories, "run",
        lambda **kw: calls["stories"].append(kw) or
        {"ok": True, "eligible": 6, "generated": 0, "reused": 6, "staged": 6,
         "repaired": 0, "occupied": 0, "blocked": 0})
    monkeypatch.setattr(
        calendar_autopublish, "publish_due",
        lambda *a, **kw: calls["publish"].append((a, kw)) or
        {"ok": True, "published": [{"id": 1}], "failed": [], "waiting": []})
    return calls


def test_disabled_is_noop(monkeypatch, jobs):
    monkeypatch.delenv("AGENT_LASSO_CADENCE_RECOVERY", raising=False)
    assert not lane.enabled()
    assert lane.run_prep_once("lasso_ig", now=NOW,
                              store=_AutonomyStore(True))["reason"] == "lane disabled"
    assert lane.run_publish_once(now=NOW,
                                 store=_AutonomyStore(True))["reason"] == "lane disabled"
    assert jobs == {"repair": [], "stories": [], "publish": []}


def test_unknown_account_is_noop(armed, jobs):
    out = lane.run_prep_once("eng_ig", now=NOW, store=_AutonomyStore(True))
    assert out["ok"] is False and out["reason"] == "unknown account"
    assert jobs["repair"] == [] and jobs["stories"] == []


@pytest.mark.parametrize("autonomy", [None, False])
def test_manual_or_unknown_autonomy_holds_closed(armed, jobs, autonomy):
    out = lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(autonomy))
    assert out["ok"] is False
    assert out["reason"] == "lasso autonomy off or unknown"
    out = lane.run_publish_once(now=NOW, store=_AutonomyStore(autonomy))
    assert out["ok"] is False
    assert out["reason"] == "lasso autonomy off or unknown"
    assert jobs == {"repair": [], "stories": [], "publish": []}


def test_autonomy_read_raises_holds_closed(armed, jobs):
    class Bad:
        def gym_autonomy(self, slug):
            raise RuntimeError("offline")
    assert lane.run_publish_once(now=NOW, store=Bad())["reason"] == \
        "lasso autonomy off or unknown"
    assert jobs["publish"] == []


def test_prep_kwargs_order_and_now(armed, jobs):
    out = lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    assert out["ok"] is True and out["repair"]["repaired"] == 1
    assert out["stories"]["staged"] == 6
    repair, stories = jobs["repair"][0], jobs["stories"][0]
    # Lasso-only account routing, both directions.
    assert repair["account_key"] == "lasso_ig"
    assert stories["account"] == "instagram"
    # Actual NY now, same local day; catchup from the dynamic client limits.
    assert repair["now"].tzinfo is not None
    assert repair["now"].astimezone(NY).date().isoformat() == "2026-10-08"
    assert stories["now"].astimezone(NY).date().isoformat() == "2026-10-08"
    assert isinstance(stories["catchup_days"], int) and stories["catchup_days"] >= 0
    # FB account maps independently.
    out_fb = lane.run_prep_once("lasso_fb", now=NOW, store=_AutonomyStore(True))
    assert out_fb["ok"] is True
    assert jobs["repair"][-1]["account_key"] == "lasso_fb"
    assert jobs["stories"][-1]["account"] == "facebook"


def test_prep_failure_isolation(armed, jobs, monkeypatch):
    monkeypatch.setattr(lasso_held_media_repair, "run",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    out = lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    assert out["ok"] is False
    assert out["repair"]["reason"] == "RuntimeError"
    # The blocked render never stalls the paired-Story prep of the same cycle.
    assert len(jobs["stories"]) == 1 and out["stories"]["staged"] == 6


def test_publish_kwargs_and_lock_separation(armed, jobs, monkeypatch):
    monkeypatch.setattr(calendar_autopublish, "_client_publish_limits",
                        lambda gym, day, cap: (7, 12))
    out = lane.run_publish_once(now=NOW, store=_AutonomyStore(True))
    assert out["ok"] is True and out["published"] == 1
    args, kw = jobs["publish"][0]
    assert args == ("2026-10-08",)
    assert kw["gym_id"] == "lasso"
    assert kw["catch_all"] is False and kw["approved_only"] is False
    assert kw["catchup_days"] == 7 and kw["daily_cap"] == 12
    assert kw["now"].astimezone(NY).date().isoformat() == "2026-10-08"


def test_kv_not_durable_fails_closed(armed, jobs, monkeypatch):
    monkeypatch.setattr(db, "kv_is_durable", lambda: False)
    out = lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    assert out["ok"] is False and "lock" in out["reason"]
    assert jobs["repair"] == [] and jobs["stories"] == []


def test_held_flock_skips_and_finally_releases(armed, jobs, monkeypatch):
    import fcntl as real_fcntl
    ops = []
    contend = {"active": True}

    def fake_flock(fd, op):
        ops.append(op)
        if op == (real_fcntl.LOCK_EX | real_fcntl.LOCK_NB) and contend["active"]:
            raise BlockingIOError("held")

    monkeypatch.setattr(lane.fcntl, "flock", fake_flock)
    # First cycle: another worker holds the lock -> skip, nothing runs.
    out = lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    assert out["ok"] is False and "lock" in out["reason"]
    assert jobs["repair"] == []
    # Second cycle: lock acquired, job raises -> the lock is still released.
    contend["active"] = False
    monkeypatch.setattr(lasso_held_media_repair, "run",
                        lambda **kw: (_ for _ in ()).throw(ValueError("x")))
    lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    assert real_fcntl.LOCK_UN in ops


def test_kv_receipts_are_safe_counts_only(armed, jobs, monkeypatch):
    written = {}
    monkeypatch.setattr(lane, "_record",
                        lambda worker, receipt: written.setdefault(worker, []).append(receipt))
    lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    lane.run_publish_once(now=NOW, store=_AutonomyStore(True))
    assert [r["state"] for r in written["prep-lasso_ig"]] == ["begun", "completed"]
    assert [r["state"] for r in written["publish"]] == ["begun", "completed"]
    blob = json.dumps(written)
    assert '"caption"' not in blob and "token" not in blob
    assert written["prep-lasso_ig"][-1]["summary"]["stories"]["staged"] == 6
    assert written["publish"][-1]["summary"]["published"] == 1


def test_workers_progress_independently_with_blocked_prep(armed):
    """A deliberately blocked prep never stalls the publisher thread."""
    prep_entered = threading.Event()
    release_prep = threading.Event()
    publish_ran = threading.Event()
    stop = threading.Event()

    def blocked_prep():
        prep_entered.set()
        release_prep.wait(5)

    prep_thread = threading.Thread(
        target=lane.prep_worker,
        args=("lasso_ig",),
        kwargs={"stop": stop.is_set, "sleep": lambda s: stop.wait(s),
                "step": blocked_prep},
        daemon=True)
    pub_thread = threading.Thread(
        target=lane.publish_worker,
        kwargs={"stop": stop.is_set, "sleep": lambda s: stop.wait(s),
                "step": lambda: (publish_ran.set(), stop.set())},
        daemon=True)
    prep_thread.start()
    pub_thread.start()
    assert prep_entered.wait(5)          # prep is running (and blocked)
    assert publish_ran.wait(5)           # publisher still progressed
    release_prep.set()
    stop.set()
    prep_thread.join(5)
    pub_thread.join(5)
    assert not prep_thread.is_alive() and not pub_thread.is_alive()


def test_concurrent_start_creates_no_extra_threads(armed, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None, kwargs=None):
            started.append(name)
            self.name = name

        def start(self):
            pass

    real_thread = threading.Thread
    monkeypatch.setattr(listener.threading, "Thread", FakeThread)
    monkeypatch.setattr(listener, "_lasso_cadence_recovery_started", False)
    results = []
    callers = [real_thread(target=lambda: results.append(listener._start_lasso_cadence_recovery()))
               for _ in range(4)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(5)
        assert not caller.is_alive()
    assert results.count(True) == 1
    assert sorted(started) == [
        "lasso-cadence-prep-lasso_fb",
        "lasso-cadence-prep-lasso_ig",
        "lasso-cadence-publish",
    ]
    # A second boot adds nothing.
    assert listener._start_lasso_cadence_recovery() is False
    assert len(started) == 3
    monkeypatch.setattr(listener, "_lasso_cadence_recovery_started", False)


def test_start_disabled_starts_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_LASSO_CADENCE_RECOVERY", raising=False)
    monkeypatch.setattr(listener, "_lasso_cadence_recovery_started", False)
    started = []

    class FakeThread:
        def __init__(self, **kw):
            started.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(listener.threading, "Thread", FakeThread)
    assert listener._start_lasso_cadence_recovery() is False
    assert started == []


def test_lane_never_invokes_run_daily():
    assert "run_daily(" not in inspect.getsource(lane)
    assert "build_client_month(" not in inspect.getsource(lane)


def test_interval_clamps(monkeypatch):
    monkeypatch.setenv("AGENT_LASSO_CADENCE_RECOVERY_PUB_SECONDS", "1")
    monkeypatch.setenv("AGENT_LASSO_CADENCE_RECOVERY_PREP_SECONDS", "99999")
    assert lane.publish_interval_seconds() == 15
    assert lane.prep_interval_seconds() == 3600
    monkeypatch.setenv("AGENT_LASSO_CADENCE_RECOVERY_PUB_SECONDS", "junk")
    assert lane.publish_interval_seconds() == lane.PUBLISH_INTERVAL_SECONDS


@pytest.mark.parametrize("flag", [
    "AGENT_LASSO_CADENCE_RECOVERY", "AGENT_PUBLISH_ENABLED",
    "AGENT_CALENDAR_AUTOPUBLISH", "AGENT_LASSO_3X_ENABLED",
    "AGENT_LASSO_VIA_ZERNIO", "AGENT_ZERNIO_PUBLISH",
])
def test_each_publish_gate_is_reread_between_ticks(armed, jobs, monkeypatch, flag):
    store = _AutonomyStore(True)
    assert lane.run_publish_once(now=NOW, store=store)["ok"] is True
    monkeypatch.setenv(flag, "false")
    assert lane.run_publish_once(now=NOW, store=store)["ok"] is False
    assert len(jobs["publish"]) == 1
    monkeypatch.setenv(flag, "true")
    assert lane.run_publish_once(now=NOW, store=store)["ok"] is True
    assert len(jobs["publish"]) == 2


@pytest.mark.parametrize("child", ["repair", "stories"])
def test_returned_child_failure_is_truthful_and_backed_off(armed, jobs, monkeypatch, child):
    module = lasso_held_media_repair if child == "repair" else lasso_daily_paired_stories
    monkeypatch.setattr(module, "run", lambda **kw: {"ok": False, "reason": "preflight blocked"})
    written = []
    monkeypatch.setattr(lane, "_record", lambda worker, receipt: written.append(receipt))
    result = lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    assert result["ok"] is False
    assert written[-1]["summary"][child]["reason"] == "preflight blocked"
    delays = []
    lane.prep_worker("lasso_ig", stop=lambda: len(delays) == 4,
                     sleep=delays.append, step=lambda: result)
    assert delays == [600, 900, 900, 900]


@pytest.mark.parametrize("child", ["repair", "stories"])
def test_partial_errors_or_blocked_counts_trigger_backoff(armed, jobs, monkeypatch, child):
    module = lasso_held_media_repair if child == "repair" else lasso_daily_paired_stories
    field = "errors" if child == "repair" else "blocked"
    monkeypatch.setattr(module, "run", lambda **kw: {"ok": True, field: 1})
    result = lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))
    assert result["ok"] is False and result[child][field] == 1


def test_zero_eligible_or_occupied_is_success(armed, jobs, monkeypatch):
    monkeypatch.setattr(lasso_held_media_repair, "run", lambda **kw: {"ok": True, "attempted": 0})
    monkeypatch.setattr(lasso_daily_paired_stories, "run", lambda **kw: {"ok": True, "eligible": 0, "occupied": 3, "blocked": 0})
    assert lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))["ok"] is True


def test_lock_open_error_fails_closed(armed, jobs, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(lane, "open", denied, raising=False)
    assert lane.run_prep_once("lasso_ig", now=NOW, store=_AutonomyStore(True))["ok"] is False
    assert lane.run_publish_once(now=NOW, store=_AutonomyStore(True))["ok"] is False
    assert jobs == {"repair": [], "stories": [], "publish": []}


def test_real_flock_is_stable_nonblocking_and_reusable(armed):
    with lane._worker_flock("prep-lasso_ig") as held:
        assert held
        path = db.db_path() + ".lasso-cadence-prep-lasso_ig.lock"
        inode = os.stat(path).st_ino
        with lane._worker_flock("prep-lasso_ig") as contender:
            assert contender is False
        with lane._worker_flock("publish") as publisher:
            assert publisher is True
    with lane._worker_flock("prep-lasso_ig") as again:
        assert again and os.stat(path).st_ino == inode


def test_worker_exception_backoff_recovers():
    delays = []
    def step():
        if len(delays) < 2:
            raise ValueError("synthetic")
        return {"ok": True}
    lane._worker_loop(step, lambda: 60, lambda: len(delays) == 3,
                      delays.append, "test")
    assert delays == [120, 240, 60]


def test_authoritative_autonomy_is_reread_after_success(armed, jobs):
    store = _AutonomyStore(True)
    assert lane.run_publish_once(now=NOW, store=store)["ok"] is True
    store._autonomous = False
    assert lane.run_publish_once(now=NOW, store=store)["ok"] is False
    store._autonomous = None
    assert lane.run_publish_once(now=NOW, store=store)["ok"] is False
    assert len(jobs["publish"]) == 1
