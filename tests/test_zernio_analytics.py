"""
LIVE Zernio analytics -> portal metrics, all offline.

Two surfaces:
  * agent/zernio_analytics.map_metrics — the PURE mapper (null-not-zero, window filter,
    follower sums, engagement mean, current_posts_per_week math, benchmark null).
  * agent/portal_social.handle_metrics with AGENT_ZERNIO_ANALYTICS_ENABLED — flag ON +
    a fake client with hasAnalyticsAccess=true -> real values; false -> null shape;
    Zernio raises -> null shape (no 500); flag OFF -> the existing null shape unchanged.

No network: a fake Zernio client is injected; the Stripe reader is a fake ACTIVE reader.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import portal_social as ps
from agent import zernio_analytics as za
from agent import db as _db


UTCNOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _post(days_ago, status="published", platform="instagram", analytics=None):
    return {
        "_id": f"p{days_ago}",
        "publishedAt": _iso(UTCNOW - timedelta(days=days_ago)),
        "status": status,
        "platform": platform,
        "isAd": False,
        "analytics": analytics or {},
    }


# ---------------------------------------------------------------------------
# map_metrics — the pure mapper
# ---------------------------------------------------------------------------

def test_followers_sums_nonnull():
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [
            {"platform": "instagram", "followersCount": 1200},
            {"platform": "facebook", "followersCount": None},  # FB returns null
            {"platform": "instagram", "followersCount": 300},
        ],
        "posts": [],
    }
    out = za.map_metrics(aj, 14, 3, "2026-07-01T00:00:00Z")
    assert out["audience"]["followers"] == 1500


def test_followers_all_null_is_null_not_zero():
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [
            {"platform": "facebook", "followersCount": None},
            {"platform": "facebook", "followersCount": None},
        ],
        "posts": [],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["audience"]["followers"] is None


def test_present_zero_stays_zero_absent_metric_stays_null():
    # likes present as 0 across posts -> real 0; saves entirely absent -> null.
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _post(1, analytics={"likes": 0, "comments": 5}),
            _post(2, analytics={"likes": 0, "comments": 3}),
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["totals"]["likes"] == 0            # present zeros -> real 0
    assert out["totals"]["comments"] == 8         # summed
    assert out["totals"]["saves"] is None         # absent everywhere -> null
    assert out["totals"]["shares"] is None


def test_totals_audience_summed_in_window():
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [{"platform": "instagram", "followersCount": 500}],
        "posts": [
            _post(1, analytics={"likes": 10, "reach": 100, "impressions": 200, "shares": 2}),
            _post(3, analytics={"likes": 5, "reach": 50, "impressions": 80, "shares": 1}),
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["totals"]["likes"] == 15
    assert out["totals"]["shares"] == 3
    assert out["audience"]["reach"] == 150
    assert out["audience"]["impressions"] == 280


def test_out_of_window_posts_excluded():
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _post(2, analytics={"likes": 10, "reach": 100}),      # in window
            _post(40, analytics={"likes": 999, "reach": 9999}),   # older than 14d
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["totals"]["likes"] == 10
    assert out["audience"]["reach"] == 100
    assert out["totals"]["posts_published"] == 1


def test_engagement_rate_mean_and_null():
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _post(1, analytics={"engagementRate": 4.0}),
            _post(2, analytics={"engagementRate": 6.0}),
            _post(3, analytics={"likes": 1}),  # no engagementRate -> ignored in mean
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["engagement_rate"] == 5.0

    none_aj = {"hasAnalyticsAccess": True, "accounts": [], "posts": [_post(1, analytics={"likes": 1})]}
    assert za.map_metrics(none_aj, 14, None, None)["engagement_rate"] is None


def test_benchmark_always_null():
    aj = {"hasAnalyticsAccess": True, "accounts": [], "posts": [_post(1, analytics={"likes": 1})]}
    assert za.map_metrics(aj, 14, None, None)["benchmark"] is None


def test_current_posts_per_week_math():
    # 4 published posts in a 14-day window -> 14/7 = 2 weeks -> 2.0 posts/week.
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [_post(i, analytics={"likes": 1}) for i in (1, 3, 5, 7)],
    }
    out = za.map_metrics(aj, 14, 3, "2026-07-01T00:00:00Z")
    assert out["frequency"]["current_posts_per_week"] == 2.0
    assert out["frequency"]["baseline_posts_per_week"] == 3
    assert out["frequency"]["baseline_captured_at"] == "2026-07-01T00:00:00Z"


def test_current_ppw_null_when_days_nonpositive():
    aj = {"hasAnalyticsAccess": True, "accounts": [], "posts": [_post(1, analytics={"likes": 1})]}
    out = za.map_metrics(aj, 0, None, None)
    assert out["frequency"]["current_posts_per_week"] is None


def test_posts_published_counts_only_published_in_window():
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _post(1, status="published", analytics={"likes": 1}),
            _post(2, status="scheduled", analytics={"likes": 1}),
            _post(3, status="published", analytics={"likes": 1}),
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["totals"]["posts_published"] == 2


def test_data_source_and_available_flag():
    on = za.map_metrics({"hasAnalyticsAccess": True, "accounts": [], "posts": []}, 14, None, None)
    assert on["analytics_available"] is True
    assert on["data_source"] == "zernio"
    off = za.map_metrics({"hasAnalyticsAccess": False, "accounts": [], "posts": []}, 14, None, None)
    assert off["analytics_available"] is False
    assert off["data_source"] is None


# ---------------------------------------------------------------------------
# before_after (proof of growth) + learnings (what performed best)
# ---------------------------------------------------------------------------

def _dated_post(days_ago, status="published", platform="instagram",
                analytics=None, caption=None, category=None):
    p = _post(days_ago, status=status, platform=platform, analytics=analytics)
    if caption is not None:
        p["caption"] = caption
    if category is not None:
        p["category"] = category
    return p


def test_before_after_split_by_cutoff():
    """before = per-month avg over posts BEFORE the Echo-start cutoff; after = per-month
    rate from the recent in-window posts. Both are real numbers here."""
    cutoff_iso = _iso(UTCNOW - timedelta(days=60))  # Echo started 60 days ago
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            # AFTER (in a 30d window): reach 300 total -> /1 month = 300, saves 30 -> 30
            _dated_post(2, analytics={"reach": 200, "saves": 20}),
            _dated_post(5, analytics={"reach": 100, "saves": 10}),
            # BEFORE (pre-cutoff, 60d..150d ago): earliest 150d, latest 90d ->
            # ~3 months span; reach 600 total -> ~200/mo, saves 60 -> ~20/mo
            _dated_post(90, analytics={"reach": 300, "saves": 30}),
            _dated_post(150, analytics={"reach": 300, "saves": 30}),
        ],
    }
    out = za.map_metrics(aj, 30, None, None, echo_start=cutoff_iso)
    ba = out["before_after"]
    assert ba["reach_per_month"]["after"] == 300.0
    assert ba["saves_per_month"]["after"] == 30.0
    # before is a positive per-month rate derived from the pre-cutoff era
    assert ba["reach_per_month"]["before"] is not None
    assert ba["reach_per_month"]["before"] > 0
    assert ba["saves_per_month"]["before"] is not None
    # followers per month has no dated series -> both legs null (never fabricated)
    assert ba["followers_per_month"] == {"before": None, "after": None}


def test_before_after_cutoff_from_baseline_captured_at():
    """With no explicit echo_start, the cutoff derives from baseline_captured_at."""
    baseline_at = _iso(UTCNOW - timedelta(days=45))
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _dated_post(3, analytics={"reach": 500}),      # after
            _dated_post(80, analytics={"reach": 100}),     # before
            _dated_post(120, analytics={"reach": 100}),    # before
        ],
    }
    out = za.map_metrics(aj, 30, 3, baseline_at)
    ba = out["before_after"]
    assert ba["reach_per_month"]["after"] == 500.0
    assert ba["reach_per_month"]["before"] is not None


def test_before_after_leg_with_no_data_is_null_not_zero():
    """A before/after leg with no real metric stays null, never a fabricated 0."""
    cutoff_iso = _iso(UTCNOW - timedelta(days=60))
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            # after posts report reach only; saves entirely absent
            _dated_post(2, analytics={"reach": 100}),
            # before posts report saves only; reach absent
            _dated_post(90, analytics={"saves": 5}),
        ],
    }
    out = za.map_metrics(aj, 30, None, None, echo_start=cutoff_iso)
    ba = out["before_after"]
    assert ba["reach_per_month"]["after"] == 100.0
    assert ba["saves_per_month"]["after"] is None       # absent -> null, not 0
    assert ba["saves_per_month"]["before"] is not None  # present before
    assert ba["reach_per_month"]["before"] is None      # absent before -> null


def test_before_after_present_zero_stays_zero():
    """A genuine present-0 total over a real span is a real rate of 0, not null."""
    cutoff_iso = _iso(UTCNOW - timedelta(days=60))
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _dated_post(2, analytics={"saves": 0}),   # after: present 0
            _dated_post(5, analytics={"saves": 0}),
        ],
    }
    out = za.map_metrics(aj, 30, None, None, echo_start=cutoff_iso)
    assert out["before_after"]["saves_per_month"]["after"] == 0


def test_before_after_no_cutoff_before_all_null():
    """No echo_start and no baseline -> the before era can't be bounded -> before null."""
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [_dated_post(2, analytics={"reach": 100, "saves": 10})],
    }
    out = za.map_metrics(aj, 30, None, None)
    ba = out["before_after"]
    assert ba["reach_per_month"]["after"] == 100.0
    assert ba["reach_per_month"]["before"] is None
    assert ba["saves_per_month"]["before"] is None


def test_learnings_from_top_posts():
    """best_post=max reach, most_saved=max saves, most_shared=max shares; top_topics
    from the real categories; next_month_focus restates them as actions."""
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _dated_post(1, analytics={"reach": 500, "saves": 5, "shares": 2},
                        caption="Beat the Monday slump", category="motivation"),
            _dated_post(2, analytics={"reach": 100, "saves": 40, "shares": 1},
                        caption="Meal prep in 20 minutes", category="nutrition"),
            _dated_post(3, analytics={"reach": 200, "saves": 3, "shares": 30},
                        caption="Bring a friend Friday", category="community"),
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    lrn = out["learnings"]
    assert lrn is not None
    assert lrn["best_post"]["caption"] == "Beat the Monday slump"
    assert lrn["best_post"]["metric_label"] == "reach"
    assert lrn["best_post"]["metric_value"] == 500
    assert lrn["most_saved"]["caption"] == "Meal prep in 20 minutes"
    assert lrn["most_saved"]["metric_value"] == 40
    assert lrn["most_shared"]["caption"] == "Bring a friend Friday"
    assert lrn["most_shared"]["metric_value"] == 30
    # topics come from the real categories of the top posts, de-duped in priority order
    assert lrn["top_topics"] == ["motivation", "nutrition", "community"]
    assert lrn["next_month_focus"] == ["more motivation", "more nutrition",
                                       "more community"]


def test_learnings_null_when_no_published_metrics():
    """No published post with a usable metric -> learnings is None (not a shell)."""
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _dated_post(1, status="scheduled", analytics={"reach": 999}),  # not published
            _dated_post(2, status="published", analytics={}),              # no metrics
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["learnings"] is None


def test_learnings_topic_never_invented():
    """A top post with no category contributes no topic (topics are never invented)."""
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [
            _dated_post(1, analytics={"reach": 300, "saves": 5, "shares": 2},
                        caption="No category here"),  # no category field
        ],
    }
    out = za.map_metrics(aj, 14, None, None)
    lrn = out["learnings"]
    assert lrn is not None
    assert lrn["best_post"]["caption"] == "No category here"
    assert lrn["top_topics"] == []           # nothing invented
    assert lrn["next_month_focus"] == []


def test_seed_shape_before_after_all_null_learnings_null(monkeypatch):
    """The seed/unavailable shape carries before_after with all-null legs and
    learnings None, with no invented numbers."""
    monkeypatch.setenv("AGENT_ZERNIO_ANALYTICS_ENABLED", "true")
    shape = ps._metrics_shape("gymX", 30)
    ba = shape["before_after"]
    for metric in ("followers_per_month", "reach_per_month", "saves_per_month"):
        assert ba[metric] == {"before": None, "after": None}, metric
    assert shape["learnings"] is None


# ---------------------------------------------------------------------------
# metrics honesty gate (Part B)
# ---------------------------------------------------------------------------

def test_map_metrics_narrative_null_and_no_invented_prose():
    """map_metrics never emits invented narrative prose; narrative is null even on a
    live (hasAnalyticsAccess) payload because Echo composes no metrics prose."""
    on = za.map_metrics({"hasAnalyticsAccess": True, "accounts": [], "posts": []},
                        14, None, None)
    assert on["narrative"] is None
    off = za.map_metrics({"hasAnalyticsAccess": False, "accounts": [], "posts": []},
                         14, None, None)
    assert off["narrative"] is None


def test_follower_delta_null_when_only_total_present():
    """followers is a TOTAL from accounts[].followersCount; follower_delta stays null
    (never a delta derived from the total)."""
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [{"platform": "instagram", "followersCount": 1200}],
        "posts": [],
    }
    out = za.map_metrics(aj, 14, None, None)
    assert out["audience"]["followers"] == 1200      # the real total survives
    assert out["audience"]["follower_delta"] is None  # no fabricated 30-day delta


def test_seed_shape_data_source_none_and_narrative_null(monkeypatch):
    """The SEED / unavailable shape (portal_social._metrics_shape) carries data_source
    None (not 'zernio' just because the flag is on), narrative null, follower_delta null,
    and only real-or-null numbers (never a fabricated 0)."""
    monkeypatch.setenv("AGENT_ZERNIO_ANALYTICS_ENABLED", "true")
    shape = ps._metrics_shape("gymX", 30)
    assert shape["data_source"] is None
    assert shape["narrative"] is None
    assert shape["audience"]["followers"] is None
    assert shape["audience"]["follower_delta"] is None
    for k in ("likes", "comments", "saves", "shares", "posts_published"):
        assert shape["totals"][k] is None, f"{k} must be null, never a fabricated 0"


def test_rhythm_from_real_publishes_not_a_constant():
    """current_posts_per_week is computed from ACTUAL in-window published posts, not a
    seeded 14 or 35; baseline_posts_per_week is the passed real baseline."""
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [],
        "posts": [_post(1), _post(3), _post(5), _post(50)],  # 3 in a 14d window
    }
    out = za.map_metrics(aj, 14, baseline_ppw=0.25, baseline_at="2026-07-01T00:00:00Z")
    assert out["frequency"]["current_posts_per_week"] == 1.5   # 3 / (14/7)
    assert out["frequency"]["baseline_posts_per_week"] == 0.25
    assert out["frequency"]["current_posts_per_week"] not in (14, 35)


# ---------------------------------------------------------------------------
# analytics_window — pages until out of window, stops on total, offline
# ---------------------------------------------------------------------------

class _PagingHttp:
    """A fake `requests`-like http returning analytics pages by (skip)."""

    def __init__(self, pages, total):
        self._pages = pages   # list of post-lists, newest first
        self._total = total
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        skip = int((params or {}).get("skip", 0))
        limit = int((params or {}).get("limit", 50))
        flat = [p for page in self._pages for p in page]
        window = flat[skip:skip + limit]

        class _R:
            status_code = 200

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        return _R({
            "hasAnalyticsAccess": True,
            "accounts": [],
            "posts": window,
            "pagination": {"total": self._total, "skip": skip, "limit": limit},
        })


def test_analytics_window_pages_and_stops_out_of_window():
    from agent import zernio as z
    # page 1: recent posts (in a 14d window); page 2: an old post (out of window)
    recent = [_post(1), _post(2)]
    old = [_post(50)]
    http = _PagingHttp([recent, old], total=3)
    c = z.ZernioClient(api_key="sk", base="https://api.zernio.com", http=http)
    merged = c.analytics_window("pid", days=14, page_limit=2, max_pages=20)
    # it must have paged at least twice to discover the window boundary
    assert http.calls >= 2
    assert merged["hasAnalyticsAccess"] is True
    assert merged["_pages_capped"] is False
    # the old post is fetched but the mapper (not the pager) filters it out
    out = za.map_metrics(merged, 14, None, None)
    assert out["totals"]["posts_published"] == 2


# ---------------------------------------------------------------------------
# handle_metrics wiring — flag ON/OFF, fake client, error -> null (no 500)
# ---------------------------------------------------------------------------

class _ActiveReader:
    def available(self):
        return True

    def social_active(self, customer_id, product_id):
        return True


class _FakeZernio:
    """A fake ZernioClient: resolves any name to a profile id and returns a fixed
    analytics JSON. `raise_on_analytics` makes analytics_window blow up (to prove the
    endpoint falls to the null shape rather than 500ing)."""

    def __init__(self, analytics_json, raise_on_analytics=False):
        self._aj = analytics_json
        self._raise = raise_on_analytics

    def find_profile_id(self, name):
        return "pid_fake"

    def analytics_window(self, profile_id, days, page_limit=50, max_pages=20):
        if self._raise:
            raise RuntimeError("zernio down")
        return self._aj


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_PORTAL_SOCIAL_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SOCIAL_PRODUCT_ID", "prod_social")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "false")
    monkeypatch.delenv("AGENT_MONTHLY_REPORT_ENABLED", raising=False)
    yield


def _mark_customer(account_key):
    _db.gym_upsert(account_key, stripe_customer_id="cus_1")


def _live_json():
    return {
        "hasAnalyticsAccess": True,
        "accounts": [
            {"platform": "instagram", "followersCount": 800},
            {"platform": "facebook", "followersCount": None},
        ],
        "posts": [
            _post(1, analytics={"likes": 12, "comments": 3, "reach": 200,
                                "impressions": 400, "engagementRate": 5.0}),
            _post(4, analytics={"likes": 8, "comments": 1, "reach": 90,
                                "impressions": 150, "engagementRate": 3.0}),
        ],
    }


def test_flag_on_has_access_returns_real_values(db_env, monkeypatch):
    monkeypatch.setenv("AGENT_ZERNIO_ANALYTICS_ENABLED", "true")
    _mark_customer("gymA")
    fake = _FakeZernio(_live_json())
    status, body = ps.handle_metrics("gymA", days=14, reader=_ActiveReader(), zclient=fake)
    assert status == 200
    assert body["analytics_available"] is True
    assert body["data_source"] == "zernio"
    assert body["audience"]["followers"] == 800       # FB null excluded
    assert body["totals"]["likes"] == 20
    assert body["audience"]["reach"] == 290
    assert body["engagement_rate"] == 4.0
    assert body["benchmark"] is None
    assert body["totals"]["posts_published"] == 2
    assert body["gaps"] == []


def test_flag_on_live_emits_before_after_and_learnings(db_env, monkeypatch):
    """The live path emits before_after + learnings from real posts; data_source stays
    'zernio' only on this live pull."""
    monkeypatch.setenv("AGENT_ZERNIO_ANALYTICS_ENABLED", "true")
    _mark_customer("gymA")
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [{"platform": "instagram", "followersCount": 800}],
        "posts": [
            _dated_post(1, analytics={"reach": 300, "saves": 20, "shares": 4},
                        caption="Best post", category="motivation"),
            _dated_post(4, analytics={"reach": 100, "saves": 5, "shares": 1},
                        caption="Second", category="nutrition"),
        ],
    }
    fake = _FakeZernio(aj)
    status, body = ps.handle_metrics("gymA", days=30, reader=_ActiveReader(), zclient=fake)
    assert status == 200
    assert body["data_source"] == "zernio"
    assert "before_after" in body
    assert body["before_after"]["reach_per_month"]["after"] == 400.0
    assert body["learnings"] is not None
    assert body["learnings"]["best_post"]["caption"] == "Best post"


def test_flag_on_no_access_returns_null_shape(db_env, monkeypatch):
    monkeypatch.setenv("AGENT_ZERNIO_ANALYTICS_ENABLED", "true")
    _mark_customer("gymA")
    fake = _FakeZernio({"hasAnalyticsAccess": False, "accounts": [], "posts": []})
    status, body = ps.handle_metrics("gymA", days=14, reader=_ActiveReader(), zclient=fake)
    assert status == 200
    assert body["analytics_available"] is True   # reflects the flag in the null shape
    assert body["totals"]["likes"] is None
    assert body["audience"]["followers"] is None
    assert body["frequency"]["current_posts_per_week"] is None


def test_zernio_error_does_not_500(db_env, monkeypatch):
    monkeypatch.setenv("AGENT_ZERNIO_ANALYTICS_ENABLED", "true")
    _mark_customer("gymA")
    fake = _FakeZernio(_live_json(), raise_on_analytics=True)
    status, body = ps.handle_metrics("gymA", days=14, reader=_ActiveReader(), zclient=fake)
    assert status == 200
    assert body["totals"]["likes"] is None        # honest null, not a 500, not a 0
    assert body["audience"]["followers"] is None


def test_flag_off_returns_existing_null_shape(db_env, monkeypatch):
    monkeypatch.delenv("AGENT_ZERNIO_ANALYTICS_ENABLED", raising=False)
    _mark_customer("gymA")
    # a fake that would blow the assertions if it were ever called
    fake = _FakeZernio(_live_json())
    status, body = ps.handle_metrics("gymA", days=14, reader=_ActiveReader(), zclient=fake)
    assert status == 200
    assert body["analytics_available"] is False
    assert body["totals"]["likes"] is None
    assert body["audience"]["followers"] is None
    assert body["gaps"], "flag off must carry the honest unavailable note"
    # the real-path-only keys are NOT added on the null shape
    assert "engagement_rate" not in body
