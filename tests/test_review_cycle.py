"""
14-day review cycle (Stage 2 Part 2). Offline.

Asserts: the assembler windows on AGENT_REVIEW_WINDOW_DAYS (default 14) and is
exercised at 7, 14, and 30; the pre-Echo posting-cadence baseline comparison
stays on the fixed 30-day basis whatever the window; the creative refresh ask
fires exactly once per cycle per account and only when AGENT_REVIEW_CYCLE_ENABLED
is armed.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, day30, db, ops_alerts  # noqa: E402


def _seed(account_key, days_ago_list):
    with db.connect() as conn:
        base = datetime.now(timezone.utc)
        for i, days_ago in enumerate(days_ago_list):
            conn.execute(
                "INSERT INTO posts (draft_id, account_key, platform, caption, "
                "media_id, mode, published_at, likes, views) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"rc{account_key}{i}", account_key, "instagram", f"c{i}",
                 f"m{i}", "published",
                 (base - timedelta(days=days_ago)).isoformat(), 10, 100))
        conn.commit()


# ---- window parameterization at 7, 14, 30 ------------------------------------------------

def test_window_7(monkeypatch):
    monkeypatch.setenv("AGENT_REVIEW_WINDOW_DAYS", "7")
    acct = "rw7_probe"
    _seed(acct, [2, 5, 10, 20, 28])            # 2 inside 7 days, 5 inside 30
    r = day30.assemble(acct)
    assert r["window_days"] == 7
    assert r["posts_published"] == 2
    assert r["baseline_window_days"] == 30


def test_window_14_is_the_default(monkeypatch):
    monkeypatch.delenv("AGENT_REVIEW_WINDOW_DAYS", raising=False)
    assert config.review_window_days() == 14
    acct = "rw14_probe"
    _seed(acct, [2, 5, 10, 20, 28])            # 3 inside 14 days
    r = day30.assemble(acct)
    assert r["window_days"] == 14
    assert r["posts_published"] == 3


def test_window_30(monkeypatch):
    monkeypatch.setenv("AGENT_REVIEW_WINDOW_DAYS", "30")
    acct = "rw30_probe"
    _seed(acct, [2, 5, 10, 20, 28])            # all 5 inside 30 days
    r = day30.assemble(acct)
    assert r["window_days"] == 30
    assert r["posts_published"] == 5


def test_baseline_comparison_stays_30_day_basis(monkeypatch):
    """The cadence multiplier never shrinks with a shorter cycle: freq_after is
    measured over the fixed 30-day basis whatever the review window is."""
    acct = "rwbase_probe"
    _seed(acct, [2, 5, 10, 20, 28])
    monkeypatch.setenv("AGENT_REVIEW_WINDOW_DAYS", "30")
    after_30 = day30.assemble(acct)["freq_after"]
    monkeypatch.setenv("AGENT_REVIEW_WINDOW_DAYS", "7")
    after_7 = day30.assemble(acct)["freq_after"]
    assert after_7 == after_30, "baseline comparison drifted with the window"


def test_title_names_the_cycle(monkeypatch):
    from agent.accounts import get_account
    acct_key = "lasso_ig"
    monkeypatch.setenv("AGENT_REVIEW_WINDOW_DAYS", "14")
    r = day30.assemble(acct_key)
    text = day30.render_text(get_account(acct_key), r)
    assert "CYCLE REPORT (14 DAYS)" in text
    monkeypatch.setenv("AGENT_REVIEW_WINDOW_DAYS", "30")
    r = day30.assemble(acct_key)
    text = day30.render_text(get_account(acct_key), r)
    assert "DAY 30 REPORT" in text            # the 30-day title is unchanged


# ---- creative refresh ask: once per cycle, flag gated -------------------------------------

def _kv_delete(key):
    with db._lock, db.connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
        conn.commit()


def test_refresh_ask_flag_off_never_fires(monkeypatch):
    monkeypatch.delenv("AGENT_REVIEW_CYCLE_ENABLED", raising=False)
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    assert day30.maybe_refresh_ask("ra_off_probe") is False
    assert fired == []


def test_refresh_ask_fires_once_per_cycle(monkeypatch):
    monkeypatch.setenv("AGENT_REVIEW_CYCLE_ENABLED", "true")
    acct = "ra_once_probe"
    idx = day30.cycle_index()
    _kv_delete(f"refresh_ask_{acct}_{idx}")
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    assert day30.maybe_refresh_ask(acct) is True
    assert day30.maybe_refresh_ask(acct) is False   # same cycle: no re-ask
    assert len(fired) == 1
    assert acct in fired[0] and "refresh" in fired[0]
    _kv_delete(f"refresh_ask_{acct}_{idx}")


def test_refresh_ask_fires_again_next_cycle(monkeypatch):
    monkeypatch.setenv("AGENT_REVIEW_CYCLE_ENABLED", "true")
    monkeypatch.setenv("AGENT_REVIEW_WINDOW_DAYS", "14")
    acct = "ra_next_probe"
    now = datetime.now(timezone.utc)
    later = now + timedelta(days=14)
    for t in (now, later):
        _kv_delete(f"refresh_ask_{acct}_{day30.cycle_index(now=t)}")
    fired = []
    monkeypatch.setattr(ops_alerts, "alert", lambda msg, **kw: fired.append(msg))
    assert day30.maybe_refresh_ask(acct, now=now) is True
    assert day30.maybe_refresh_ask(acct, now=now) is False
    assert day30.maybe_refresh_ask(acct, now=later) is True  # a new cycle asks anew
    assert len(fired) == 2
    for t in (now, later):
        _kv_delete(f"refresh_ask_{acct}_{day30.cycle_index(now=t)}")


def test_refresh_ask_stamp_after_post(monkeypatch):
    """Silent-miss law: a failed alert post leaves the cycle un-stamped so the
    next pass retries the ask."""
    monkeypatch.setenv("AGENT_REVIEW_CYCLE_ENABLED", "true")
    acct = "ra_fail_probe"
    idx = day30.cycle_index()
    _kv_delete(f"refresh_ask_{acct}_{idx}")

    def _boom(msg, **kw):
        raise RuntimeError("slack down")

    monkeypatch.setattr(ops_alerts, "alert", _boom)
    try:
        day30.maybe_refresh_ask(acct)
    except RuntimeError:
        pass
    assert db.kv_get(f"refresh_ask_{acct}_{idx}") == "", (
        "stamp must not advance when the alert post fails")
    _kv_delete(f"refresh_ask_{acct}_{idx}")
