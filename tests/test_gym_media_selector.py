"""gym_media_drive §6: selection order, 90-day cooldown + never-twice-a-month,
excluded never selectable, empty pool alert, deny rollback, tenant isolation."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_selector as sel  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)


def test_picks_least_used_longest_unused():
    store = FakeMediaStore(assets=[
        make_asset("a", used_count=3, last_used_at="2026-01-01T00:00:00+00:00"),
        make_asset("b", used_count=0, last_used_at=None),
        make_asset("c", used_count=1, last_used_at="2026-02-01T00:00:00+00:00"),
    ])
    got = sel.pick_media("pierce", store=store, now=NOW)
    assert got["id"] == "b"           # used_count 0, never used


def test_90_day_cooldown_and_month_guard():
    recent = (NOW - timedelta(days=10)).isoformat()
    store = FakeMediaStore(assets=[make_asset("a", used_count=0, last_used_at=recent)])
    assert sel.pick_media("pierce", store=store, now=NOW) is None   # inside 90d


def test_used_this_month_excluded():
    this_month = NOW.replace(day=2).isoformat()
    store = FakeMediaStore(assets=[make_asset("a", used_count=0,
                                             last_used_at=this_month)])
    assert sel.pick_media("pierce", store=store, now=NOW) is None


def test_excluded_by_coach_never_selectable():
    store = FakeMediaStore(assets=[make_asset("a", excluded_by_coach=True)])
    assert sel.pick_media("pierce", store=store, now=NOW) is None


def test_ineligible_and_unprobed_never_selectable():
    store = FakeMediaStore(assets=[
        make_asset("bad", eligible=False),
        make_asset("unprobed", eligible=None),
    ])
    assert sel.pick_media("pierce", store=store, now=NOW) is None


def test_kind_preference_filters():
    store = FakeMediaStore(assets=[
        make_asset("v1", kind="video"),
        make_asset("p1", kind="photo"),
    ])
    got = sel.pick_media("pierce", kind_preference="photo", store=store, now=NOW)
    assert got["id"] == "p1"


def test_empty_pool_fires_one_deduped_alert(monkeypatch):
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append((k, m)) or True)
    store = FakeMediaStore(assets=[])
    assert sel.pick_media("pierce", store=store, now=NOW) is None
    assert fired and "pierce" in fired[0][1] and "ask for photos" in fired[0][1]


def test_tenant_isolation_never_selects_other_gym(monkeypatch):
    """A row tagged for gym B can never be picked for gym A, even if a store bug
    returned it. list_assets already filters, and pick_media re-asserts."""
    class LeakyStore(FakeMediaStore):
        def list_assets(self, gym_id, source_id=None):
            # Deliberately leak a foreign-gym asset to prove the re-assertion.
            return [make_asset("foreign", gym_id="other_gym")]
    monkeypatch.setattr("agent.gym_media_index.dedup_alert", lambda k, m: True)
    store = LeakyStore()
    assert sel.pick_media("pierce", store=store, now=NOW) is None


def test_stamp_and_rollback(monkeypatch):
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    store = FakeMediaStore(assets=[make_asset("a", used_count=0, last_used_at=None)])
    asset = store.get_asset("a")
    sel.stamp_use(asset, "pierce", "2026-08-27", store=store, now=NOW)
    assert store.assets["a"]["used_count"] == 1
    assert store.assets["a"]["last_used_at"] == NOW.isoformat()
    # Deny rollback returns the asset exactly as it was.
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is True
    assert store.assets["a"]["used_count"] == 0
    assert store.assets["a"]["last_used_at"] is None
    # Idempotent second rollback.
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is False
