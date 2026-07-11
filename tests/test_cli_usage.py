"""
CLI truth: help lists every command the dispatcher accepts, an unknown
command prints the command list (not a flag dump), and run-daily output
states the reason instead of a bare contradictory count.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import agent.__main__ as mm
from agent.drafter import Draft, DraftStatus


def _dispatch_commands():
    """Every literal command main() accepts, parsed from its source."""
    src = inspect.getsource(mm.main)
    cmds = set(re.findall(r'cmd == "([a-z0-9-]+)"', src))
    for group in re.findall(r'cmd in \(([^)]*)\)', src):
        cmds.update(re.findall(r'"([a-z0-9-]+)"', group))
    return cmds - {"--help", "-h"}


def test_usage_covers_every_dispatch_command(capsys):
    mm._usage()
    out = capsys.readouterr().out
    missing = [c for c in sorted(_dispatch_commands()) if c not in out]
    assert not missing, (
        f"help omits commands the dispatcher accepts: {missing} — "
        "add them to _COMMANDS in agent/__main__.py")


def test_unknown_command_prints_command_list(capsys):
    mm.main(["calender"])  # a typo
    out = capsys.readouterr().out
    assert "unknown command: calender" in out
    assert "usage: python -m agent" in out
    assert "run-daily" in out
    # the old behavior dumped the flag list instead of the command list
    assert "capability flags" not in out


def test_help_command_exists(capsys):
    mm.main(["help"])
    out = capsys.readouterr().out
    assert "usage: python -m agent" in out


def _fake_draft(status):
    return Draft(draft_id="d", account_key="a", platform="instagram",
                 caption="", hashtags=[], creative_path="",
                 creative_public_url="", scheduled_for="", status=status,
                 day_key="2026-07-08", draft_type="feed")


def test_run_daily_print_disabled_reason(capsys):
    mm._print_run_daily({"status": "disabled", "drafts": []})
    out = capsys.readouterr().out
    assert "AGENT_ENABLED" in out
    assert "Nothing drafted" in out


def test_run_daily_print_no_voice_reason(capsys):
    mm._print_run_daily({"status": "no_voice", "drafts": []})
    out = capsys.readouterr().out
    assert "voice doc" in out.lower()


def test_run_daily_print_zero_drafts_names_possible_causes(capsys):
    mm._print_run_daily({"status": "drafted", "drafts": []})
    out = capsys.readouterr().out
    assert "0 draft(s)" in out
    assert "skip day" in out


def test_run_daily_print_splits_pending_and_blocked(capsys):
    drafts = [_fake_draft(DraftStatus.PENDING), _fake_draft(DraftStatus.BLOCKED)]
    mm._print_run_daily({"status": "drafted", "drafts": drafts})
    out = capsys.readouterr().out
    assert "1 pending" in out and "1 blocked" in out


def test_run_daily_print_survives_missing_drafts_key(capsys):
    """The old print indexed out['drafts'] unconditionally — KeyError on an
    early-return payload."""
    mm._print_run_daily({"status": "drafted"})
    out = capsys.readouterr().out
    assert "0 draft(s)" in out
