"""
Gemini spend cap is per account (10-gym launch isolation).

The counter used to be one global "gemini_calls" bucket: one client's image
volume burned the shared daily cap and starved every other client's creative
for the rest of the day. Now each account has its own bucket; account-less
work (DAM autotag, library regen) shares the global bucket. One ops alert
per bucket per day, naming the bucket.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.creative_studio import spend_allowed

DAY = "2026-07-08"


def _arm(monkeypatch, tmp_path, cap="2"):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.setenv("AGENT_SPEND_CAP_ENABLED", "true")
    monkeypatch.setenv("AGENT_GEMINI_DAILY_CAP", cap)


def test_flag_off_allows_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))
    monkeypatch.delenv("AGENT_SPEND_CAP_ENABLED", raising=False)
    for _ in range(5):
        assert spend_allowed(account_key="gym_a", day=DAY) is True


def test_cap_applies_per_account(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, cap="2")
    assert spend_allowed(account_key="gym_a", day=DAY) is True
    assert spend_allowed(account_key="gym_a", day=DAY) is True
    assert spend_allowed(account_key="gym_a", day=DAY) is False  # gym_a spent


def test_one_account_cannot_starve_another(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, cap="1")
    assert spend_allowed(account_key="gym_a", day=DAY) is True
    assert spend_allowed(account_key="gym_a", day=DAY) is False
    # gym_b's bucket is untouched by gym_a's burn
    assert spend_allowed(account_key="gym_b", day=DAY) is True


def test_global_bucket_is_separate_from_accounts(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, cap="1")
    assert spend_allowed(account_key="gym_a", day=DAY) is True
    assert spend_allowed(account_key="gym_a", day=DAY) is False
    # account-less work still runs on its own bucket
    assert spend_allowed(account_key=None, day=DAY) is True
    assert spend_allowed(account_key=None, day=DAY) is False


def test_one_alert_per_bucket(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, cap="1")
    alerts = []
    monkeypatch.setattr("agent.ops_alerts.alert", lambda m, **kw: alerts.append(m))
    spend_allowed(account_key="gym_a", day=DAY)
    spend_allowed(account_key="gym_a", day=DAY)   # cap hit -> alert
    spend_allowed(account_key="gym_a", day=DAY)   # already alerted -> silent
    assert len(alerts) == 1
    assert "gym_a" in alerts[0]


def test_dam_shares_the_global_bucket(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, cap="1")
    from agent.dam import _spend_allowed
    assert _spend_allowed(DAY) is True
    assert _spend_allowed(DAY) is False
    # and the global burn does not touch an account bucket
    assert spend_allowed(account_key="gym_a", day=DAY) is True
