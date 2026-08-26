"""tests/test_metrics_sync.py — Wave 7.1 metrics ingestion.

Fixtures mirror the REAL Zernio shapes (probed live 2026-08-26):
  * analytics records carry platformPostId NULL at the top level; the real ids
    live in platforms[] (one entry per platform, each with its own analytics)
  * isExternal comes back true even for posts Echo itself published, so it is
    NEVER the classifier — external is the JOIN's verdict alone
  * the record `_id` is NOT the Zernio post id late_post_id stores; the bridge
    is the profile's Zernio-created posts (platformPostId -> post _id)

Required by spec:
  1. snapshot dedupe by platformPostId across duplicate accounts (the same
     entry under two account ids -> ONE row)
  2. an entry that maps (via the zernio posts map) to a calendar late_post_id
     lands external=false with calendar_id set — EVEN when isExternal=true
  3. an entry with no map hit lands external=true, calendar_id null
  4. a cross-posted record (2 platform entries) -> 2 rows, distinct ids
  5. flag OFF -> no-op (nothing pulled, nothing written)
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import metrics_sync

NOW = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)


def _entry(ppid, platform="instagram", account_id="acct_a", likes=10):
    """One platforms[] entry of an ANALYTICS record (real shape: per-entry
    platformPostId + analytics; accountId is a plain string here)."""
    return {
        "platform": platform,
        "platformPostId": ppid,
        "accountId": account_id,
        "accountUsername": "gym_handle",
        "syncStatus": "completed",
        "platformPostUrl": f"https://instagram.com/p/{ppid}",
        "analytics": {"likes": likes, "reach": 200, "comments": 2},
    }


def _record(zid, entries, published, external=True):
    """One analytics record. REAL shape: top-level platformPostId is NULL and
    isExternal=true even for Echo's own posts."""
    return {
        "_id": zid,
        "platform": (entries[0]["platform"] if entries else "instagram"),
        "platformPostId": None,
        "publishedAt": published,
        "isExternal": external,
        "mediaProductType": "FEED",
        "analytics": {"likes": 1, "reach": 5},  # record-level fallback only
        "platforms": entries,
    }


def _zpost(zernio_id, entries):
    """One Zernio-CREATED post (GET /v1/posts shape): `_id` is what
    content_calendar.late_post_id stores; accountId comes back POPULATED."""
    return {
        "_id": zernio_id,
        "status": "published",
        "scheduledFor": "2026-08-18T15:00:00.000Z",
        "platforms": [
            {"platform": e["platform"], "platformPostId": e["platformPostId"],
             "accountId": {"_id": e["accountId"], "platform": e["platform"]}}
            for e in entries
        ],
    }


_ACCOUNTS = [
    {"_id": "acct_a", "platform": "instagram", "followersCount": 1500},
    {"_id": "acct_b", "platform": "instagram", "followersCount": 1500},
    {"_id": "acct_fb", "platform": "facebook", "followersCount": 900},
]

PUBLISHED = "2026-08-18T15:00:00.000Z"  # age ~7.5 days -> day-7 snapshot due


class FakeZernio:
    def __init__(self, analytics_json, zernio_posts=None, profile_id="prof1"):
        self.analytics_json = analytics_json
        self.zernio_posts = zernio_posts or []
        self.profile_id = profile_id
        self.calls = []
        self.posts_calls = []

    def find_profile_id(self, name):
        return self.profile_id

    def analytics_window(self, profile_id, days, source=None, **kwargs):
        self.calls.append({"profile_id": profile_id, "days": days, "source": source})
        return self.analytics_json

    def posts_window(self, profile_id, days, **kwargs):
        self.posts_calls.append({"profile_id": profile_id, "days": days})
        return self.zernio_posts


class FakeStore:
    """calendar_matches maps a lookup value (a Zernio post id from the posts
    map, OR a raw platformPostId for legacy rows) -> calendar row, mirroring
    the real store's late_post_id-first join."""

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


def _run(monkeypatch, aj, store=None, zernio_posts=None, flag="true"):
    monkeypatch.setenv("AGENT_METRICS_SYNC", flag)
    store = store or FakeStore()
    zernio = FakeZernio(aj, zernio_posts=zernio_posts)
    result = metrics_sync.run(gyms=["gym1"], now=NOW, zernio=zernio, store=store)
    return result, store, zernio


_CAL = {"id": "11111111-1111-1111-1111-111111111111",
        "pillar": "community", "format": "feed",
        "hook_family": "question", "ask_type": "booking_link",
        "time_slot": "morning", "caption_len_band": "mid"}


# ---------------------------------------------------------------------------
# 1. Dedupe by platformPostId across duplicate accounts -> one row wins
# ---------------------------------------------------------------------------

def test_duplicate_account_same_post_yields_one_row(monkeypatch):
    """The duplicate lassoframework IG connection returns the SAME
    platformPostId under two account ids (two records). Exactly ONE
    post_metrics row per snapshot day survives."""
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [
              _record("rec_a", [_entry("ig_post_1", account_id="acct_a")], PUBLISHED),
              _record("rec_b", [_entry("ig_post_1", account_id="acct_b")], PUBLISHED),
          ]}
    result, store, _ = _run(monkeypatch, aj)
    assert result["ok"] is True
    keys = [(r["platform"], r["platform_post_id"], r["snapshot_day"])
            for r in store.inserted]
    assert keys == [("instagram", "ig_post_1", 7)]
    assert len(set(keys)) == len(keys)


def test_platform_entries_pure_function():
    """Entry-level flatten + dedupe: same platformPostId twice -> one view;
    the view carries the ENTRY's analytics, not the record fallback."""
    records = [
        _record("rec_a", [_entry("same_id", account_id="acct_a", likes=42)], PUBLISHED),
        _record("rec_b", [_entry("same_id", account_id="acct_b")], PUBLISHED),
        _record("rec_c", [_entry("other_id")], PUBLISHED),
    ]
    views = metrics_sync.platform_entries(records)
    assert [v["platformPostId"] for v in views] == ["same_id", "other_id"]
    assert views[0]["analytics"]["likes"] == 42  # entry analytics, first wins
    # a record with NO platforms[] degrades to one view keyed by its _id
    bare = {"_id": "rec_bare", "platform": "instagram", "platformPostId": None,
            "publishedAt": PUBLISHED, "analytics": {"likes": 3}}
    v = metrics_sync.platform_entries([bare])
    assert len(v) == 1
    assert v[0]["platformPostId"] == "rec_bare"       # _id fallback, never null
    assert v[0]["entry_platform_post_id"] is None     # no fake join key invented
    assert v[0]["analytics"] == {"likes": 3}


# ---------------------------------------------------------------------------
# 2. THE JOIN: map hit -> external=false EVEN when isExternal=true
# ---------------------------------------------------------------------------

def test_echo_post_joins_despite_is_external_true(monkeypatch):
    """An entry whose platformPostId maps (via the zernio posts map) to a
    calendar late_post_id lands external=false with calendar_id set — even
    though Zernio stamped the record isExternal=true (the flag is a liar for
    Echo's own posts and is never consulted)."""
    entry = _entry("ig_post_1")
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("rec_1", [entry], PUBLISHED, external=True)]}
    store = FakeStore(calendar_matches={"zern_post_1": _CAL})
    zposts = [_zpost("zern_post_1", [entry])]
    result, store, _ = _run(monkeypatch, aj, store=store, zernio_posts=zposts)
    assert result["ok"] is True
    assert len(store.inserted) == 1
    row = store.inserted[0]
    assert row["external"] is False
    assert row["calendar_id"] == _CAL["id"]
    assert row["hook_family"] == "question"
    gym = result["gyms"][0]
    assert gym["matched_posts"] == 1
    assert gym["external_posts"] == 0


def test_legacy_late_post_id_holding_platform_id_still_joins(monkeypatch):
    """Legacy rows (e.g. lasso's pre-Zernio publishes) stored the PLATFORM
    post id in late_post_id. With NO posts-map hit, the raw platformPostId
    fallback still finds the calendar row -> external=false."""
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("rec_1", [_entry("18116029643485308")], PUBLISHED)]}
    store = FakeStore(calendar_matches={"18116029643485308": _CAL})
    result, store, _ = _run(monkeypatch, aj, store=store, zernio_posts=[])
    row = store.inserted[0]
    assert row["external"] is False
    assert row["calendar_id"] == _CAL["id"]


# ---------------------------------------------------------------------------
# 3. No map hit, no calendar match -> external=true, calendar_id null
# ---------------------------------------------------------------------------

def test_unmatched_entry_flagged_external(monkeypatch):
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("rec_x", [_entry("ext_post_9")], PUBLISHED)]}
    result, store, _ = _run(monkeypatch, aj, zernio_posts=[])
    assert result["ok"] is True
    assert len(store.inserted) == 1
    row = store.inserted[0]
    assert row["external"] is True
    assert row["calendar_id"] is None
    # external rows carry NO calendar levers (nothing invented for a post
    # Echo did not shape)
    assert row["pillar"] is None
    assert row["hook_family"] is None
    gym = result["gyms"][0]
    assert gym["matched_posts"] == 0
    assert gym["external_posts"] == 1


# ---------------------------------------------------------------------------
# 4. Cross-posted record: 2 platform entries -> 2 rows, distinct ids
# ---------------------------------------------------------------------------

def test_cross_posted_record_yields_one_row_per_platform_entry(monkeypatch):
    ig = _entry("ig_post_7", platform="instagram", likes=30)
    fb = _entry("fb_page_77", platform="facebook", account_id="acct_fb", likes=4)
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("rec_cross", [ig, fb], PUBLISHED)]}
    store = FakeStore(calendar_matches={"zern_cross": _CAL})
    zposts = [_zpost("zern_cross", [ig, fb])]
    result, store, _ = _run(monkeypatch, aj, store=store, zernio_posts=zposts)
    assert result["ok"] is True
    assert len(store.inserted) == 2
    by_pid = {r["platform_post_id"]: r for r in store.inserted}
    assert set(by_pid) == {"ig_post_7", "fb_page_77"}
    assert by_pid["ig_post_7"]["platform"] == "instagram"
    assert by_pid["fb_page_77"]["platform"] == "facebook"
    # each row carries ITS entry's analytics, not the record's
    assert by_pid["ig_post_7"]["likes"] == 30
    assert by_pid["fb_page_77"]["likes"] == 4
    # both entries joined to the same calendar row
    assert by_pid["ig_post_7"]["external"] is False
    assert by_pid["fb_page_77"]["external"] is False
    assert result["gyms"][0]["matched_posts"] == 2


# ---------------------------------------------------------------------------
# 5. Flag OFF -> no-op
# ---------------------------------------------------------------------------

def test_flag_off_is_a_noop(monkeypatch):
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("z", [_entry("p")], PUBLISHED)]}
    result, store, zernio = _run(monkeypatch, aj, flag="false")
    assert result["ok"] is False
    assert "AGENT_METRICS_SYNC" in result["reason"]
    assert store.inserted == []
    assert zernio.calls == []
    assert zernio.posts_calls == []


# ---------------------------------------------------------------------------
# supporting honesty checks
# ---------------------------------------------------------------------------

def test_pull_uses_source_all_and_fetches_posts_map(monkeypatch):
    """Blake's rail: metrics come from Zernio analytics with source=all so
    external posts arrive too; the posts map is pulled over the same window."""
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS, "posts": []}
    _, _, zernio = _run(monkeypatch, aj)
    assert zernio.calls and zernio.calls[0]["source"] == "all"
    assert zernio.posts_calls and \
        zernio.posts_calls[0]["days"] == zernio.calls[0]["days"]


def test_posts_pull_failure_is_reported_not_guessed(monkeypatch):
    """A failed Zernio posts pull skips the gym with a reason — a missing map
    would silently flag Echo's own posts external."""
    monkeypatch.setenv("AGENT_METRICS_SYNC", "true")

    class BrokenPostsZernio(FakeZernio):
        def posts_window(self, profile_id, days, **kwargs):
            raise RuntimeError("boom")

    store = FakeStore()
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("r", [_entry("p1")], PUBLISHED)]}
    result = metrics_sync.run(gyms=["gym1"], now=NOW,
                              zernio=BrokenPostsZernio(aj), store=store)
    gym = result["gyms"][0]
    assert gym["ok"] is False
    assert "posts pull failed" in gym["reason"]
    assert store.inserted == []


def test_build_posts_map_pure_function():
    zposts = [
        _zpost("zern_1", [_entry("ig_1"), _entry("fb_1", platform="facebook")]),
        _zpost("zern_2", [_entry("ig_2")]),
        {"_id": "zern_bad"},          # no platforms[] -> contributes nothing
        "not a dict",                  # tolerated
    ]
    m = metrics_sync.build_posts_map(zposts)
    assert m == {"ig_1": "zern_1", "fb_1": "zern_1", "ig_2": "zern_2"}


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
    entry = _entry("p1")
    entry["analytics"] = {"likes": 5}  # no reach/saves/shares reported
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("z1", [entry], PUBLISHED)]}
    _, store, _ = _run(monkeypatch, aj)
    row = store.inserted[0]
    assert row["likes"] == 5
    assert row["saves"] is None
    assert row["shares"] is None
    assert row["reach"] is None


# ---------------------------------------------------------------------------
# hook-quality fields (20260827): reels_skip_rate, watch_total_ms,
# engagement_rate, is_ad — entry-level first, record fallback, null-not-zero.
# ---------------------------------------------------------------------------

def test_hook_quality_fields_land_from_realistic_payload(monkeypatch):
    entry = _entry("reel_1")
    entry["analytics"] = {
        "likes": 42, "reach": 900, "comments": 4, "views": 1200,
        "reelsSkipRate": 0.37, "igReelsVideoViewTotalTime": 5400000,
        "engagementRate": 0.051, "igReelsAvgWatchTime": 4500,
        "videoDurationSeconds": 12,
    }
    rec = _record("z1", [entry], PUBLISHED)
    rec["mediaProductType"] = "REELS"
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS, "posts": [rec]}
    _, store, _ = _run(monkeypatch, aj)
    row = store.inserted[0]
    assert row["reels_skip_rate"] == 0.37
    assert row["watch_total_ms"] == 5400000
    assert row["engagement_rate"] == 0.051
    assert row["is_ad"] is False  # not reported -> honest default false


def test_is_ad_captured_entry_level_first_record_fallback(monkeypatch):
    # entry-level isAd wins
    entry = _entry("ad_1")
    entry["isAd"] = True
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("z1", [entry], PUBLISHED)]}
    _, store, _ = _run(monkeypatch, aj)
    assert store.inserted[0]["is_ad"] is True

    # record-level fallback when the entry carries none
    rec = _record("z2", [_entry("ad_2")], PUBLISHED)
    rec["isAd"] = True
    aj2 = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS, "posts": [rec]}
    _, store2, _ = _run(monkeypatch, aj2)
    assert store2.inserted[0]["is_ad"] is True


def test_hook_quality_fields_stay_null_not_zero(monkeypatch):
    aj = {"hasAnalyticsAccess": True, "accounts": _ACCOUNTS,
          "posts": [_record("z1", [_entry("p9")], PUBLISHED)]}
    _, store, _ = _run(monkeypatch, aj)
    row = store.inserted[0]
    assert row["reels_skip_rate"] is None
    assert row["watch_total_ms"] is None
    assert row["engagement_rate"] is None
    assert row["is_ad"] is False  # the one non-null field (column default false)


def test_learning_score_reel_helpers_pure():
    from agent import learning_score as ls
    # skip rate: the stored value or None, never fabricated
    assert ls.reel_skip_rate({"reels_skip_rate": 0.4}) == 0.4
    assert ls.reel_skip_rate({"reels_skip_rate": None}) is None
    assert ls.reel_skip_rate({}) is None
    assert ls.reel_skip_rate({"reels_skip_rate": True}) is None  # bool rejected
    # watch ratio: direct avg first
    direct = ls.reel_watch_ratio(
        {"watch_time_ms": 6000, "video_seconds": 12})
    assert direct == 0.5
    # derived from watch_total_ms / views when the avg is absent
    derived = ls.reel_watch_ratio(
        {"watch_total_ms": 5400000, "views": 1200, "video_seconds": 12})
    assert abs(derived - (5400000 / 1200 / 1000 / 12)) < 1e-9
    # no honest computation -> None
    assert ls.reel_watch_ratio({"watch_total_ms": 5400000,
                                "video_seconds": 12}) is None
    assert ls.reel_watch_ratio({"views": 1200, "video_seconds": 12}) is None
    assert ls.reel_watch_ratio({"watch_time_ms": 6000}) is None
