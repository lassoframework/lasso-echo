"""
client_support_scan.py — READ-ONLY scan of the native support bus, returning
structured exception IDs/counts for the Help Center "working" reconciliation.

This is the fetch/aggregate shell around client_support_reconciler.py's pure
classifier. It:

  * queries support_tickets with a server-side source IN filter
    (website_tab, slack_conversation, coach_portal) BEFORE keyset paging,
    selecting only the fields the classifier needs;
  * fetches each referenced gym by exact id (select id,slug), cached per
    client_id -- a null client_id is an anomaly and needs no gym fetch;
  * fetches all support_messages per ticket by exact ticket_id with keyset
    paging, selecting metadata columns plus attachments only -- NEVER
    body/raw_text;
  * classifies via reconcile() and returns ticket IDs, categories, reasons and
    counts only -- never raw ticket text, attachments, gym names/slugs, or
    arbitrary error strings.

Fail closed: any ticket/gym/message read error, malformed page, or pagination
fault returns {"ok": False} with a fixed reason string and NO partial results.
This module performs no writes, no sends, no scheduling, no alerts, and never
calls a client-visible send/closeout method.
"""
from __future__ import annotations

from agent.jobs.client_support_reconciler import (
    CLIENT_FACING_SOURCES,
    EXCEPTION,
    ANOMALY,
    OUT_OF_SCOPE,
    SATISFIED,
    PaginationError,
    fetch_pages,
    reconcile,
)

_TICKET_FIELDS = (
    "id,product,source,client_id,bot_identity,slack_user_id,classification,"
    "status,resolved_at,request_version,client_delivery_guard_required,"
    "is_test,created_at"
)
_MESSAGE_FIELDS = (
    "id,ticket_id,created_at,direction,delivery_status,"
    "delivery_request_version,attachments"
)
_GYM_FIELDS = "id,slug"

# Fixed failure reasons -- never leak raw exception text into a report.
FAIL_TICKET_READ = "ticket_read_failed"
FAIL_GYM_READ = "gym_read_failed"
FAIL_MESSAGE_READ = "message_read_failed"
FAIL_BUS_UNAVAILABLE = "bus_unavailable"
FAIL_CLASSIFICATION = "classification_failed"


def _default_bus():
    from agent.slack_convo.bus import Bus
    return Bus()


def _failed(reason):
    return {"ok": False, "error": reason}


def _order_clause():
    return "created_at.asc,id.asc"


def _keyset_or(cursor):
    created_at, row_id = cursor
    return (f"(created_at.gt.{created_at},"
            f"and(created_at.eq.{created_at},id.gt.{row_id}))")


def scan_support_bus(bus=None, *, page_size=500, max_pages=1000):
    """Run the read-only scan. Returns the structured report dict.

    On any read/pagination failure returns {"ok": False, "error": <fixed
    reason>} with no partial results.
    """
    if bus is None:
        try:
            bus = _default_bus()
        except Exception:
            return _failed(FAIL_BUS_UNAVAILABLE)
        if not bus.available():
            return _failed(FAIL_BUS_UNAVAILABLE)
    get = getattr(bus, "_get", None)
    if not callable(get):
        return _failed(FAIL_BUS_UNAVAILABLE)

    source_filter = f"in.({','.join(sorted(CLIENT_FACING_SOURCES))})"

    def fetch_ticket_page(cursor, size):
        params = {
            "select": _TICKET_FIELDS,
            "source": source_filter,
            "order": _order_clause(),
        }
        if cursor is not None:
            params["or"] = _keyset_or(cursor)
        params["limit"] = str(size)
        return get("support_tickets", params)

    try:
        tickets = fetch_pages(fetch_ticket_page, page_size=page_size,
                              max_pages=max_pages)
    except PaginationError:
        return _failed(FAIL_TICKET_READ)
    except Exception:
        return _failed(FAIL_TICKET_READ)

    # Gyms: exact-id lookup, cached per client_id. Null client is an anomaly
    # handled by the classifier and needs no fetch.
    gym_cache = {}
    for ticket in tickets:
        client_id = ticket.get("client_id")
        if client_id is not None and not isinstance(client_id, str):
            return _failed(FAIL_GYM_READ)
        if client_id is None or client_id in gym_cache:
            continue
        try:
            rows = get("gyms", {"select": _GYM_FIELDS, "id": f"eq.{client_id}",
                                "limit": "1"})
        except Exception:
            return _failed(FAIL_GYM_READ)
        if not isinstance(rows, list):
            return _failed(FAIL_GYM_READ)
        gym_cache[client_id] = rows[0] if rows else None

    def fetch_messages_for(ticket_id):
        def fetch_page(cursor, size):
            params = {
                "select": _MESSAGE_FIELDS,
                "ticket_id": f"eq.{ticket_id}",
                "order": _order_clause(),
            }
            if cursor is not None:
                params["or"] = _keyset_or(cursor)
            params["limit"] = str(size)
            return get("support_messages", params)
        return fetch_pages(fetch_page, page_size=page_size, max_pages=max_pages)

    pairs = []
    for ticket in tickets:
        try:
            messages = fetch_messages_for(ticket["id"])
        except PaginationError:
            return _failed(FAIL_MESSAGE_READ)
        except Exception:
            return _failed(FAIL_MESSAGE_READ)
        pairs.append((ticket, messages))

    try:
        summary = reconcile(pairs, gym_for=gym_cache.get)
    except Exception:
        return _failed(FAIL_CLASSIFICATION)

    counts = {SATISFIED: 0, EXCEPTION: 0, ANOMALY: 0, OUT_OF_SCOPE: 0}
    for category, results in summary.items():
        counts[category] = counts.get(category, 0) + len(results)

    def _brief(results):
        return [{"ticket_id": r.get("ticket_id"), "reason": r.get("reason")}
                for r in results]

    client_visible_working = sum(
        1 for bucket in summary.values() for r in bucket
        if r.get("client_visible_working"))

    return {
        "ok": True,
        "scanned": len(tickets),
        "counts": counts,
        "client_visible_working": client_visible_working,
        "exceptions": _brief(summary.get(EXCEPTION, [])),
        "anomalies": _brief(summary.get(ANOMALY, [])),
        "out_of_scope": counts.get(OUT_OF_SCOPE, 0),
    }
