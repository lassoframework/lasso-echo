"""
Reporting live path tests. Offline (fake Graph http). Asserts: snapshots use
VIEWS never impressions (request field names checked); assembler math from
fixture snapshots; before/after posting frequency uses the baseline file; top
and bottom 3 posts correct; health read; the refresh section cites approved
sources only; everything inert while AGENT_REPORTING_ENABLED is OFF.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, monthly_report, reporting_live  # noqa: E402

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeGraph:
    """Records every request; serves account insights, follower reads, post reads."""

    def __init__(self):
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append({"url": url, "params": params or {}})
        if url.endswith("/insights"):
            metrics = (params or {}).get("metric", "")
            data = [{"name": m, "total_value": {"value": 10},
                     "values": [{"value": 10}]} for m in metrics.split(",")]
            return FakeResp({"data": data})
        return FakeResp({"followers_count": 500})


def _ig_account(monkeypatch):
    from agent.accounts import Account, Platform
    monkeypatch.setenv("T_TOKEN", "tok-xyz")
    return Account(key="lasso_ig", display_name="IG", platform=Platform.INSTAGRAM,
                   token_env="T_TOKEN", target_id_env="T_ID")


# ---- inert when OFF --------------------------------------------------------------
def test_snapshot_inert_when_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_REPORTING_ENABLED", raising=False)
    assert reporting_live.snapshot_all(http=FakeGraph()) is None


def test_monthly_report_inert_when_flag_off(monkeypatch, capsys):
    monkeypatch.delenv("AGENT_REPORTING_ENABLED", raising=False)
    assert monthly_report.run() is None
    assert "OFF" in capsys.readouterr().out


# ---- views never impressions -------------------------------------------------------
def test_snapshot_requests_views_never_impressions(monkeypatch):
    monkeypatch.setenv("T_ID", "IG123")
    acct = _ig_account(monkeypatch)
    http = FakeGraph()
    out = reporting_live.fetch_account_snapshot(acct, "tok-xyz", http=http)
    metric_params = [r["params"].get("metric", "") for r in http.requests]
    joined = ",".join(metric_params)
    assert "views" in joined
    assert "impressions" not in joined            # by design, never requested
    assert out["views"] == 10 and out["followers"] == 500
    # module-level constants carry no impressions either
    assert "impressions" not in reporting_live.IG_ACCOUNT_METRICS
    assert "impressions" not in reporting_live.IG_POST_METRICS


# ---- assembler math ------------------------------------------------------------------
def _seed_snapshots(account_key, days):
    with db.connect() as conn:
        for date, metrics in days:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) "
                "VALUES (?,?,?)", (account_key, date, json.dumps(metrics)))
        conn.commit()


def _seed_post(account_key, caption, likes, comments, saves, shares,
               creative_key="lasso_p1_x.jpg", archetype="flow", set_name="brand"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts (draft_id, account_key, platform, caption, media_id, "
            "mode, published_at, creative_key, archetype, set_name, likes, comments, "
            "saves, shares, views, reach) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("d", account_key, "instagram", caption, "M", "published",
             "2026-07-15T10:00:00", creative_key, archetype, set_name,
             likes, comments, saves, shares, 100, 90))
        conn.commit()


def test_assembler_math_and_top_bottom(tmp_path):
    _seed_snapshots("lasso_ig", [
        ("2026-07-10", {"views": 100, "reach": 80, "likes": 10, "comments": 2,
                        "saves": 3, "shares": 1, "followers": 500}),
        ("2026-07-20", {"views": 300, "reach": 200, "likes": 20, "comments": 4,
                        "saves": 5, "shares": 2, "followers": 550}),
    ])
    for i, name in enumerate(["alpha", "beta", "gamma", "delta"]):
        _seed_post("lasso_ig", name, likes=10 * (i + 1), comments=0, saves=0, shares=0)
    (tmp_path / "baseline_2026-07.json").write_text(json.dumps(
        {"accounts": {"lasso_ig": {"avg_posts_per_week": 1.5}}}), encoding="utf-8")

    snaps, posts = monthly_report.gather("lasso_ig", now=NOW)
    r = monthly_report.assemble("lasso_ig", snaps, posts,
                                baseline_month="2026-07", base_dir=str(tmp_path))
    assert r["views"] == 400 and r["reach"] == 280
    assert r["likes"] == 30 and r["comments"] == 6
    assert r["saves"] == 8 and r["shares"] == 3
    assert r["engagements"] == 47
    assert r["engagement_rate"] == round(47 / 400, 4)
    assert r["followers"] == 550 and r["follower_net"] == 50
    assert r["follower_rate"] == 0.1
    assert r["posting_freq_before"] == 1.5              # the baseline file
    assert r["posts_published"] == 4
    # top 3 = delta(40), gamma(30), beta(20); bottom 3 = alpha(10), beta, gamma
    assert [p["caption"] for p in r["top_posts"]] == ["delta", "gamma", "beta"]
    assert r["bottom_posts"][0]["caption"] == "alpha"
    assert r["health"] in ("growing", "flat", "declining")
    assert r["health"] == "growing"                     # +10 percent followers


def test_missing_baseline_is_a_gap(tmp_path):
    _seed_snapshots("lasso_ig", [("2026-07-10", {"views": 10, "followers": 100})])
    snaps, posts = monthly_report.gather("lasso_ig", now=NOW)
    r = monthly_report.assemble("lasso_ig", snaps, posts,
                                baseline_month="2026-07", base_dir=str(tmp_path))
    assert any("baseline" in g for g in r["gaps"])


# ---- refresh section: approved sources only ---------------------------------------
def test_refresh_cites_approved_sources_only(monkeypatch, tmp_path):
    src = tmp_path / "lasso_now.md"
    src.write_text("""# LASSO Now
## Pillars
- Speed To Lead
## Pillar copy bank
### Pillar: Speed To Lead
Hook: Leads go cold in minutes.
Body: Answer fast.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
""", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)
    _seed_post("lasso_ig", "one", 10, 1, 1, 0, creative_key="lasso_p1_a.jpg",
               archetype="flow", set_name="brand")
    _seed_post("lasso_ig", "two", 2, 0, 0, 0, creative_key="lasso_p2_b.jpg",
               archetype="hero", set_name="service")
    _snaps, posts = monthly_report.gather("lasso_ig", now=NOW)
    refresh = monthly_report.refresh_section("lasso_ig", posts)
    assert len(refresh["proposals"]) >= 1
    for p in refresh["proposals"]:
        assert ("Angle from" in p) and (str(src) in p or "knowledge USE stat" in p)
    # performance ranks strongest/weakest from real data
    pillars = dict(refresh["performance"]["pillar"])
    assert pillars["p1"] > pillars["p2"]
    assert refresh["asks"]                          # the plain raw-material ask list


# ---- end to end run: html + slack summary -----------------------------------------
def test_run_writes_html_and_posts_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    _seed_snapshots("lasso_ig", [("2026-07-10", {"views": 100, "likes": 5,
                                                 "followers": 500})])

    class Rec:
        notices = []

        def post_notice(self, text):
            Rec.notices.append(text)
            return {"ok": True}

    out = monthly_report.run(account="lasso_ig", poster=Rec(), now=NOW,
                             base_dir=str(tmp_path))
    assert "lasso_ig" in out
    html = open(out["lasso_ig"], encoding="utf-8").read()
    assert "LASSO 30 day report" in html
    assert "#121E3C" in html and "#FAF6F0" in html       # V3 brand
    assert "impressions" not in html.lower()
    for ch in ("—", "–"):                       # no dash characters in copy
        assert ch not in html
    assert len(Rec.notices) == 1 and "lasso_ig" in Rec.notices[0]
