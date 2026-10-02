"""Startup recovery checks for the cross-service media runway projection."""

import inspect
import os
import sys
import threading
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import accounts, config, echo_clients, listener, media_bridge as bridge


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))


class _Store:
    def __init__(self, failing=(), rows=None):
        self.calls = []
        self.failing = set(failing)
        self.rows = dict(rows or {})

    def read(self, base):
        row = self.rows.get(base)
        return dict(row) if row else None

    def compare_and_swap(self, base, *, expected_revision, revision,
                         fallback_episode, notice_state):
        if base in self.failing:
            raise RuntimeError("shared store unavailable")
        current = self.rows.get(base)
        current_revision = current["revision"] if current else 0
        if current_revision != expected_revision:
            return False
        self.rows[base] = {
            "gym_id": base, "revision": revision,
            "fallback_episode": fallback_episode,
            "notice_state": notice_state,
        }
        self.calls.append({
            "base": base,
            "expected_revision": expected_revision,
            "revision": revision,
            "fallback_episode": fallback_episode,
            "notice_state": notice_state,
        })
        return True


def _latest_by_base(store):
    return store.rows


def test_startup_refresh_projects_active_and_clear_canonical_clients(monkeypatch):
    now = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    store = _Store()
    monkeypatch.setattr(config, "posting_timezone_for", lambda _: "UTC")
    monkeypatch.setattr(bridge, "_shared_store", lambda: store)
    monkeypatch.setattr(bridge, "_registered_client_bases", lambda: ["active", "clear"])

    assert bridge.episode("active", now=now)
    store.calls.clear()
    summary = bridge.refresh_all_shared_status(now=now)

    assert summary == {"checked": 2, "materialized": 1, "mirrored": 2, "failed": 0}
    rows = _latest_by_base(store)
    assert rows["active"]["revision"] == 1
    assert rows["active"]["fallback_episode"]["active"] is True
    assert rows["clear"]["revision"] == 1
    assert rows["clear"]["fallback_episode"] is None
    assert rows["clear"]["notice_state"] == {
        "status": "none", "episode_id": None, "created_at": None,
        "delivery_confirmed": False,
    }


def test_startup_discovery_includes_dynamic_client_when_scan_flags_are_off(monkeypatch):
    class _Account:
        def __init__(self, key):
            self.key = key

    monkeypatch.delenv("AGENT_CLIENT_SCAN_DYNAMIC", raising=False)
    monkeypatch.delenv("AGENT_DYNAMIC_ACCOUNTS", raising=False)
    monkeypatch.setattr(accounts, "all_accounts", lambda: [_Account("hard_ig")])
    monkeypatch.setattr(accounts, "_load_registry_rows",
                        lambda: [{"base": "portalclient", "gym_id": "gym-1"}])
    monkeypatch.setattr(echo_clients, "only_client_bases",
                        lambda bases: [base for base in bases if base in {"hard", "portalclient"}])

    assert bridge._registered_client_bases() == ["hard", "portalclient"]


def test_startup_refresh_is_idempotent_for_a_clear_client(monkeypatch):
    now = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    store = _Store()
    monkeypatch.setattr(bridge, "_shared_store", lambda: store)

    first = bridge.refresh_shared_status("clear", now=now)
    second = bridge.refresh_shared_status("clear", now=now)

    assert first == {"base": "clear", "revision": 1, "materialized": True, "mirrored": True}
    assert second == {"base": "clear", "revision": 1, "materialized": False, "mirrored": True}
    assert [row["revision"] for row in store.calls] == [1]


def test_fresh_local_counter_repairs_divergent_shared_revision_one(monkeypatch):
    now = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(config, "posting_timezone_for", lambda _: "UTC")
    local_episode = {
        "id": "local", "depleted_on": "2026-09-18",
        "start": "2026-09-19", "end": "2026-09-20",
    }
    with bridge.db.connect() as conn:
        bridge._put(conn, "media_bridge_episode_gymx", local_episode)
        conn.commit()
    store = _Store(rows={"gymx": {
        "gym_id": "gymx", "revision": 1, "fallback_episode": None,
        "notice_state": {"status": "none", "episode_id": None,
                         "created_at": None, "delivery_confirmed": False},
    }})
    monkeypatch.setattr(bridge, "_shared_store", lambda: store)

    result = bridge.refresh_shared_status("gymx", now=now)

    assert result == {"base": "gymx", "revision": 2,
                      "materialized": True, "mirrored": True}
    assert store.rows["gymx"]["revision"] == 2
    assert store.rows["gymx"]["fallback_episode"]["episode_id"] == "local"
    assert store.calls[-1]["expected_revision"] == 1


def test_fresh_local_counter_seeds_above_existing_shared_revision_n(monkeypatch):
    store = _Store(rows={"gymx": {
        "gym_id": "gymx", "revision": 37,
        "fallback_episode": {"active": True, "episode_id": "stale",
                             "depleted_on": "2026-09-01",
                             "dates": ["2026-09-02", "2026-09-03"],
                             "drafts_need_review": True, "status": None},
        "notice_state": {"status": "unresolved", "episode_id": "stale",
                         "created_at": None, "delivery_confirmed": False},
    }})
    monkeypatch.setattr(bridge, "_shared_store", lambda: store)

    result = bridge.refresh_shared_status("gymx")

    assert result == {"base": "gymx", "revision": 38,
                      "materialized": True, "mirrored": True}
    assert store.rows["gymx"]["fallback_episode"] is None
    assert store.calls[-1]["expected_revision"] == 37


def test_reconciliation_retries_after_cas_race_without_stale_overwrite(monkeypatch):
    class RacingStore(_Store):
        def __init__(self):
            super().__init__(rows={"gymx": {
                "gym_id": "gymx", "revision": 5, "fallback_episode": None,
                "notice_state": {"status": "none", "episode_id": None,
                                 "created_at": None, "delivery_confirmed": False},
            }})
            self.expected = []

        def compare_and_swap(self, base, **candidate):
            self.expected.append(candidate["expected_revision"])
            if len(self.expected) == 1:
                self.rows[base] = {**self.rows[base], "revision": 6}
                return False
            return super().compare_and_swap(base, **candidate)

    store = RacingStore()
    monkeypatch.setattr(bridge, "_shared_store", lambda: store)

    assert bridge.refresh_shared_status("gymx")["mirrored"] is True
    assert store.expected == [5, 6]
    assert store.rows["gymx"]["revision"] == 7


def test_startup_refresh_keeps_going_after_one_tenant_store_failure(monkeypatch):
    now = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    store = _Store(failing={"broken"})
    monkeypatch.setattr(bridge, "_shared_store", lambda: store)

    summary = bridge.refresh_all_shared_status(
        bases=["good", "broken", "good"], now=now, logger=lambda _: None)

    assert summary == {"checked": 2, "materialized": 2, "mirrored": 1, "failed": 1}
    assert _latest_by_base(store)["good"]["revision"] == 1


def test_scheduler_off_still_starts_refresh_once(monkeypatch):
    started = []

    class Thread:
        def __init__(self, *, target, name, daemon):
            started.append((target, name, daemon))

        def start(self):
            return None

    monkeypatch.setattr(listener.threading, "Thread", Thread)
    monkeypatch.setattr(listener, "_shared_media_refresh_started", False)
    monkeypatch.setenv("AGENT_SCHEDULER_ENABLED", "false")

    assert listener._start_shared_media_runway_refresh() is True
    assert listener._start_shared_media_runway_refresh() is False
    assert started == [(listener._refresh_shared_media_runway,
                        "shared-media-runway-refresh", True)]
    source = inspect.getsource(listener.run_listener)
    assert source.index("_start_shared_media_runway_refresh()") < source.index(
        'if str(os.environ.get("AGENT_SCHEDULER_ENABLED"')


def test_slow_or_failing_refresh_never_blocks_listener_or_scheduler_start(monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def slow_failure():
        entered.set()
        release.wait(2)
        raise RuntimeError("shared plane down")

    monkeypatch.setattr(bridge, "refresh_all_shared_status", slow_failure)
    monkeypatch.setattr(listener, "_shared_media_refresh_started", False)

    assert listener._start_shared_media_runway_refresh() is True
    assert entered.wait(1)
    # The daemon start returned while its refresh remains blocked.  Scheduler
    # recovery contains no inline shared refresh and can proceed independently.
    assert not release.is_set()
    scheduler_source = inspect.getsource(listener._daily_scheduler)
    assert "_refresh_shared_media_runway()" not in scheduler_source
    release.set()
