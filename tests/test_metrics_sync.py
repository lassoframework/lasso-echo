"""tests/test_metrics_sync.py — Wave 7.1 metrics ingestion.

Required by spec:
  1. snapshot dedupe by platformPostId across duplicate accounts (the same post
     under two account ids -> ONE row)
  2. external flagging (isExternal=true / no calendar match -> external=true,
     calendar_id null)
  3. calendar join via late_post_id, then the platformPostId fallback
  4. flag OFF -> no-op (nothing pulled, nothing written)
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import metrics_sync

NOW = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)


def _post(zid, ppid, published, account_id="acct_a", external=False, likes=10):
    return {
        "_id": zid,
        "platform": "instagram",
        "platformPostId": ppid,
        "publishedAt": published,
        "accountId": account_id,
        "isExternal": external,
        "mediaProductType": "FEED",
        "analytics": {"likes": likes, "reach": 200, "comments": 2},
    }


_ACCOUNTS = [
    {"_id": "acct_a", "platform": "instagram", "followersCount": 1500},
    {"_id": "acct_b", "platform": "instagram", "followersCount": 1500},
]


class FakeZernio:
    def __init__(self, analytics_json, profile_id="prof1"):
        self.analytics_json = analytics_json
        self.profile_id = profile_id
        self.calls = []

    def find_profile_id(self, name):
        return self.profile_id

    def analytics_window(self, profile_id, days, source=None, **kwargs):
        self.calls.append({"profile_id": profile_id, "days": days, "source": source})
        return self.analytics_json


class FakeStore:
    """calendar_matches maps a lookup value (late_post_id OR platformPostId)
    -> calendar row, mirroring the real store's late_post_id-first join."""

    def __init__(self, calendar_matches=None):
        self.calendar_matches = calendar_matches or {}
        self.inserted = []
        self.calendar_lookups = []

    def existing_days(self, gym_id, platform, platform_post_id):
        return set()

    def find_calendar(self, gym_id, late_post_id=None, platform_post_id=None):
        self.calendar_lookups.append((late_post_id, platform_post_id))
        for value in (late_post_id, platform_post_id):
            if value and value in self.calendar_matches:
                return self.calendar_matches[value]
        return None

    def insert_metrics(self, rows):
        self.inserted.extend(rows)
        return len(rows)


def _run(monkeypatch, aj, store=None, flag="true"):
    monkeypatch.setenv("AGENT_METRICS_SYNC", flag)
    store = store or FakeStore()
    zernio = FakeZernio(aj)
    result = metrics_sync.run(gyms=["gym1"], now=NOW, zernio=zernio, store=store)
    return result, store, zernio


# ---------------------------------------------------------------------------
# 1. Dedupe by platformPostId across duplicate accounts -> one row wins
# ---------------------------------------------------------------------------

def test_duplicate_account_same_post_yields_one_row(monkeypatch):
    """The duplicate lassoframework IG connection returns the SAME post under
    two account ids. Exactly ONE post_metrics row per snapshot day survives."""
    published = "2026-08-18T15:00:00.000Z"  # age ~7.5 days -> day-7 snapshot due
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [
              _post("zern_a", "ig_post_1", published, account_id="acct_a"),
              _post("zern_b", "ig_post_1", published, account_id="acct_b"),
          ]}
    result, store, _ = _run(monkeypatch, aj)
    assert result["ok"] is True
    keys = [(r["platform"], r["platform_post_id"], r["snapshot_day"])
            for r in store.inserted]
    assert keys == [("instagram", "ig_post_1", 7)]
    assert len(set(keys)) == len(keys)


def test_dedupe_posts_pure_function():
    posts = [
        _post("a", "same_id", "2026-08-18T15:00:00Z", account_id="acct_a"),
        _post("b", "same_id", "2026-08-18T15:00:00Z", account_id="acct_b"),
        _post("c", "other_id", "2026-08-18T15:00:00Z"),
    ]
    deduped = metrics_sync.dedupe_posts(posts)
    assert [p["platformPostId"] for p in deduped] == ["same_id", "other_id"]


# ---------------------------------------------------------------------------
# 2. External flagging: no calendar match -> external=true, calendar_id null
# ---------------------------------------------------------------------------

def test_external_post_flagged_and_never_carries_calendar_id(monkeypatch):
    published = "2026-08-18T15:00:00.000Z"
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_post("zern_x", "ext_post_9", published, external=True)]}
    result, store, _ = _run(monkeypatch, aj)
    assert result["ok"] is True
    assert len(store.inserted) == 1
    row = store.inserted[0]
    assert row["external"] is True
    assert row["calendar_id"] is None
    # external rows carry NO calendar levers (nothing invented for a post
    # Echo did not shape)
    assert row["pillar"] is None
    assert row["hook_family"] is None


# ---------------------------------------------------------------------------
# 3. Calendar join: late_post_id first, platformPostId fallback
# ---------------------------------------------------------------------------

def test_calendar_join_via_late_post_id_then_platform_post_id_fallback(monkeypatch):
    published = "2026-08-18T15:00:00.000Z"
    cal_a = {"id": "11111111-1111-1111-1111-111111111111",
             "pillar": "community", "format": "feed",
             "hook_family": "question", "ask_type": "booking_link",
             "time_slot": "morning", "caption_len_band": "mid"}
    cal_b = {"id": "22222222-2222-2222-2222-222222222222",
             "pillar": "proof", "format": "feed",
             "hook_family": "story_open", "ask_type": "dm",
             "time_slot": "evening", "caption_len_band": "short"}
    store = FakeStore(calendar_matches={
        "zern_1": cal_a,       # matched by late_post_id (the Zernio post _id)
        "ig_post_2": cal_b,    # matched only by platformPostId (the fallback)
    })
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [
              _post("zern_1", "ig_post_1", published),
              _post("zern_2", "ig_post_2", published),
          ]}
    result, store, _ = _run(monkeypatch, aj, store=store)
    assert result["ok"] is True
    by_pid = {r["platform_post_id"]: r for r in store.inserted}
    assert by_pid["ig_post_1"]["calendar_id"] == cal_a["id"]
    assert by_pid["ig_post_1"]["external"] is False
    assert by_pid["ig_post_1"]["hook_family"] == "question"
    assert by_pid["ig_post_2"]["calendar_id"] == cal_b["id"]
    assert by_pid["ig_post_2"]["external"] is False
    assert by_pid["ig_post_2"]["hook_family"] == "story_open"
    # both lookup values were offered on every join attempt
    assert ("zern_1", "ig_post_1") in store.calendar_lookups


# ---------------------------------------------------------------------------
# 4. Flag OFF -> no-op
# ---------------------------------------------------------------------------

def test_flag_off_is_a_noop(monkeypatch):
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_post("z", "p", "2026-08-18T15:00:00.000Z")]}
    result, store, zernio = _run(monkeypatch, aj, flag="false")
    assert result["ok"] is False
    assert "AGENT_METRICS_SYNC" in result["reason"]
    assert store.inserted == []
    assert zernio.calls == []


# ---------------------------------------------------------------------------
# supporting honesty checks
# ---------------------------------------------------------------------------

def test_pull_uses_source_all(monkeypatch):
    """Blake's rail: metrics come from Zernio analytics with source=all so
    external posts arrive too."""
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS, "posts": []}
    _, _, zernio = _run(monkeypatch, aj)
    assert zernio.calls and zernio.calls[0]["source"] == "all"


def test_due_snapshot_days_honest_aging():
    now = NOW
    # 7.5 days old -> only the day-7 snapshot is due (day 1 and 3 are gone —
    # a stale pull may never masquerade as an early snapshot)
    assert metrics_sync.due_snapshot_days("2026-08-18T15:00:00Z", now) == [7]
    # 1.1 days old -> day-1 due
    assert metrics_sync.due_snapshot_days("2026-08-24T22:00:00Z", now) == [1]
    # already recorded -> nothing due
    assert metrics_sync.due_snapshot_days(
        "2026-08-18T15:00:00Z", now, existing_days={7}) == []
    # unparseable -> nothing due, never guessed
    assert metrics_sync.due_snapshot_days(None, now) == []


def test_missing_metric_stays_null_not_zero(monkeypatch):
    published = "2026-08-18T15:00:00.000Z"
    post = _post("z1", "p1", published)
    post["analytics"] = {"likes": 5}  # no reach/saves/shares reported
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS, "posts": [post]}
    _, store, _ = _run(monkeypatch, aj)
    row = store.inserted[0]
    assert row["likes"] == 5
    assert row["saves"] is None
    assert row["shares"] is None
    assert row["reach"] is None
