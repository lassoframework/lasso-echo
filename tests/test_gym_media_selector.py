"""gym_media_drive §6: selection order, 90-day cooldown + never-twice-a-month,
excluded never selectable, empty pool alert, deny rollback, tenant isolation."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import gym_media_selector as sel  # noqa: E402
from tests.gym_media_fakes import FakeMediaStore, make_asset  # noqa: E402

NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)


def reviewed_asset(*args, **kwargs):
    row = make_asset(*args, **kwargs)
    row.update(review_status="approved", reviewed_by="operator",
               reviewed_at="2026-08-26T00:00:00Z", moderation_status="clean",
               consent_status="not_required")
    return row


def test_drive_claim_hides_asset_from_every_selection_path(monkeypatch, tmp_path):
    from agent import db
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    store = FakeMediaStore(assets=[
        reviewed_asset("claimed", content_hash="a" * 64),
        reviewed_asset("free", content_hash="b" * 64)])
    claim_id = sel.drive_asset_claim_id("pierce", "claimed")
    assert db.socialapi_claim(claim_id, "pierce_gbp")[0] == "won"
    assert [a["id"] for a in sel.pickable("pierce", store=store)] == ["free"]
    assert [a["id"] for a in sel.cooldown_fallback("pierce", store=store)] == ["free"]
    db.socialapi_claim_done(claim_id, "pierce_gbp", "claimed")
    assert [a["id"] for a in sel.pickable("pierce", store=store)] == ["free"]


def test_claim_hides_same_byte_alias_and_unreadable_claim_closes_pool(monkeypatch,
                                                                    tmp_path):
    from agent import db
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    store = FakeMediaStore(assets=[
        reviewed_asset("claimed", content_hash="a" * 64),
        reviewed_asset("alias", content_hash="a" * 64),
        reviewed_asset("free", content_hash="b" * 64)])
    claim_id = sel.drive_asset_claim_id("pierce", "claimed")
    assert db.socialapi_claim(claim_id, "pierce_gbp")[0] == "won"
    for select in (sel.pickable, sel.cooldown_fallback):
        assert [a["id"] for a in select("pierce", store=store)] == ["free"]
    store.assets["claimed"].pop("content_hash")
    for select in (sel.pickable, sel.cooldown_fallback):
        assert select("pierce", store=store) == []
    store.assets.pop("claimed")
    for select in (sel.pickable, sel.cooldown_fallback):
        assert select("pierce", store=store) == []


def test_legacy_id_claim_blocks_new_canonical_alias_claim(monkeypatch, tmp_path):
    from agent import db
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    original = reviewed_asset("old-id", content_hash="a" * 64)
    alias = reviewed_asset("new-id", content_hash="a" * 64)
    store = FakeMediaStore(assets=[original, alias])
    assert db.socialapi_claim(
        sel.drive_asset_claim_id("pierce", "old-id"), "pierce_gbp")[0] == "won"
    assert sel.claim_drive_content("pierce", alias, store) is None


def test_claim_requires_verified_hash(monkeypatch, tmp_path):
    import pytest
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    asset = reviewed_asset("no-hash", content_hash="unverified")
    store = FakeMediaStore(assets=[asset])
    with pytest.raises(ValueError, match="verified content hash"):
        sel.claim_drive_content("pierce", asset, store)


def test_claim_read_failure_closes_drive_pool(monkeypatch):
    from agent import db
    store = FakeMediaStore(assets=[reviewed_asset("free")])
    monkeypatch.setattr(db, "drive_asset_claimed_ids",
                        lambda *_: (_ for _ in ()).throw(OSError("db down")))
    assert sel.pickable("pierce", store=store) == []
    assert sel.cooldown_fallback("pierce", store=store) == []


def test_picks_least_used_longest_unused():
    store = FakeMediaStore(assets=[
        reviewed_asset("a", used_count=3, last_used_at="2026-01-01T00:00:00+00:00"),
        reviewed_asset("b", used_count=0, last_used_at=None),
        reviewed_asset("c", used_count=1, last_used_at="2026-02-01T00:00:00+00:00"),
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


def test_once_used_asset_never_auto_selected_even_after_cooldown():
    """GLOBAL ONCE-USED RULE (2026-10-02): used_count > 0 is out forever, even
    years after the 90-day cooldown and the calendar-month guard have passed."""
    store = FakeMediaStore(assets=[
        reviewed_asset("used-long-ago", used_count=1,
                       last_used_at=(NOW - timedelta(days=900)).isoformat()),
        reviewed_asset("fresh", used_count=0, last_used_at=None),
    ])
    assert sel.pick_media("pierce", store=store, now=NOW)["id"] == "fresh"
    # Only the used asset remains: the pool is empty, it is NOT re-selected.
    store2 = FakeMediaStore(assets=[store.assets["used-long-ago"]])
    assert sel.pickable("pierce", store=store2, now=NOW) == []
    assert sel.pick_media("pierce", store=store2, now=NOW) is None


def test_cooldown_fallback_cannot_bypass_once_used_rule():
    """The explicit swap lane skips the cooldown clocks but must never re-offer
    an asset that has already been staged (used_count > 0)."""
    store = FakeMediaStore(assets=[
        reviewed_asset("oldest", gym_id="swiftrivercrossfit", used_count=4,
                       last_used_at=(NOW - timedelta(days=70)).isoformat()),
        reviewed_asset("newer", gym_id="swiftrivercrossfit", used_count=1,
                       last_used_at=(NOW - timedelta(days=10)).isoformat()),
        reviewed_asset("on-book", gym_id="swiftrivercrossfit", used_count=2,
                       last_used_at=(NOW - timedelta(days=80)).isoformat()),
    ])
    assert sel.cooldown_fallback(
        "swiftrivercrossfit", store=store, exclude_ids=("on-book",)) == []
    # An unused (never-staged) asset is still eligible for the explicit lane.
    store.assets["virgin"] = reviewed_asset(
        "virgin", gym_id="swiftrivercrossfit", used_count=0, last_used_at=None)
    got = sel.cooldown_fallback(
        "swiftrivercrossfit", store=store, exclude_ids=("on-book",))
    assert [a["id"] for a in got] == ["virgin"]


def test_reuploaded_same_bytes_remain_used_and_other_tenant_does_not_leak():
    digest = "a" * 32
    store = FakeMediaStore(assets=[
        reviewed_asset("original", used_count=1, content_hash=digest),
        reviewed_asset("reupload", used_count=0, content_hash=digest),
        reviewed_asset("other-gym", gym_id="elsewhere", used_count=1,
                       content_hash="b" * 32),
        reviewed_asset("fresh", used_count=0, content_hash="b" * 32),
    ])
    assert [a["id"] for a in sel.pickable("pierce", store=store, now=NOW)] == ["fresh"]
    assert [a["id"] for a in sel.cooldown_fallback("pierce", store=store)] == ["fresh"]


def test_timestamp_or_invalid_counter_blocks_reuse():
    store = FakeMediaStore(assets=[
        reviewed_asset("timestamp", used_count=0,
                       last_used_at="2020-01-01T00:00:00+00:00"),
        reviewed_asset("bad-count", used_count="invalid"),
    ])
    assert sel.pickable("pierce", store=store, now=NOW) == []
    assert sel.cooldown_fallback("pierce", store=store) == []


def test_explicit_nine_month_client_never_gets_cooldown_fallback():
    store = FakeMediaStore(assets=[
        reviewed_asset("recent", gym_id="zanshinfitness630e22",
                       last_used_at=(NOW - timedelta(days=100)).isoformat())])
    assert sel.cooldown_fallback("zanshinfitness630e22", store=store) == []


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
        reviewed_asset("v1", kind="video"),
        reviewed_asset("p1", kind="photo"),
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


def test_stamp_and_deny_settle(monkeypatch):
    """PERMANENT STAGE-USE (2026-10-02): a coach deny settles the use-record but
    NEVER restores the counters — once staged, a photo is never offered again."""
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    store = FakeMediaStore(assets=[make_asset("a", used_count=0, last_used_at=None)])
    asset = store.get_asset("a")
    sel.stamp_use(asset, "pierce", "2026-08-27", store=store, now=NOW)
    assert store.assets["a"]["used_count"] == 1
    assert store.assets["a"]["last_used_at"] == NOW.isoformat()
    # Deny settles the record; the stamp stays so the asset stays out of the pool.
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is True
    assert store.assets["a"]["used_count"] == 1
    assert store.assets["a"]["last_used_at"] == NOW.isoformat()
    assert sel.pickable("pierce", store=store, now=NOW) == []
    # Idempotent second settle.
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is False


def test_abandoned_before_calendar_insert_restores_unused_photo(monkeypatch):
    kv = {}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    store = FakeMediaStore(assets=[make_asset("a", used_count=0, last_used_at=None)])
    sel.stamp_use(store.get_asset("a"), "pierce", "2026-08-27", store=store, now=NOW)
    assert sel.rollback_use("pierce", "2026-08-27", store=store,
                            asset_id="a", restore_unstaged=True) is True
    assert store.assets["a"]["used_count"] == 0
    assert store.assets["a"]["last_used_at"] is None
    assert sel.rollback_use("pierce", "2026-08-27", store=store,
                            asset_id="a", restore_unstaged=True) is False


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
    # Both records settled; both stamps stay (stage-use is permanent).
    assert store.assets["am"]["used_count"] == 1
    assert store.assets["pm"]["used_count"] == 1
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
    # The hidden asset's record is settled but its stamp is permanent: it can
    # never be re-offered even if the coach later un-hides it.
    assert store.assets["pm"]["used_count"] == 1
    assert store.assets["am"]["used_count"] == 1, "the standing post lost its stamp"
    # And the day's remaining record still settles on a full deny.
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is True
    assert store.assets["am"]["used_count"] == 1


def test_legacy_single_dict_record_still_settles(monkeypatch):
    """Records written before the list format (a bare dict) must still settle."""
    import json as _json
    kv = {"gym_media_use:pierce:2026-08-27": _json.dumps({
        "asset_id": "old", "gym_id": "pierce", "prev_used_count": 0,
        "prev_last_used_at": None, "staged_at": NOW.isoformat(), "rolled_back": False})}
    monkeypatch.setattr("agent.db.kv_get", lambda k, d="": kv.get(k, d))
    monkeypatch.setattr("agent.db.kv_set", lambda k, v: kv.__setitem__(k, v))
    store = FakeMediaStore(assets=[make_asset("old", used_count=1)])
    assert sel.rollback_use("pierce", "2026-08-27", store=store) is True
    assert store.assets["old"]["used_count"] == 1


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
    # The denied day's own record IS settled, and the stamp is permanent: the
    # counters stay exactly as the day-95 staging left them. Published history is
    # never rewritten and the denied asset never returns to the pool.
    day95 = _json.loads(_db.kv_get("gym_media_use:pierce:2026-04-05", "[]"))
    assert day95 and day95[0]["rolled_back"] is True
    assert store.assets["a"]["used_count"] == 2
    assert store.assets["a"]["last_used_at"] == later.isoformat()
    assert sel.pickable("pierce", store=store, now=later) == []


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
    # The record is settled; the stamp stays — a denied photo is never re-offered.
    assert store.assets["a1"]["used_count"] == 1
    assert sel.pickable("pierce", store=store, now=NOW) == []
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


def test_empty_pool_alerts_for_a_publishing_gym(monkeypatch, tmp_path):
    """A live gym's empty photo pool is a real problem: staff still get the one
    deduped 'ask for photos' alert."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo3.db"))
    monkeypatch.setattr(sel, "_publishing_gym", lambda base: True)
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append(k) or True)
    assert sel.pick_media("pierce", store=FakeMediaStore(assets=[]), now=NOW) is None
    assert fired == ["pool_empty:pierce"]


def test_empty_pool_stays_quiet_while_a_gym_is_onboarding(monkeypatch, tmp_path):
    """A gym whose publish flag is still OFF has an empty pool by definition —
    paging staff every build buries the alerts that matter. Selection behavior is
    unchanged (still None); only the alert is withheld."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo4.db"))
    monkeypatch.setattr(sel, "_publishing_gym", lambda base: False)
    fired = []
    monkeypatch.setattr("agent.gym_media_index.dedup_alert",
                        lambda k, m: fired.append(k) or True)
    assert sel.pick_media("newgym", store=FakeMediaStore(assets=[]), now=NOW) is None
    assert fired == []


def test_publishing_gym_fails_loud_when_the_registry_cannot_answer(monkeypatch):
    """Unknown gym or a broken lookup must answer True: a live gym's empty pool is
    never silently swallowed."""
    monkeypatch.setattr("agent.db.gym_get", lambda *a, **k: None)
    assert sel._publishing_gym("nobody") is True

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("agent.db.gym_get", boom)
    assert sel._publishing_gym("pierce") is True
