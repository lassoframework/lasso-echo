"""
bus.py — the FIXER bus store: support_tickets + support_messages over Supabase REST.

Same transport pattern as portal_calendar_store / gbp_store (requests + service-role key,
read lazily, never logged). This module knows nothing about Slack events or replies; it
only knows rows.

THREAD EQUALS TICKET is enforced by the DB (uq_support_tickets_slack_thread, migration
0309), not here. get_or_create_ticket does a plain INSERT and, on the unique violation
(PostgREST 409 / Postgres 23505), re-reads the winner. Two workers racing, a restart
replaying, a redelivered event -- one of them creates, the rest read. No adapter memory is
ever the source of truth for the mapping.

DEDUPE ON EVENT ID is the same shape: uq_support_messages_slack_event. record_inbound does
a plain INSERT and reports duplicate=True on 409, so a redelivered Slack event is a no-op.

Why plain INSERT + 409 rather than PostgREST's on_conflict / ignore-duplicates: both unique
indexes are PARTIAL (WHERE ... IS NOT NULL). Postgres only infers a partial unique index
for ON CONFLICT when the clause repeats the index predicate, which PostgREST's on_conflict
parameter does not emit -- so ON CONFLICT would fail with "no unique or exclusion constraint
matching" at runtime. Catching the violation is the reliable form.
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from . import testdata as _td
from .. import config

_TICKETS = "support_tickets"
_MESSAGES = "support_messages"

# Whole-attachment CAS key-removal sentinel: updates may map a key to _REMOVE to
# drop it from the merged jsonb in the same compare-and-set write.
_REMOVE = object()

OPEN_STATUSES = ("new", "triage", "fixing", "verification", "hold", "approved")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIXER_RECEIPT_NAMESPACE = uuid.UUID("f1cb5464-b72e-4b0b-a9c2-fc07383017be")
_FIXER_SUPPRESSED_ALERT_NAMESPACE = uuid.UUID("1f1455e2-5a9d-4e4b-b332-6bd425d915fe")


def _fixer_receipt_id(source_message_id):
    return str(uuid.uuid5(_FIXER_RECEIPT_NAMESPACE, str(source_message_id)))


def _suppressed_current_notice_alert_id(source_message_id):
    return str(uuid.uuid5(_FIXER_SUPPRESSED_ALERT_NAMESPACE, str(source_message_id)))


def _unsent_suppressed_current_notice(row, identity):
    att = (row or {}).get("attachments") or {}
    return bool(row and row.get("delivery_status") == "suppressed"
                and row.get("direction") == "outbound"
                and row.get("author_type") == identity
                and _UUID.fullmatch(str(row.get("id") or ""))
                and att.get("identity") == identity
                and att.get("fixer") is True
                and _UUID.fullmatch(str(att.get("fixer_current_attempt_token") or ""))
                and att.get("fixer_slack_delivery_intent") is None
                and not row.get("slack_ts") and not row.get("slack_event_id")
                and att.get("delivery_readback_verified") is not True
                and att.get("fixer_route_uncertain") is not True
                and att.get("fixer_slack_delivery_uncertain") is not True)


def _suppressed_notice_alert_body(notice):
    return (f"FIXER notice {notice['id']} on ticket {notice['ticket_id']} was canceled "
            "before Slack delivery. Review the ticket before opening another notice.")


def _verified_suppressed_notice_alert(alert, notice, identity):
    att = (alert or {}).get("attachments") or {}
    if (not _unsent_suppressed_current_notice(notice, identity)
            or not alert or alert.get("ticket_id") != notice.get("ticket_id")
            or alert.get("direction") != "outbound" or alert.get("author_type") != "system"
            or att.get("identity") != identity or att.get("kind") != _a_kind_escalation()
            or att.get("suppressed_message_id") != notice.get("id")
            or att.get("fixer") is True or att.get("fixer_current_attempt_token")
            or att.get("fixer_route_pending") is True):
        return False
    mid, tid = notice["id"], notice["ticket_id"]
    if alert.get("id") == _suppressed_current_notice_alert_id(mid):
        return (att.get("fixer_suppressed_notice_alert") is True
                and alert.get("body") == _suppressed_notice_alert_body(notice))
    legacy_bodies = {
        (f"FIXER first-contact notice {mid} on ticket {tid} was canceled before Slack delivery. "
         "Review the ticket before opening another notice."),
        (f"FIXER notice {mid} on ticket {tid} was suppressed after a pre-send claim expired. "
         "Review the ticket before a new customer notice."),
    }
    reason = (notice.get("attachments") or {}).get("suppressed_why")
    if isinstance(reason, str) and reason:
        legacy_bodies.add(f"SUPPRESSED reply on ticket {tid} ({identity}): {reason}. "
                          "Nothing was posted; a person should look.")
    return bool(_UUID.fullmatch(str(alert.get("id") or ""))
                and alert.get("body") in legacy_bodies)


def _a_kind_escalation():
    from .adapter import KIND_ESCALATION
    return KIND_ESCALATION


class BusError(RuntimeError):
    def __init__(self, status, detail=""):
        self.status = status
        self.detail = detail
        super().__init__(f"bus {status}: {detail}")


def _is_unique_violation(status_code, text):
    if status_code != 409:
        return False
    t = (text or "").lower()
    return "23505" in t or "duplicate key" in t or "unique" in t


class TicketPage(list):
    """find_new_tickets' result: a plain list of work-ready rows, plus the two facts a
    keyset-paginating caller needs about the RAW page they were filtered from:

      * raw_count -- rows the database returned before the local strict test filter;
      * raw_last  -- the last raw row (the cursor advances past it, so a run of rows the
        filter removed still moves the window forward instead of being re-fetched forever).

    A subclass rather than a tuple so every pre-pagination caller and test fake that
    treats the result as a list is unaffected."""

    def __init__(self, rows=(), *, raw_count=0, raw_last=None):
        super().__init__(rows)
        self.raw_count = raw_count
        self.raw_last = raw_last


class Bus:
    def __init__(self, url=None, service_key=None, http=None):
        self._url = (url if url is not None else config.supabase_url())
        self._key = (service_key if service_key is not None else config.supabase_service_key())
        self._http = http

    # ---- transport ----------------------------------------------------------------------
    def available(self):
        return bool(self._url and self._key)

    def _client(self):
        if self._http is not None:
            return self._http
        import requests  # lazy, repo pattern
        return requests

    def _headers(self, extra=None):
        h = {"apikey": self._key, "Authorization": f"Bearer {self._key}",
             "Accept": "application/json", "Content-Type": "application/json"}
        if extra:
            h.update(extra)
        return h

    def _rest(self, table):
        return f"{self._url}/rest/v1/{table}"

    def _get(self, table, params):
        r = self._client().get(self._rest(table), params=params, headers=self._headers(),
                               timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, (r.text or "")[:200])
        return r.json() or []

    def _insert(self, table, row):
        """Returns (row_or_None, duplicate: bool). Raises BusError on any other failure."""
        r = self._client().post(self._rest(table), data=json.dumps(row),
                                headers=self._headers({"Prefer": "return=representation"}),
                                timeout=30)
        if _is_unique_violation(r.status_code, r.text):
            return None, True
        if r.status_code >= 400:
            raise BusError(r.status_code, (r.text or "")[:200])
        data = r.json() or []
        return (data[0] if isinstance(data, list) and data else data), False

    def _patch(self, table, match, fields):
        r = self._client().patch(self._rest(table), params=match, data=json.dumps(fields),
                                 headers=self._headers({"Prefer": "return=representation"}),
                                 timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, (r.text or "")[:200])
        data = r.json() or []
        return data[0] if isinstance(data, list) and data else None

    # ---- tickets ------------------------------------------------------------------------
    def find_ticket_by_thread(self, channel_id, thread_ts):
        rows = self._get(_TICKETS, {
            "slack_channel_id": f"eq.{channel_id}", "slack_thread_ts": f"eq.{thread_ts}",
            "select": "*", "limit": "1"})
        return rows[0] if rows else None

    def find_open_ticket_in_conversation(self, channel_id, within_days):
        """The most recent OPEN ticket in this DM/group DM whose creation is inside the
        window. People do not thread in DMs; the next top-level message continues the open
        conversation rather than opening a new ticket."""
        since = (datetime.now(timezone.utc) - timedelta(days=int(within_days))).isoformat()
        rows = self._get(_TICKETS, {
            "slack_channel_id": f"eq.{channel_id}",
            "status": f"in.({','.join(OPEN_STATUSES)})",
            "created_at": f"gte.{since}",
            "select": "*", "order": "created_at.desc", "limit": "1"})
        return rows[0] if rows else None

    def get_or_create_ticket(self, *, channel_id, thread_ts, product, bot_identity,
                             slack_user_id, identity_kind, client_id, reporter, raw_text,
                             classification=None, request_type=None):
        """Race-safe: INSERT, and on the thread unique violation read the winner.
        Returns (ticket, created: bool)."""
        row = {
            "product": product, "source": "slack_conversation",
            "client_id": client_id or None, "reporter": reporter or None,
            "raw_text": (raw_text or "")[:4000], "status": "new",
            "slack_channel_id": channel_id, "slack_thread_ts": thread_ts,
            "slack_user_id": slack_user_id, "identity_kind": identity_kind,
            "bot_identity": bot_identity,
        }
        if classification:
            row["classification"] = classification
        if request_type:
            row["request_type"] = request_type
        created, dup = self._insert(_TICKETS, row)
        if dup:
            existing = self.find_ticket_by_thread(channel_id, thread_ts)
            if existing is None:
                raise BusError(409, "thread unique violation but winner not readable")
            return existing, False
        return created, True

    def ticket(self, ticket_id):
        rows = self._get(_TICKETS, {"id": f"eq.{ticket_id}", "select": "*", "limit": "1"})
        return rows[0] if rows else None

    def set_ticket(self, ticket_id, **fields):
        return self._patch(_TICKETS, {"id": f"eq.{ticket_id}"}, fields)

    def patch_ticket_if_current(self, expected_ticket, **fields):
        """CAS a worker transition against the exact request cycle and owner."""
        version = expected_ticket.get("request_version")
        if type(version) is not int or version < 0:
            raise BusError(400, "invalid ticket transition version")

        def match_value(value):
            return "is.null" if value is None else f"eq.{value}"

        identity_fields = ("status", "classification", "product", "source", "client_id",
                           "reporter", "bot_identity", "slack_user_id",
                           "slack_channel_id", "slack_thread_ts", "hold_tier")
        match = {"id": f"eq.{expected_ticket['id']}",
                 "request_version": f"eq.{version}",
                 "escalated": match_value(expected_ticket.get("escalated"))}
        match.update({field: match_value(expected_ticket.get(field))
                      for field in identity_fields})
        return self._patch(_TICKETS, match, fields)

    def adopt_independent_release(self, expected_ticket, *, fix_pr_url,
                                  verification_before, verification_after):
        """One operator adoption CAS, preserving concurrent proof and request identity.

        Call support_release_adoption.adopt_release to validate real release and
        business evidence first. This transport cannot be used to resolve a ticket.
        Both JSON columns are compared in full; a concurrent receipt writer wins
        instead of losing evidence to a replacement assembled from an older read.
        """
        if (expected_ticket.get('product') != 'echo'
                or expected_ticket.get('source') != 'slack_conversation'
                or expected_ticket.get('classification') != 'code_fix'
                or expected_ticket.get('status') != 'verification'
                or expected_ticket.get('escalated') is not False
                or expected_ticket.get('hold_tier') is not None
                or type(expected_ticket.get('request_version')) is not int):
            raise BusError(400, 'ineligible independent release adoption')
        fields = ('id', 'request_version', 'status', 'classification', 'source', 'product',
                  'client_id', 'bot_identity', 'identity_kind', 'slack_user_id',
                  'slack_channel_id', 'slack_thread_ts', 'fix_pr_url', 'escalated', 'hold_tier')
        match = {key: 'is.null' if expected_ticket.get(key) is None else
                 f'eq.{str(expected_ticket[key]).lower() if type(expected_ticket[key]) is bool else expected_ticket[key]}'
                 for key in fields}
        for key in ('verification_before', 'verification_after'):
            value = expected_ticket.get(key)
            match[key] = 'is.null' if value is None else 'eq.' + json.dumps(value, separators=(',', ':'))
        return self._patch(_TICKETS, match, {'status':'merged', 'fix_pr_url':fix_pr_url,
            'verification_before':verification_before, 'verification_after':verification_after})

    def stamp_outreach_ticket_if_current(self, ticket_id, *, expected_ticket,
                                         channel_id, thread_ts, slack_user_id,
                                         bot_identity, identity_kind):
        """Stamp the DM only while the complete pre-send request still owns it."""
        version = expected_ticket.get("request_version")
        if type(version) is not int or version < 0:
            raise BusError(400, "invalid outreach request version")

        def match_value(value):
            return "is.null" if value is None else f"eq.{value}"

        fields = ("status", "classification", "product", "client_id",
                  "bot_identity", "slack_user_id", "slack_channel_id",
                  "slack_thread_ts")
        match = {"id": f"eq.{ticket_id}", "request_version": f"eq.{version}",
                 "escalated": "eq.false", "hold_tier": "is.null"}
        match.update({field: match_value(expected_ticket.get(field)) for field in fields})
        return self._patch(_TICKETS, match, {
            "slack_channel_id": channel_id, "slack_thread_ts": thread_ts,
            "slack_user_id": slack_user_id, "bot_identity": bot_identity,
            "identity_kind": identity_kind,
        })

    def resolve_current_delivery(self, ticket_id, expected_request_version,
                                 expected_status, expected_classification,
                                 expected_product, expected_client_id,
                                 expected_bot_identity, expected_slack_user_id,
                                 expected_slack_channel_id, expected_slack_thread_ts):
        """Atomically resolve one exact, still-current FIXER client delivery.

        Migration 0364 owns the authoritative eligibility envelope. Every mutable
        tenant, requester and destination field that was validated before delivery
        is repeated here; an empty representation is a lost CAS, never success.
        """
        if (not _UUID.fullmatch(str(ticket_id or ""))
                or not isinstance(expected_request_version, int)
                or isinstance(expected_request_version, bool)
                or expected_request_version < 0):
            raise BusError(400, "invalid current-delivery identity")
        body = {
            "p_ticket_id": ticket_id,
            "p_expected_request_version": expected_request_version,
            "p_expected_status": expected_status,
            "p_expected_classification": expected_classification,
            "p_expected_product": expected_product,
            "p_expected_client_id": expected_client_id,
            "p_expected_bot_identity": expected_bot_identity,
            "p_expected_slack_user_id": expected_slack_user_id,
            "p_expected_slack_channel_id": expected_slack_channel_id,
            "p_expected_slack_thread_ts": expected_slack_thread_ts,
        }
        r = self._client().post(
            f"{self._url}/rest/v1/rpc/fixer_resolve_current_delivery",
            data=json.dumps(body), headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, (r.text or "")[:200])
        data = r.json() or []
        if isinstance(data, dict):
            return data
        return data[0] if isinstance(data, list) and len(data) == 1 else None

    def _current_notice_rpc(self, name, body):
        # These RPCs are idempotent for the same notice UUID and token. A lost
        # HTTP response may follow a committed reservation/bind/close.
        for attempt in range(2):
            try:
                r = self._client().post(
                    f"{self._url}/rest/v1/rpc/{name}", data=json.dumps(body),
                    headers=self._headers(), timeout=30)
                break
            except Exception:
                if attempt:
                    raise
        if r.status_code >= 400:
            raise BusError(r.status_code, (r.text or "")[:200])
        return r.json()

    def begin_current_notice(self, ticket, notice_id, *, unrouted=False):
        """Reserve one exact notice before its outbound INSERT (portal 0384)."""
        if not config.slack_convo_echo_current_notice_enabled():
            return None
        if not _UUID.fullmatch(str(notice_id or "")):
            raise BusError(400, "invalid current notice id")
        version = ticket.get("request_version")
        if type(version) is not int or version < 0:
            raise BusError(400, "invalid current notice request version")
        body = {
            "p_ticket_id": ticket["id"],
            "p_expected_request_version": version,
            "p_expected_status": ticket.get("status"),
            "p_expected_classification": ticket.get("classification"),
            "p_expected_product": ticket.get("product"),
            "p_expected_client_id": ticket.get("client_id"),
            "p_expected_bot_identity": ticket.get("bot_identity"),
            "p_expected_slack_user_id": ticket.get("slack_user_id"),
            "p_notice_message_id": notice_id,
        }
        if not unrouted:
            body["p_expected_slack_channel_id"] = ticket.get("slack_channel_id")
            body["p_expected_slack_thread_ts"] = ticket.get("slack_thread_ts")
        name = ("fixer_begin_current_delivery_unrouted" if unrouted else
                "fixer_begin_current_delivery")
        token = self._current_notice_rpc(name, body)
        return token if isinstance(token, str) and _UUID.fullmatch(token) else None

    def bind_current_notice_route(self, ticket_id, request_version, notice_id,
                                  token, channel_id, thread_ts):
        if not config.slack_convo_echo_current_notice_enabled():
            return False
        return self._current_notice_rpc("fixer_bind_current_delivery_route", {
            "p_ticket_id": ticket_id,
            "p_expected_request_version": request_version,
            "p_notice_message_id": notice_id,
            "p_attempt_token": token,
            "p_returned_channel_id": channel_id,
            "p_returned_thread_ts": thread_ts,
        }) is True

    def resolve_current_notice(self, ticket, notice_id, token, delivery_status):
        """Close only the posted row named by this current attempt."""
        if not config.slack_convo_echo_current_notice_enabled():
            return None
        result = self._current_notice_rpc("fixer_resolve_current_notice", {
            "p_ticket_id": ticket["id"],
            "p_expected_request_version": ticket["request_version"],
            "p_expected_status": ticket["status"],
            "p_expected_delivery_status": delivery_status,
            "p_expected_classification": ticket["classification"],
            "p_expected_product": ticket.get("product"),
            "p_expected_client_id": ticket.get("client_id"),
            "p_expected_bot_identity": ticket.get("bot_identity"),
            "p_expected_slack_user_id": ticket.get("slack_user_id"),
            "p_expected_slack_channel_id": ticket.get("slack_channel_id"),
            "p_expected_slack_thread_ts": ticket.get("slack_thread_ts"),
            "p_notice_message_id": notice_id,
            "p_attempt_token": token,
        })
        if isinstance(result, dict):
            return result
        return result[0] if isinstance(result, list) and len(result) == 1 else None

    def portal_client_id(self, gym_key):
        """The one portal UUID mapped to an exact Echo account key, or None.

        This is the tenant-binding lookup used by structured automation tickets.
        It deliberately refuses display-name and registry fallbacks: a buildable
        FIXER row may only carry the portal's own client identity.
        """
        rows = self._get("echo_intake_tokens", {
            "echo_account_key": f"eq.{gym_key}",
            "select": "gym_id,echo_account_key", "limit": "2"})
        ids = {str(row.get("gym_id") or "") for row in rows
               if isinstance(row, dict) and row.get("echo_account_key") == gym_key}
        if len(ids) != 1:
            return None
        client_id = next(iter(ids))
        return client_id if _UUID.fullmatch(client_id) else None

    def record_seeded_ops_fix(self, row):
        """Insert one deterministic structured ops-fix ticket.

        The caller supplies the fully materialized ticket.  This boundary checks
        the fields that make it autonomous and tenant-safe, then confirms the
        returned representation.  A retry may hit the deterministic primary key;
        it succeeds only when the existing row carries the same immutable source
        event and business-check pointer.
        """
        if not isinstance(row, dict):
            raise BusError(400, "seeded ops-fix row must be an object")
        before = row.get("verification_before")
        fixer = before.get("fixer") if isinstance(before, dict) else None
        check = fixer.get("business_check") if isinstance(fixer, dict) else None
        event = fixer.get("source_event") if isinstance(fixer, dict) else None
        valid = (
            row.get("product") == "echo" and row.get("source") == "ops_fix"
            and row.get("status") == "new" and row.get("classification") == "code_fix"
            and _UUID.fullmatch(str(row.get("id") or ""))
            and _UUID.fullmatch(str(row.get("client_id") or ""))
            and isinstance(check, dict) and check.get("ticket_id") == row.get("id")
            and check.get("client_id") == row.get("client_id")
            and check.get("schema_version") == 1
            and check.get("contract_version") == "echo-business-evidence-v1"
            and check.get("check_id") == "forward_book_grade_at_least"
            and isinstance(check.get("params"), dict)
            and set(check["params"]) == {"min_total"}
            and isinstance(check["params"]["min_total"], int)
            and not isinstance(check["params"]["min_total"], bool)
            and 0 <= check["params"]["min_total"] <= 100
            and _HASH.fullmatch(str(check.get("request_key") or ""))
            and isinstance(event, dict)
            and event.get("schema_version") == 1
            and event.get("source") == "echo.grade_sweep.forward_book_drop"
            and _HASH.fullmatch(str(event.get("source_event_id") or ""))
            and isinstance(event.get("gym_key"), str)
            and isinstance(event.get("previous_total"), int)
            and not isinstance(event.get("previous_total"), bool)
            and 0 <= event["previous_total"] <= 100
            and check["params"]["min_total"] == event.get("previous_total")
            and isinstance(event.get("observed_total"), int)
            and not isinstance(event.get("observed_total"), bool)
            and 0 <= event["observed_total"] <= 100
            and event["observed_total"] < event.get("previous_total"))
        if not valid and isinstance(event, dict) and event.get("source") == "echo.stories.media_hold":
            from ..fixer_business_seed import valid_story_ticket
            valid = valid_story_ticket(row)
            if valid:
                mapping = self._get("echo_intake_tokens", {
                    "gym_id": f"eq.{row['client_id']}",
                    "select": "gym_id,echo_account_key", "limit": "2"})
                valid = (isinstance(mapping, list) and len(mapping) == 1
                         and mapping[0].get("gym_id") == row["client_id"]
                         and mapping[0].get("echo_account_key") == event["gym_key"])
        if not valid:
            raise BusError(400, "invalid seeded ops-fix identity or contract")
        if self.portal_client_id(event["gym_key"]) != row["client_id"]:
            raise BusError(409, "seeded ops-fix tenant mapping changed")

        created, duplicate = self._insert(_TICKETS, row)
        stored = self.ticket(row["id"]) if duplicate else created
        if not isinstance(stored, dict):
            raise BusError(503, "seeded ops-fix write was not confirmed")
        stored_before = stored.get("verification_before")
        stored_fixer = stored_before.get("fixer") if isinstance(stored_before, dict) else None
        stored_check = stored_fixer.get("business_check") if isinstance(stored_fixer, dict) else None
        stored_event = stored_fixer.get("source_event") if isinstance(stored_fixer, dict) else None
        if (stored.get("id") != row["id"] or stored.get("product") != "echo"
                or stored.get("source") != "ops_fix"
                or stored.get("client_id") != row["client_id"]
                or stored_check != check or stored_event != event):
            raise BusError(409, "seeded ops-fix readback identity mismatch")
        return stored, duplicate

    def find_new_tickets(self, *, product, source, limit=20, after=None):
        """D46: the portal-ticket worker's poll query. A non-Slack-sourced ticket
        (product/source given explicitly, never a wildcard) that has not been classified
        yet -- `status=eq.new` AND `classification=is.null` together are what "not yet
        picked up by anything" means for this bus; a ticket already routed to a
        classification (question/code_fix/action_request) or otherwise past 'new' is
        never re-fetched here, so a slow worker restart can never double-process one.

        2026-09-23 starvation fix: the window is now BOUNDED AND FAIR, not "the oldest 20
        forever".

          * `is_test=eq.false` is filtered SERVER-SIDE (the column exists since migration
            support_tickets_is_test_20260905, not-null default false), so a block of
            flagged probe rows no longer fills the page before the local strict filter
            ever runs. If an older database without the column answers 400, the query is
            retried once without that filter -- the local strict predicate below still
            applies either way, and any other error (auth, transport) raises unchanged.
          * `after` is an optional KEYSET cursor {"created_at", "id"}: only rows strictly
            past it are returned (created_at, id ascending -- id is the tiebreak so equal
            timestamps cannot skip or repeat a row). Keyset, never OFFSET: an offset page
            shifts under concurrent inserts and silently skips rows. The caller
            (echo_ticket_worker.intake_pass) owns advancing and persisting the cursor,
            so a page of permanently-failing rows is walked PAST instead of monopolising
            every poll, and the cursor wraps to the top once the tail is reached.

        Returns a TicketPage: a plain list of the work-ready rows (strict test filter
        applied), carrying `.raw_count` / `.raw_last` facts about the unfiltered page so
        the caller can advance its cursor past rows the filter removed."""
        params = {
            "product": f"eq.{product}", "source": f"eq.{source}", "status": "eq.new",
            "classification": "is.null", "is_test": "eq.false", "select": "*",
            "order": "created_at.asc,id.asc", "limit": str(int(limit))}
        if after:
            ts = str(after.get("created_at") or "").replace('"', "")
            tid = str(after.get("id") or "").replace('"', "")
            if ts and tid:
                params["or"] = (f'(created_at.gt."{ts}",'
                                f'and(created_at.eq."{ts}",id.gt."{tid}"))')
        try:
            rows = self._get(_TICKETS, params)
        except BusError as e:
            if e.status != 400 or "is_test" not in params:
                raise
            # is_test may not exist yet on an older database; drop the server-side filter
            # (never the status/classification predicates) and rely on the strict local
            # predicate below, the same fallback count_tickets_for_user_today uses.
            params.pop("is_test")
            rows = self._get(_TICKETS, params)
        # 2026-09-05: our own arming probes are never work. Eight of them sat in #fixer
        # looking exactly like unhandled client tickets; a re-run of this poll must not put
        # any of them back on a card. testdata.py is the single predicate for that, shared
        # with every report and metric so they can never disagree.
        return TicketPage(_td.exclude_test_strict(rows),
                          raw_count=len(rows),
                          raw_last=(rows[-1] if rows else None))

    def find_fixing_tickets(self, *, product, limit=20):
        """The second-stage poll: code_fix tickets already dispatched to the fixer
        worker (status='fixing', set by the worker that wrote the fixer_request), whose
        verification the worker may or may not have written back yet.

        MINOR 5 (audit 8): this was the one poll with no test-ticket filter, so a probe
        parked in 'fixing' produced a #fixer card every day forever -- the opposite of the
        "never resurface" the probe purge was for."""
        rows = self._get(_TICKETS, {
            "product": f"eq.{product}", "status": "eq.fixing", "select": "*",
            "order": "created_at.asc", "limit": str(int(limit))})
        return _td.exclude_test_strict(rows)

    def count_tickets_for_user_today(self, slack_user_id, bot_identity=None):
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                   microsecond=0).isoformat()
        # Finding 7 (2026-09-05 audit 3): this selected "id" only, so every row handed to
        # the filter was {"id": ...} and the filter could never match anything -- inert in
        # production, the exact pattern D56 names, inside the fix for a different one. The
        # columns the predicate reads are selected now, and it uses the STRICT predicate: a
        # daily cap is a safety limit, and loosening it on a tag a client could type would
        # hand anyone an unbounded cap.
        params = {"slack_user_id": f"eq.{slack_user_id}", "created_at": f"gte.{start}",
                  "select": "id,raw_text,reporter,slack_user_id,is_test"}
        if bot_identity:
            params["bot_identity"] = f"eq.{bot_identity}"
        try:
            rows = self._get(_TICKETS, params)
        except BusError:
            # is_test may not exist yet on an older database; fall back to the columns that
            # always have, rather than failing the cap read open.
            params["select"] = "id,raw_text,reporter,slack_user_id"
            rows = self._get(_TICKETS, params)
        return len(_td.exclude_test_strict(rows))

    def find_recent_ticket_for_user_today(self, slack_user_id, bot_identity=None):
        """RB2/D25 (2026-09-03, MAJOR): the most recent ticket this user opened today, in ANY
        channel. Used ONLY once the daily cap is hit, so a user over the cap attaches to a
        ticket they already have instead of minting a fresh one -- the per-ticket noise caps
        can only bound total noise per user per day if the ticket count per user per day is
        itself actually bounded, which this closes.

        E1 (2026-09-03, MAJOR, 4th audit): scoped by `bot_identity` -- one Slack user can be
        capped on Echo while also messaging Ranger. Without this scope, a message could reuse
        the OTHER identity's ticket (same user, wrong bot_identity); every row written to it
        would carry the calling identity's own `attachments.identity` while the ticket's
        `bot_identity` stayed the other bot's, and _dispatch_one's ownership check means
        NEITHER identity's outbox loop would ever pick the row up -- a row and its hold_notice
        stranded in 'ready' forever, with no error and no alert. `bot_identity` is optional
        only so an older caller that predates this fix still runs (unscoped, the old bug);
        the adapter always passes its own identity name."""
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                   microsecond=0).isoformat()
        params = {"slack_user_id": f"eq.{slack_user_id}", "created_at": f"gte.{start}",
                  "select": "*", "order": "created_at.desc", "limit": "1"}
        if bot_identity:
            params["bot_identity"] = f"eq.{bot_identity}"
        rows = self._get(_TICKETS, params)
        return rows[0] if rows else None

    # ---- messages -----------------------------------------------------------------------
    def record_inbound(self, *, ticket_id, slack_event_id, slack_ts, author_type,
                       author_id, body, meta=None):
        """A human spoke. INSERT first; duplicate event id -> (None, True), a no-op.
        `meta` (e.g. Slack's raw event_id, the surface) rides in attachments."""
        row = {"ticket_id": ticket_id, "author_type": author_type, "author_id": author_id,
               "body": (body or "")[:8000], "attachments": (meta or None),
               "direction": "inbound",
               "slack_event_id": slack_event_id or None, "slack_ts": slack_ts or None}
        return self._insert(_MESSAGES, row)

    def record_outbound(self, *, ticket_id, author_type, body, delivery_status, kind,
                        meta=None, expected_request_version=None, message_id=None):
        """The bot's reply AS A ROW. Nothing posts until the outbox reads it back in 'ready'.
        `kind` (ack | answer | template | escalation | fixer_request | hold_notice | status)
        rides in attachments so the outbox can apply the verification gate per kind without a
        schema change."""
        att = {"kind": kind}
        if meta:
            att.update(meta)
        if (att.get("fixer_current_attempt_token")
                and not config.slack_convo_echo_current_notice_enabled()):
            raise BusError(409, "current notice capability disabled")
        # Portal 0384 requires the exact notice ID and attempt token at INSERT.
        # Only a current unheld Echo website-tab completion enters this path.
        current_notice = (att.get("fixer") is True
                          and (kind == "answer" or
                               kind == "status" and att.get("resolve_notice") is True))
        if current_notice and message_id is None:
            ticket = self.ticket(ticket_id)
            current_notice = ((ticket or {}).get("product") == "echo"
                              and (ticket or {}).get("source") == "website_tab")
            if current_notice:
                if kind == "answer":
                    from . import adapter as _adapter
                    if (_adapter.promises_human_follow_up(body or "")
                            or _adapter.answer_commits_to_action(body or "")):
                        current_notice = False
                if (ticket.get("status") not in ("verification", "merged")
                        or ticket.get("escalated") is True
                        or ticket.get("hold_tier") is not None):
                    current_notice = False
            if current_notice:
                if not config.slack_convo_echo_current_notice_enabled():
                    raise BusError(409, "current notice capability disabled")
                message_id = str(uuid.uuid4())
                token = self.begin_current_notice(ticket, message_id)
                if not token:
                    raise BusError(409, "current notice reservation refused")
                att["fixer_current_attempt_token"] = token
                att.setdefault("request_version", ticket["request_version"])
                att.update({
                    "delivery_identity_fence": True,
                    "delivery_expected_status": ticket.get("status"),
                    "delivery_expected_classification": ticket.get("classification"),
                    "delivery_expected_product": ticket.get("product"),
                    "delivery_expected_client_id": ticket.get("client_id"),
                    "delivery_expected_bot_identity": ticket.get("bot_identity"),
                    "delivery_expected_slack_user_id": ticket.get("slack_user_id"),
                    "delivery_expected_slack_channel_id": ticket.get("slack_channel_id"),
                    "delivery_expected_slack_thread_ts": ticket.get("slack_thread_ts"),
                })
                expected_request_version = ticket.get("request_version")
                if (ticket.get("slack_channel_id")
                        and att.get("recipient_kind") not in ("staff", "coach")):
                    mention = f"<@{config.APPROVER_SLACK_ID}>"
                    if mention not in (body or ""):
                        body = f"{mention} {body or ''}"
        row = {"ticket_id": ticket_id, "author_type": author_type, "author_id": None,
               "body": (body or "")[:8000], "attachments": att, "direction": "outbound",
               "delivery_status": delivery_status}
        if message_id is not None:
            if not _UUID.fullmatch(str(message_id)):
                raise BusError(400, "invalid outbound message id")
            row["id"] = message_id
        if expected_request_version is not None:
            if type(expected_request_version) is not int or expected_request_version < 0:
                raise BusError(400, "invalid outbound expected request version")
            # Migration 0381 compares this expected value with the ticket under its
            # insert lock. A new requester cycle between Python read and INSERT
            # refuses the row before any Slack post can use it as completion proof.
            row["delivery_request_version"] = expected_request_version
        try:
            created, dup = self._insert(_MESSAGES, row)
        except Exception:
            # A lost response is indistinguishable from a committed INSERT.
            # Exact-ID readback resolves it without minting another notice.
            existing = self.message(message_id) if message_id else None
            if not existing or any(existing.get(k) != row.get(k) for k in
                                   ("id", "ticket_id", "author_type", "body",
                                    "direction", "delivery_request_version")) \
                    or (existing.get("attachments") or {}) != att:
                raise
            return existing
        if dup:
            # A lost INSERT response may leave the designated row in the database.
            # Accept only the exact row and original immutable content/fence.
            existing = self.message(message_id) if message_id else None
            if not existing or any(existing.get(k) != row.get(k) for k in
                                   ("id", "ticket_id", "author_type", "body",
                                    "direction", "delivery_request_version")) \
                    or (existing.get("attachments") or {}) != att:
                raise BusError(409, "conflicting outbound notice id")
            return existing
        return created

    def inbound_count(self, ticket_id):
        rows = self._get(_MESSAGES, {"ticket_id": f"eq.{ticket_id}",
                                     "direction": "eq.inbound", "select": "id"})
        return len(rows)

    def messages(self, ticket_id, limit=40):
        return self._get(_MESSAGES, {"ticket_id": f"eq.{ticket_id}", "select": "*",
                                     "order": "created_at.asc", "limit": str(int(limit))})

    def recent_messages(self, ticket_id, limit=200):
        """The NEWEST rows on a ticket, newest first. `messages` is ascending, which makes a
        client-side scan of a long ticket read only its oldest rows (audit 8, MINOR 1)."""
        return self._get(_MESSAGES, {"ticket_id": f"eq.{ticket_id}", "select": "*",
                                     "order": "created_at.desc", "limit": str(int(limit))})

    def message(self, message_id):
        rows = self._get(_MESSAGES, {"id": f"eq.{message_id}", "select": "*", "limit": "1"})
        return rows[0] if rows else None

    def claim_message(self, message_id):
        """Atomic compare-and-swap: ready -> posting, in ONE round trip (WHERE id=... AND
        delivery_status='ready'). Returns True only if THIS call won the row -- PostgREST
        returns the updated row(s) with Prefer: return=representation, empty when the WHERE
        matched nothing because someone else already moved it. This is what makes two
        concurrent consumers of the same row (a redeploy overlap, a second Wrangler pointed
        at the same rows per D2) safe: at most one of them gets a non-empty result (N4)."""
        r = self._client().patch(self._rest(_MESSAGES),
                                 params={"id": f"eq.{message_id}", "delivery_status": "eq.ready"},
                                 data=json.dumps({"delivery_status": "posting"}),
                                 headers=self._headers({"Prefer": "return=representation"}),
                                 timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, (r.text or "")[:200])
        data = r.json() or []
        return bool(data)

    def claim_fixer_message(self, message_id, expected_attachments, claimed_at,
                            protocol):
        """Atomically claim a modern FIXER row and stamp its pre-POST protocol.

        A crash after this PATCH but before delivery-intent persistence is therefore
        distinguishable from an unmarked legacy posting whose send outcome is unknown.
        The complete attachment snapshot prevents a concurrent release/edit from being
        overwritten by the claim metadata write.
        """
        snapshot = dict(expected_attachments or {})
        next_att = {**snapshot, "claimed_at": claimed_at,
                    "fixer_slack_delivery_protocol": protocol}
        changed = self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.ready",
            "attachments": ("is.null" if expected_attachments is None else
                            "eq." + json.dumps(snapshot, separators=(",", ":"),
                                               sort_keys=True)),
        }, {"delivery_status": "posting", "attachments": next_att})
        return changed

    def requeue_unattempted_fixer_delivery(self, message_id, protocol, recovered_at):
        """CAS a stale modern pre-intent claim back to ready; Slack was not called."""
        row = self.message(message_id)
        raw_att = (row or {}).get("attachments")
        snapshot = dict(raw_att or {})
        if (not row or row.get("delivery_status") != "posting"
                or snapshot.get("fixer_slack_delivery_protocol") != protocol
                or snapshot.get("fixer_slack_delivery_intent") is not None
                or snapshot.get("fixer_slack_delivery_uncertain")
                or row.get("slack_ts")):
            return None
        next_att = {**snapshot, "reclaimed_stale_pre_intent_at": recovered_at,
                    "claimed_at": recovered_at}
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.posting",
            "slack_ts": "is.null",
            "attachments": ("is.null" if raw_att is None else
                            "eq." + json.dumps(snapshot, separators=(",", ":"),
                                               sort_keys=True)),
        }, {"delivery_status": "ready", "attachments": next_att})

    def count_outbound_kind_since(self, ticket_id, kind, since_iso):
        """Server-side count of outbound rows of one `kind` on a ticket since a timestamp.
        Used for the daily noise caps (N3/RA-M3): a client-side scan of bus.messages(tid,
        limit=200) undercounts once a ticket has more than 200 rows today (a chatty or
        long-lived thread), which silently loosens every cap built on it."""
        rows = self._get(_MESSAGES, {
            "ticket_id": f"eq.{ticket_id}", "direction": "eq.outbound",
            "attachments->>kind": f"eq.{kind}", "created_at": f"gte.{since_iso}",
            "select": "id"})
        return len(rows)

    def count_escalation_cards_since(self, ticket_id, since_iso):
        """Escalation rows on a ticket since a timestamp, EXCLUDING receipts.

        Receipts ride on kind='escalation' (the portal's client-visibility denylist has no
        'receipt' entry, so a new kind would be readable by the client). They are marked with
        attachments.receipt, and every bound that means "have we already told a human about
        this" must exclude them -- otherwise one receipt suppresses a real card for the rest
        of the day (audit 6, finding 3). Filtered server-side, so it cannot undercount the
        way a client-side scan of the oldest 200 rows did."""
        rows = self._get(_MESSAGES, {
            "ticket_id": f"eq.{ticket_id}", "direction": "eq.outbound",
            "attachments->>kind": f"eq.{_a_kind_escalation()}",
            "attachments->>receipt": "is.null",
            "created_at": f"gte.{since_iso}", "select": "id"})
        return len(rows)

    # Portal 0633 durable send admission: a row stamped with this lease but without a
    # trusted completion has an UNKNOWN durable effect no matter what delivery_status
    # a later edit left behind. It must block any drain acknowledgment until the
    # admission lane itself reports zero unresolved for the paused generation.
    SUPPORT_SEND_LANE = "support-resolution-send"
    SUPPORT_SEND_ADMISSION_KEY = "support_resolution_send_admission"

    def support_uncertain_outbound(self, limit=1000):
        """Status-independent read: uncertainty survives even a mistaken status edit."""
        markers = ("fixer_slack_delivery_uncertain", "fixer_route_uncertain",
                   "fixer_route_pending", "outreach_delivery_uncertain",
                   "slack_replay_delivery_uncertain", self.SUPPORT_SEND_ADMISSION_KEY)
        return self._get(_MESSAGES, {
            "direction": "eq.outbound", "select": "*", "order": "created_at.asc,id.asc",
            "limit": str(int(limit)),
            "or": "(" + ",".join(f"attachments->>{key}.not.is.null" for key in markers) + ")",
        })

    def support_admission_status_lane(self, lane=SUPPORT_SEND_LANE):
        """Fresh portal 0633 durable admission lane status, fail closed on transport
        or malformed receipts. Callers validate the payload; this never retries."""
        r = self._client().post(self._rest("rpc/support_admission_status_lane"),
                                data=json.dumps({"p_lane": lane}),
                                headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, "support admission status unavailable")
        result = r.json()
        if not isinstance(result, dict):
            raise BusError(400, "support admission status malformed")
        return result

    def support_admission_send_receipt(self, *, invocation_id, generation,
                                       ticket_id, request_version, message_id,
                                       slack_ts):
        """Transport-level portal 0633 one-shot Slack timestamp receipt.

        Records the single Slack API ts against the exact admitted send
        binding. Fail closed on transport or malformed receipts; callers
        validate every returned field and never retry an uncertain call --
        a duplicate receipt is itself a durable effect and never reposted.
        """
        body = {"p_invocation_id": invocation_id, "p_generation": generation,
                "p_ticket_id": ticket_id, "p_expected_request_version": request_version,
                "p_message_id": message_id, "p_slack_ts": slack_ts}
        r = self._client().post(self._rest("rpc/support_admission_send_receipt"),
                                data=json.dumps(body),
                                headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, "support admission send receipt unavailable")
        result = r.json()
        if not isinstance(result, dict):
            raise BusError(400, "support admission send receipt malformed")
        return result

    def support_admission_inventory(self, lane, *, limit=200, after_started=None,
                                    after_invocation=None):
        """Read-only portal 0633 paginated invocation inventory, fail closed.

        Keyset cursor: (after_started, after_invocation) move together or not at
        all. Never retries; callers validate every page field before trusting it.
        """
        if (after_started is None) != (after_invocation is None):
            raise BusError(400, "support admission inventory cursor malformed")
        body = {"p_lane": lane, "p_limit": int(limit),
                "p_after_started": after_started,
                "p_after_invocation": after_invocation}
        r = self._client().post(self._rest("rpc/support_admission_inventory"),
                                data=json.dumps(body),
                                headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, "support admission inventory unavailable")
        result = r.json()
        if not isinstance(result, dict):
            raise BusError(400, "support admission inventory malformed")
        return result

    def support_cutover_reservation_status(self, reservation_id, epoch):
        """Frozen Portal DRAFT_0639 durable cutover reservation status receipt
        (public.support_cutover_reservation_status(uuid,bigint)). Fail closed on
        transport or malformed receipts; callers validate every field."""
        body = {"p_reservation_id": reservation_id, "p_epoch": int(epoch)}
        r = self._client().post(self._rest("rpc/support_cutover_reservation_status"),
                                data=json.dumps(body),
                                headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, "support cutover reservation status unavailable")
        result = r.json()
        if not isinstance(result, dict):
            raise BusError(400, "support cutover reservation status malformed")
        return result

    def support_cutover_reservation_inventory(self, reservation_id, epoch,
                                              lane, *, limit=200,
                                              after_started=None,
                                              after_invocation=None):
        """Frozen Portal DRAFT_0639 read-only paginated invocation inventory
        pinned to the held cutover reservation
        (public.support_cutover_reservation_inventory(uuid,bigint,text,integer,
        timestamptz,uuid)). Fail closed; never retries."""
        if (after_started is None) != (after_invocation is None):
            raise BusError(400, "support cutover inventory cursor malformed")
        body = {"p_reservation_id": reservation_id, "p_epoch": int(epoch),
                "p_lane": lane, "p_limit": int(limit),
                "p_after_started": after_started,
                "p_after_invocation": after_invocation}
        r = self._client().post(self._rest("rpc/support_cutover_reservation_inventory"),
                                data=json.dumps(body),
                                headers=self._headers(), timeout=30)
        if r.status_code >= 400:
            raise BusError(r.status_code, "support cutover reservation inventory unavailable")
        result = r.json()
        if not isinstance(result, dict):
            raise BusError(400, "support cutover reservation inventory malformed")
        return result

    def outbox(self, status="ready", limit=50, identity=None):
        """Outbound rows in one delivery state, oldest first. `identity` narrows to rows this
        bot wrote (attachments.identity), so two identities' loops never read each other's
        queue (V-M8); rows with no identity stamp are returned to every caller and the
        outbox suppresses them (V-M7: fail closed, never post an unattributed row)."""
        params = {"direction": "eq.outbound", "delivery_status": f"eq.{status}",
                  "select": "*", "order": "created_at.asc", "limit": str(int(limit))}
        if identity:
            params["or"] = f"(attachments->>identity.eq.{identity},attachments->>identity.is.null)"
        return self._get(_MESSAGES, params)

    def pending_fixer_holds(self, identity, marker, limit=200, after=None):
        """One keyset page of held FIXER rows carrying an exact recovery marker.

        Marker-specific paging prevents unrelated trust-ladder holds from
        starving delivery reconciliation while keeping each sweep bounded.
        """
        if marker not in {"fixer_slack_delivery_uncertain",
                          "fixer_route_uncertain",
                          "fixer_slack_route_missing",
                          "fixer_slack_config_missing"}:
            raise BusError(400, "invalid FIXER hold marker")
        params = {
            "direction": "eq.outbound", "delivery_status": "eq.held",
            "attachments->>identity": f"eq.{identity}",
            f"attachments->>{marker}": "eq.true",
            "select": "*", "order": "created_at.asc,id.asc",
            "limit": str(int(limit)),
        }
        if after:
            ts = str(after.get("created_at") or "").replace('"', "")
            mid = str(after.get("id") or "").replace('"', "")
            if ts and mid:
                params["or"] = (f'(created_at.gt."{ts}",'
                                f'and(created_at.eq."{ts}",id.gt."{mid}"))')
        return self._get(_MESSAGES, params)

    def mark_message(self, message_id, delivery_status, slack_ts=None, meta_update=None):
        """Move a row between delivery states. `meta_update` merges keys into attachments
        (read-merge-write; jsonb PATCH replaces the whole value otherwise)."""
        fields = {"delivery_status": delivery_status}
        if slack_ts:
            fields["slack_ts"] = slack_ts
        if meta_update:
            cur = self.message(message_id) or {}
            att = dict(cur.get("attachments") or {})
            att.update(meta_update)
            fields["attachments"] = att
        return self._patch(_MESSAGES, {"id": f"eq.{message_id}"}, fields)

    def transition_fixer_delivery(self, message_id, delivery_status, *,
                                  slack_ts=None, meta_update=None,
                                  expected_intent=None, expected_ts=None,
                                  attempts=3):
        """CAS a FIXER Slack row from posting, preserving its delivery intent.

        A stale sweeper may quarantine an uncertain post while the original worker
        waits on Slack. Neither worker may overwrite the other's terminal state.
        """
        if delivery_status == "posted" and (
                not isinstance(expected_intent, dict)
                or not isinstance(expected_ts, str) or not expected_ts):
            return None
        if expected_ts is not None and slack_ts is not None and slack_ts != expected_ts:
            return None
        for _ in range(max(1, int(attempts))):
            row = self.message(message_id)
            att = (row or {}).get("attachments")
            snapshot = dict(att or {})
            if (not row or row.get("delivery_status") != "posting"
                    or not snapshot.get("fixer_slack_delivery_intent")
                    or (expected_intent is not None
                        and snapshot.get("fixer_slack_delivery_intent") != expected_intent)
                    or (expected_ts is not None and row.get("slack_ts") != expected_ts)):
                return None
            fields = {"delivery_status": delivery_status}
            if slack_ts:
                fields["slack_ts"] = slack_ts
            if meta_update:
                fields["attachments"] = {**snapshot, **meta_update}
            match = {
                "id": f"eq.{message_id}", "delivery_status": "eq.posting",
                "attachments": ("is.null" if att is None else
                                "eq." + json.dumps(snapshot, separators=(",", ":"),
                                                   sort_keys=True)),
            }
            if expected_ts is not None:
                match["slack_ts"] = f"eq.{expected_ts}"
            changed = self._patch(_MESSAGES, match, fields)
            if changed:
                return changed
        return None

    def record_fixer_delivery_timestamp(self, message_id, intent, slack_ts, attempts=3):
        """Persist Slack's successful POST timestamp across a stale-sweeper race.

        A second worker may quarantine posting -> held while the POST caller is
        waiting on Slack.  The timestamp still belongs to this row only when the
        complete durable intent is unchanged and no different timestamp exists.
        Compare the full attachment snapshot and exact status on every attempt;
        conflicts fail closed instead of making the row resendable.
        """
        if not isinstance(intent, dict) or not isinstance(slack_ts, str) or not slack_ts:
            return None
        for _ in range(max(1, int(attempts))):
            row = self.message(message_id)
            if not row or row.get("delivery_status") not in {"posting", "held"}:
                return None
            att = row.get("attachments")
            snapshot = dict(att or {})
            if snapshot.get("fixer_slack_delivery_intent") != intent:
                return None
            if row.get("delivery_status") == "held":
                known_route_uncertain = (
                    snapshot.get("fixer_slack_delivery_uncertain") is True
                    and snapshot.get("fixer_route_pending") is not True)
                pending_route_uncertain = (
                    snapshot.get("fixer_route_pending") is True
                    and snapshot.get("fixer_route_uncertain") is True
                    and bool(snapshot.get("fixer_current_attempt_token")))
                if not (known_route_uncertain or pending_route_uncertain):
                    return None
            existing = row.get("slack_ts")
            if existing:
                return row if existing == slack_ts else None
            changed = self._patch(_MESSAGES, {
                "id": f"eq.{message_id}",
                "delivery_status": f"eq.{row['delivery_status']}",
                "slack_ts": "is.null",
                "attachments": ("is.null" if att is None else
                                "eq." + json.dumps(snapshot, separators=(",", ":"),
                                                   sort_keys=True)),
            }, {"slack_ts": slack_ts})
            if changed:
                return changed
        return None

    def prepare_fixer_delivery(self, message_id, intent, *, expected_attachments):
        """Persist intent only while the exact claimant snapshot still owns posting."""
        if not isinstance(expected_attachments, dict):
            return None
        snapshot = dict(expected_attachments)
        att = {**snapshot, "fixer_slack_delivery_intent": intent,
               "claimed_at": intent.get("not_before") or intent["claimed_at"]}
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.posting",
            "slack_ts": "is.null",
            "attachments": "eq." + json.dumps(
                snapshot, separators=(",", ":"), sort_keys=True),
        }, {"attachments": att})

    def suppress_unattempted_current_notice(self, message_id, reason, *,
                                            expected_identity=None, expected_token=None):
        """Terminally park an exact ready/claimed notice before durable Slack intent."""
        row = self.message(message_id)
        raw_att = (row or {}).get("attachments")
        att = dict(raw_att or {})
        if (not row or row.get("delivery_status") not in {"ready", "posting"}
                or not att.get("fixer_current_attempt_token")
                or expected_identity is not None and att.get("identity") != expected_identity
                or expected_token is not None and att.get("fixer_current_attempt_token") != expected_token
                or att.get("fixer_slack_delivery_intent") is not None
                or row.get("slack_ts") or row.get("slack_event_id")
                or att.get("delivery_readback_verified") is True):
            return None
        next_att = {**att, "suppressed_why": str(reason)[:300]}
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": f"eq.{row['delivery_status']}",
            "slack_ts": "is.null", "slack_event_id": "is.null",
            "attachments": ("is.null" if raw_att is None else
                            "eq." + json.dumps(att, separators=(",", ":"),
                                               sort_keys=True)),
        }, {"delivery_status": "suppressed", "attachments": next_att})

    def suppress_unclaimed_current_notice(self, message_id, reason):
        """Cancel an exact reserved ready row after a failed direct claim."""
        row = self.message(message_id)
        raw_att = (row or {}).get("attachments")
        att = dict(raw_att or {})
        if (not row or row.get("delivery_status") != "ready"
                or not att.get("fixer_current_attempt_token")
                or att.get("fixer_route_pending") is not True
                or att.get("fixer_slack_delivery_intent") is not None
                or row.get("slack_ts") or row.get("slack_event_id")
                or att.get("delivery_readback_verified") is True):
            return None
        next_att = {**att, "suppressed_why": str(reason)[:300]}
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.ready",
            "slack_ts": "is.null", "slack_event_id": "is.null",
            "attachments": ("is.null" if raw_att is None else
                            "eq." + json.dumps(att, separators=(",", ":"),
                                               sort_keys=True)),
        }, {"delivery_status": "suppressed", "attachments": next_att})

    def ensure_suppressed_current_notice_alert(self, message_id, identity):
        """Queue one stable staff row without mutating or resending the notice.

        Suppression itself is the durable retry record. Read it freshly, and
        accept only a definitely pre-POST terminal notice. The deterministic
        alert UUID makes INSERT retries and concurrent reconcilers idempotent;
        existing alerts retain their delivery state and transport metadata.
        """
        notice = self.message(message_id)
        if not _unsent_suppressed_current_notice(notice, identity):
            return None
        ticket_id = notice["ticket_id"]
        alert_id = _suppressed_current_notice_alert_id(message_id)
        body = _suppressed_notice_alert_body(notice)
        meta = {"identity": identity, "suppressed_message_id": message_id,
                "fixer_suppressed_notice_alert": True}

        def exact_alert(row):
            return bool(row and row.get("id") == alert_id
                        and _verified_suppressed_notice_alert(row, notice, identity))

        existing = self.message(alert_id)
        if existing:
            if not exact_alert(existing):
                raise BusError(409, "suppressed notice alert identity conflict")
            return existing
        # Recognize staff rows emitted before stable alert IDs were introduced.
        # An already queued or posted legacy escalation must not be duplicated.
        legacy = self._get(_MESSAGES, {
            "ticket_id": f"eq.{ticket_id}", "direction": "eq.outbound",
            "author_type": "eq.system", "attachments->>kind": "eq.escalation",
            "attachments->>identity": f"eq.{identity}",
            "attachments->>suppressed_message_id": f"eq.{message_id}",
            "select": "*", "order": "created_at.asc,id.asc", "limit": "20",
        })
        for row in legacy:
            if _verified_suppressed_notice_alert(row, notice, identity):
                return row
        try:
            created = self.record_outbound(
                ticket_id=ticket_id, author_type="system", body=body,
                delivery_status="ready", kind=_a_kind_escalation(),
                meta=meta, message_id=alert_id)
            if not exact_alert(created):
                raise BusError(409, "suppressed notice alert insert not confirmed")
            return created
        except Exception:
            # A committed INSERT may already have been claimed/posted before
            # the response arrives. Validate its immutable identity only; never
            # overwrite added delivery metadata or return it to ready.
            existing = self.message(alert_id)
            if exact_alert(existing):
                return existing
            raise

    def verified_suppressed_current_notice_alert(self, alert, ticket, identity):
        """An informational internal alert may retain its original dispatcher.

        Ticket ownership can change after suppression. Only freshly verified
        source-linked system escalations may cross that identity fence; this
        grants no customer delivery, action button or ticket state transition.
        """
        if not ticket or (alert or {}).get("ticket_id") != ticket.get("id"):
            return False
        mid = ((alert or {}).get("attachments") or {}).get("suppressed_message_id")
        if not _UUID.fullmatch(str(mid or "")):
            return False
        notice = self.message(mid)
        return _verified_suppressed_notice_alert(alert, notice, identity)

    def suppressed_unattempted_current_notices(self, identity, *, limit=20, after=None):
        """Bounded keyset page; unrelated suppressed rows cannot starve alerts."""
        params = {
            "direction": "eq.outbound", "delivery_status": "eq.suppressed",
            "attachments->>identity": f"eq.{identity}",
            "attachments->>fixer": "eq.true",
            "attachments->>fixer_current_attempt_token": "not.is.null",
            "attachments->>fixer_slack_delivery_intent": "is.null",
            "slack_ts": "is.null", "slack_event_id": "is.null",
            "select": "*", "order": "created_at.asc,id.asc",
            "limit": str(min(20, max(1, int(limit)))),
        }
        if after:
            ts = str(after.get("created_at") or "").replace('"', "")
            mid = str(after.get("id") or "").replace('"', "")
            if ts and mid:
                params["or"] = (f'(created_at.gt."{ts}",'
                                f'and(created_at.eq."{ts}",id.gt."{mid}"))')
        return self._get(_MESSAGES, params)

    def hold_uncertain_fixer_delivery(self, message_id, reason):
        """Quarantine an uncertain client post; never put it back in ready."""
        row = self.message(message_id)
        if not row or row.get("delivery_status") != "posting":
            return row
        old_att = row.get("attachments")
        att = dict(old_att or {})
        if att.get("fixer_route_pending") is True:
            # 0384 permits only this marker on the reserved unrouted row.
            att["fixer_route_uncertain"] = True
        else:
            att.update(fixer_slack_delivery_uncertain=True,
                       held_why=str(reason)[:300])
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.posting",
            "attachments": ("is.null" if old_att is None else
                            "eq." + json.dumps(old_att, separators=(",", ":"),
                                               sort_keys=True)),
        }, {"delivery_status": "held", "attachments": att})

    def hold_fixer_config_missing(self, message_id, reason):
        """Hold a deterministic pre-POST configuration failure for safe retry."""
        row = self.message(message_id)
        raw_att = (row or {}).get("attachments")
        att = dict(raw_att or {})
        if (not row or row.get("delivery_status") != "posting"
                or att.get("fixer_slack_delivery_intent") is not None
                or att.get("fixer_slack_delivery_uncertain")
                or row.get("slack_ts")):
            return None
        next_att = {**att,
                    "fixer_slack_config_missing": True,
                    "held_why": str(reason)[:300]}
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.posting",
            "slack_ts": "is.null",
            "attachments": ("is.null" if raw_att is None else
                            "eq." + json.dumps(att, separators=(",", ":"),
                                               sort_keys=True)),
        }, {"delivery_status": "held", "attachments": next_att})

    def pending_held_fixer_delivery(self, identity, limit=200, after=None):
        params = {
            "direction": "eq.outbound", "delivery_status": "eq.held",
            "attachments->>identity": f"eq.{identity}",
            "attachments->>fixer_slack_delivery_intent": "not.is.null",
            "select": "*", "order": "created_at.asc,id.asc",
            "limit": str(int(limit)),
        }
        if after:
            ts = str(after.get("created_at") or "").replace('"', "")
            mid = str(after.get("id") or "").replace('"', "")
            if ts and mid:
                params["or"] = (f'(created_at.gt."{ts}",'
                                f'and(created_at.eq."{ts}",id.gt."{mid}"))')
        return self._get(_MESSAGES, params)

    def _patch_held_fixer_attachments(self, message_id, *, eligible, updates,
                                      fields=None, match_update=None, attempts=3):
        """Merge a held FIXER attachment update without dropping a concurrent one.

        PostgREST replaces a jsonb column when it is PATCHed.  Every caller must
        therefore compare the complete attachment snapshot it read, not merely the
        one marker it happens to care about.  A different held-row consumer may win
        between our GET and PATCH; bounded retries re-read that winner and merge our
        change on top.  Exhaustion is a failed CAS, never implied success.
        """
        for _ in range(max(1, int(attempts))):
            row = self.message(message_id)
            att = (row or {}).get("attachments")
            snapshot = dict(att or {})
            if (not row or row.get("delivery_status") != "held"
                    or not eligible(row, snapshot)):
                return None
            patch = updates(row, snapshot)
            next_att = {key: value for key, value in {**snapshot, **patch}.items()
                        if value is not _REMOVE}
            match = {
                "id": f"eq.{message_id}",
                "delivery_status": "eq.held",
                "attachments": ("is.null" if att is None else
                                "eq." + json.dumps(snapshot, separators=(",", ":"),
                                                   sort_keys=True)),
            }
            if match_update:
                match.update(match_update(row, snapshot))
            changed = self._patch(
                _MESSAGES, match, {**(fields or {}), "attachments": next_att})
            if changed:
                return changed
        return None

    def requeue_route_missing_fixer(self, message_id, recovered_at):
        """CAS one never-attempted route hold back to ready.

        A route hold is safe to retry only while it has no durable intent, no
        uncertainty marker and no Slack timestamp.  The caller revalidates the
        current ticket and route immediately before this CAS; normal dispatch
        repeats every gate after the transition and before POST.
        """
        row = self.message(message_id)
        att = (row or {}).get("attachments") or {}
        if (not row or row.get("delivery_status") != "held"
                or att.get("fixer_slack_route_missing") is not True
                or att.get("fixer_slack_delivery_intent") is not None
                or att.get("fixer_slack_delivery_uncertain")
                or row.get("slack_ts")):
            return None
        next_att = {
            **att,
            "fixer_slack_route_missing": False,
            "fixer_slack_route_recovered_at": recovered_at,
            # Freshness starts at the recovery boundary, not when the portal-only
            # completion was first written (which may have been days earlier).
            "claimed_at": recovered_at,
        }
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}",
            "delivery_status": "eq.held",
            "slack_ts": "is.null",
            "attachments->>fixer_slack_route_missing": "eq.true",
            "attachments->>fixer_slack_delivery_intent": "is.null",
            "attachments->>fixer_slack_delivery_uncertain": "is.null",
        }, {"delivery_status": "ready", "attachments": next_att})

    def requeue_config_missing_fixer(self, message_id, recovered_at):
        """CAS one never-attempted configuration hold back to ready."""
        return self._patch_held_fixer_attachments(
            message_id,
            eligible=lambda row, att: (
                att.get("fixer_slack_config_missing") is True
                and att.get("fixer_slack_delivery_intent") is None
                and not att.get("fixer_slack_delivery_uncertain")
                and not row.get("slack_ts")),
            updates=lambda _row, att: {
                "fixer_slack_config_missing": False,
                "fixer_slack_config_recovered_at": recovered_at,
                "claimed_at": recovered_at,
            },
            fields={"delivery_status": "ready"},
            match_update=lambda _row, _att: {"slack_ts": "is.null"},
        )

    def defer_held_fixer_reconcile(self, message_id, next_at):
        return self._patch_held_fixer_attachments(
            message_id,
            eligible=lambda _row, att: bool(att.get("fixer_slack_delivery_uncertain")),
            updates=lambda _row, _att: {"fixer_reconcile_next_at": next_at})

    def reconcile_held_fixer_delivery(self, message_id, proof, *,
                                      expected_intent, expected_ts):
        """Promote a held uncertain delivery only after exact Slack readback."""
        if (not isinstance(proof, dict)
                or proof.get("delivery_readback_verified") is not True
                or not isinstance(expected_ts, str) or not expected_ts
                or proof.get("delivery_readback_ts") != expected_ts):
            return None
        return self._patch_held_fixer_attachments(
            message_id,
            eligible=lambda _row, att: bool(
                att.get("fixer_slack_delivery_uncertain")
                and att.get("fixer_slack_delivery_intent")
                and att.get("fixer_slack_delivery_intent") == expected_intent
                and (not att.get("fixer_current_attempt_token")
                     or (att.get("delivery_readback_verified") is True
                         and att.get("delivery_readback_ts") == expected_ts))
                and _row.get("slack_ts") == expected_ts),
            # Clear the uncertainty marker in the same whole-attachment CAS that
            # promotes the row, retaining the exact proof and durable intent.
            updates=lambda _row, _att: {**proof,
                                        "fixer_slack_delivery_uncertain": _REMOVE},
            fields={"delivery_status": "posted",
                    "slack_ts": proof["delivery_readback_ts"]},
            match_update=lambda _row, _att: {"slack_ts": f"eq.{expected_ts}"})

    def record_held_fixer_readback(self, message_id, proof, *,
                                  expected_intent, expected_ts):
        """Stage exact Slack proof on a known-route held notice before promotion."""
        if (not isinstance(proof, dict)
                or proof.get("delivery_readback_verified") is not True
                or proof.get("delivery_readback_ts") != expected_ts):
            return None
        return self._patch_held_fixer_attachments(
            message_id,
            eligible=lambda row, att: bool(
                att.get("fixer_route_pending") is not True
                and att.get("fixer_slack_delivery_uncertain") is True
                and att.get("fixer_current_attempt_token")
                and att.get("fixer_slack_delivery_intent") == expected_intent
                and row.get("slack_ts") == expected_ts),
            updates=lambda _row, _att: proof,
            match_update=lambda _row, _att: {"slack_ts": f"eq.{expected_ts}"})

    def record_held_current_notice_readback(self, message_id, proof, *,
                                            expected_intent, expected_ts):
        """Persist exact readback on a held pending-route row before SQL binds it.

        The route binder alone may move this row back to posting. This method
        never makes a row resendable or marks a notice posted.
        """
        if (not isinstance(proof, dict)
                or proof.get("delivery_readback_verified") is not True
                or proof.get("delivery_readback_ts") != expected_ts):
            return None
        return self._patch_held_fixer_attachments(
            message_id,
            eligible=lambda row, att: bool(
                att.get("fixer_route_pending") is True
                and att.get("fixer_current_attempt_token")
                and att.get("fixer_slack_delivery_intent") == expected_intent
                and row.get("slack_ts") == expected_ts),
            updates=lambda _row, _att: proof,
            match_update=lambda _row, _att: {"slack_ts": f"eq.{expected_ts}"})

    def uncertain_fixer_alert_status(self, message_id):
        """Posted is proof of an alert; failed notices remain retryable."""
        rows = self._get(_MESSAGES, {
            "attachments->>fixer_uncertain_row_id": f"eq.{message_id}",
            "select": "delivery_status", "order": "created_at.desc", "limit": "100",
        })
        states = {r.get("delivery_status") for r in rows}
        if "posted" in states:
            return "posted"
        if states & {"ready", "posting"}:
            return "pending"
        return "failed" if states else None

    def rearm_uncertain_fixer_alert(self, message_id):
        """Retry the newest failed staff alert in place; never mint alert rows forever."""
        rows = self._get(_MESSAGES, {
            "attachments->>fixer_uncertain_row_id": f"eq.{message_id}",
            "select": "*", "order": "created_at.desc", "limit": "100",
        })
        if any(r.get("delivery_status") == "posted" for r in rows):
            return None
        if any(r.get("delivery_status") in {"ready", "posting"} for r in rows):
            return None
        alert = next((r for r in rows
                      if r.get("delivery_status") in {"failed", "suppressed"}), None)
        if not alert:
            return None
        return self._patch(_MESSAGES, {
            "id": f"eq.{alert['id']}",
            "delivery_status": f"eq.{alert['delivery_status']}",
        }, {"delivery_status": "ready"})

    def reserve_uncertain_fixer_alert_retry(self, message_id, expected_at, next_at):
        """CAS the held row's retry deadline before an alert can be rearmed."""
        return self._patch_held_fixer_attachments(
            message_id,
            eligible=lambda _row, att: att.get("fixer_alert_retry_after") == expected_at,
            updates=lambda _row, _att: {"fixer_alert_retry_after": next_at})

    def mark_uncertain_fixer_alerted(self, message_id):
        return self._patch_held_fixer_attachments(
            message_id,
            eligible=lambda _row, _att: True,
            updates=lambda _row, _att: {"fixer_staff_alerted": True})

    def pending_fixer_finalization(self, identity, limit=100):
        """Verified Slack posts whose ticket close may have been interrupted."""
        return self._get(_MESSAGES, {
            "direction": "eq.outbound", "delivery_status": "eq.posted",
            "attachments->>identity": f"eq.{identity}",
            "attachments->>fixer_slack_delivery_intent": "not.is.null",
            "attachments->>fixer_delivery_finalized_at": "is.null",
            "select": "*", "order": "created_at.desc", "limit": str(int(limit)),
        })

    def fixer_receipt_exists(self, message_id, ticket_id, kind):
        """Require the complete immutable identity of a FIXER delivery receipt."""
        rows = self._get(_MESSAGES, {
            "id": f"eq.{_fixer_receipt_id(message_id)}",
            "ticket_id": f"eq.{ticket_id}",
            "direction": "eq.outbound",
            "attachments->>receipt": "eq.true",
            "attachments->>receipt_for": f"eq.{message_id}",
            "attachments->>kind": f"eq.{kind}",
            "select": "id", "limit": "1",
        })
        return bool(rows)

    def record_fixer_receipt_once(self, *, source_message_id, ticket_id,
                                  author_type, body, delivery_status, kind, meta):
        """Insert exactly one receipt for a delivered FIXER message.

        The receipt's deterministic UUID is the database uniqueness boundary. Two
        processes may race this INSERT; one wins and the other reads that same row.
        """
        receipt_id = _fixer_receipt_id(source_message_id)
        att = {"kind": kind, **dict(meta or {}),
               "receipt": True, "receipt_for": str(source_message_id)}
        row = {"id": receipt_id, "ticket_id": ticket_id,
               "author_type": author_type, "author_id": None,
               "body": (body or "")[:8000], "attachments": att,
               "direction": "outbound", "delivery_status": delivery_status}
        created, duplicate = self._insert(_MESSAGES, row)
        if not duplicate:
            return created
        existing = self.message(receipt_id)
        existing_att = (existing or {}).get("attachments") or {}
        if ((existing or {}).get("ticket_id") == ticket_id
                and (existing or {}).get("direction") == "outbound"
                and existing_att.get("receipt") is True
                and existing_att.get("receipt_for") == str(source_message_id)
                and existing_att.get("kind") == kind):
            return existing
        raise BusError(409, "FIXER receipt id collision")

    def finalize_fixer_delivery(self, message_id, reason):
        row = self.message(message_id)
        if (not row or row.get("delivery_status") != "posted"
                or not (row.get("attachments") or {}).get("delivery_readback_verified")):
            return None
        att = {**(row.get("attachments") or {}),
               "fixer_delivery_finalized_at": datetime.now(timezone.utc).isoformat(),
               "fixer_delivery_finalized_reason": reason}
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.posted",
            "attachments->>delivery_readback_verified": "eq.true",
        }, {"attachments": att})

    def hold_uncertain_outreach(self, message_id):
        """Preserve a committed posted receipt; quarantine only an unposted outreach row."""
        row = self.message(message_id)
        if not row or (row.get("attachments") or {}).get("outreach") is not True:
            return None
        state = row.get("delivery_status")
        if state == "posted" or state == "held":
            return row
        if state not in ("posting", "ready"):
            return row
        att = dict(row.get("attachments") or {})
        att.update({"outreach_delivery_uncertain": True,
                    "held_why": "Slack may have delivered; staff must reconcile before resend"})
        updated = self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": f"eq.{state}",
            "attachments->>outreach": "eq.true",
        }, {"delivery_status": "held", "attachments": att})
        return updated or self.message(message_id)

    def uncertain_outreach_alert_exists(self, ticket_id, message_id):
        rows = self._get(_MESSAGES, {
            "ticket_id": f"eq.{ticket_id}", "direction": "eq.outbound",
            "attachments->>outreach_uncertain_row_id": f"eq.{message_id}",
            "select": "id", "limit": "1",
        })
        return bool(rows)

    def mark_uncertain_outreach_alerted(self, message_id):
        row = self.message(message_id)
        if not row or row.get("delivery_status") != "held":
            return None
        att = dict(row.get("attachments") or {})
        att["outreach_staff_alerted"] = True
        return self._patch(_MESSAGES, {
            "id": f"eq.{message_id}", "delivery_status": "eq.held",
            "attachments->>outreach_delivery_uncertain": "eq.true",
        }, {"attachments": att})

    def set_message_body_if_posting(self, message_id, body):
        """Store the exact Slack text only while this worker owns the claimed row."""
        return self._patch(_MESSAGES,
                           {"id": f"eq.{message_id}", "delivery_status": "eq.posting"},
                           {"body": body})
