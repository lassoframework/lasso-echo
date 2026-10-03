"""Re-surface long-unanswered, client-bound Echo holds to the internal owner.

This job does not resolve tickets, change ticket state, or contact clients. It writes
only ``escalation`` rows, which the existing Slack outbox routes to Echo's fixer
channel. The feature is deliberately OFF unless explicitly enabled.

The UUID primary key on support_messages is the durable claim: UUIDv5 binds one
notice to (ticket, request version, UTC day). On a conflict, the stored row must
match the complete contract and body before the result is accepted as a duplicate.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from agent.slack_convo import adapter as _a
from agent.slack_convo.bus import Bus, BusError

CONTRACT = "held-client-ticket-reconcile-v1"
PAGE_SIZE = 200
MAX_TICKET_PAGES = 100
MAX_MESSAGE_PAGES = 10
MIN_WAIT_HOURS = 4.0
RETRY_READ_FAILURE = timedelta(minutes=1)
RETRY_UNVERIFIED_ACTIVITY = timedelta(minutes=15)
CLIENT_VISIBLE_KINDS = frozenset({"ack", "answer", "template", "status"})


def _enabled():
    return os.environ.get("AGENT_HELD_CLIENT_TICKET_RECONCILE_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"}


def _parse_ts(value):
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _valid_ticket(ticket):
    if not isinstance(ticket, dict):
        return False
    version = ticket.get("request_version")
    if (ticket.get("product") != "echo"
            or ticket.get("source") not in {"slack_conversation", "website_tab"}
            or ticket.get("status") != "hold"
            or ticket.get("escalated") is not True
            or ticket.get("resolved_at") is not None
            or ticket.get("is_test") is not False
            or ticket.get("bot_identity") != "echo"
            or not isinstance(ticket.get("client_id"), str)
            or not ticket["client_id"].strip()
            or not isinstance(version, int) or isinstance(version, bool) or version < 0
            or not _parse_ts(ticket.get("created_at"))):
        return False
    if ticket["source"] == "slack_conversation":
        return (ticket.get("identity_kind") in {"client", "coach"}
                and isinstance(ticket.get("slack_channel_id"), str)
                and bool(ticket["slack_channel_id"].strip())
                and isinstance(ticket.get("slack_thread_ts"), str)
                and bool(ticket["slack_thread_ts"].strip()))
    return True


def _read_all(bus, table, params, *, max_pages):
    """Keyset-page a stable (created_at,id) order. Any malformed/incomplete page fails."""
    rows = []
    cursor = None
    seen_ids = set()
    for page_number in range(max_pages):
        query = dict(params)
        query.update({"select": query.get("select", "*"),
                      "order": "created_at.asc,id.asc", "limit": str(PAGE_SIZE)})
        if cursor:
            created_at, row_id = cursor
            query["or"] = (f"(created_at.gt.{created_at},and(created_at.eq.{created_at},"
                           f"id.gt.{row_id}))")
        page = bus._get(table, query)  # noqa: SLF001 - no public bounded held query exists
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise BusError(502, f"malformed {table} page")
        if len(page) > PAGE_SIZE:
            raise BusError(502, f"oversized {table} page")
        if page:
            previous_key = ((_parse_ts(cursor[0]), cursor[1]) if cursor else None)
            for row in page:
                created_at, row_id = row.get("created_at"), row.get("id")
                created = _parse_ts(created_at)
                if (not isinstance(created_at, str) or not isinstance(row_id, str)
                        or not row_id or created is None):
                    raise BusError(502, f"malformed {table} keyset row")
                key = (created, row_id)
                if (row_id in seen_ids or
                        (previous_key is not None and key <= previous_key)):
                    raise BusError(502, f"unordered or duplicate {table} keyset row")
                seen_ids.add(row_id)
                previous_key = key
            cursor = (page[-1]["created_at"], page[-1]["id"])
            rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
    # An exactly-full final page is not proof that the result set ended.
    raise BusError(503, f"{table} pagination ceiling reached")


def _activity_anchor(bus, ticket_id):
    # Bodies and user text are intentionally not selected or logged.
    messages = _read_all(bus, "support_messages", {
        "ticket_id": f"eq.{ticket_id}",
        "select": "id,created_at,direction,author_type,delivery_status,attachments,slack_ts",
    }, max_pages=MAX_MESSAGE_PAGES)
    # NULL author_type is possible on historical Portal inbound rows. It also
    # advances the request version, so conservatively treat it as requester activity.
    inbound = [m for m in messages if m.get("direction") == "inbound"
               and m.get("author_type") in {None, "client", "coach"}]
    if not inbound:
        return None
    inbound_ts = [_parse_ts(m.get("created_at")) for m in inbound]
    if any(ts is None for ts in inbound_ts):
        return None
    latest_inbound = max(inbound_ts)
    # After a delivered client-visible update, allow the same response window again.
    visible = []
    for message in messages:
        if (message.get("direction") != "outbound"
                or message.get("delivery_status") != "posted"):
            continue
        attachments = message.get("attachments")
        kind = attachments.get("kind") if isinstance(attachments, dict) else None
        if kind not in CLIENT_VISIBLE_KINDS:
            continue
        sent_at = _delivery_timestamp(message)
        if sent_at is None:
            # Enqueue time is not proof that the client actually received a reply.
            return None
        if sent_at >= latest_inbound:
            visible.append(sent_at)
    return max([latest_inbound, *visible])


def _delivery_timestamp(message):
    """Return authoritative delivery time, never the row's enqueue timestamp."""
    slack_ts = message.get("slack_ts")
    if isinstance(slack_ts, str) and slack_ts.strip():
        try:
            value = float(slack_ts)
            if value >= 0:
                return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError):
            return None
        return None
    attachments = message.get("attachments")
    if (isinstance(attachments, dict)
            and attachments.get("delivered_via") == "portal_thread"):
        return _parse_ts(attachments.get("delivered_at"))
    return None


def _notice_identity(ticket, now):
    version = ticket["request_version"]
    day = now.astimezone(timezone.utc).date().isoformat()
    message_id = str(uuid5(NAMESPACE_URL,
                           f"lasso:held-client-ticket-reconcile:{ticket['id']}:{version}:{day}"))
    body = (f"UNRESOLVED CLIENT HOLD: ticket {ticket['id']} (Echo, "
            f"{ticket['source']}, request version {version}) remains in hold and needs "
            "human owner review. This is an internal reminder; the ticket is unchanged.")
    attachments = {"kind": _a.KIND_ESCALATION, "identity": "echo",
                   "surface": "held_client_ticket_reconcile", "contract": CONTRACT,
                   "ticket_id": ticket["id"], "request_version": version,
                   "notice_day": day, "source": ticket["source"],
                   "client_id": ticket["client_id"]}
    row = {"id": message_id, "ticket_id": ticket["id"], "author_type": "system",
           "author_id": None, "body": body, "attachments": attachments,
           "direction": "outbound", "delivery_status": "ready"}
    return row


def _same_claim(stored, expected):
    if not isinstance(stored, dict):
        return False
    if not all(stored.get(field) == expected.get(field)
               for field in ("id", "ticket_id", "author_type", "author_id", "body",
                             "direction")):
        return False
    actual_attachments = stored.get("attachments")
    expected_attachments = expected.get("attachments")
    if not isinstance(actual_attachments, dict) or not isinstance(expected_attachments, dict):
        return False
    if any(actual_attachments.get(key) != value
           for key, value in expected_attachments.items()):
        return False
    if type(actual_attachments.get("request_version")) is not int:
        return False
    extra = set(actual_attachments) - set(expected_attachments)
    # These are the only keys the existing outbox may merge while claiming/recovering
    # an escalation row or this reconciler may add while deferring a transient check.
    # Arbitrary metadata cannot turn an unrelated row into a claim.
    if extra - {"claimed_at", "reclaimed_stale_posting",
                "held_reconcile_retry_after", "held_reconcile_retry_reason"}:
        return False
    if ("claimed_at" in actual_attachments
            and _parse_ts(actual_attachments["claimed_at"]) is None):
        return False
    if ("reclaimed_stale_posting" in actual_attachments
            and actual_attachments["reclaimed_stale_posting"] is not True):
        return False
    if ("held_reconcile_retry_after" in actual_attachments
            and _parse_ts(actual_attachments["held_reconcile_retry_after"]) is None):
        return False
    if "held_reconcile_retry_reason" in actual_attachments:
        retry_reason = actual_attachments["held_reconcile_retry_reason"]
        if (not isinstance(retry_reason, str) or retry_reason not in {
                "dispatch_check_failed", "requester_activity_unverified",
                "requester_activity_too_recent", "feature_disabled"}):
            return False
    return stored.get("delivery_status") in {"ready", "posting", "posted", "failed"}


def _claim_notice(bus, expected):
    try:
        created, duplicate = bus._insert("support_messages", expected)  # noqa: SLF001
    except Exception:
        # A transport/server error can happen after the insert committed. Read by
        # deterministic id and accept only an exact contract match; unreadable or
        # mismatched state remains an error and is never treated as a successful claim.
        stored = bus.message(expected["id"])
        if _same_claim(stored, expected):
            return "duplicate"
        raise
    if not duplicate:
        if not _same_claim(created, expected):
            raise BusError(502, "inserted reminder readback mismatch")
        return "created"
    stored = bus.message(expected["id"])
    if not _same_claim(stored, expected):
        # A UUID conflict with different metadata/body is not evidence of our claim.
        raise BusError(409, "reminder claim collision identity mismatch")
    return "duplicate"


def held_reconcile_retry_pending(row, *, now=None):
    """Pure check used to avoid rereading a deferred row before its retry time."""
    if not isinstance(row, dict):
        return False
    att = row.get("attachments")
    retry_at = _parse_ts(att.get("held_reconcile_retry_after")) if isinstance(att, dict) else None
    now = now or datetime.now(timezone.utc)
    return bool(retry_at and now.astimezone(timezone.utc) < retry_at)


def dispatch_eligibility(bus, row, *, now=None):
    """Revalidate a claimed reminder against current ticket and customer activity.

    Called by the outbox immediately before posting only for our exact marker.
    Returns (True|False|None, reason, retry_after). ``False`` is proven terminal
    staleness/invalidity; ``None`` means evidence is temporarily unavailable or the
    quiet window has not elapsed and the row should remain ready for a later check.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return None, "dispatch_check_failed", now.replace(tzinfo=timezone.utc) + RETRY_READ_FAILURE
    if held_reconcile_retry_pending(row, now=now):
        att = row.get("attachments") or {}
        return None, att.get("held_reconcile_retry_reason") or "requester_activity_unverified", \
            _parse_ts(att.get("held_reconcile_retry_after"))
    if not _enabled():
        return None, "feature_disabled", now.astimezone(timezone.utc) + RETRY_UNVERIFIED_ACTIVITY
    if not isinstance(row, dict):
        return False, "row_invalid", None
    att = row.get("attachments")
    if (not isinstance(att, dict)
            or att.get("surface") != "held_client_ticket_reconcile"
            or att.get("contract") != CONTRACT
            or att.get("kind") != _a.KIND_ESCALATION
            or att.get("identity") != "echo"):
        return False, "contract_invalid", None
    ticket_id = row.get("ticket_id")
    if not isinstance(ticket_id, str) or att.get("ticket_id") != ticket_id:
        return False, "ticket_identity_mismatch", None
    raw_day = att.get("notice_day")
    try:
        parsed_day = date.fromisoformat(str(raw_day or ""))
    except (TypeError, ValueError):
        return False, "contract_invalid", None
    if parsed_day.isoformat() != raw_day:
        return False, "contract_invalid", None
    try:
        notice_day = datetime.combine(parsed_day, datetime.min.time(), tzinfo=timezone.utc)
        expected = None
        ticket = bus.ticket(ticket_id)
        if not _valid_ticket(ticket):
            return False, "ticket_not_current_hold", None
        if (att.get("source") != ticket.get("source")
                or att.get("client_id") != ticket.get("client_id")
                or att.get("request_version") != ticket.get("request_version")):
            return False, "ticket_version_or_tenant_changed", None
        expected = _notice_identity(ticket, notice_day)
        if not _same_claim(row, expected):
            return False, "row_contract_mismatch", None
        anchor = _activity_anchor(bus, ticket_id)
        if anchor is None:
            return None, "requester_activity_unverified", \
                now.astimezone(timezone.utc) + RETRY_UNVERIFIED_ACTIVITY
        eligible_at = anchor + timedelta(hours=MIN_WAIT_HOURS)
        if now.astimezone(timezone.utc) < eligible_at:
            return None, "requester_activity_too_recent", eligible_at
        # A final ticket read after the activity query closes changes during that read.
        latest = bus.ticket(ticket_id)
        if not _valid_ticket(latest) or any(
                latest.get(key) != ticket.get(key)
                for key in ("product", "source", "client_id", "bot_identity",
                            "request_version", "status", "escalated", "resolved_at")):
            return False, "ticket_changed_during_dispatch_check", None
        return True, "eligible", None
    except Exception:
        return None, "dispatch_check_failed", \
            now.astimezone(timezone.utc) + RETRY_READ_FAILURE


def run(*, bus=None, now=None, enabled=None, minimum_wait_hours=MIN_WAIT_HOURS,
        log=print):
    """Queue at most one internal reminder per eligible ticket/version/UTC day."""
    if enabled is None:
        enabled = _enabled()
    if enabled is not True:
        return {"ok": True, "queued": [], "reason": "disabled"}
    if (not isinstance(minimum_wait_hours, (int, float))
            or isinstance(minimum_wait_hours, bool)
            or minimum_wait_hours <= 0):
        return {"ok": False, "queued": [], "reason": "invalid_wait_interval"}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return {"ok": False, "queued": [], "reason": "naive_clock"}
    bus = bus or Bus()
    if not bus.available():
        return {"ok": False, "queued": [], "reason": "bus_unavailable"}
    try:
        tickets = _read_all(bus, "support_tickets", {
            "product": "eq.echo", "source": "in.(slack_conversation,website_tab)",
            "status": "eq.hold", "escalated": "eq.true", "resolved_at": "is.null",
            "is_test": "eq.false",
            "select": ("id,product,source,status,escalated,resolved_at,is_test,bot_identity,"
                       "client_id,identity_kind,slack_channel_id,slack_thread_ts,"
                       "request_version,created_at"),
        }, max_pages=MAX_TICKET_PAGES)
    except Exception as exc:  # noqa: BLE001 - incomplete scan means no writes
        log(f"[held-ticket-reconcile] scan failed closed: {type(exc).__name__}")
        return {"ok": False, "queued": [], "reason": "ticket_scan_incomplete"}

    queued, duplicates = [], []
    threshold = timedelta(hours=float(minimum_wait_hours))
    for candidate in tickets:
        if not _valid_ticket(candidate):
            continue
        ticket_id = candidate["id"]
        try:
            # Re-read the complete row before messages; it may have resolved or advanced.
            fresh = bus.ticket(ticket_id)
            if not _valid_ticket(fresh) or any(
                    fresh.get(key) != candidate.get(key)
                    for key in ("product", "source", "client_id", "bot_identity",
                                "request_version", "status", "escalated", "resolved_at")):
                continue
            anchor = _activity_anchor(bus, ticket_id)
            if anchor is None or now.astimezone(timezone.utc) - anchor < threshold:
                continue
            # Ticket state/version may have changed during the message scan.
            current = bus.ticket(ticket_id)
            if not _valid_ticket(current) or any(
                    current.get(key) != fresh.get(key)
                    for key in ("product", "source", "client_id", "bot_identity",
                                "request_version", "status", "escalated", "resolved_at")):
                continue
            expected = _notice_identity(current, now)
            result = _claim_notice(bus, expected)
            (queued if result == "created" else duplicates).append(ticket_id)
        except Exception as exc:  # noqa: BLE001 - one bad row does not starve other holds
            log(f"[held-ticket-reconcile] ticket {ticket_id} skipped: {type(exc).__name__}")
    return {"ok": True, "queued": queued, "duplicates": duplicates}
