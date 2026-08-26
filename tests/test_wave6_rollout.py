"""tests/test_wave6_rollout.py — Wave 6 rollout infrastructure tests.

5 required tests per spec:
  1. calendar_grade_enabled_for('lasso'): with global OFF + per-gym ON -> True
  2. calendar_grade_enabled_for('eng'): with global OFF + no per-gym flag -> False
  3. calendar_grade_enabled_for('topfuel'): with global ON -> True (inherits)
  4. rollout_digest.run() returns list of strings with gym name + "READY FOR FLAG FLIP"
  5. WAVE6_HUMAN_TAPS.md exists and mentions both human taps
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# 1. Per-gym flag ON overrides global OFF
# ---------------------------------------------------------------------------

def test_calendar_grade_enabled_for_per_gym_on_overrides_global_off(monkeypatch):
    """AGENT_CALENDAR_GRADE=false but AGENT_CALENDAR_GRADE_LASSO=true -> True for lasso."""
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "false")
    monkeypatch.setenv("AGENT_CALENDAR_GRADE_LASSO", "true")
    from agent import config
    assert config.calendar_grade_enabled_for("lasso") is True


# ---------------------------------------------------------------------------
# 2. No per-gym flag -> inherits global (OFF)
# ---------------------------------------------------------------------------

def test_calendar_grade_enabled_for_no_per_gym_flag_inherits_global_off(monkeypatch):
    """AGENT_CALENDAR_GRADE=false + no AGENT_CALENDAR_GRADE_ENG -> False for eng."""
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "false")
    monkeypatch.delenv("AGENT_CALENDAR_GRADE_ENG", raising=False)
    from agent import config
    assert config.calendar_grade_enabled_for("eng") is False


# ---------------------------------------------------------------------------
# 3. No per-gym flag -> inherits global (ON)
# ---------------------------------------------------------------------------

def test_calendar_grade_enabled_for_inherits_global_on(monkeypatch):
    """AGENT_CALENDAR_GRADE=true + no per-gym flag for topfuel -> True (inherits)."""
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")
    monkeypatch.delenv("AGENT_CALENDAR_GRADE_TOPFUEL", raising=False)
    from agent import config
    assert config.calendar_grade_enabled_for("topfuel") is True


# ---------------------------------------------------------------------------
# 4. rollout_digest.run() returns list of strings with gym name + "READY FOR FLAG FLIP"
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal injectable store for digest tests. Returns no data for any gym."""
    def latest_grade(self, gym_id, window):
        return None

    def count_denied_purge(self, gym_id):
        return 3

    def count_pending(self, gym_id):
        return 14

    def count_allowlist(self, gym_id):
        return 2


def test_rollout_digest_run_returns_list_with_expected_content(monkeypatch):
    """run() returns a list of strings; each entry contains the gym name and the
    'READY FOR FLAG FLIP' sentinel that signals the tap is pending."""
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "true")
    from agent.jobs import rollout_digest

    store = _FakeStore()
    result = rollout_digest.run(gyms=["lasso", "eng"], store=store)

    assert isinstance(result, list), "run() must return a list"
    assert len(result) == 2, f"expected 2 digests, got {len(result)}"

    for digest in result:
        assert isinstance(digest, str), "each digest must be a string"
        assert "READY FOR FLAG FLIP" in digest, (
            "each digest must include 'READY FOR FLAG FLIP' for the human-tap sentinel"
        )

    # gym names (upper-cased in the digest header)
    assert "LASSO" in result[0]
    assert "ENG" in result[1]


def test_rollout_digest_run_returns_flag_off_message_when_grade_off(monkeypatch):
    """When AGENT_CALENDAR_GRADE is OFF, run() returns a single informational string."""
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "false")
    from agent.jobs import rollout_digest

    result = rollout_digest.run(gyms=["lasso"])
    assert isinstance(result, list)
    assert len(result) == 1
    assert "AGENT_CALENDAR_GRADE" in result[0]


# ---------------------------------------------------------------------------
# 5. WAVE6_HUMAN_TAPS.md exists and mentions both human taps
# ---------------------------------------------------------------------------

def test_wave6_human_taps_md_exists_and_covers_both_taps():
    """WAVE6_HUMAN_TAPS.md must exist at the repo root and mention both human taps:
    TAP 1 (publisher disconnect) and TAP 2 (per-gym flag flip)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, "WAVE6_HUMAN_TAPS.md")
    assert os.path.isfile(path), "WAVE6_HUMAN_TAPS.md must exist at the repo root"

    content = open(path).read()

    assert "TAP 1" in content, "WAVE6_HUMAN_TAPS.md must document TAP 1 (publisher disconnect)"
    assert "TAP 2" in content, "WAVE6_HUMAN_TAPS.md must document TAP 2 (per-gym flag flips)"
    # Both taps should reference the human action required
    assert "PENDING BLAKE TAP" in content, (
        "both taps must be marked PENDING BLAKE TAP"
    )
    # TAP 1 — publisher disconnect evidence
    assert "wave0_publisher_finding" in content or "disconnect" in content.lower(), (
        "TAP 1 must reference the publisher disconnect"
    )
    # TAP 2 — per-gym flag names
    assert "AGENT_CALENDAR_GRADE_LASSO" in content, (
        "TAP 2 must list AGENT_CALENDAR_GRADE_LASSO as the first rollout step"
    )


# ---------------------------------------------------------------------------
# Bonus: hyphen-to-underscore normalization for gym_ids with dashes
# ---------------------------------------------------------------------------

def test_calendar_grade_enabled_for_normalizes_dash_in_gym_id(monkeypatch):
    """A gym_id like 'pierce-fitness' must resolve to AGENT_CALENDAR_GRADE_PIERCE_FITNESS."""
    monkeypatch.setenv("AGENT_CALENDAR_GRADE", "false")
    monkeypatch.setenv("AGENT_CALENDAR_GRADE_PIERCE_FITNESS", "true")
    monkeypatch.delenv("AGENT_CALENDAR_GRADE_PIERCE-FITNESS", raising=False)
    from agent import config
    assert config.calendar_grade_enabled_for("pierce-fitness") is True
