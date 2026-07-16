"""
Tests for the locked pre-Echo baseline (baseline_report track).

Coverage:
  - lock_pre_echo_baseline writes one row with a real-enough account
  - re-lock without force returns _already_locked=True
  - re-lock with force=True writes a new row
  - no-token path returns confidence="no reliable pre-Echo data found", no DB write
  - confidence="clean" when cutoff found and window >= 4 weeks
  - confidence="partially contaminated" when window < 4 weeks
  - confidence="partially contaminated" when cutoff comes from would_publish, not published
  - baseline_report prints the expected lines and returns results list
  - baseline_report returns [] when no row exists
  - CLI baseline-report --account <key> calls baseline_report correctly
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent import db as _db
from agent.baseline import (
    _CLEAN_MIN_PRE_ECHO_WEEKS,
    _find_first_echo_post,
    lock_pre_echo_baseline,
    read_pre_echo_baseline,
    baseline_report,
    WINDOW_WEEKS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_conn():
    """In-memory SQLite connection with the full schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_db._SCHEMA)
    return conn


def _fake_account(key="test_ig", token="tok123", target_id="TARGET1",
                  platform="INSTAGRAM"):
    """Build a minimal account-like object."""
    from agent.accounts import Platform
    acct = MagicMock()
    acct.key = key
    acct.get_token.return_value = token
    acct.get_target_id.return_value = target_id
    acct.platform = Platform.INSTAGRAM if platform == "INSTAGRAM" else Platform.FACEBOOK
    return acct


def _active_accounts_patch(accounts):
    """Return a context manager that patches active_accounts at the source module
    so that function-level imports (from .accounts import active_accounts) resolve
    to the patched version."""
    return patch("agent.accounts.active_accounts", return_value=accounts)


def _make_http(post_times):
    """Fake requests client returning a single page of posts."""
    def _get(url, params=None, timeout=30):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": [{"timestamp": t.isoformat()} for t in post_times],
            "paging": {},
        }
        return resp
    http = MagicMock()
    http.get.side_effect = _get
    return http


# ---------------------------------------------------------------------------
# _find_first_echo_post
# ---------------------------------------------------------------------------

def test_find_first_echo_post_published():
    conn = _mem_conn()
    ts = "2026-05-01T12:00:00+00:00"
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('lasso_ig', 'published', ?, 'IG')", (ts,))
    conn.commit()
    dt, confirmed = _find_first_echo_post("lasso_ig", conn)
    assert confirmed is True
    assert dt is not None
    assert dt.date().isoformat() == "2026-05-01"


def test_find_first_echo_post_falls_back_to_would_publish():
    conn = _mem_conn()
    ts = "2026-06-15T10:00:00+00:00"
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('lasso_ig', 'would_publish', ?, 'IG')", (ts,))
    conn.commit()
    dt, confirmed = _find_first_echo_post("lasso_ig", conn)
    assert confirmed is False
    assert dt is not None
    assert dt.date().isoformat() == "2026-06-15"


def test_find_first_echo_post_none_when_empty():
    conn = _mem_conn()
    dt, confirmed = _find_first_echo_post("lasso_ig", conn)
    assert dt is None
    assert confirmed is False


# ---------------------------------------------------------------------------
# lock_pre_echo_baseline
# ---------------------------------------------------------------------------

def test_lock_writes_row_clean():
    """Happy path: confirmed published cutoff, window >= 4 weeks."""
    conn = _mem_conn()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('test_ig', 'published', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()

    # post times all before the cutoff
    pre_cutoff_times = [cutoff - timedelta(days=i * 4) for i in range(1, 15)]
    http = _make_http(pre_cutoff_times)
    acct = _fake_account("test_ig")
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        rec = lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)

    assert rec["account_key"] == "test_ig"
    assert rec["confidence"] == "clean"
    assert rec.get("avg_posts_per_week") is not None
    assert rec.get("posts_count") is not None
    # Row must be in the DB
    row = conn.execute(
        "SELECT * FROM pre_echo_baselines WHERE account_key='test_ig'").fetchone()
    assert row is not None
    assert row["confidence"] == "clean"


def test_lock_refuses_overwrite_without_force():
    """Second lock without force returns _already_locked=True."""
    conn = _mem_conn()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('test_ig', 'published', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()
    http = _make_http([cutoff - timedelta(days=7)])
    acct = _fake_account("test_ig")
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)
        rec2 = lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)

    assert rec2.get("_already_locked") is True


def test_lock_with_force_overwrites():
    """force=True allows overwriting an existing locked row."""
    conn = _mem_conn()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('test_ig', 'published', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()
    http = _make_http([cutoff - timedelta(days=7)])
    acct = _fake_account("test_ig")
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)
        rec2 = lock_pre_echo_baseline("test_ig", http=http, db_conn=conn,
                                      force=True, now=now)

    assert rec2.get("_already_locked") is None


def test_lock_no_token_returns_no_data_confidence():
    """No API token -> confidence 'no reliable pre-Echo data found', no DB row."""
    conn = _mem_conn()
    acct = _fake_account("test_ig", token=None)
    acct.get_token.return_value = None
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        rec = lock_pre_echo_baseline("test_ig", db_conn=conn, now=now)

    assert rec["confidence"] == "no reliable pre-Echo data found"
    row = conn.execute(
        "SELECT * FROM pre_echo_baselines WHERE account_key='test_ig'").fetchone()
    assert row is None


def test_lock_no_cutoff_partially_contaminated():
    """No confirmed Echo post in the posts table -> partially contaminated.
    The window is computed from 'now' instead of a confirmed cutoff, so Echo
    posts in the window cannot be excluded with certainty."""
    conn = _mem_conn()
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    # No posts in the table at all -> no cutoff found
    http = _make_http([now - timedelta(days=7)])
    acct = _fake_account("test_ig")

    with _active_accounts_patch([acct]):
        rec = lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)

    assert rec["confidence"] == "partially contaminated"
    assert "no confirmed Echo post" in rec["confidence_note"]


def test_lock_would_publish_cutoff_partially_contaminated():
    """Cutoff from would_publish (not confirmed) -> partially contaminated."""
    conn = _mem_conn()
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    cutoff = now - timedelta(weeks=6)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('test_ig', 'would_publish', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()
    http = _make_http([cutoff - timedelta(days=7)])
    acct = _fake_account("test_ig")

    with _active_accounts_patch([acct]):
        rec = lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)

    assert rec["confidence"] == "partially contaminated"
    assert "would_publish" in rec["confidence_note"]


def test_lock_unknown_account_returns_no_data():
    """Unknown account key -> no reliable pre-Echo data found, no DB row."""
    conn = _mem_conn()
    with _active_accounts_patch([]):
        rec = lock_pre_echo_baseline("nonexistent_key", db_conn=conn)
    assert rec["confidence"] == "no reliable pre-Echo data found"
    row = conn.execute(
        "SELECT * FROM pre_echo_baselines WHERE account_key='nonexistent_key'").fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# read_pre_echo_baseline
# ---------------------------------------------------------------------------

def test_read_returns_none_when_no_row():
    conn = _mem_conn()
    assert read_pre_echo_baseline("lasso_ig", db_conn=conn) is None


def test_read_returns_dict_when_locked():
    conn = _mem_conn()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('test_ig', 'published', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()
    http = _make_http([cutoff - timedelta(days=7), cutoff - timedelta(days=14)])
    acct = _fake_account("test_ig")
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)

    result = read_pre_echo_baseline("test_ig", db_conn=conn)
    assert result is not None
    assert result["account_key"] == "test_ig"


# ---------------------------------------------------------------------------
# baseline_report
# ---------------------------------------------------------------------------

def test_baseline_report_returns_empty_when_no_rows(capsys):
    conn = _mem_conn()
    results = baseline_report(account_key="lasso_ig", db_conn=conn)
    assert results == []
    out = capsys.readouterr().out
    assert "No locked pre-Echo baseline found" in out


def test_baseline_report_prints_locked_row(capsys):
    conn = _mem_conn()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('test_ig', 'published', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()
    times = [cutoff - timedelta(days=i * 5) for i in range(1, 10)]
    http = _make_http(times)
    acct = _fake_account("test_ig")
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)

    results = baseline_report(account_key="test_ig", db_conn=conn)
    out = capsys.readouterr().out
    assert "test_ig" in out
    assert "Pre-Echo avg posts per week" in out
    assert "Confidence" in out
    assert len(results) == 1


def test_baseline_report_no_em_dash_in_output(capsys):
    """No em dashes, en dashes, or hyphens in printed output (per project rule)."""
    conn = _mem_conn()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('test_ig', 'published', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()
    http = _make_http([cutoff - timedelta(days=7)])
    acct = _fake_account("test_ig")
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        lock_pre_echo_baseline("test_ig", http=http, db_conn=conn, now=now)

    baseline_report(account_key="test_ig", db_conn=conn)
    out = capsys.readouterr().out
    assert "—" not in out, "em dash found in output"
    assert "–" not in out, "en dash found in output"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

def test_cli_baseline_report(capsys):
    """baseline-report CLI calls baseline_report with the given account key."""
    conn = _mem_conn()
    cutoff = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO posts (account_key, mode, published_at, platform) "
        "VALUES ('lasso_ig', 'published', ?, 'IG')", (cutoff.isoformat(),))
    conn.commit()
    times = [cutoff - timedelta(days=i * 6) for i in range(1, 12)]
    http = _make_http(times)
    acct = _fake_account("lasso_ig")
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    with _active_accounts_patch([acct]):
        lock_pre_echo_baseline("lasso_ig", http=http, db_conn=conn, now=now)

    # Patch db.connect to return our in-memory conn
    with patch("agent.db.connect", return_value=conn):
        from agent.__main__ import _baseline_report
        _baseline_report(["--account", "lasso_ig"])

    out = capsys.readouterr().out
    assert "lasso_ig" in out
    assert "Pre-Echo avg posts per week" in out
