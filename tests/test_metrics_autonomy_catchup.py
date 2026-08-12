"""
Three builds (Blake 2026-08-12), fully OFFLINE:

1. METRICS: new followers (posts[].analytics.follows) + impressions flow into the
   portal metrics payload; null-not-zero holds when absent.
2. PER-GYM AUTONOMY in the client publish lane: an autonomous gym's PENDING rows
   publish on their own; a non-autonomous gym still requires approval. Wired per
   gym, never portfolio-wide. Safe default on any read error.
3. CATCH-UP report: honest per-gym coverage, daily dedupe, goes quiet only after
   all caught up, resumes when a gym falls behind. Flag defaults OFF.

Plus: scheduled_at stamping in publish_due (display metadata; never blocks).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, catchup_report, zernio_analytics as za  # noqa: E402
from agent import calendar_autopublish as cap  # noqa: E402


# ---- 1. metrics: follows + impressions ----------------------------------------

def _post(published_at, **analytics):
    return {"publishedAt": published_at, "status": "published",
            "analytics": analytics}


def test_follows_becomes_new_followers_and_impressions_flow():
    aj = {
        "hasAnalyticsAccess": True,
        "accounts": [{"followersCount": 1200}],
        "posts": [
            _post("2026-08-10T12:00:00Z", follows=5, impressions=900, reach=800),
            _post("2026-08-11T12:00:00Z", follows=3, impressions=1100, reach=700),
        ],
    }
    out = za.map_metrics(aj, 30, None, None,
                         now=za._parse_iso("2026-08-12T00:00:00Z"))
    assert out["audience"]["follower_delta"] == 8          # real new followers
    assert out["audience"]["impressions"] == 2000
    assert out["audience"]["followers"] == 1200


def test_no_follows_reported_stays_null_not_zero():
    aj = {"hasAnalyticsAccess": True, "accounts": [],
          "posts": [_post("2026-08-10T12:00:00Z", likes=4)]}
    out = za.map_metrics(aj, 30, None, None,
                         now=za._parse_iso("2026-08-12T00:00:00Z"))
    assert out["audience"]["follower_delta"] is None       # gap, never 0
    assert out["audience"]["impressions"] is None


def test_followers_per_month_before_after_from_follows():
    # cutoff (echo start) 2026-08-01; one pre-Echo post, two after
    aj = {
        "hasAnalyticsAccess": True, "accounts": [],
        "posts": [
            _post("2026-07-02T12:00:00Z", follows=30),      # before era
            _post("2026-08-10T12:00:00Z", follows=5),
            _post("2026-08-11T12:00:00Z", follows=4),
        ],
    }
    out = za.map_metrics(aj, 30, None, "2026-08-01T00:00:00Z",
                         now=za._parse_iso("2026-08-12T00:00:00Z"))
    ba = out["before_after"]["followers_per_month"]
    assert ba["after"] is not None and ba["after"] > 0      # real post-Echo rate
    assert ba["before"] is not None and ba["before"] > 0    # real pre-Echo rate


def test_after_leg_excludes_pre_echo_posts_inside_window():
    """A1 fix: a gym onboarded 5 days ago with a 30-day window must NOT count the
    pre-Echo posts (still inside the raw window) in the 'after Echo' rate."""
    aj = {
        "hasAnalyticsAccess": True, "accounts": [],
        "posts": [
            # pre-Echo but INSIDE the 30-day window (10 days ago; cutoff is 5 days ago)
            _post("2026-08-02T12:00:00Z", follows=100),
            # after Echo
            _post("2026-08-09T12:00:00Z", follows=6),
        ],
    }
    now = za._parse_iso("2026-08-12T00:00:00Z")
    out = za.map_metrics(aj, 30, None, "2026-08-07T00:00:00Z", now=now)
    ba = out["before_after"]["followers_per_month"]
    # after = 6 followers over the ~5-day post-Echo span, NOT 106 blended with pre-Echo.
    # The pre-Echo 100 lands only in the 'before' leg.
    assert ba["after"] is not None and ba["after"] < 100    # pre-Echo 100 excluded
    # sanity: the after rate reflects only the 6 follows (normalized up from ~5 days)
    assert ba["after"] < ba["before"]                       # before (100) dwarfs after


# ---- 2. per-gym autonomy in the client publish lane ----------------------------

class _LaneStore:
    """Store-faithful (claim precondition) + per-gym autonomy answer."""

    def __init__(self, rows, autonomy=None):
        self._rows = {r["id"]: dict(r) for r in rows}
        self._autonomy = autonomy or {}
        self.published = []

    def due_rows(self, gym_id, run_date):
        return [dict(r) for r in self._rows.values() if r["gym_id"] == gym_id]

    def gym_autonomy(self, slug):
        return self._autonomy.get(slug)

    def mark_publishing(self, row_id):
        r = self._rows[row_id]
        if r.get("published_at") or r.get("status") not in ("pending", "approved"):
            return False
        r["status"] = "publishing"
        return True

    def mark_published(self, row_id, media_id, published_at):
        self._rows[row_id].update(status="published", published_at=published_at)
        self.published.append(row_id)
        return {"id": row_id}

    def mark_publish_failed(self, row_id, revert_status="pending"):
        self._rows[row_id]["status"] = revert_status
        return {"id": row_id}

    def stamp_scheduled(self, row_id, iso):
        self._rows[row_id]["scheduled_at"] = iso
        return {"id": row_id}


def _row(rid, gym, status):
    return {"id": rid, "gym_id": gym, "account": "instagram", "status": status,
            "post_date": "2026-08-13", "format": "feed",
            "image_url": "https://r2/i.jpg", "caption": "hi"}


def _arm_lane(monkeypatch):
    monkeypatch.setenv("AGENT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("AGENT_ZERNIO_PUBLISH", "true")
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")


def _fake_zernio_ok(draft, account, scheduled_for=None):
    from agent.zernio_publisher import PublishResult
    return PublishResult(ok=True, mode="published", media_id="z1")


def test_autonomous_gym_publishes_pending_on_its_own(monkeypatch):
    _arm_lane(monkeypatch)
    monkeypatch.setattr(cap, "client_gym_bases", lambda: ["eng"])
    from agent import db as _db
    monkeypatch.setattr(_db, "is_autonomous", lambda k: False)   # kv off; portal ON
    store = _LaneStore([_row("p1", "eng", "pending")], autonomy={"eng": True})
    out = cap.publish_client_gyms("2026-08-13", store=store,
                                  zernio_publish=_fake_zernio_ok)
    assert out and out[0]["gym"] == "eng" and out[0]["autonomous"] is True
    assert store.published == ["p1"]                    # pending published on its own


def test_kv_autonomy_also_arms_the_gym(monkeypatch):
    """Echo's own kv autonomy flag (POST /portal/<token>/autonomy) arms the lane too."""
    _arm_lane(monkeypatch)
    monkeypatch.setattr(cap, "client_gym_bases", lambda: ["eng"])
    from agent import db as _db
    monkeypatch.setattr(_db, "is_autonomous", lambda k: k == "eng")
    store = _LaneStore([_row("p1b", "eng", "pending")], autonomy={"eng": None})
    out = cap.publish_client_gyms("2026-08-13", store=store,
                                  zernio_publish=_fake_zernio_ok)
    assert out[0]["autonomous"] is True and store.published == ["p1b"]


def test_non_autonomous_gym_still_requires_approval(monkeypatch):
    _arm_lane(monkeypatch)
    monkeypatch.setattr(cap, "client_gym_bases", lambda: ["eng"])
    from agent import db as _db
    monkeypatch.setattr(_db, "is_autonomous", lambda k: False)
    store = _LaneStore([_row("p2", "eng", "pending")], autonomy={"eng": False})
    out = cap.publish_client_gyms("2026-08-13", store=store,
                                  zernio_publish=_fake_zernio_ok)
    assert out[0]["autonomous"] is False
    assert store.published == []                        # pending held for approval


def test_autonomy_is_per_gym_not_portfolio(monkeypatch):
    """The toggle arms ONE gym only: eng autonomous publishes pending; gritx (not
    autonomous) holds its pending row for approval in the SAME pass."""
    _arm_lane(monkeypatch)
    monkeypatch.setattr(cap, "client_gym_bases", lambda: ["eng", "gritx"])
    from agent import db as _db
    monkeypatch.setattr(_db, "is_autonomous", lambda k: False)
    store = _LaneStore([_row("e1", "eng", "pending"), _row("g1", "gritx", "pending")],
                       autonomy={"eng": True, "gritx": False})
    out = cap.publish_client_gyms("2026-08-13", store=store,
                                  zernio_publish=_fake_zernio_ok)
    by_gym = {s["gym"]: s for s in out}
    assert by_gym["eng"]["autonomous"] is True
    assert by_gym["gritx"]["autonomous"] is False
    assert store.published == ["e1"]                    # gritx untouched


def test_autonomy_read_error_defaults_to_approval_required(monkeypatch):
    _arm_lane(monkeypatch)
    monkeypatch.setattr(cap, "client_gym_bases", lambda: ["eng"])
    from agent import db as _db
    monkeypatch.setattr(_db, "is_autonomous",
                        lambda k: (_ for _ in ()).throw(RuntimeError("kv down")))
    store = _LaneStore([_row("p3", "eng", "pending")], autonomy={"eng": True})
    out = cap.publish_client_gyms("2026-08-13", store=store,
                                  zernio_publish=_fake_zernio_ok)
    assert out[0]["autonomous"] is False               # safe side on error
    assert store.published == []


def test_scheduled_at_is_stamped_for_waiting_rows(monkeypatch):
    """Even a row still waiting on approval gets its go-live time stamped so the
    portal can show the client WHEN it will post."""
    _arm_lane(monkeypatch)
    store = _LaneStore([_row("p4", "eng", "pending")])
    out = cap.publish_due("2026-08-13", gym_id="eng", store=store,
                          approved_only=True, catch_all=True,
                          zernio_publish=lambda *a, **k: None)
    assert "p4" in out["waiting"]
    assert store._rows["p4"].get("scheduled_at", "").startswith("2026-08-13T")


# ---- 3. catch-up report ---------------------------------------------------------

class _KV(dict):
    def get(self, k, default=""):
        return dict.get(self, k, default)

    def set(self, k, v):
        self[k] = v


def _gyms():
    return [{"slug": "eng", "name": "CrossFit ENG", "created_at": "2026-08-01"},
            {"slug": "newbie", "name": "New Gym", "created_at": "2026-08-10"}]


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_CATCHUP_REPORT", raising=False)
    assert config.catchup_report_enabled() is False
    assert catchup_report.run_daily() is None


def test_report_lists_behind_gyms_honestly(monkeypatch):
    cov = {"eng": {"upcoming": 8, "pending": 2, "approved": 6, "published_7d": 3},
           "newbie": {"upcoming": 1, "pending": 1, "approved": 0, "published_7d": 0}}
    out = catchup_report.build_report(
        now=za._parse_iso("2026-08-12T12:00:00Z"),
        recent_gyms=_gyms(), coverage=lambda slug, today: cov[slug])
    assert out["all_caught_up"] is False
    eng = [g for g in out["gyms"] if g["slug"] == "eng"][0]
    newbie = [g for g in out["gyms"] if g["slug"] == "newbie"][0]
    assert eng["caught_up"] is True and newbie["caught_up"] is False
    assert "needs 6 more post(s)" in out["text"]


def test_read_failure_is_unknown_not_zero():
    """C1 fix: a coverage read failure surfaces as 'unknown', never a fabricated 0,
    and blocks the all-caught-up state so the report is never wrongly silenced."""
    def cov(slug, today):
        if slug == "newbie":
            raise catchup_report.CatchupReadError("coverage read 500")
        return {"upcoming": 9, "pending": 0, "approved": 9, "published_7d": 5}

    out = catchup_report.build_report(
        now=za._parse_iso("2026-08-12T12:00:00Z"),
        recent_gyms=_gyms(), coverage=cov)
    assert out["all_caught_up"] is False                   # unknown blocks caught-up
    assert out["read_failed"] is True and "New Gym" in out["unknown"]
    assert "unknown, not zero" in out["text"]
    assert "0 upcoming" not in out["text"]                  # never a fabricated zero


def test_recent_gyms_read_failure_reports_unknown(monkeypatch):
    def boom(now):
        raise catchup_report.CatchupReadError("gyms read 503")
    monkeypatch.setattr(catchup_report, "_recent_gyms_default", boom)
    out = catchup_report.build_report(now=za._parse_iso("2026-08-12T12:00:00Z"),
                                      recent_gyms=None, coverage=lambda s, t: {})
    assert out["all_caught_up"] is False and out["read_failed"] is True
    assert "UNKNOWN, not zero" in out["text"]


def test_run_daily_dedupes_and_goes_quiet_when_caught_up(monkeypatch):
    monkeypatch.setenv("AGENT_CATCHUP_REPORT", "true")
    kv, alerts = _KV(), []
    good = {"upcoming": 9, "pending": 0, "approved": 9, "published_7d": 5}
    kwargs = dict(kv=kv, alert=lambda m, **k: alerts.append(m),
                  recent_gyms=_gyms(), coverage=lambda s, t: dict(good))
    r1 = catchup_report.run_daily(now=za._parse_iso("2026-08-12T12:00:00Z"), **kwargs)
    assert r1 and r1["all_caught_up"] and len(alerts) == 1      # final confirmation
    r2 = catchup_report.run_daily(now=za._parse_iso("2026-08-12T13:00:00Z"), **kwargs)
    assert r2 is None and len(alerts) == 1                      # same-day dedupe
    r3 = catchup_report.run_daily(now=za._parse_iso("2026-08-13T12:00:00Z"), **kwargs)
    assert r3 is None and len(alerts) == 1                      # quiet while caught up
    # a gym falls behind -> the report resumes
    bad = {"upcoming": 2, "pending": 2, "approved": 0, "published_7d": 0}
    r4 = catchup_report.run_daily(now=za._parse_iso("2026-08-14T12:00:00Z"),
                                  kv=kv, alert=lambda m, **k: alerts.append(m),
                                  recent_gyms=_gyms(), coverage=lambda s, t: dict(bad))
    assert r4 and not r4["all_caught_up"] and len(alerts) == 2
