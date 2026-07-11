"""
Scheduler dormancy visibility: every scheduled lane announces armed/dormant
at startup, so a job that never fires is visibly off instead of silently
absent (the plan-month "0 written with no reason" class, applied to the
scheduled jobs).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.listener import _print_scheduled_lanes


def test_all_dormant_lanes_say_so(monkeypatch, capsys):
    for env in ("AGENT_INTAKE_ENABLED", "AGENT_OPUS_ENABLED",
                "AGENT_PODCAST_ENABLED", "AGENT_EPISODE_INBOX_ENABLED",
                "AGENT_REPORTING_ENABLED", "AGENT_DIGEST_ENABLED",
                "AGENT_WEEKLY_REPORT_ENABLED", "AGENT_BRAIN_PROPOSALS_ENABLED",
                "AGENT_BACKUP_ENABLED"):
        monkeypatch.delenv(env, raising=False)
    _print_scheduled_lanes()
    out = capsys.readouterr().out
    for lane in ("intake ingest", "podcast feed", "evening digest",
                 "weekly report", "nightly brain", "nightly backup"):
        assert f"[scheduler] {lane}: dormant" in out, out
    # each dormant line names the env var that arms it
    assert "AGENT_DIGEST_ENABLED" in out


def test_armed_lane_reads_armed(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DIGEST_ENABLED", "true")
    _print_scheduled_lanes()
    out = capsys.readouterr().out
    assert "[scheduler] evening digest: ARMED" in out
