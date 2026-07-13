"""
Services content category tests (Track 5).

Gates verified:
  - Services NEVER drafts for a client account.
  - An empty or stub-only lasso_services.md produces SKIP + ops alert, never a draft.
  - A file with real (non-TODO, non-heading) content returns the slot dict.
  - Flag defaults OFF: nothing drafts even for a LASSO account.
  - SERVICES_SLOT_INTERVAL sits in the 10-14 day range.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from agent.content_categories import (
    SERVICES_SLOT_INTERVAL,
    draft_services_slot,
    is_lasso_own_account,
    is_services_stub,
)
from agent.accounts import Account, Platform


# ---- helpers -----------------------------------------------------------------------

def _account(key, platform=Platform.INSTAGRAM):
    return Account(
        key=key,
        display_name=key,
        platform=platform,
        token_env="AGENT_TEST_TOKEN",
        target_id_env="AGENT_TEST_ID",
    )


# ---- 1. services NEVER for a client account ----------------------------------------

def test_services_never_for_client(monkeypatch, tmp_path):
    """A client account (non-lasso_ prefix) must never get a services slot."""
    monkeypatch.setenv("AGENT_SERVICES_CATEGORY", "true")

    # Write a fully-populated source doc so the only gate that could fire is the
    # account check, not the stub check.
    src = tmp_path / "lasso_services.md"
    src.write_text(
        "# LASSO Services\n\nWe help gyms grow. Real offer. Real results.\n",
        encoding="utf-8",
    )

    client = _account("gym_alpha_ig")
    result = draft_services_slot(client, source_path=str(src))
    assert result is None


# ---- 2. skips an empty source doc --------------------------------------------------

def test_services_skips_empty_doc(monkeypatch, tmp_path):
    """An empty lasso_services.md returns None and fires an ops alert."""
    monkeypatch.setenv("AGENT_SERVICES_CATEGORY", "true")

    src = tmp_path / "lasso_services.md"
    src.write_text("", encoding="utf-8")

    fired = []

    import agent.ops_alerts as _ops
    monkeypatch.setattr(_ops, "alert", lambda msg, **kw: fired.append(msg))

    account = _account("lasso_ig")
    result = draft_services_slot(account, source_path=str(src))

    assert result is None
    assert len(fired) == 1
    assert "services" in fired[0].lower()


# ---- 3. skips a stub-only doc (all TODO lines) -------------------------------------

def test_services_skips_stub_doc(monkeypatch, tmp_path):
    """A file whose every non-blank line is a heading or TODO is treated as a stub."""
    monkeypatch.setenv("AGENT_SERVICES_CATEGORY", "true")

    src = tmp_path / "lasso_services.md"
    src.write_text(
        "# LASSO Services\n\n"
        "TODO: Fill in the real service offerings here.\n\n"
        "## Done-for-You Organic Social\n\n"
        "TODO: Add the real offer here.\n",
        encoding="utf-8",
    )

    fired = []
    import agent.ops_alerts as _ops
    monkeypatch.setattr(_ops, "alert", lambda msg, **kw: fired.append(msg))

    account = _account("lasso_ig")
    result = draft_services_slot(account, source_path=str(src))

    assert result is None
    assert len(fired) == 1


# ---- 4. returns slot dict when doc has real content --------------------------------

def test_services_returns_slot_with_content(monkeypatch, tmp_path):
    """A doc with at least one real non-TODO line unlocks the slot."""
    monkeypatch.setenv("AGENT_SERVICES_CATEGORY", "true")

    src = tmp_path / "lasso_services.md"
    src.write_text(
        "# LASSO Services\n\n"
        "We build done-for-you organic social for gym owners who want leads.\n",
        encoding="utf-8",
    )

    account = _account("lasso_ig")
    result = draft_services_slot(account, source_path=str(src))

    assert result is not None
    assert result["category"] == "services"
    assert result["account_key"] == "lasso_ig"
    assert result["source"] == str(src)


# ---- 5. flag defaults OFF ----------------------------------------------------------

def test_services_off_by_default(monkeypatch, tmp_path):
    """Without AGENT_SERVICES_CATEGORY set, nothing drafts even for a LASSO account."""
    monkeypatch.delenv("AGENT_SERVICES_CATEGORY", raising=False)

    src = tmp_path / "lasso_services.md"
    src.write_text(
        "# LASSO Services\n\nReal content that would normally pass the stub check.\n",
        encoding="utf-8",
    )

    account = _account("lasso_ig")
    result = draft_services_slot(account, source_path=str(src))
    assert result is None


# ---- 6. SERVICES_SLOT_INTERVAL in range 10-14 --------------------------------------

def test_services_slot_interval():
    """SERVICES_SLOT_INTERVAL must be between 10 and 14 days inclusive."""
    assert 10 <= SERVICES_SLOT_INTERVAL <= 14
