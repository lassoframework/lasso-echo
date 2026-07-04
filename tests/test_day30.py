"""
Day 30 assembler tests (readiness Part A). Offline. Asserts, adversarially:
the IG report NEVER carries a frequency comparison outside the internal do not
publish appendix, even when seeded data would make frequency look spectacular;
the FB report LEADS with the frequency story (before vs after and the
multiplier); top and bottom 3 reconcile with the stored insights; --dry writes
nothing (store byte identical, no files); output is dash free; missing data
reads as honest gaps.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import day30, db  # noqa: E402
from agent.accounts import get_account  # noqa: E402

_DASH_RE = re.compile(r"[—–]")
NOW = None  # assemble defaults to utcnow; posts seeded relative to today


def _seed_posts(account_key, rows):
    with db.connect() as conn:
        for i, r in enumerate(rows):
            conn.execute(
                "INSERT INTO posts (draft_id, account_key, platform, caption, "
                "media_id, mode, published_at, likes, comments, saves, shares, "
                "views, reach) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"d{i}", account_key, "instagram", r.get("caption", f"post {i}"),
                 f"m{i}", "published",
                 r.get("published_at", "2099-01-01T00:00:00"),
                 r.get("likes"), r.get("comments"), r.get("saves"),
                 r.get("shares"), r.get("views"), r.get("reach")))
        conn.commit()


def _seed_recent(account_key, n=6):
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc)
    rows = []
    for i in range(n):
        rows.append({"caption": f"caption number {i}",
                     "published_at": (base - timedelta(days=i + 1)).isoformat(),
                     "likes": 10 * (i + 1), "comments": i, "saves": 2 * i,
                     "shares": 0, "views": 500 + 100 * i, "reach": 400})
    _seed_posts(account_key, rows)


def _baseline(tmp_path, account_key, per_week):
    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    (tmp_path / f"baseline_{month}.json").write_text(json.dumps(
        {"accounts": {account_key: {"avg_posts_per_week": per_week}}}))
    return str(tmp_path)


# ---- IG: the frequency story NEVER ships, adversarially ---------------------------------
def test_ig_report_never_ships_frequency_comparison(monkeypatch, tmp_path):
    # ADVERSARIAL SEED: frequency would look spectacular (0.25/wk -> daily)
    base_dir = _baseline(tmp_path, "lasso_ig", 0.25)
    _seed_recent("lasso_ig", 30)                    # ~7 posts/week = 28x story
    acct = get_account("lasso_ig")
    assert day30.framing_for(acct) == "engagement"
    report = day30.assemble("lasso_ig", base_dir=base_dir)
    text = day30.render_text(acct, report)
    main, _, appendix = text.partition(day30.DO_NOT_PUBLISH)
    # the publishable body carries NO frequency comparison, no multiplier
    low = main.lower()
    assert "per week" not in low
    assert "posts/wk" not in low and "cadence" not in low
    assert "28x" not in main and "0.25" not in main
    assert "HEADLINE: engagement per post" in main
    # the appendix exists, is flagged, and is stripped from publishable text
    assert appendix.strip()
    assert "0.25" in appendix and "7" in appendix
    pub = day30.publishable_text(acct, report)
    assert day30.DO_NOT_PUBLISH not in pub and "0.25" not in pub


def test_fb_report_leads_with_frequency_story(monkeypatch, tmp_path):
    base_dir = _baseline(tmp_path, "lasso_fb", 0.25)
    _seed_recent("lasso_fb", 30)
    acct = get_account("lasso_fb")
    assert day30.framing_for(acct) == "frequency"
    report = day30.assemble("lasso_fb", base_dir=base_dir)
    text = day30.render_text(acct, report)
    headline = text.splitlines()[1]                 # the line after the title
    assert headline.startswith("HEADLINE: from 0.25 posts per week")
    assert "28x" in headline                        # 7 / 0.25 = 28
    # missing baseline: the story is honest, never a guessed multiplier
    report2 = day30.assemble("lasso_fb", base_dir=str(tmp_path / "nope"))
    text2 = day30.render_text(acct, report2)
    assert "never guessed" in text2.splitlines()[1]


# ---- numbers reconcile ------------------------------------------------------------------
def test_top_bottom_3_reconcile_with_stored_insights(tmp_path):
    _seed_recent("lasso_ig", 6)
    report = day30.assemble("lasso_ig")
    # engagement per post i: likes 10(i+1) + comments i + saves 2i = 13i + 10
    assert [p["engagement"] for p in report["top_posts"]] == [75, 62, 49]
    assert [p["engagement"] for p in report["bottom_posts"]] == [10, 23, 36]
    assert report["posts_published"] == 6
    assert report["likes"] == sum(10 * (i + 1) for i in range(6))
    assert report["views"] == sum(500 + 100 * i for i in range(6))
    assert report["engagement_rate"] is not None


def test_missing_insights_are_honest_gaps():
    _seed_posts("lasso_ig", [{"published_at": "2099-01-01T00:00:00"}])  # no metrics
    report = day30.assemble("lasso_ig")
    assert any("missing insights" in g for g in report["gaps"])
    assert report["engagement_rate"] is None        # never fabricated
    acct = get_account("lasso_ig")
    assert "no data" in day30.render_text(acct, report)


# ---- dry writes nothing; output dash free ------------------------------------------------
def test_dry_writes_nothing_and_watermarks(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)                     # any stray file would land here
    _seed_recent("lasso_ig", 3)
    with db.connect() as conn:
        before = "\n".join(conn.iterdump())
    files_before = sorted(os.listdir(tmp_path))
    day30.report_cli("lasso_ig", dry=True)
    printed = capsys.readouterr().out
    assert "DRY" in printed and "nothing was written" in printed
    assert "DAY 30 REPORT" in printed
    with db.connect() as conn:
        assert "\n".join(conn.iterdump()) == before  # store byte identical
    assert sorted(os.listdir(tmp_path)) == files_before
    assert not _DASH_RE.search(printed)


def test_cli_requires_dry_and_known_account(capsys):
    day30.report_cli("lasso_ig", dry=False)
    assert "usage" in capsys.readouterr().out
    day30.report_cli("nope", dry=True)
    assert "unknown account" in capsys.readouterr().out
