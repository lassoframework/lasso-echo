"""
Portal calendar, library, and draft-action endpoints.

All routes gated by AGENT_PORTAL_APPROVALS (default OFF).
Token→account resolution happens in intake_web.py before these handlers
are called, so every function here receives a validated account_key.

Action routes (approve/edit/deny/kill) delegate to portal_approvals, which
owns per-gym scoping, actor authorization, and approvals.handle_action —
the same path Slack uses, so portal and Slack act on the same draft records.
"""

from . import config, db as _db
from . import portal_approvals as _pa
from .accounts import get_account
from .library import list_creatives


def _flag_off(route):
    return 403, {"error": "AGENT_PORTAL_APPROVALS is OFF", "route": route}


def handle_portal_calendar(account_key, month, store=None):
    """
    GET /portal/<token>/calendar?month=YYYY-MM

    Returns up to 60 days of drafts for the account in the given calendar
    month. `month` must match YYYY-MM exactly; absent or malformed → 400.

    Response:
        {account_key, month, drafts: [{draft_id, day_key, draft_type,
          status, platform, caption, creative_public_url,
          scheduled_for, blocked_reason}]}
    """
    if not config.portal_approvals_enabled():
        return _flag_off("calendar")

    if not account_key:
        return 400, {"error": "missing account_key"}

    if not month or len(month) != 7 or month[4] != "-":
        return 400, {"error": "month must be YYYY-MM"}

    prefix = month + "-"

    if store is not None:
        pending = store.list_pending()
        rows = [d for d in pending if d.account_key == account_key
                and (d.day_key or "").startswith(prefix)]
        drafts = [_draft_to_dict(d) for d in rows]
    else:
        try:
            with _db.connect() as conn:
                from .store import _SELECT, _row_to_draft
                results = conn.execute(
                    f"SELECT {_SELECT} FROM drafts "
                    "WHERE account_key=? AND day_key LIKE ?",
                    (account_key, prefix + "%")
                ).fetchall()
            drafts = [_draft_to_dict(_row_to_draft(r))
                      for r in results if _row_to_draft(r) is not None]
        except Exception as exc:
            return 500, {"error": f"db error: {type(exc).__name__}"}

    return 200, {"account_key": account_key, "month": month, "drafts": drafts}


def handle_portal_library(account_key):
    """
    GET /portal/<token>/library

    Returns the account's creative library (local-disk path resolved via
    account.library_path). Each item carries path, media_type, public_url,
    client_note.

    Response:
        {account_key, creatives: [{stem, path, media_type,
          public_url, client_note}]}
    """
    if not config.portal_approvals_enabled():
        return _flag_off("library")

    if not account_key:
        return 400, {"error": "missing account_key"}

    account = get_account(account_key)
    if account is None:
        return 404, {"error": f"unknown account: {account_key}"}

    try:
        library_path = account.library_path
    except Exception:
        library_path = None

    creatives = list_creatives(library_path) if library_path else []

    return 200, {
        "account_key": account_key,
        "creatives": [
            {
                "stem": c.stem,
                "path": c.path,
                "media_type": c.media_type,
                "public_url": c.public_url,
                "client_note": c.client_note,
            }
            for c in creatives
        ],
    }


def handle_portal_action(action, account_key, draft_id, actor_id, note="", store=None):
    """
    POST /portal/<token>/{approve|edit|deny|kill}

    Body: {draft_id, actor_id, note?}

    Delegates to portal_approvals, which owns per-gym scoping +
    actor authorization + approvals.handle_action (same path as Slack).

    action must be one of: approve, edit, deny, kill.
    """
    if not config.portal_approvals_enabled():
        return _flag_off(action)

    if action not in ("approve", "edit", "deny", "kill"):
        return 400, {"error": f"unknown action: {action}"}

    if not draft_id:
        return 400, {"error": "draft_id required"}
    if not actor_id:
        return 400, {"error": "actor_id required"}

    fn = getattr(_pa, action)
    if action in ("edit", "deny", "kill"):
        result = fn(account_key, draft_id, actor_id, note=note, store=store)
    else:
        result = fn(account_key, draft_id, actor_id, store=store)

    ok = result.get("ok", False)
    return (200 if ok else 403), result


# ---- helpers -------------------------------------------------------------------

def _draft_to_dict(draft):
    if draft is None:
        return None
    return {
        "draft_id": draft.draft_id,
        "day_key": draft.day_key,
        "draft_type": getattr(draft, "draft_type", None),
        "status": draft.status.value if hasattr(draft.status, "value") else str(draft.status),
        "platform": getattr(draft, "platform", None),
        "caption": getattr(draft, "caption", None),
        "creative_public_url": getattr(draft, "creative_public_url", None),
        "scheduled_for": getattr(draft, "scheduled_for", None),
        "blocked_reason": getattr(draft, "blocked_reason", None),
    }
