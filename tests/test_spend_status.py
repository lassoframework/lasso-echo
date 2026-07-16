"""
Spend surface tests (visibility only, no auto-reload).

Covers: snapshot with the cap armed and not armed; the CLI lines in both
states; the digest alert fires at the 80% threshold; the alert does not fire
twice (dedup); the [SPEND ALERT] line lands in the digest output when armed.
Counters are bumped through the real spend gate (creative_studio.spend_allowed).
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import spend  # noqa: E402
from agent.creative_studio import spend_allowed  # noqa: E402

DAY = "2026-07-16"


def _arm(monkeypatch, cap="10"):
    monkeypatch.setenv("AGENT_SPEND_CAP_ENABLED", "true")
    monkeypatch.setenv("AGENT_GEMINI_DAILY_CAP", cap)


def _disarm(monkeypatch, cap="10"):
    monkeypatch.delenv("AGENT_SPEND_CAP_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_GEMINI_DAILY_CAP", cap)


def _burn(account_key, n, day=DAY):
    for _ in range(n):
        spend_allowed(account_key=account_key, day=day)


def test_snapshot_with_cap_armed(monkeypatch):
    _arm(monkeypatch, cap="10")
    _burn("lasso_ig", 3)
    snap = spend.spend_snapshot(day=DAY)
    assert snap["cap"] == 10
    assert snap["cap_armed"] is True
    assert snap["day"] == DAY
    by_label = {b["bucket"]: b for b in snap["buckets"]}
    # active accounts and the shared pool always present
    assert "lasso_ig" in by_label
    assert "shared pool" in by_label
    assert by_label["lasso_ig"]["calls"] == 3
    assert by_label["lasso_ig"]["pct"] == 30.0
    # shared pool untouched
    assert by_label["shared pool"]["calls"] == 0


def test_snapshot_without_cap_armed(monkeypatch):
    _disarm(monkeypatch, cap="10")
    # flag OFF: spend_allowed does not bump, so counts stay at zero
    _burn("lasso_ig", 5)
    snap = spend.spend_snapshot(day=DAY)
    assert snap["cap_armed"] is False
    by_label = {b["bucket"]: b for b in snap["buckets"]}
    assert by_label["lasso_ig"]["calls"] == 0


def test_status_lines_armed_and_disarmed(monkeypatch):
    _arm(monkeypatch, cap="10")
    _burn("lasso_ig", 2)
    armed = "\n".join(spend.spend_status_lines(day=DAY))
    assert "[ARMED]" in armed
    assert "Cap per account: 10 calls" in armed
    assert "Auto-reload: not configured" in armed
    # no hyphens, no em/en dashes anywhere in the output copy
    for ch in ("—", "–"):
        assert ch not in armed

    _disarm(monkeypatch, cap="10")
    off = "\n".join(spend.spend_status_lines(day=DAY))
    assert "NOT ARMED" in off
    assert "no cap enforced" in off


def test_alert_fires_at_threshold(monkeypatch):
    _arm(monkeypatch, cap="10")
    # below 80%: no alert
    _burn("lasso_ig", 7)
    assert spend.should_alert_spend("lasso_ig", day=DAY) is False
    # cross into 80% (8/10)
    _burn("lasso_ig", 1)
    assert spend.should_alert_spend("lasso_ig", day=DAY) is True


def test_alert_silent_when_cap_not_armed(monkeypatch):
    _disarm(monkeypatch, cap="10")
    # even a full bucket does not alert when the cap is not armed
    assert spend.should_alert_spend("lasso_ig", day=DAY) is False


def test_alert_does_not_fire_twice(monkeypatch):
    _arm(monkeypatch, cap="10")
    _burn("lasso_ig", 8)
    assert spend.should_alert_spend("lasso_ig", day=DAY) is True
    spend.mark_spend_alerted("lasso_ig", day=DAY)
    # dedup: already alerted today
    assert spend.should_alert_spend("lasso_ig", day=DAY) is False


def test_digest_line_appears_when_alerted(monkeypatch):
    from agent import digest
    _arm(monkeypatch, cap="10")
    monkeypatch.setenv("AGENT_DIGEST_ENABLED", "true")
    monkeypatch.setenv("AGENT_DIGEST_HOUR_UTC", "23")

    # burn lasso_ig to 80% of cap for TODAY (maybe_send uses the real today)
    today = datetime.now(timezone.utc).date().isoformat()
    _burn("lasso_ig", 8, day=today)

    class Poster:
        def __init__(self):
            self.notices = []

        def post_notice(self, text):
            self.notices.append(text)
            return {"ok": True}

    poster = Poster()
    at_hour = datetime.now(timezone.utc).replace(hour=23, minute=5)
    combined = digest.maybe_send(poster, now=at_hour)
    assert combined is not None
    assert "[SPEND ALERT] lasso_ig" in combined
    assert "80% of daily cap (8/10)" in combined
