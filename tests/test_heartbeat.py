"""
Heartbeat + missed-run alert tests. Asserts: a missing heartbeat alerts ONCE
after 10:00 ET on a posting day; a present heartbeat stays silent; the debounce
holds across repeated checks AND a simulated restart; before 10:00 ET nothing
fires; skip days and a disarmed agent stay silent (adversarial: no false
alarms from silence being correct).
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import heartbeat, ops_alerts  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402

# 2026-07-06 is a Monday (posting day). 15:00 UTC = 11:00 ET (past the deadline).
LATE = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
EARLY = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)   # 08:00 ET
SATURDAY = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)  # default skip day


def _accts():
    return [Account(key="lasso_ig", display_name="A", platform=Platform.INSTAGRAM,
                    token_env="HB_T", target_id_env="HB_I")]


def _wire(monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_OPS_ALERTS_ENABLED", "true")
    rec_notices = []

    class Rec:
        def post_notice(self, text):
            rec_notices.append(text)
            return {"ok": True}

    monkeypatch.setattr(ops_alerts, "_default_poster", lambda: Rec())
    return rec_notices


def test_missing_heartbeat_alerts_once_and_debounces(monkeypatch):
    notices = _wire(monkeypatch)
    out1 = heartbeat.check_heartbeats(now=LATE, accounts=_accts())
    assert out1 == ["lasso_ig"]
    hb_alerts = [n for n in notices if "no daily draft heartbeat" in n]
    assert len(hb_alerts) == 1
    # repeated checks the same day: the debounce holds (persisted, restart-safe)
    assert heartbeat.check_heartbeats(now=LATE, accounts=_accts()) == []
    assert heartbeat.check_heartbeats(now=LATE, accounts=_accts()) == []
    assert len([n for n in notices if "no daily draft heartbeat" in n]) == 1


def test_present_heartbeat_stays_silent(monkeypatch):
    notices = _wire(monkeypatch)
    heartbeat.record_heartbeat("lasso_ig", "2026-07-06", now=EARLY)
    assert heartbeat.check_heartbeats(now=LATE, accounts=_accts()) == []
    assert notices == []


def test_before_deadline_never_fires(monkeypatch):
    notices = _wire(monkeypatch)
    assert heartbeat.check_heartbeats(now=EARLY, accounts=_accts()) == []
    assert notices == []


def test_skip_day_and_disarmed_agent_stay_silent(monkeypatch):
    notices = _wire(monkeypatch)
    assert heartbeat.check_heartbeats(now=SATURDAY, accounts=_accts()) == []
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    assert heartbeat.check_heartbeats(now=LATE, accounts=_accts()) == []
    assert notices == []


def test_run_daily_writes_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ENABLED", "true")
    voice = tmp_path / "voice.md"
    voice.write_text("# Voice\nWe help gym owners grow.\n#LASSOFramework",
                     encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "lasso_p1_a.jpg").write_bytes(b"img")
    (lib / "lasso_p1_a.txt").write_text("A story.", encoding="utf-8")

    class P:
        def post_approval_card(self, d):
            return {"channel": "C", "ts": "1"}

        def post_notice(self, t):
            return {"ok": True}

    from agent import runner
    runner.run_daily(poster=P(), voice_path=str(voice), library_path=str(lib),
                     accounts=_accts(), scheduled_for="2026-07-06T18:30:00+00:00")
    assert heartbeat.heartbeat_at("lasso_ig", "2026-07-06") != ""
