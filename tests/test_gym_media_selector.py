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


def test_two_assets_on_one_date_both_roll_back(monkeypatch):
    """A 2x day stages TWO gym-media posts on one date, and Story Studio stamps every
    segment asset under one pseudo-date key. The old single-record write clobbered all
    but the LAST, so the earlier assets could never be returned to the pool and sat out
    the 90-day cooldown. Every record on the key must survive and roll back."""
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    store = FakeMediaStore(assets=[
        make_asset("am", used_count=0, last_used_at=None),
        make_asset("pm", used_count=0, last_used_at=None)])
    sel.stamp_use(store.get_asset("am"), "pierce", "2026-08-27", store=store, now=NOW)
    sel.stamp_use(store.get_asset("pm"), "pierce", "2026-08-27", store=store, now=NOW)
    assert store.assets["am"]["used_count"] == 1
    assert store.assets["pm"]["used_count"] == 1

    assert sel.rollback_use("pierce", "2026-08-27", store=store) is True
    assert store.assets["am"]["used_count"] == 0, "the AM asset was stranded"
    assert store.assets["pm"]["used_count"] == 0
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is False


def test_rollback_asset_returns_one_and_leaves_the_days_other_post_stamped(monkeypatch, tmp_path):
    """Denying ONE post of a 2x day must return only ITS photo — the other post is
    still standing and must keep its asset stamped. Uses a REAL temp kv because
    rollback_asset scans the kv table directly (as it does in production)."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    store = FakeMediaStore(assets=[
        make_asset("am", used_count=0, last_used_at=None),
        make_asset("pm", used_count=0, last_used_at=None)])
    sel.stamp_use(store.get_asset("am"), "pierce", "2026-08-27", store=store, now=NOW)
    sel.stamp_use(store.get_asset("pm"), "pierce", "2026-08-27", store=store, now=NOW)

    assert sel.rollback_asset("pm", store=store) is True
    assert store.assets["pm"]["used_count"] == 0
    assert store.assets["am"]["used_count"] == 1, "the standing post lost its stamp"
    # And the day's remaining record still rolls back on a full deny.
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is True
    assert store.assets["am"]["used_count"] == 0


def test_legacy_single_dict_record_still_rolls_back(monkeypatch):
    """Records written before the list format (a bare dict) must still roll back."""
    import json as _json
    kv = {"gym_media_use:pierce:2026-08-27": _json.dumps({
        "asset_id": "old", "gym_id": "pierce", "prev_used_count": 0,
        "prev_last_used_at": None, "staged_at": NOW.isoformat(), "rolled_back": False})}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    store = FakeMediaStore(assets=[make_asset("old", used_count=1)])
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is True
    assert store.assets["old"]["used_count"] == 0


def test_deny_never_rolls_back_the_same_photos_earlier_published_use(monkeypatch, tmp_path):
    """A denied post must NOT undo the same asset's EARLIER, still-published use.

    Use records are never cleared on publish, so an asset re-staged after its 90-day
    cooldown carries both records. A cross-date rollback would restore the live post's
    counters and hand a photo that is currently on the gym's feed straight back to the
    pool. on_draft_denied must stay scoped to the denied draft's own date."""
    import json as _json
    from datetime import timedelta
    from agent import db as _db
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    later = NOW + timedelta(days=95)
    store = FakeMediaStore(assets=[make_asset("a", used_count=0, last_used_at=None)])
    # Day 1: staged and PUBLISHED (its record stays un-rolled forever — nothing
    # clears a use-record on publish).
    sel.stamp_use(store.get_asset("a"), "pierce", "2026-01-01", store=store, now=NOW)
    # Day 95: the cooldown has passed, so the same photo is legitimately re-staged.
    sel.stamp_use(store.get_asset("a"), "pierce", "2026-04-05", store=store, now=later)
    assert store.assets["a"]["used_count"] == 2

    class _Draft:
        draft_type = "gym_media"
        day_key = "2026-04-05"
        account_key = "pierce_ig"
        source_media_asset_id = "a"

    assert sel.on_draft_denied(_Draft(), store=store) is True

    # THE DISCRIMINATING ASSERTION: the PUBLISHED day-1 record must still be
    # un-rolled. A cross-date rollback flips it to True — and because that record's
    # prev_ values are the pre-publish ones, the live photo is handed back to the
    # pool as if it had never run. (used_count alone does NOT catch this: both
    # orderings happen to land on 1.)
    day1 = _json.loads(_db.kv_get("gym_media_use:pierce:2026-01-01", "[]"))
    assert day1 and day1[0]["rolled_back"] is False, \
        "the live published use was rolled back — a posted photo returned to the pool"
    # The denied day's own record IS rolled back, and the asset keeps the published use.
    day95 = _json.loads(_db.kv_get("gym_media_use:pierce:2026-04-05", "[]"))
    assert day95 and day95[0]["rolled_back"] is True
    assert store.assets["a"]["used_count"] == 1
    assert store.assets["a"]["last_used_at"] == NOW.isoformat(), \
        "the asset must fall back to its PUBLISHED day-1 timestamp, not to never-used"


def test_observe_denials_works_on_LIVE_SHAPED_rows(monkeypatch, tmp_path):
    """THE DEAD BACKSTOP. This sweep filtered candidate rows with
    draft_type == 'gym_media', but content_calendar has NO draft_type column: it
    reads None on every live row (measured across 229 ENG rows, 2026-08-30). So the
    filter was ALWAYS empty and this sweep has never rolled a single asset back
    since it was written. The sibling podcast_selector.observe_denials was written
    correctly against `pillar` and this one was simply never ported, which meant the
    portal-deny rollback had no second line of defence at all. A denied photo's real
    signal is a non-empty source_media_asset_id."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce")])
    sel.stamp_use(store.get_asset("a1"), "pierce", "2026-08-27",
                  store=store, now=NOW)
    assert store.assets["a1"]["used_count"] == 1

    def fetch_rows(gym_id, post_date):
        # exactly the shape the live table returns: no draft_type key at all
        return [{"id": "r1", "status": "denied", "pillar": "faces",
                 "source_media_asset_id": "a1"}]

    summary = sel.observe_denials(store=store, fetch_rows=fetch_rows)
    assert summary["rolled_back"] == 1, "the nightly backstop is still a no-op"
    assert store.assets["a1"]["used_count"] == 0
    # idempotent: a second sweep rolls nothing twice
    assert sel.observe_denials(store=store, fetch_rows=fetch_rows)["rolled_back"] == 0


def test_observe_denials_leaves_a_generated_row_alone(monkeypatch, tmp_path):
    """A generated pillar carries no source_media_asset_id, so there is no photo to
    return. Rolling one back would re-pool an asset this row never used."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo2.db"))
    store = FakeMediaStore(assets=[make_asset("a1", gym_id="pierce")])
    sel.stamp_use(store.get_asset("a1"), "pierce", "2026-08-27",
                  store=store, now=NOW)

    def fetch_rows(gym_id, post_date):
        return [{"id": "r1", "status": "denied", "pillar": "about",
                 "source_media_asset_id": ""}]

    assert sel.observe_denials(store=store, fetch_rows=fetch_rows)["rolled_back"] == 0
    assert store.assets["a1"]["used_count"] == 1
