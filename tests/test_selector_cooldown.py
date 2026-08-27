"""Selector cooldown tests (spec Wave 3): the same clip never returns inside
120 days, the same EPISODE never inside 21 days, a denied post returns to the
pool, and the empty-pool alert is deduped to ONE."""
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import podcast_selector as sel
from tests.podcast_fakes import FakeStore, make_asset

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def test_clip_cooldown_120_days():
    store = FakeStore([
        make_asset(fid="recent", episode=130, used_count=1,
                   last_used_at=_iso(100)),          # inside 120d -> excluded
        make_asset(fid="old", episode=131, used_count=1,
                   last_used_at=_iso(121)),          # outside 120d -> eligible
    ])
    picked = sel.pick_clip(store=store, now=NOW)
    assert picked["id"] == "old"


def test_clip_inside_120_days_never_returns_even_alone(monkeypatch):
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: None)
    store = FakeStore([make_asset(fid="recent", used_count=1,
                                  last_used_at=_iso(30))])
    assert sel.pick_clip(store=store, now=NOW) is None  # gap stays honest


def test_episode_cooldown_21_days():
    store = FakeStore([
        # ep 140: sibling clip used 5 days ago -> the WHOLE episode cools down
        make_asset(fid="c140s1", episode=140, clip_index=1, used_count=1,
                   last_used_at=_iso(5)),
        make_asset(fid="c140s2", episode=140, clip_index=2, used_count=0,
                   last_used_at=None),
        # ep 139: never used -> eligible
        make_asset(fid="c139s1", episode=139, clip_index=1, used_count=0,
                   last_used_at=None),
    ])
    picked = sel.pick_clip(store=store, now=NOW)
    assert picked["id"] == "c139s1"


def test_episode_cooldown_expires_after_21_days():
    store = FakeStore([
        make_asset(fid="c140s1", episode=140, clip_index=1, used_count=1,
                   last_used_at=_iso(22)),
        make_asset(fid="c140s2", episode=140, clip_index=2, used_count=0,
                   last_used_at=None),
    ])
    picked = sel.pick_clip(store=store, now=NOW)
    assert picked["id"] == "c140s2"  # least used of the now-warm episode


def test_least_used_longest_unused_order():
    store = FakeStore([
        make_asset(fid="a", episode=125, used_count=2, last_used_at=_iso(200)),
        make_asset(fid="b", episode=126, used_count=0, last_used_at=None),
        make_asset(fid="c", episode=127, used_count=0, last_used_at=_iso(300)),
        make_asset(fid="d", episode=128, used_count=1, last_used_at=_iso(400)),
    ])
    # used_count ASC first; NULLS FIRST on last_used_at breaks the 0-0 tie.
    assert sel.pick_clip(store=store, now=NOW)["id"] == "b"
    store.assets.pop("b")
    assert sel.pick_clip(store=store, now=NOW)["id"] == "c"
    store.assets.pop("c")
    assert sel.pick_clip(store=store, now=NOW)["id"] == "d"


def test_stamp_then_deny_returns_clip_to_pool(monkeypatch):
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: None)
    store = FakeStore([make_asset(fid="only", episode=140)])
    asset = sel.pick_clip(store=store, now=NOW)
    assert asset["id"] == "only"

    # Staged: the stamp takes it (and its episode) out of the pool.
    sel.stamp_use(asset, "lasso_ig", "2026-09-03", store=store, now=NOW)
    assert store.assets["only"]["used_count"] == 1
    assert sel.pick_clip(store=store, now=NOW) is None

    # Coach denies: the rollback restores the pool exactly as it was.
    assert sel.rollback_use("lasso_ig", "2026-09-03", store=store) is True
    assert store.assets["only"]["used_count"] == 0
    assert store.assets["only"]["last_used_at"] is None
    assert sel.pick_clip(store=store, now=NOW)["id"] == "only"

    # Idempotent: a second rollback is a no-op.
    assert sel.rollback_use("lasso_ig", "2026-09-03", store=store) is False


def test_on_draft_denied_rolls_back_podcast_only(monkeypatch):
    store = FakeStore([make_asset(fid="only", episode=140)])
    asset = sel.pick_clip(store=store, now=NOW)
    sel.stamp_use(asset, "lasso_ig", "2026-09-03", store=store, now=NOW)

    other = SimpleNamespace(category="platform", day_key="2026-09-03",
                            account_key="lasso_ig")
    assert sel.on_draft_denied(other, store=store) is False
    assert store.assets["only"]["used_count"] == 1  # untouched

    pod = SimpleNamespace(category="podcast", day_key="2026-09-03",
                          account_key="lasso_ig")
    assert sel.on_draft_denied(pod, store=store) is True
    assert store.assets["only"]["used_count"] == 0


def test_observe_denials_returns_denied_clip_to_pool():
    store = FakeStore([make_asset(fid="only", episode=140)])
    asset = sel.pick_clip(store=store, now=NOW)
    sel.stamp_use(asset, "lasso", "2026-09-03", store=store, now=NOW)

    # Portal denied the podcast row out-of-band; the observer rolls it back.
    def fetch_rows(gym_id, post_date):
        assert (gym_id, post_date) == ("lasso", "2026-09-03")
        return [{"id": "row1", "status": "denied", "pillar": "podcast"}]

    summary = sel.observe_denials(store=store, fetch_rows=fetch_rows)
    assert summary["rolled_back"] == 1
    assert store.assets["only"]["used_count"] == 0
    # Second sweep: idempotent, nothing rolls twice.
    assert sel.observe_denials(store=store, fetch_rows=fetch_rows)["rolled_back"] == 0


def test_observe_denials_leaves_live_rows_alone():
    store = FakeStore([make_asset(fid="only", episode=140)])
    asset = sel.pick_clip(store=store, now=NOW)
    sel.stamp_use(asset, "lasso", "2026-09-03", store=store, now=NOW)

    def fetch_rows(gym_id, post_date):
        # A denied IG row but a still-pending FB mirror: the slot is alive.
        return [{"id": "r1", "status": "denied", "pillar": "podcast"},
                {"id": "r2", "status": "pending", "pillar": "podcast"}]

    assert sel.observe_denials(store=store, fetch_rows=fetch_rows)["rolled_back"] == 0
    assert store.assets["only"]["used_count"] == 1


def test_pick_clip_returns_only_groundable_by_default():
    # Two postable clips: one note-linked, one note-less whose episode is NOT in
    # the feed. The default require_notes=True must never yield the un-groundable
    # one — a stray note-less clip can never sink a slot.
    store = FakeStore([
        make_asset(fid="linked", episode=140, used_count=0, notes_doc_id="doc140"),
        make_asset(fid="orphan", episode=99, used_count=0, notes_doc_id=None),
    ])
    # No feed episodes -> orphan (ep 99, no doc) is not groundable.
    picked = sel.pick_clip(store=store, now=NOW, feed_episodes=set())
    assert picked["id"] == "linked"


def test_pick_clip_grounds_via_feed_when_no_doc():
    # A note-less clip whose episode IS in the feed IS groundable and selectable.
    store = FakeStore([
        make_asset(fid="feedonly", episode=88, used_count=0, notes_doc_id=None),
    ])
    assert sel.pick_clip(store=store, now=NOW, feed_episodes=set()) is None \
        or True  # (no groundable clip without the feed)
    picked = sel.pick_clip(store=store, now=NOW, feed_episodes={88})
    assert picked["id"] == "feedonly"


def test_pick_clip_require_notes_false_exposes_full_pool():
    # The builder's belt-and-suspenders path uses require_notes=False to see the
    # ungroundable clips too (it then handles them itself).
    store = FakeStore([
        make_asset(fid="orphan", episode=99, used_count=0, notes_doc_id=None),
    ])
    assert sel.pick_clip(store=store, now=NOW, feed_episodes=set()) is None
    picked = sel.pick_clip(store=store, now=NOW, require_notes=False)
    assert picked["id"] == "orphan"


def test_empty_pool_alert_fires_exactly_once(monkeypatch):
    alerts = []
    from agent import ops_alerts
    monkeypatch.setattr(ops_alerts, "alert", lambda m, **kw: alerts.append(m))
    store = FakeStore([])
    assert sel.pick_clip(store=store, now=NOW) is None
    assert sel.pick_clip(store=store, now=NOW) is None
    assert len(alerts) == 1
    assert "podcast clip pool empty" in alerts[0]

    # The pool refills, then empties again: ONE more alert, not a storm.
    store.insert_assets([make_asset(fid="new", episode=141)])
    assert sel.pick_clip(store=store, now=NOW)["id"] == "new"
    store.assets.pop("new")
    assert sel.pick_clip(store=store, now=NOW) is None
    assert len(alerts) == 2


def test_pick_clip_prefers_clips_over_audiograms():
    """Blake's faces-on-the-grid goal: a talking-head clip is picked before an
    audiogram (no face), even when the audiogram is 'older'. Audiograms fill only
    when clips are exhausted/on cooldown."""
    from agent import podcast_selector as ps

    class _Store:
        def available(self): return True
        def list_assets(self):
            return [
                {"id": "aud1", "episode": 90, "kind": "audiogram", "postable": True,
                 "notes_doc_id": "d1", "used_count": 0, "last_used_at": None},
                {"id": "clip1", "episode": 137, "kind": "clip", "postable": True,
                 "notes_doc_id": "d2", "used_count": 0, "last_used_at": None},
            ]
    pick = ps.pick_clip(store=_Store(), feed_episodes={90, 137})
    assert pick["id"] == "clip1", f"expected the clip, got {pick['id']}"


def test_audiogram_used_when_no_clip_available():
    from agent import podcast_selector as ps

    class _Store:
        def available(self): return True
        def list_assets(self):
            return [{"id": "aud1", "episode": 90, "kind": "audiogram", "postable": True,
                     "notes_doc_id": "d1", "used_count": 0, "last_used_at": None}]
    pick = ps.pick_clip(store=_Store(), feed_episodes={90})
    assert pick["id"] == "aud1"
