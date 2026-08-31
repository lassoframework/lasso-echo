"""
Portal-callable approval functions scoped to one gym.

Gated by AGENT_PORTAL_APPROVALS (default OFF). When OFF, every function
returns {ok: False, detail: ..., action: <name>} immediately with no side
effects. When ON, each function loads the draft for the named account, verifies
the actor is authorized for THAT account only (gym A's approver cannot touch
gym B), then delegates to approvals.handle_action.

Per-gym scoping is strict: _is_authorized_for_account checks only the
per-account approver list, not the global approver, so authority is
explicitly account-scoped. The global approver (APPROVER_SLACK_ID) retains
access to every account because account.approver_ids() already falls back to
the global approver when the per-account list is empty.
"""

from . import config
from .approvals import handle_action, _is_approver
from .accounts import get_account
from .drafter import DraftStatus


def _gate_open():
    """The account-scoped approval functions are callable when EITHER the original
    portal-approvals flag OR the Part B client-social master flag is armed. Part B's
    token-scoped endpoints run under AGENT_PORTAL_SOCIAL_ENABLED and delegate here, so
    the master flag alone is enough to act; the older AGENT_PORTAL_APPROVALS lane keeps
    working exactly as before. Both OFF -> the functions are inert (unchanged)."""
    return config.portal_approvals_enabled() or config.portal_social_enabled()


def _flag_off_response(action):
    return {"ok": False, "detail": "AGENT_PORTAL_APPROVALS is OFF", "action": action}


def _load_draft(account_key, draft_id, store=None):
    """Load a draft by draft_id for the given account. Returns None if not found.
    store is injectable for tests; defaults to the live draft store."""
    if store is not None:
        return store.get(draft_id)
    try:
        from . import db
        return db.load_draft(draft_id, account_key=account_key)
    except Exception:
        return None


def _authorized_for_account(actor_id, account):
    """True if actor_id is an approver for this specific account.
    Uses _is_approver, which checks the global approver and the
    account's own approver_ids list. Account-level: actor authorized
    for gym A cannot act on gym B because each call passes the specific
    account object."""
    return _is_approver(actor_id, account=account)


def _result(ok, action, draft_id, detail):
    return {"ok": ok, "action": action, "draft_id": draft_id, "detail": detail}


def _owns_draft(draft, account_key):
    """Defense-in-depth cross-gym ownership backstop. The draft is loaded via
    _load_draft (store.get is NOT account-scoped), and authority is checked
    against the CALLER's own account_key, so a regression in the upstream
    _load_owned_draft guard would otherwise let gym A act on gym B's draft.
    This is a SECOND, independent gate: the loaded draft must belong to the
    passed account_key. Callers reject with the same not-found result shape,
    so a cross-gym id never confirms the other gym's draft exists."""
    return (getattr(draft, "account_key", None) or "") == account_key


def approve(account_key, draft_id, actor_id, store=None, **kwargs):
    """Approve a draft for this gym. Actor must be authorized for account_key."""
    if not _gate_open():
        return _flag_off_response("approve")
    account = get_account(account_key)
    if account is None:
        return _result(False, "approve", draft_id, f"Unknown account: {account_key}")
    if not _authorized_for_account(actor_id, account):
        return _result(False, "approve", draft_id,
                       f"Denied: {actor_id} is not authorized for {account_key}.")
    draft = _load_draft(account_key, draft_id, store=store)
    if draft is None:
        return _result(False, "approve", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    if not _owns_draft(draft, account_key):
        # Cross-gym backstop: same not-found shape, never confirm it exists elsewhere.
        return _result(False, "approve", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    # FORWARD THE STORE. handle_action's publish claim is a compare-and-swap on the
    # store the draft actually lives in. Without this, `store` was consumed only by
    # _load_draft and handle_action built a DEFAULT PendingStore, so the claim checked
    # a table the draft is not in and granted every time: an exactly-once guard that
    # silently guards nothing. Dormant today, because the live portal path writes
    # straight to content_calendar and never reaches here, but this is the fallback
    # that takes over if the shared plane is briefly unreachable, which is exactly
    # when a retry storm would find the hole.
    result = handle_action("approve", draft, actor_id, account=account,
                           store=store, **kwargs)
    return _result(result.ok, result.action, result.draft_id, result.detail)


def edit(account_key, draft_id, actor_id, note="", store=None, **kwargs):
    """Request a revision for a draft for this gym."""
    if not _gate_open():
        return _flag_off_response("edit")
    account = get_account(account_key)
    if account is None:
        return _result(False, "edit", draft_id, f"Unknown account: {account_key}")
    if not _authorized_for_account(actor_id, account):
        return _result(False, "edit", draft_id,
                       f"Denied: {actor_id} is not authorized for {account_key}.")
    draft = _load_draft(account_key, draft_id, store=store)
    if draft is None:
        return _result(False, "edit", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    if not _owns_draft(draft, account_key):
        # Cross-gym backstop: same not-found shape, never confirm it exists elsewhere.
        return _result(False, "edit", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    result = handle_action("edit", draft, actor_id, note=note, account=account, **kwargs)
    return _result(result.ok, result.action, result.draft_id, result.detail)


def deny(account_key, draft_id, actor_id, note="", store=None, **kwargs):
    """Deny a draft for this gym. Marks it BLOCKED and costs one recreate."""
    if not _gate_open():
        return _flag_off_response("deny")
    account = get_account(account_key)
    if account is None:
        return _result(False, "deny", draft_id, f"Unknown account: {account_key}")
    if not _authorized_for_account(actor_id, account):
        return _result(False, "deny", draft_id,
                       f"Denied: {actor_id} is not authorized for {account_key}.")
    draft = _load_draft(account_key, draft_id, store=store)
    if draft is None:
        return _result(False, "deny", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    if not _owns_draft(draft, account_key):
        # Cross-gym backstop: same not-found shape, never confirm it exists elsewhere.
        return _result(False, "deny", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    result = handle_action("deny", draft, actor_id, note=note, account=account, **kwargs)
    return _result(result.ok, result.action, result.draft_id, result.detail)


def kill(account_key, draft_id, actor_id, confirmed=False, store=None, **kwargs):
    """Permanently ban the creative concept for this gym only.
    Requires confirmed=True. Actor must be authorized for account_key."""
    if not _gate_open():
        return _flag_off_response("kill")
    account = get_account(account_key)
    if account is None:
        return _result(False, "kill", draft_id, f"Unknown account: {account_key}")
    if not _authorized_for_account(actor_id, account):
        return _result(False, "kill", draft_id,
                       f"Denied: {actor_id} is not authorized for {account_key}.")
    draft = _load_draft(account_key, draft_id, store=store)
    if draft is None:
        return _result(False, "kill", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    if not _owns_draft(draft, account_key):
        # Cross-gym backstop: same not-found shape, never confirm it exists elsewhere.
        return _result(False, "kill", draft_id,
                       f"Draft {draft_id} not found for account {account_key}.")
    result = handle_action("kill", draft, actor_id, account=account,
                           confirmed=confirmed, **kwargs)
    return _result(result.ok, result.action, result.draft_id, result.detail)
