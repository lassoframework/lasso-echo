"""
Reporting tests: engagement rate on VIEWS (never impressions), top/bottom 3 by
engagement, follower net + growth, posting freq before vs after, health read, and
gaps flagged (never guessed). fetch_insights is read-only and None while off.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import reporting  # noqa: E402


def _posts():
    return [
        {"id": "p1", "views": 1000, "engagements": 100},
        {"id": "p2", "views": 500, "engagements": 10},
        {"id": "p3", "views": 800, "engagements": 60},
        {"id": "p4", "views": 400, "engagements": 40},
        {"id": "p5", "views": 900, "engagements": 5},
    ]


def _current():
    return {"followers": 1100, "views": 2000, "engagements": 100, "posts": 30}


def _baseline():
    return {"followers": 1000, "views": 1600, "engagements": 64, "posts": 20}


# ---- read-only + flag gate --------------------------------------------------
def test_fetch_insights_none_when_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_REPORTING_ENABLED", raising=False)  # OFF
    assert reporting.fetch_insights(account="lasso_ig") is None


# ---- engagement rate on VIEWS ----------------------------------------------
def test_engagement_rate_on_views():
    r = reporting.build_report("lasso_ig", _current(), _baseline(), _posts())
    assert r["engagement_rate"] == 0.05          # 100 / 2000 views
    assert r["engagement_rate_baseline"] == 0.04  # 64 / 1600


def test_impressions_are_not_used_and_missing_views_is_a_gap():
    cur = {"followers": 1100, "impressions": 9999, "engagements": 100, "posts": 30}
    r = reporting.build_report("lasso_ig", cur, _baseline(), _posts())
    assert r["engagement_rate"] is None          # no VIEWS -> no rate, never impressions
    assert "views" in r["gaps"]


# ---- top / bottom 3 by engagement ------------------------------------------
def test_top_and_bottom_three_by_engagement():
    r = reporting.build_report("lasso_ig", _current(), _baseline(), _posts())
    assert [p["id"] for p in r["top_posts"]] == ["p1", "p3", "p4"]      # 100, 60, 40
    assert [p["id"] for p in r["bottom_posts"]] == ["p5", "p2", "p4"]   # 5, 10, 40


# ---- followers net + growth rate; posting freq before vs after --------------
def test_followers_and_posting_frequency():
    r = reporting.build_report("lasso_ig", _current(), _baseline(), _posts())
    assert r["followers_net"] == 100
    assert r["followers_growth_rate"] == 0.1     # 100 / 1000
    assert r["posting_freq_current"] == 30
    assert r["posting_freq_baseline"] == 20


# ---- health read ------------------------------------------------------------
def test_health_read_growing_flat_declining():
    grow = reporting.build_report("a", _current(), _baseline(), _posts())
    assert grow["health"] == "growing"           # followers up + engagement up
    decline = reporting.build_report(
        "a", {"followers": 900, "views": 2000, "engagements": 40, "posts": 10},
        {"followers": 1000, "views": 1600, "engagements": 64, "posts": 20}, _posts())
    assert decline["health"] == "declining"
    flat = reporting.build_report(
        "a", {"followers": 1000, "views": 1600, "engagements": 64, "posts": 20}, _baseline(), _posts())
    assert flat["health"] == "flat"              # no follower change, same engagement


# ---- gaps flagged, never guessed --------------------------------------------
def test_missing_metrics_flagged_not_guessed():
    r = reporting.build_report("a", {"views": 2000, "engagements": 100}, {}, [{"id": "x"}])
    assert "followers" in r["gaps"]
    assert "baseline_followers" in r["gaps"]
    assert r["followers_net"] is None            # not fabricated
    assert any("missing engagement" in g for g in r["gaps"])  # the post with no signal
