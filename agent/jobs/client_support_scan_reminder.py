"""Queue internal reminders for unresolved tickets found by the support scan.

This job does not resolve tickets, change ticket state, or contact clients. It
writes only ``escalation`` rows, which the existing Slack outbox routes to the
owner's internal fixer channel. The feature is deliberately OFF unless
explicitly enabled via AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED.

The UUID primary key on support_messages is the durable claim: UUIDv5 binds one
reminder to (ticket, current request version, UTC day). On a conflict, the
stored row must match the complete contract and body before the result is
accepted as a duplicate.

The outbox revalidates ticket, gym, message receipts, owner route and age
before posting to an internal channel. The job stays disabled by default
until the live owner routes and degradation monitoring are verified.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from agent.jobs import client_support_scan as scan
from agent.jobs.client_support_reconciler import CLIENT_FACING_SOURCES
from agent.slack_convo import adapter as _a
from agent.slack_convo.bus import Bus, BusError
from agent.slack_convo.identities import get as get_identity

CONTRACT = "client-support-scan-reminder-v1"
SURFACE = "client_support_scan_reminder"
_SUPPORT_OWNER_BY_PRODUCT = {"echo": "echo", "portal": "scout",
                             "websites": "wrangler"}

_REASON_TOKENS = frozenset({
    "client_request_open",
    "terminal_missing_completion_receipt",
    "resolved_missing_resolved_at",
    "merged_missing_resolved_at",
})
NEW_INTAKE_GRACE = timedelta(minutes=30)


def _enabled():
    return os.environ.get(
        "AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on"}


def _parse_ts(value):
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _aged_new_ticket(entry, now):
    """A received request needs an owner alert after ten normal poll cycles."""
    if entry.get("status") != "new":
        return True
    raw = entry.get("created_at")
    try:
        created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (created.tzinfo is not None and now.tzinfo is not None
            and now.astimezone(timezone.utc) - created.astimezone(timezone.utc)
            >= NEW_INTAKE_GRACE)


def _active_internal_identities():
    """Only identities with a running listener and an internal owner route."""
    from agent import config
    from agent.slack_convo.identities import startable

    if not config.slack_convo_enabled():
        return frozenset()
    return frozenset(
        identity.name for identity in startable()
        if config.slack_convo_identity_enabled(identity.name)
        and identity.fixer_channel()
    )


def _valid_entry(entry, *, armed_identities=None):
    """True iff an actionable scan entry is a bounded, armed, real-client row.

    Invalid, test, or unarmed-identity entries are skipped safely: they never
    produce a row and never raise.
    """
    if not isinstance(entry, dict):
        return False
    if armed_identities is None:
        armed_identities = _active_internal_identities()
    version = entry.get("request_version")
    identity_name = entry.get("bot_identity")
    try:
        required_owner = _SUPPORT_OWNER_BY_PRODUCT.get(entry.get("product"))
        if required_owner is None:
            required_owner = (identity_name if get_identity(identity_name).product
                              == entry.get("product") else None)
        owner_product_matches = identity_name == required_owner
    except (KeyError, TypeError, AttributeError):
        owner_product_matches = False
    return (
        isinstance(entry.get("ticket_id"), str) and bool(entry["ticket_id"].strip())
        and entry.get("reason") in _REASON_TOKENS
        and isinstance(entry.get("product"), str) and bool(entry["product"].strip())
        and entry.get("source") in CLIENT_FACING_SOURCES
        and identity_name in armed_identities
        and owner_product_matches
        and isinstance(entry.get("client_id"), str) and bool(entry["client_id"].strip())
        and isinstance(version, int) and not isinstance(version, bool)
        and 0 <= version <= 2**53 - 1
        and isinstance(entry.get("status"), str) and bool(entry["status"].strip())
        and (entry.get("status") != "new"
             or _parse_ts(entry.get("created_at")) is not None)
    )


def _notice_identity(entry, now):
    """Deterministic reminder row for one ticket/version/UTC day.

    The body carries only the ticket ID and the fixed classifier reason token
    -- never client text, gym names, or free-form strings.
    """
    version = entry["request_version"]
    day = now.astimezone(timezone.utc).date().isoformat()
    message_id = str(uuid5(
        NAMESPACE_URL,
        f"lasso:client-support-scan-reminder:{entry['ticket_id']}:{version}:{day}"))
    if entry["status"] == "new":
        body = (f"CLIENT SUPPORT SCAN REMINDER: ticket {entry['ticket_id']} "
                "remains received and untriaged after 30 minutes. This is an "
                "internal reminder; the ticket is unchanged.")
    else:
        body = (f"CLIENT SUPPORT SCAN REMINDER: ticket {entry['ticket_id']} still "
                f"displays working to the client (reason: {entry['reason']}). This "
                "is an internal reminder; the ticket is unchanged.")
    attachments = {"kind": _a.KIND_ESCALATION,
                   "identity": entry["bot_identity"],
                   "surface": SURFACE, "contract": CONTRACT,
                   "ticket_id": entry["ticket_id"],
                   "request_version": version,
                   "reason": entry["reason"],
                   "notice_day": day,
                   "product": entry["product"],
                   "source": entry["source"],
                   "client_id": entry["client_id"],
                   "status": entry["status"]}
    if entry["status"] == "new":
        attachments["created_at"] = entry["created_at"]
    return {"id": message_id, "ticket_id": entry["ticket_id"],
            "author_type": "system", "author_id": None, "body": body,
            "attachments": attachments,
            "direction": "outbound", "delivery_status": "ready"}


def _same_claim(stored, expected):
    if not isinstance(stored, dict):
        return False
    if not all(stored.get(field) == expected.get(field)
               for field in ("id", "ticket_id", "author_type", "author_id",
                             "body", "direction")):
        return False
    actual_attachments = stored.get("attachments")
    expected_attachments = expected.get("attachments")
    if (not isinstance(actual_attachments, dict)
            or not isinstance(expected_attachments, dict)):
        return False
    if any(actual_attachments.get(key) != value
           for key, value in expected_attachments.items()):
        return False
    if type(actual_attachments.get("request_version")) is not int:
        return False
    extra = set(actual_attachments) - set(expected_attachments)
    # Only keys the existing outbox may merge while claiming/recovering an
    # escalation row. Arbitrary metadata cannot turn an unrelated row into a
    # claim.
    if extra - {"claimed_at", "reclaimed_stale_posting",
                 "scan_reminder_slack_intent", "scan_reminder_slack_uncertain",
                 "scan_reminder_hold_reason", "scan_reminder_readback",
                 "scan_reminder_no_post"}:
        return False
    if ("claimed_at" in actual_attachments
            and _parse_ts(actual_attachments["claimed_at"]) is None):
        return False
    if ("reclaimed_stale_posting" in actual_attachments
            and actual_attachments["reclaimed_stale_posting"] is not True):
        return False
    intent = actual_attachments.get("scan_reminder_slack_intent")
    if "scan_reminder_slack_intent" in actual_attachments and intent is not None:
        if (not isinstance(intent, dict)
                or not isinstance(intent.get("channel"), str)
                or not intent["channel"]
                or intent.get("thread_ts") is not None
                or not isinstance(intent.get("body"), str)
                or not intent["body"]
                or not isinstance(intent.get("sender"), str)
                or not intent["sender"]
                or type(intent.get("request_version")) is not int
                or intent["request_version"] != expected_attachments["request_version"]
                or _parse_ts(intent.get("not_before")) is None):
            return False
    if ("scan_reminder_slack_uncertain" in actual_attachments
            and type(actual_attachments["scan_reminder_slack_uncertain"]) is not bool):
        return False
    if ("scan_reminder_hold_reason" in actual_attachments
            and not isinstance(actual_attachments["scan_reminder_hold_reason"], str)):
        return False
    if ("scan_reminder_readback" in actual_attachments
            and not isinstance(actual_attachments["scan_reminder_readback"], dict)):
        return False
    if ("scan_reminder_no_post" in actual_attachments
            and type(actual_attachments["scan_reminder_no_post"]) is not bool):
        return False
    return stored.get("delivery_status") in {"ready", "posting", "posted",
                                             "failed", "held"}


def _claim_notice(bus, expected):
    try:
        created, duplicate = bus._insert("support_messages", expected)  # noqa: SLF001
    except Exception:
        # A transport/server error can happen after the insert committed. Read
        # by deterministic id and accept only an exact contract match;
        # unreadable or mismatched state remains an error and is never treated
        # as a successful claim.
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
        # A UUID conflict with different metadata/body is not evidence of our
        # claim.
        raise BusError(409, "reminder claim collision identity mismatch")
    return "duplicate"


def run(*, bus=None, now=None, enabled=None, log=print):
    """Queue at most one internal reminder per actionable ticket/version/day."""
    if enabled is None:
        enabled = _enabled()
    if enabled is not True:
        return {"ok": True, "queued": [], "reason": "disabled"}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return {"ok": False, "queued": [], "reason": "naive_clock"}
    bus = bus or Bus()
    if not bus.available():
        return {"ok": False, "queued": [], "reason": "bus_unavailable"}

    report = scan.scan_support_bus(bus)
    if not isinstance(report, dict) or report.get("ok") is not True:
        # Fail closed: an incomplete scan means no writes at all.
        return {"ok": False, "queued": [], "reason": "scan_failed"}
    actionable = report.get("actionable")
    if (not isinstance(actionable, list)
            or any(not isinstance(entry, dict) for entry in actionable)):
        return {"ok": False, "queued": [], "reason": "scan_report_malformed"}
    if report.get("client_visible_working") != sum(
            entry.get("status") != "new" for entry in actionable):
        return {"ok": False, "queued": [], "reason": "scan_report_malformed"}

    queued, duplicates, skipped = [], [], []
    armed_identities = _active_internal_identities()
    for entry in actionable:
        if not _valid_entry(entry, armed_identities=armed_identities):
            skipped.append(entry.get("ticket_id"))
            continue
        if not _aged_new_ticket(entry, now):
            continue
        ticket_id = entry["ticket_id"]
        try:
            expected = _notice_identity(entry, now)
            result = _claim_notice(bus, expected)
            (queued if result == "created" else duplicates).append(ticket_id)
        except Exception as exc:  # noqa: BLE001 - one bad row does not starve others
            log(f"[client-support-scan-reminder] ticket {ticket_id} skipped: "
                f"{type(exc).__name__}")
            skipped.append(ticket_id)
    return {"ok": True, "queued": queued, "duplicates": duplicates,
            "skipped": skipped, "degraded": bool(skipped)}
