"""tests/test_mentions.py — Wave 4 AGENT_MENTIONS tag_allowlist tests.

Uses injectable fake stores so no Supabase call is ever made.
All tests run with AGENT_MENTIONS unset (default OFF) unless explicitly patched.
"""
from __future__ import annotations
import os
import pytest

# ---------------------------------------------------------------------------
# Fake store helpers
# ---------------------------------------------------------------------------

class FakeStore:
    """Injectable store for tag_allowlist functions."""

    def __init__(self, rows: list[dict]):
        # rows: list of {gym_id, handle, kind, consent}
        self._rows = rows

    def get_allowlist(self, gym_id: str) -> list[dict]:
        return [r for r in self._rows if r["gym_id"] == gym_id]


def _make_store(*rows) -> FakeStore:
    return FakeStore(list(rows))


def _row(gym_id, handle, kind, consent=True):
    return {"gym_id": gym_id, "handle": handle, "kind": kind, "consent": consent}


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from agent.tag_allowlist import (
    allowlisted_handles,
    validate_mentions,
    handles_for_category,
)


# ---------------------------------------------------------------------------
# 1. validate_mentions: handle on allowlist (own, consent=True) -> returned
# ---------------------------------------------------------------------------

def test_validate_mentions_own_consented():
    store = _make_store(_row("gym1", "myhandle", "own", consent=True))
    result = validate_mentions("gym1", ["myhandle"], store=store)
    assert result == ["myhandle"]


# ---------------------------------------------------------------------------
# 2. validate_mentions: handle NOT on allowlist -> silently dropped
# ---------------------------------------------------------------------------

def test_validate_mentions_not_on_allowlist():
    store = _make_store(_row("gym1", "myhandle", "own", consent=True))
    result = validate_mentions("gym1", ["stranger_handle"], store=store)
    assert result == []


# ---------------------------------------------------------------------------
# 3. validate_mentions: member handle without consent -> silently dropped
# ---------------------------------------------------------------------------

def test_validate_mentions_member_no_consent():
    store = _make_store(_row("gym1", "jane_fit", "member", consent=False))
    result = validate_mentions("gym1", ["jane_fit"], store=store)
    assert result == []


# ---------------------------------------------------------------------------
# 4. validate_mentions: member handle WITH consent -> returned
# ---------------------------------------------------------------------------

def test_validate_mentions_member_with_consent():
    store = _make_store(_row("gym1", "jane_fit", "member", consent=True))
    result = validate_mentions("gym1", ["jane_fit"], store=store)
    assert result == ["jane_fit"]


# ---------------------------------------------------------------------------
# 5. handles_for_category: AGENT_MENTIONS=OFF -> returns []
# ---------------------------------------------------------------------------

def test_handles_for_category_flag_off(monkeypatch):
    monkeypatch.delenv("AGENT_MENTIONS", raising=False)
    store = _make_store(_row("gym1", "coach_mike", "coach", consent=True))
    result = handles_for_category("gym1", "faces", store=store)
    assert result == []


# ---------------------------------------------------------------------------
# 6. handles_for_category: AGENT_MENTIONS=ON, category='faces' -> coach handles
# ---------------------------------------------------------------------------

def test_handles_for_category_faces_returns_coaches(monkeypatch):
    monkeypatch.setenv("AGENT_MENTIONS", "true")
    store = _make_store(
        _row("gym1", "coach_mike", "coach", consent=True),
        _row("gym1", "member_jane", "member", consent=True),
        _row("gym1", "gymhandle", "own", consent=True),
    )
    result = handles_for_category("gym1", "faces", store=store)
    assert "coach_mike" in result
    assert "member_jane" not in result
    assert "gymhandle" not in result


# ---------------------------------------------------------------------------
# 7. allowlisted_handles: kind='member', consent_only=True -> only consented members
# ---------------------------------------------------------------------------

def test_allowlisted_handles_member_consent_only(monkeypatch):
    store = _make_store(
        _row("gym1", "member_yes", "member", consent=True),
        _row("gym1", "member_no", "member", consent=False),
        _row("gym1", "coach_bob", "coach", consent=True),
    )
    result = allowlisted_handles("gym1", kind="member", consent_only=True, store=store)
    assert "member_yes" in result
    assert "member_no" not in result
    assert "coach_bob" not in result


# ---------------------------------------------------------------------------
# 8. copy_gate.scrub leaves @handle untouched in caption
# ---------------------------------------------------------------------------

def test_copy_gate_scrub_preserves_handle():
    from agent.copy_gate import scrub
    text = "@coach_amanda great session"
    result = scrub(text)
    assert "@coach_amanda" in result


# ---------------------------------------------------------------------------
# Bonus: validate_mentions strips leading @ on input handles
# ---------------------------------------------------------------------------

def test_validate_mentions_strips_leading_at():
    store = _make_store(_row("gym1", "myhandle", "own", consent=True))
    result = validate_mentions("gym1", ["@myhandle"], store=store)
    assert result == ["myhandle"]


# ---------------------------------------------------------------------------
# Bonus: handles_for_category with empty allowlist -> []
# ---------------------------------------------------------------------------

def test_handles_for_category_empty_allowlist(monkeypatch):
    monkeypatch.setenv("AGENT_MENTIONS", "true")
    store = _make_store()  # no rows
    result = handles_for_category("gym1", "results", store=store)
    assert result == []


# ---------------------------------------------------------------------------
# Bonus: validate_mentions with empty input -> []
# ---------------------------------------------------------------------------

def test_validate_mentions_empty_input():
    store = _make_store(_row("gym1", "myhandle", "own", consent=True))
    result = validate_mentions("gym1", [], store=store)
    assert result == []


# ---------------------------------------------------------------------------
# Bonus: handles_for_category 'results' returns member (consented) + own
# ---------------------------------------------------------------------------

def test_handles_for_category_results(monkeypatch):
    monkeypatch.setenv("AGENT_MENTIONS", "true")
    store = _make_store(
        _row("gym1", "member_jane", "member", consent=True),
        _row("gym1", "member_no", "member", consent=False),
        _row("gym1", "gymhandle", "own", consent=True),
        _row("gym1", "coach_mike", "coach", consent=True),
    )
    result = handles_for_category("gym1", "results", store=store)
    assert "member_jane" in result
    assert "gymhandle" in result
    assert "member_no" not in result
    assert "coach_mike" not in result
