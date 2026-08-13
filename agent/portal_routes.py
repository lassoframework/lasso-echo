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
from . import portal_calendar_store as _pcs
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

    # Shared Supabase data plane wins when creds are present (the live portal
    # path). No creds -> the existing SQLite behavior below, unchanged.
    if store is None and config.portal_calendar_supabase_enabled():
        try:
            sb = _pcs.SupabaseCalendarStore()
            rows = sb.list_month(account_key, month)
            drafts = [_pcs.map_row(r) for r in rows]
        except Exception as exc:
            return 500, {"error": f"store error: {type(exc).__name__}"}
        return 200, {"account_key": account_key, "month": month, "drafts": drafts}

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

    # Shared Supabase data plane wins when creds are present (the live portal
    # path). No creds -> the existing portal_approvals/SQLite path, unchanged.
    if store is None and config.portal_calendar_supabase_enabled():
        return _handle_action_supabase(action, account_key, draft_id, note)

    fn = getattr(_pa, action)
    if action in ("edit", "deny", "kill"):
        result = fn(account_key, draft_id, actor_id, note=note, store=store)
    else:
        result = fn(account_key, draft_id, actor_id, store=store)

    ok = result.get("ok", False)
    return (200 if ok else 403), result


def handle_portal_report(account_key, days):
    """
    GET /portal/<token>/report?days=N

    Live analytics are not connected yet, so every metric answers null (never a
    fabricated 0). Shape matches the portal's mapReport exactly. Gated by
    AGENT_PORTAL_APPROVALS.
    """
    if not config.portal_approvals_enabled():
        return _flag_off("report")

    if not account_key:
        return 400, {"error": "missing account_key"}

    try:
        window = int(days)
    except (TypeError, ValueError):
        window = 30
    if window < 1:
        window = 30

    return 200, {
        "account_key": account_key,
        "window_days": window,
        "posts_published": None,
        "engagement_rate": None,
        "likes": None,
        "comments": None,
        "saves": None,
        "shares": None,
        "views": None,
        "reach": None,
        "follower_delta": None,
        "gaps": ["Live analytics are not connected yet; no numbers are shown "
                 "rather than a made up zero."],
        "health": {"label": None},
        "top_posts": [],
    }


# ---- helpers -------------------------------------------------------------------

def _handle_action_supabase(action, account_key, draft_id, note):
    """
    Supabase content_calendar action path. approve/deny/kill flip status; edit
    keeps status pending and echoes the note (no schema change, never fails on a
    missing note column). TOKEN ISOLATION is double guarded: a pre fetch by id
    scoped to gym_id, plus the gym_id filter on the PATCH itself. A row whose
    gym_id != account_key (or a missing row) is a 404 that never reveals it
    exists and never issues a write. NOTHING here publishes.
    """
    try:
        sb = _pcs.SupabaseCalendarStore()
        # Pre check: the row must exist AND belong to this gym.
        row = sb.get_row(account_key, draft_id)
        if row is None:
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}

        if action == "edit":
            if not note:
                return 400, {"ok": False, "action": "edit", "draft_id": draft_id,
                             "error": "note (new caption text) is required for edit"}
            updated = sb.patch_caption(account_key, draft_id, note)
            if updated is None:
                return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
            return 200, {"ok": True, "action": "edit", "draft_id": draft_id,
                         "caption": updated.get("caption", ""),
                         "status": updated.get("status", "pending")}

        new_status = _pcs.action_status(action)
        if new_status is None:
            return 400, {"error": f"unknown action: {action}"}

        updated = sb.set_status(account_key, draft_id, new_status)
        if updated is None:
            # Zero rows matched the id+gym_id filter -> treat as not found.
            return 404, {"ok": False, "error": "draft not found", "draft_id": draft_id}
        return 200, {"ok": True, "action": action, "draft_id": draft_id}
    except Exception as exc:
        return 500, {"ok": False, "error": f"store error: {type(exc).__name__}",
                     "draft_id": draft_id}


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
