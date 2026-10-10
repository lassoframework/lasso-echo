"""
client_support_reconciler.py — READ-ONLY classification of client-facing support
tickets for the Help Center "working" reconciliation.

WHY THIS EXISTS
---------------
The Help Center shows a ticket as "working" when a client-facing source ticket
(website_tab, slack_conversation, coach_portal) sits in a non-terminal status,
or when a terminal status (resolved/merged/done) lacks a qualifying
CURRENT-REQUEST posted outbound completion receipt. The existing
stale_escalation_reminder.py selects the oldest 200 hold+escalated tickets and
starves client requests behind internal ops_fix noise.

This module is the pure classification core plus a bounded keyset-paginated
read-only fetch interface that a later internal daily reconciler can call. It
writes NOTHING, sends NO alerts, and posts NO client messages. Results contain
ticket IDs, categories and reasons only -- never raw ticket text, bodies, or
secrets.

Receipt parity: portal main src/lib/support/client-visible.ts (SHA 4cdbcdf9)
plus the Echo writer conventions in agent/slack_convo/outbox.py and
agent/echo_ticket_worker.py:

  * ``delivery_request_version`` is a TOP-LEVEL support_messages column, not an
    attachments key.
  * Attachments carry ``delivery_expected_product``,
    ``delivery_expected_client_id``, ``delivery_expected_bot_identity``,
    ``delivery_expected_slack_user_id``, ``delivery_expected_status`` (the
    PREDECESSOR status at send time), ``delivery_expected_classification`` and
    ``delivery_identity_fence``.
  * Nullable identity fields follow the portal's sameNullableText: both null is
    a match, a MISSING expected key is a failure, exactly one null is a failure.

Portal status display parity (main):

  * Terminal: resolved/merged/done. Merged displays done only with resolved_at
    AND a receipt; resolved/done need a receipt only.
  * new/triage/working/fixing/verification/hold display received/working.
  * approved displays approved; failed displays refused -- neither is "working".
  * Unknown statuses are exceptions, never silent success.
"""
from __future__ import annotations

from datetime import datetime, timezone

CLIENT_FACING_SOURCES = frozenset({"website_tab", "slack_conversation", "coach_portal"})
TERMINAL_STATUSES = frozenset({"resolved", "merged", "done"})
WORKING_STATUSES = frozenset({
    "new", "triage", "working", "fixing", "verification", "hold"})
INTERNAL_KINDS = frozenset({"escalation", "fixer_request", "hold_notice"})
DEFAULT_PAGE_SIZE = 500
DEFAULT_MAX_PAGES = 1000

# Attachments identity keys compared against the ticket with sameNullableText
# semantics (both null match; missing expected key fails; one null fails).
_EXPECTED_IDENTITY_FIELDS = frozenset({
    "product", "client_id", "bot_identity", "slack_user_id"})

# (predecessor status, predecessor classification) pairs a guarded completion
# receipt may close out. The expected status is the PREDECESSOR, never the
# ticket's current status.
_ALLOWED_PREDECESSORS = frozenset({
    ("verification", "answerable_question"),
    ("verification", "code_fix"),
    ("verification", "ops_fix"),
    ("verification", "action_request"),
    ("merged", "code_fix"),
    ("resolved", "answerable_question"),
    ("resolved", "code_fix"),
    ("resolved", "ops_fix"),
    ("resolved", "action_request"),
})

# Result categories
SATISFIED = "satisfied"
EXCEPTION = "exception"
ANOMALY = "anomaly"
OUT_OF_SCOPE = "out_of_scope"


def _safe_nonnegative_int(value):
    """True iff value is a real integer >= 0 (bool is not an int here)."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= 2**53 - 1)


def _same_nullable_text(attachments, key, actual):
    """Portal sameNullableText parity for ``attachments[key]`` vs ``actual``.

    Both null match; a MISSING expected key fails; exactly one null fails;
    otherwise exact equality.
    """
    if key not in attachments:
        return False
    expected = attachments[key]
    if expected is not None and not isinstance(expected, str):
        return False
    if actual is not None and not isinstance(actual, str):
        return False
    if expected is None or actual is None:
        return expected is None and actual is None
    return expected == actual


def _receipt_is_client_visible(message):
    """Portal parity baseline: outbound + posted + not an internal-only kind."""
    if not isinstance(message, dict):
        return False
    if message.get("direction") != "outbound":
        return False
    if message.get("delivery_status") != "posted":
        return False
    attachments = message.get("attachments")
    kind = attachments.get("kind") if isinstance(attachments, dict) else None
    if kind in INTERNAL_KINDS:
        return False
    return True


def _guarded_receipt_qualifies(ticket, message):
    """Stricter current-request receipt for guarded tickets.

    Parity with portal main: ``delivery_request_version`` is a top-level
    message column; attachments carry the ``delivery_expected_*`` identity
    fence and the predecessor status/classification. All null/missing
    distinctions are conservative: a missing expected key fails.
    """
    if not _receipt_is_client_visible(message):
        return False
    attachments = message.get("attachments")
    if not isinstance(attachments, dict):
        return False
    if (attachments.get("purpose") == "support_postclose_ack"
            or attachments.get("postclose_ack") is True):
        return False
    request_version = ticket.get("request_version")
    if not _safe_nonnegative_int(request_version):
        return False
    # TOP-LEVEL column, not an attachments key.
    if (not _safe_nonnegative_int(message.get("delivery_request_version"))
            or message["delivery_request_version"] != request_version):
        return False
    kind = attachments.get("kind")
    if kind == "answer":
        pass
    elif kind == "status" and attachments.get("resolve_notice") is True:
        pass
    else:
        return False
    if attachments.get("delivery_identity_fence") is not True:
        return False
    for field in _EXPECTED_IDENTITY_FIELDS:
        if not _same_nullable_text(
                attachments, f"delivery_expected_{field}", ticket.get(field)):
            return False
    expected_status = attachments.get("delivery_expected_status")
    if not isinstance(expected_status, str):
        return False
    # Expected classification equals the ticket's CURRENT classification;
    # compared with the same nullable rule (missing key already failed above
    # only for identity fields, so check the key explicitly here).
    if "delivery_expected_classification" not in attachments:
        return False
    expected_classification = attachments["delivery_expected_classification"]
    if expected_classification != ticket.get("classification"):
        return False
    return (expected_status, expected_classification) in _ALLOWED_PREDECESSORS


def _guard_required(ticket):
    """The guarded branch is taken unless explicitly opted out with False.

    None/absent means GUARDED -- an unset flag is never a downgrade path.
    """
    return ticket.get("client_delivery_guard_required") is not False


def qualifying_completion_receipt(ticket, messages):
    """Return the first qualifying current-request completion receipt, or None.

    Guarded tickets (the default) need the full identity-fenced receipt. Only a
    ticket with client_delivery_guard_required explicitly False accepts any
    client-visible posted outbound.
    """
    if not isinstance(ticket, dict):
        return None
    ticket_id = ticket.get("id")
    if not isinstance(ticket_id, str) or not ticket_id:
        return None
    guarded = _guard_required(ticket)
    for message in messages or []:
        if not isinstance(message, dict) or message.get("ticket_id") != ticket_id:
            continue
        attachments = message.get("attachments")
        if isinstance(attachments, dict) and (
                attachments.get("purpose") == "support_postclose_ack"
                or attachments.get("postclose_ack") is True):
            continue
        if guarded:
            if _guarded_receipt_qualifies(ticket, message):
                return message
        elif _receipt_is_client_visible(message):
            return message
    return None


def _gym_slug_is_test(slug):
    """A gym slug marking a test/demo tenant. Slug is never an ID."""
    if not isinstance(slug, str):
        return False
    lowered = slug.strip().lower()
    return (lowered == "zz-test-gym" or lowered.startswith("zz-test-")
            or lowered == "demo" or lowered.startswith("demo-"))


def classify_ticket(ticket, messages, gym=None):
    """Classify one ticket against its messages. Pure; no I/O, no mutation.

    ``gym`` is the verified gym record (mapping with ``id`` and ``slug``) for
    the ticket's client_id, or None when no mapping was available. A ticket is
    only treated as a real client request when gym.id == ticket.client_id and
    the gym slug is not a test/demo slug. Without a verified gym the ticket is
    a ``gym_unverified`` anomaly, never a real-client result.

    Returns {"ticket_id", "category", "reason", "client_visible_working"}.
    Never includes raw text. client_visible_working reflects what the portal
    actually displays, NOT the anomaly count.
    """
    if not isinstance(ticket, dict):
        return {"ticket_id": None, "category": ANOMALY, "reason": "malformed_ticket",
                "client_visible_working": False}
    ticket_id = ticket.get("id")
    result = {"ticket_id": ticket_id if isinstance(ticket_id, str) else None,
              "client_visible_working": False}

    # Data-quality anomalies first: detected separately, never silently treated
    # as a real client request or as completed work.
    client_id = ticket.get("client_id")
    if client_id is None:
        result.update(category=ANOMALY, reason="null_client_id")
        return result
    if gym is None:
        # No verified gym record/mapping: never call this a real client.
        result.update(category=ANOMALY, reason="gym_unverified")
        return result
    if not isinstance(gym, dict) or gym.get("id") != client_id:
        result.update(category=ANOMALY, reason="gym_mismatch")
        return result
    if not isinstance(gym.get("slug"), str) or not gym["slug"].strip():
        result.update(category=ANOMALY, reason="gym_slug_unverified")
        return result
    if _gym_slug_is_test(gym.get("slug")):
        # A test/demo gym is out of scope even when is_test was left false.
        result.update(category=OUT_OF_SCOPE, reason="test_gym")
        return result
    if ticket.get("is_test") is True:
        result.update(category=OUT_OF_SCOPE, reason="test_ticket")
        return result
    source = ticket.get("source")
    if source not in CLIENT_FACING_SOURCES:
        result.update(category=OUT_OF_SCOPE, reason="non_client_facing_source")
        return result

    status = ticket.get("status")
    if not isinstance(status, str) or not status.strip():
        result.update(category=ANOMALY, reason="missing_status")
        return result
    status = status.strip().lower()
    resolved_at = ticket.get("resolved_at")

    if status in TERMINAL_STATUSES:
        receipt = qualifying_completion_receipt(ticket, messages)
        if status == "merged" and not resolved_at:
            # Merged displays done only with resolved_at AND a receipt; without
            # the timestamp the portal still shows working.
            result.update(category=ANOMALY, reason="merged_missing_resolved_at",
                          client_visible_working=True)
            return result
        if status == "resolved" and not resolved_at:
            # Keep the null timestamp as an anomaly, but reflect what the UI
            # actually shows: a receipt displays done despite the null
            # timestamp; without one the client sees working.
            result.update(category=ANOMALY, reason="resolved_missing_resolved_at",
                          client_visible_working=receipt is None)
            return result
        if receipt is not None:
            result.update(category=SATISFIED, reason="completion_receipt_posted")
        else:
            result.update(category=EXCEPTION,
                          reason="terminal_missing_completion_receipt",
                          client_visible_working=True)
        return result

    if status in WORKING_STATUSES:
        # Client-facing and not terminal: the client is still waiting and the
        # portal displays received/working.
        result.update(category=EXCEPTION, reason="client_request_open",
                      client_visible_working=status != "new")
        return result

    if status == "approved":
        # Portal displays approved, but the requested action is still pending.
        result.update(category=EXCEPTION, reason="client_approved_pending_action")
        return result

    if status == "failed":
        # Portal displays refused -- not working, but the failed delivery is
        # itself an exception worth surfacing.
        result.update(category=EXCEPTION, reason="client_sees_refused")
        return result

    # Unknown statuses are an exception, never silent success.
    result.update(category=EXCEPTION, reason="unknown_status")
    return result


def reconcile(tickets_with_messages, gym_for=None):
    """Classify many tickets. `tickets_with_messages` yields (ticket, messages).

    `gym_for`, when given, maps client_id -> verified gym record (or None).
    Returns {"satisfied": [...], "exception": [...], "anomaly": [...],
    "out_of_scope": [...]} of per-ticket result dicts."""
    summary = {SATISFIED: [], EXCEPTION: [], ANOMALY: [], OUT_OF_SCOPE: []}
    for ticket, messages in tickets_with_messages or []:
        gym = None
        if callable(gym_for) and isinstance(ticket, dict):
            gym = gym_for(ticket.get("client_id"))
        result = classify_ticket(ticket, messages, gym=gym)
        summary.setdefault(result["category"], []).append(result)
    return summary


class PaginationError(RuntimeError):
    """A page was malformed, unordered, duplicated, or the ceiling was hit."""


def _parse_ts(value):
    """Parse an ISO-8601 timestamp to an aware datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def fetch_pages(fetch_page, *, page_size=DEFAULT_PAGE_SIZE, max_pages=DEFAULT_MAX_PAGES):
    """True keyset pagination over a stable (created_at, id) ascending order.

    `fetch_page(cursor, page_size)` must return one page (list of dicts) where
    `cursor` is None for the first page and otherwise the last row's
    (created_at, id) key; the caller's query must apply
    ``OR (created_at > c, created_at = c AND id > i)`` semantics. Every row's
    key is validated, keys must be strictly increasing across page boundaries,
    duplicate IDs are rejected, and reaching `max_pages` with a still-full
    final page raises instead of claiming a false success.
    """
    if not callable(fetch_page):
        raise PaginationError("fetch_page must be callable")
    if not _safe_nonnegative_int(page_size) or page_size == 0:
        raise PaginationError("page_size must be a positive integer")
    if not _safe_nonnegative_int(max_pages) or max_pages == 0:
        raise PaginationError("max_pages must be a positive integer")
    rows = []
    seen_ids = set()
    cursor = None
    previous_key = None
    pages = 0
    while True:
        if pages >= max_pages:
            raise PaginationError("pagination ceiling reached")
        page = fetch_page(cursor, page_size)
        pages += 1
        if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
            raise PaginationError("malformed page")
        if len(page) > page_size:
            raise PaginationError("oversized page")
        for row in page:
            created_at, row_id = row.get("created_at"), row.get("id")
            created = _parse_ts(created_at)
            if (not isinstance(created_at, str) or not isinstance(row_id, str)
                    or not row_id or created is None):
                raise PaginationError("malformed keyset row")
            key = (created, row_id)
            if row_id in seen_ids:
                raise PaginationError("duplicate row across pages")
            if previous_key is not None and key <= previous_key:
                raise PaginationError("unordered keyset row")
            seen_ids.add(row_id)
            previous_key = key
        rows.extend(page)
        if len(page) < page_size:
            return rows
        last = page[-1]
        cursor = (last["created_at"], last["id"])
