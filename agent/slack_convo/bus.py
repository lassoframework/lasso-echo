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
from datetime import datetime, timedelta, timezone

from .. import config

_TICKETS = "support_tickets"
_MESSAGES = "support_messages"

OPEN_STATUSES = ("new", "triage", "fixing", "verification", "hold", "approved")


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

    def count_tickets_for_user_today(self, slack_user_id):
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                   microsecond=0).isoformat()
        rows = self._get(_TICKETS, {
            "slack_user_id": f"eq.{slack_user_id}", "created_at": f"gte.{start}",
            "select": "id"})
        return len(rows)

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
                        meta=None):
        """The bot's reply AS A ROW. Nothing posts until the outbox reads it back in 'ready'.
        `kind` (ack | answer | template | escalation | fixer_request | hold_notice | status)
        rides in attachments so the outbox can apply the verification gate per kind without a
        schema change."""
        att = {"kind": kind}
        if meta:
            att.update(meta)
        row = {"ticket_id": ticket_id, "author_type": author_type, "author_id": None,
               "body": (body or "")[:8000], "attachments": att, "direction": "outbound",
               "delivery_status": delivery_status}
        created, dup = self._insert(_MESSAGES, row)
        if dup:  # cannot happen (no unique key on outbound), but never mask it
            raise BusError(409, "unexpected duplicate on outbound insert")
        return created

    def inbound_count(self, ticket_id):
        rows = self._get(_MESSAGES, {"ticket_id": f"eq.{ticket_id}",
                                     "direction": "eq.inbound", "select": "id"})
        return len(rows)

    def messages(self, ticket_id, limit=40):
        return self._get(_MESSAGES, {"ticket_id": f"eq.{ticket_id}", "select": "*",
                                     "order": "created_at.asc", "limit": str(int(limit))})

    def message(self, message_id):
        rows = self._get(_MESSAGES, {"id": f"eq.{message_id}", "select": "*", "limit": "1"})
        return rows[0] if rows else None

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
