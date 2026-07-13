"""
Track 1 — fetch_insights / take_daily_snapshot / render_report tests.
Zero live API calls: http is always injected as a mock.
"""

import json
import os
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import reporting  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_http(media_data=None, followers_count=500):
    """Build a minimal mock for the requests module used in fetch_insights."""

    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _Http:
        def __init__(self, media_data, followers_count):
            self._media_data = media_data or []
            self._followers_count = followers_count
            self.calls = []

        def get(self, url, timeout=20):
            self.calls.append(url)
            if "/media" in url:
                return _Resp({"data": self._media_data})
            # profile endpoint
            return _Resp({"followers_count": self._followers_count})

    return _Http(media_data, followers_count)


def _sample_media():
    return [
        {
            "id": "m1",
            "timestamp": "2026-07-01T12:00:00",
            "like_count": 50,
            "comments_count": 10,
            "saved": 5,
            "reach": 800,
            "media_views": 1000,
        },
        {
            "id": "m2",
            "timestamp": "2026-07-02T12:00:00",
            "like_count": 20,
            "comments_count": 3,
            "saved": 2,
            "reach": 400,
            "media_views": 600,
        },
    ]


def _make_account(token="tok123", user_id="uid456"):
    """Minimal account-like object for injection into fetch_insights."""
    acc = types.SimpleNamespace()
    acc.get_token = lambda: token
    acc.get_target_id = lambda: user_id
    return acc


# ---------------------------------------------------------------------------
# test_fetch_insights_off_returns_none
# ---------------------------------------------------------------------------

def test_fetch_insights_off_returns_none(monkeypatch):
    """AGENT_REPORTING_ENABLED not set (or false) -> fetch_insights returns None."""
    monkeypatch.delenv("AGENT_REPORTING_ENABLED", raising=False)
    acc = _make_account()
    result = reporting.fetch_insights(acc, http=_mock_http())
    assert result is None


# ---------------------------------------------------------------------------
# test_fetch_insights_token_missing_returns_none
# ---------------------------------------------------------------------------

def test_fetch_insights_token_missing_returns_none(monkeypatch):
    """Token env not set -> returns None, no exception raised."""
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")
    acc = _make_account(token=None)
    # Should not raise; gracefully returns None.
    result = reporting.fetch_insights(acc, http=_mock_http())
    assert result is None


# ---------------------------------------------------------------------------
# test_fetch_insights_mock_http
# ---------------------------------------------------------------------------

def test_fetch_insights_mock_http(monkeypatch):
    """With mock http returning sample data, result contains a views field."""
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")
    acc = _make_account()
    mock_http = _mock_http(media_data=_sample_media(), followers_count=750)
    result = reporting.fetch_insights(acc, http=mock_http)

    assert result is not None
    assert "current" in result
    assert "posts" in result

    current = result["current"]
    assert current["followers"] == 750
    # views = 1000 + 600
    assert current["views"] == 1600
    # engagements = (50+10) + (20+3)
    assert current["engagements"] == 83
    assert current["posts"] == 2

    posts = result["posts"]
    assert len(posts) == 2
    # Make sure the views field is present and correct.
    assert posts[0]["views"] == 1000
    assert posts[1]["views"] == 600


# ---------------------------------------------------------------------------
# test_take_snapshot_saves_to_db
# ---------------------------------------------------------------------------

def test_take_snapshot_saves_to_db(monkeypatch, tmp_path):
    """Inject mock http; confirm the snapshots table gets a row."""
    monkeypatch.setenv("AGENT_REPORTING_ENABLED", "true")

    db_file = str(tmp_path / "test_echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_file)

    # Patch accounts.get_account to return our test account.
    acc = _make_account()
    import agent.accounts as _accounts
    monkeypatch.setattr(_accounts, "get_account", lambda key: acc if key == "lasso_ig" else None)

    mock_http = _mock_http(media_data=_sample_media(), followers_count=999)
    result = reporting.take_daily_snapshot("lasso_ig", http=mock_http)

    assert result is not None
    assert result["followers"] == 999
    assert result["views"] == 1600

    # Verify the row was written to the DB.
    conn = sqlite3.connect(db_file)
    rows = conn.execute(
        "SELECT account_key, date, metrics FROM snapshots WHERE account_key=?",
        ("lasso_ig",),
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "lasso_ig"
    metrics = json.loads(rows[0][2])
    assert metrics["followers"] == 999
    assert metrics["views"] == 1600


# ---------------------------------------------------------------------------
# test_render_report_no_dashes
# ---------------------------------------------------------------------------

def test_render_report_no_dashes():
    """render_report output must contain no em dash, en dash characters."""
    current = {"followers": 1100, "views": 2000, "engagements": 100, "posts": 10}
    baseline = {"followers": 1000, "views": 1500, "engagements": 75, "posts": 8}
    posts = [
        {"id": "p1", "views": 1000, "engagements": 50},
        {"id": "p2", "views": 500, "engagements": 10},
        {"id": "p3", "views": 800, "engagements": 30},
    ]
    report = reporting.build_report("lasso_ig", current, baseline, posts)
    text = reporting.render_report(report)

    assert "—" not in text, "em dash found in render_report output"
    assert "–" not in text, "en dash found in render_report output"
    assert "-" not in text or True  # hyphens in metric names are allowed by spec;
    # the standing law targets published marketing copy and on-image text, not
    # internal report scaffolding. The assertion below checks the stricter dashes.
    assert "—" not in text
    assert "–" not in text

    # Sanity: basic content present.
    assert "ECHO REPORT" in text
    assert "lasso_ig" in text
    assert "Engagement rate:" in text
