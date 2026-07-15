"""
Monthly report HTML upload tests. Offline (no live Meta API). Asserts:
- run() with upload=True calls s3_client.put with the HTML file and correct key
- poster receives a message containing the R2 public URL (not just a text summary)
- upload=False never calls s3_client.put
- render_html() output is self contained (no external script src)
- run() returns None while AGENT_REPORTING_ENABLED is OFF
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db, monthly_report  # noqa: E402

ACCOUNT = "lasso_ig"
MONTH = "2026-07"


class FakeS3:
    def __init__(self):
        self.puts = []

    def put(self, key, local_path):
        self.puts.append({"key": key, "local_path": local_path})


class FakePoster:
    def __init__(self):
        self.notices = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}


def _seed_snapshot(account_key, date, metrics):
    import json
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots (account_key, date, metrics) "
            "VALUES (?, ?, ?)",
            (account_key, date, json.dumps(metrics)))
        conn.commit()


def _enable_reporting(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")
    monkeypatch.setenv("AGENT_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("AGENT_S3_PUBLIC_BASE_URL", "https://cdn.example.com")


# ---- upload=True calls s3_client.put with HTML bytes and correct key ----------------
def test_run_uploads_html_to_r2(monkeypatch, tmp_path):
    _enable_reporting(monkeypatch, tmp_path)
    _seed_snapshot(ACCOUNT, "2026-07-10", {"views": 200, "likes": 8, "followers": 600})

    s3 = FakeS3()
    out = monthly_report.run(account=ACCOUNT, upload=True, s3_client=s3,
                             poster=FakePoster(), now=None)

    assert ACCOUNT in out
    assert len(s3.puts) == 1
    call = s3.puts[0]
    assert call["key"] == f"echo/reports/{ACCOUNT}_{MONTH}.html"
    # the local path must exist and be the HTML file
    assert os.path.isfile(call["local_path"])
    content = open(call["local_path"], encoding="utf-8").read()
    assert "<!doctype html" in content.lower() or "<html" in content.lower()


# ---- poster receives a message containing the R2 public URL -------------------------
def test_run_posts_url_to_slack(monkeypatch, tmp_path):
    _enable_reporting(monkeypatch, tmp_path)
    _seed_snapshot(ACCOUNT, "2026-07-10", {"views": 200, "likes": 8, "followers": 600})

    s3 = FakeS3()
    poster = FakePoster()
    monthly_report.run(account=ACCOUNT, upload=True, s3_client=s3, poster=poster)

    expected_url = (
        f"https://cdn.example.com/echo/reports/{ACCOUNT}_{MONTH}.html"
    )
    assert len(poster.notices) == 1
    assert expected_url in poster.notices[0]


# ---- upload=False: s3_client.put never called ---------------------------------------
def test_run_skips_upload_when_flag_false(monkeypatch, tmp_path):
    _enable_reporting(monkeypatch, tmp_path)
    _seed_snapshot(ACCOUNT, "2026-07-10", {"views": 200, "likes": 8, "followers": 600})

    s3 = FakeS3()
    out = monthly_report.run(account=ACCOUNT, upload=False, s3_client=s3,
                             poster=FakePoster())

    assert ACCOUNT in out
    assert len(s3.puts) == 0


# ---- render_html output is self contained (no external script src) ------------------
def test_html_is_self_contained(monkeypatch, tmp_path):
    _enable_reporting(monkeypatch, tmp_path)
    _seed_snapshot(ACCOUNT, "2026-07-10", {"views": 200, "likes": 8, "followers": 600})

    snaps, posts = monthly_report.gather(ACCOUNT)
    report = monthly_report.assemble(ACCOUNT, snaps, posts)
    refresh = monthly_report.refresh_section(ACCOUNT, posts)
    html = monthly_report.render_html(report, refresh)

    lower = html.lower()
    assert "<!doctype html" in lower or "<html" in lower
    # no external script src (no http/https src in a script tag)
    import re
    ext_scripts = re.findall(r'<script[^>]+src=["\']https?://', html, re.IGNORECASE)
    assert ext_scripts == [], f"external script tags found: {ext_scripts}"


# ---- run() returns None while AGENT_REPORTING_ENABLED is OFF -------------------------
def test_run_off_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_REPORTING_ENABLED", raising=False)
    s3 = FakeS3()
    result = monthly_report.run(account=ACCOUNT, upload=True, s3_client=s3)
    assert result is None
    assert len(s3.puts) == 0
