"""
Regression: LASSO held-media repair + paired-Story preparation must run AFTER
every calendar-mutating job in run_daily (drafting, refills, grade_sweep's
grade_fix caption remediation) and immediately before the final calendar
publish block — exactly once per draw.

Pre-fix, the pairing tick ran at the TOP of run_daily and bound each staged
Story to the feed caption present at that moment. A caption later repaired by
grade_sweep/grade_fix failed the exact caption-equality proof, holding the
feed until the next nightly draw (~24h strand). Post-fix, the preparation
sequence sees the FINAL caption in the same draw.

These tests FAIL on pre-fix code (pairing ran before grade_sweep and observed
the pre-remediation caption) and PASS after the fix.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform
from agent.runner import run_daily
from agent.store import PendingStore

_VOICE = """# Voice
We help gym owners grow.
## CTAs
- Save this post.
## Hashtags
#LASSOFramework
"""


class _FakePoster:
    def post_approval_card(self, draft):
        return {"channel": "C1", "ts": "ts1"}

    def post_notice(self, text):
        return {"ok": True}

    def mark_superseded(self, draft):
        pass

    def mark_expired(self, draft):
        pass


def _lasso_account():
    return Account(key="lasso_ig", display_name="LASSO IG",
                   platform=Platform.INSTAGRAM,
                   token_env="DUMMY_TOK", target_id_env="DUMMY_TGT")


def _world(tmp_path):
    voice = tmp_path / "voice.md"
    voice.write_text(_VOICE, encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "asset.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    (lib / "asset.txt").write_text("An approved note.", encoding="utf-8")
    return str(voice), str(lib)


def _wire_preparation_spies(monkeypatch, events, calendar_state):
    """Replace the two preparation jobs with spies that append to `events` and,
    for the paired-Story stager, record the caption it would bind at call time
    (the job binds a source_hash over feed['caption'] when it stages)."""
    import agent.jobs.lasso_held_media_repair as held_repair
    import agent.jobs.lasso_daily_paired_stories as paired

    def _held_run(now=None, account_key=None, **kwargs):
        events.append(("held_media_repair", account_key))
        return {"ok": True, "attempted": 0, "generated": 0, "reused": 0,
                "repaired": 0, "skipped": 0, "errors": 0}

    def _paired_run(now=None, account=None, catchup_days=0):
        caption_at_call = calendar_state["feeds"][account]["caption"]
        events.append(("paired_stories", account, caption_at_call))
        return {"staged": 0, "generated": 0, "reused": 0, "occupied": 0,
                "blocked": 0, "reason": "ok"}

    monkeypatch.setattr(held_repair, "run", _held_run)
    monkeypatch.setattr(paired, "run", _paired_run)


def _wire_grade_sweep_caption_repair(monkeypatch, events, calendar_state):
    """grade_sweep is not read-only: with self_fix armed, grade_fix regenerates
    forward-book captions. The spy mutates tomorrow's captions the way a real
    remediation would, so the pairing spy can observe which caption it saw."""
    import agent.jobs.grade_sweep as grade_sweep

    def _grade_run():
        events.append(("grade_sweep",))
        for feed in calendar_state["feeds"].values():
            feed["caption"] = feed["caption"] + " [remediated]"
        return {"ok": True, "gyms": {"lasso": {}}}

    monkeypatch.setattr(grade_sweep, "run", _grade_run)


def _wire_publish_due_spy(monkeypatch, events):
    import agent.calendar_autopublish as autopublish

    def _publish_due(*args, **kwargs):
        events.append(("publish_due",))
        return {"ok": True, "published": [], "skipped": [], "failed": []}

    monkeypatch.setattr(autopublish, "publish_due", _publish_due)


def test_preparation_runs_after_grade_sweep_and_sees_final_caption(
        monkeypatch, tmp_path):
    """(a) Calendar/grade mutations happen first; the Story stager binds the
    post-remediation caption in the SAME draw, before the publish block."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_LASSO_3X_ENABLED", "true")
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    voice, lib = _world(tmp_path)

    events = []
    calendar_state = {"feeds": {
        "instagram": {"caption": "draft caption ig"},
        "facebook": {"caption": "draft caption fb"},
    }}
    _wire_preparation_spies(monkeypatch, events, calendar_state)
    _wire_grade_sweep_caption_repair(monkeypatch, events, calendar_state)
    _wire_publish_due_spy(monkeypatch, events)

    out = run_daily(poster=_FakePoster(), voice_path=voice, library_path=lib,
                    scheduled_for="2026-10-05T14:30:00+00:00",
                    accounts=[_lasso_account()], store=PendingStore(path=db_path))
    assert out.get("status") == "drafted"

    kinds = [e[0] for e in events]
    assert "grade_sweep" in kinds, "grade_sweep spy never ran"
    paired = [e for e in events if e[0] == "paired_stories"]
    held = [e for e in events if e[0] == "held_media_repair"]

    # Both accounts, exactly once per draw each.
    assert {e[1] for e in paired} == {"instagram", "facebook"}
    assert len(paired) == 2, f"pairing ran {len(paired)} times, expected once per account"
    assert {e[1] for e in held} == {"lasso_ig", "lasso_fb"}

    # Held-feed repair strictly before Story preparation.
    last_held = len(kinds) - 1 - kinds[::-1].index("held_media_repair")
    assert last_held < kinds.index("paired_stories")

    # Every calendar mutation (grade_sweep remediation) lands BEFORE pairing,
    # so the stager binds the FINAL caption, not the pre-remediation one.
    assert kinds.index("grade_sweep") < kinds.index("paired_stories")
    for _, account, caption_seen in paired:
        assert caption_seen.endswith(" [remediated]"), (
            f"{account} pairing bound the pre-remediation caption "
            f"{caption_seen!r} — preparation ran before calendar mutations")

    # Preparation completes before the final publish block of the same draw.
    if "publish_due" in kinds:
        last_paired = len(kinds) - 1 - kinds[::-1].index("paired_stories")
        assert last_paired < kinds.index("publish_due")


def test_preparation_gate_flag_disabled_runs_nothing(monkeypatch, tmp_path):
    """(b) With AGENT_LASSO_3X_ENABLED off, no held-media repair and no
    paired-Story preparation calls occur at all."""
    db_path = str(tmp_path / "echo.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("AGENT_ENABLED", "true")
    monkeypatch.delenv("AGENT_LASSO_3X_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_CALENDAR_AUTOPUBLISH", "true")
    voice, lib = _world(tmp_path)

    events = []
    calendar_state = {"feeds": {
        "instagram": {"caption": "draft caption ig"},
        "facebook": {"caption": "draft caption fb"},
    }}
    _wire_preparation_spies(monkeypatch, events, calendar_state)
    _wire_publish_due_spy(monkeypatch, events)

    out = run_daily(poster=_FakePoster(), voice_path=voice, library_path=lib,
                    scheduled_for="2026-10-05T14:30:00+00:00",
                    accounts=[_lasso_account()], store=PendingStore(path=db_path))
    assert out.get("status") == "drafted"

    assert [e for e in events if e[0] == "held_media_repair"] == []
    assert [e for e in events if e[0] == "paired_stories"] == []
