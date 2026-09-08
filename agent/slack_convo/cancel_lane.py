"""
cancel_lane.py — resolve and cancel/skip ONE scheduled content_calendar post for a
Slack CLIENT, on behalf of the slack_convo adapter's CANCEL_POST classification.

This is the client-facing "cancel/skip a scheduled post" feature Blake asked Echo to
build after Joe Floria (demo_app--info538--mail) could not find any way to do this from
Slack. There WAS already a client-facing cancel action: the portal's own Deny button
(agent/portal_social.py handle_deny), token isolated, budgeted at 30/month, and blocked
from ever touching an already published or mid-publish row. This module does not
invent a second mechanism -- it resolves WHICH row a Slack message means, then calls
THAT SAME handle_deny path, so a post cancelled from Slack and a post denied from the
portal are indistinguishable afterward: same status ('denied'), same budget ledger,
same publish gate, same asset rollback.

SCOPE, deliberately narrow:
  - Only a resolved CLIENT identity (who.account_key set) can reach this; an unknown
    or ambiguous identity never gets to guess whose calendar to touch (identity_gate.py
    already refuses to resolve CLIENT for an ambiguous multi-gym owner).
  - Only PENDING or APPROVED rows are offered as a target. A published or publishing
    row is never touched (handle_deny's own _published_is_final guard is the second,
    independent backstop if this module's own filter is ever wrong).
  - No admin/elevated action: handle_deny is the exact function the client's own
    portal token calls today. This module supplies no override, no bypass, and no new
    write path -- it only picks a draft_id and an actor_id (the Slack user's own id)
    and calls the existing function.
  - The publish approval gate is untouched: a cancelled post never published in the
    first place (that is the whole point), so there is no "publish" step here to
    bypass. A post that is ALREADY published cannot be cancelled by this or any other
    lane (see _published_is_final in portal_social.py).
"""
import re
from datetime import date, timedelta

# Rows in these statuses have not gone anywhere yet, so they are the only ones a
# client can cancel/skip. Anything else (published, publishing, denied, killed,
# failed, coach_review) is either already final, already cancelled, or not yet
# client visible -- never offered as a match.
CANCELLABLE_STATUSES = frozenset({"pending", "approved"})

_TOMORROW_RE = re.compile(r"\btomorrow\b", re.IGNORECASE)
_TODAY_RE = re.compile(r"\btoday'?s?\b", re.IGNORECASE)


def day_hint(text):
    """'today' | 'tomorrow' | None, from the client's own words. None means "the next
    scheduled post, whenever that is" -- the common bare "cancel my post" case."""
    t = text or ""
    if _TOMORROW_RE.search(t):
        return "tomorrow"
    if _TODAY_RE.search(t):
        return "today"
    return None


def _window(today, hint):
    if hint == "today":
        return today, today
    if hint == "tomorrow":
        d = today + timedelta(days=1)
        return d, d
    # No day named: look ahead a generous month for the SOONEST eligible row. A client
    # who says "cancel my post" almost always means the very next one coming up.
    return today, today + timedelta(days=31)


def find_target_row(sb_store, account_key, *, today=None, hint=None):
    """The single content_calendar row a bare cancel request means, or None when there
    is nothing eligible in the resolved window. Ordered by post_date (rows_in_range's
    own ORDER BY), so with no day named this is always the NEXT upcoming eligible row,
    never a later one out of order."""
    today = today or date.today()
    start, end = _window(today, hint)
    rows = sb_store.rows_in_range(account_key, start.isoformat(), end.isoformat()) or []
    for row in rows:
        if str(row.get("status") or "").lower() in CANCELLABLE_STATUSES:
            return row
    return None


NOT_FOUND_BODY = (
    "I could not find a post scheduled for you to cancel right now. If you meant a "
    "specific day, tell me which one and I will look again.")
GENERIC_FAIL_BODY = (
    "I could not cancel that post. A person on the team will follow up here.")


def cancel_post(account_key, actor_id, text, *, sb_store=None, today=None, reader=None):
    """Cancel/skip the scheduled post the client's message points at.

    Returns {'ok': bool, 'body': str, 'row': dict|None, 'result': dict|None}. 'body' is
    the client-facing confirmation or explanation (already voice-clean: no dashes, no
    invented facts -- either the write succeeded and the day is named, or it did not
    and a person follows up; never a guess about why).

    sb_store / today are injectable for tests. reader is forwarded to
    portal_social.handle_deny (its Stripe-active check), also injectable.
    """
    from .. import portal_social as _psoc
    from ..portal_calendar_store import SupabaseCalendarStore

    store = sb_store if sb_store is not None else SupabaseCalendarStore()
    row = find_target_row(store, account_key, today=today, hint=day_hint(text))
    if row is None:
        return {"ok": False, "body": NOT_FOUND_BODY, "row": None, "result": None}

    draft_id = row.get("id")
    _status, result = _psoc.handle_deny(
        account_key, draft_id, actor_id,
        note="Cancelled by the client via Slack.",
        sb_store=store)

    if not result.get("ok"):
        # A budget-out (409) or an already-published race is still a clean, honest
        # answer to the client, not a silent escalate-and-say-nothing.
        err = (result.get("error") or "").strip()
        body = err if err else GENERIC_FAIL_BODY
        return {"ok": False, "body": body, "row": row, "result": result}

    day = row.get("post_date") or "that day"
    if result.get("idempotent"):
        body = f"That post for {day} was already cancelled."
    else:
        body = f"Done. The post scheduled for {day} is cancelled and will not go out."
    return {"ok": True, "body": body, "row": row, "result": result}
