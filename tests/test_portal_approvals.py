"""
Portal approvals: Kill/Deny actions, per-gym scoping, flag gate.

Tests:
  - test_kill_requires_confirmation: kill without confirmed=True returns ok=False
  - test_kill_with_confirmation: kill with confirmed=True returns ok=True, concept banned
  - test_deny_blocks_draft: deny action marks draft BLOCKED
  - test_cross_gym_rejected: actor authorized for gym_a cannot act on gym_b
  - test_portal_flag_off_returns_error: every portal function returns ok=False when flag is OFF
  - test_trust_level_fail_safe: unknown trust value coerces to FULL_APPROVAL
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.accounts import Account, Platform
from agent.drafter import Draft, DraftStatus
from agent.trust import TrustLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account(key="gym_a", approvers=None):
    return Account(
        key=key,
        display_name=f"Gym {key}",
        platform=Platform.INSTAGRAM,
        token_env="TOK_ENV",
        target_id_env="TGT_ENV",
        approvers=approvers or [f"U_{key}_owner"],
    )


def _draft(draft_id="draft-001", account_key="gym_a",
           creative_path="/lib/hook_v1.png", status=DraftStatus.PENDING):
    return Draft(
        draft_id=draft_id,
        account_key=account_key,
        platform="instagram",
        caption="caption",
        hashtags=[],
        creative_path=creative_path,
        creative_public_url="",
        scheduled_for="2026-07-10T18:30:00+00:00",
        status=status,
        day_key="2026-07-10",
        draft_type="feed",
    )


def _make_store(draft):
    """Minimal injectable store: maps draft_id -> Draft."""
    return {draft.draft_id: draft}


# ---------------------------------------------------------------------------
# test_kill_requires_confirmation
# ---------------------------------------------------------------------------

def test_kill_requires_confirmation(monkeypatch):
    """kill without confirmed=True returns ok=False with a confirmation message."""
    acct = _account("gym_a", approvers=["U_gym_a_owner"])
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "false")

    draft = _draft(account_key="gym_a")
    store = _make_store(draft)

    from agent import portal_approvals
    result = portal_approvals.kill(
        account_key="gym_a",
        draft_id="draft-001",
        actor_id="U_gym_a_owner",
        confirmed=False,
        store=store,
    )
    assert result["ok"] is False, f"expected ok=False, got: {result}"
    assert "confirmation" in result["detail"].lower() or "confirmed=True" in result["detail"], (
        f"unexpected detail: {result['detail']}"
    )


# ---------------------------------------------------------------------------
# test_kill_with_confirmation
# ---------------------------------------------------------------------------

def test_kill_with_confirmation(monkeypatch):
    """kill with confirmed=True returns ok=True and records the concept ban."""
    acct = _account("gym_a", approvers=["U_gym_a_owner"])
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "false")

    draft = _draft(account_key="gym_a", creative_path="/lib/hook_v1.png")
    store = _make_store(draft)

    from agent import portal_approvals
    result = portal_approvals.kill(
        account_key="gym_a",
        draft_id="draft-001",
        actor_id="U_gym_a_owner",
        confirmed=True,
        store=store,
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"
    assert "banned" in result["detail"].lower() or "will not be drafted" in result["detail"].lower(), (
        f"unexpected detail: {result['detail']}"
    )


# ---------------------------------------------------------------------------
# test_deny_blocks_draft
# ---------------------------------------------------------------------------

def test_deny_blocks_draft(monkeypatch):
    """deny action marks the draft BLOCKED and returns ok=True."""
    acct = _account("gym_a", approvers=["U_gym_a_owner"])
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "false")

    draft = _draft(account_key="gym_a")
    store = _make_store(draft)

    from agent import portal_approvals
    result = portal_approvals.deny(
        account_key="gym_a",
        draft_id="draft-001",
        actor_id="U_gym_a_owner",
        note="Wrong tone for this audience.",
        store=store,
    )
    assert result["ok"] is True, f"expected ok=True, got: {result}"
    assert draft.status == DraftStatus.BLOCKED, (
        f"draft status should be BLOCKED, got {draft.status}"
    )


# ---------------------------------------------------------------------------
# test_cross_gym_rejected
# ---------------------------------------------------------------------------

def test_cross_gym_rejected(monkeypatch):
    """Actor authorized for gym_a cannot act on gym_b's drafts."""
    gym_a = _account("gym_a", approvers=["U_gym_a_owner"])
    gym_b = _account("gym_b", approvers=["U_gym_b_owner"])
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [gym_a, gym_b])
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "true")
    monkeypatch.setenv("AGENT_TENANT_BRAIN_ENABLED", "false")

    draft_b = _draft(draft_id="draft-b", account_key="gym_b")
    store_b = _make_store(draft_b)

    from agent import portal_approvals
    # gym_a's owner tries to deny gym_b's draft
    result = portal_approvals.deny(
        account_key="gym_b",
        draft_id="draft-b",
        actor_id="U_gym_a_owner",
        note="trying to cross the fence",
        store=store_b,
    )
    assert result["ok"] is False, f"expected ok=False (cross-gym denied), got: {result}"
    assert "denied" in result["detail"].lower() or "not authorized" in result["detail"].lower(), (
        f"unexpected detail: {result['detail']}"
    )


# ---------------------------------------------------------------------------
# test_portal_flag_off_returns_error
# ---------------------------------------------------------------------------

def test_portal_flag_off_returns_error(monkeypatch):
    """Every portal function returns ok=False when AGENT_PORTAL_APPROVALS is OFF."""
    monkeypatch.setenv("AGENT_PORTAL_APPROVALS", "false")

    # Use a dummy account and draft; they should never be reached
    acct = _account("gym_a", approvers=["U_gym_a_owner"])
    monkeypatch.setattr("agent.accounts.ACCOUNTS", [acct])
    draft = _draft(account_key="gym_a")
    store = _make_store(draft)

    from agent import portal_approvals

    for fn_name, kwargs in [
        ("approve", {}),
        ("edit", {"note": "fix it"}),
        ("deny", {"note": "nope"}),
        ("kill", {"confirmed": True}),
    ]:
        fn = getattr(portal_approvals, fn_name)
        result = fn(
            account_key="gym_a",
            draft_id="draft-001",
            actor_id="U_gym_a_owner",
            store=store,
            **kwargs,
        )
        assert result["ok"] is False, (
            f"portal.{fn_name}: expected ok=False when flag is OFF, got {result}"
        )
        assert "AGENT_PORTAL_APPROVALS" in result["detail"], (
            f"portal.{fn_name}: expected flag name in detail, got {result['detail']!r}"
        )


# ---------------------------------------------------------------------------
# test_trust_level_fail_safe
# ---------------------------------------------------------------------------

def test_trust_level_fail_safe():
    """An unknown trust value (typo, out-of-range int, string) coerces to FULL_APPROVAL."""
    from agent.trust import effective_level, TrustLevel

    class _FakeAccount:
        trust = 99  # out of range

    assert effective_level(_FakeAccount()) == TrustLevel.FULL_APPROVAL

    class _StringAccount:
        trust = "trusted"  # string, not int

    assert effective_level(_StringAccount()) == TrustLevel.FULL_APPROVAL

    class _NoneAccount:
        trust = None

    assert effective_level(_NoneAccount()) == TrustLevel.FULL_APPROVAL
