from datetime import datetime, timedelta, timezone

import pytest

from agent.jobs import held_client_ticket_reconciler as R
from agent.slack_convo.bus import BusError
from agent.slack_convo import outbox as O
from agent.slack_convo.identities import IDENTITIES


NOW = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)
TICKET_ID = "7bccbd41-c3e4-4f1f-8068-b045a188a1b2"


def ticket(**updates):
    row = {
        "id": TICKET_ID, "product": "echo", "source": "website_tab",
        "status": "hold", "escalated": True, "resolved_at": None,
        "is_test": False, "bot_identity": "echo", "client_id": "gym-uuid",
        "identity_kind": None, "slack_channel_id": None, "slack_thread_ts": None,
        "request_version": 2, "created_at": (NOW - timedelta(days=2)).isoformat(),
    }
    row.update(updates)
    return row


class FakeBus:
    def __init__(self, *, ticket_row=None, messages=None):
        self.ticket_row = ticket_row or ticket()
        self.message_rows = list(messages or [])
        self.inserted = []
        self.available_calls = 0

    def available(self):
        self.available_calls += 1
        return True

    def _get(self, table, params):
        if table == "support_tickets":
            return [self.ticket_row]
        assert table == "support_messages"
        return sorted(self.message_rows, key=lambda row: (row["created_at"], row["id"]))

    def ticket(self, ticket_id):
        assert ticket_id == self.ticket_row["id"]
        return dict(self.ticket_row)

    def _insert(self, table, row):
        assert table == "support_messages"
        existing = next((message for message in self.message_rows
                         if message["id"] == row["id"]), None)
        if existing:
            return None, True
        stored = dict(row)
        stored["created_at"] = NOW.isoformat()
        self.message_rows.append(stored)
        self.inserted.append(stored)
        return stored, False

    def message(self, message_id):
        return next((dict(message) for message in self.message_rows
                     if message["id"] == message_id), None)

    def claim_message(self, message_id):
        for message in self.message_rows:
            if message["id"] == message_id and message.get("delivery_status") == "ready":
                message["delivery_status"] = "posting"
                return True
        return False

    def mark_message(self, message_id, delivery_status, slack_ts=None, meta_update=None):
        for message in self.message_rows:
            if message["id"] != message_id:
                continue
            message["delivery_status"] = delivery_status
            if slack_ts:
                message["slack_ts"] = slack_ts
            if meta_update:
                message.setdefault("attachments", {}).update(meta_update)
            return dict(message)
        return None


def inbound(created_at=None):
    return {"id": "inbound-1", "ticket_id": TICKET_ID, "created_at":
            (created_at or NOW - timedelta(hours=7)).isoformat(),
            "direction": "inbound", "author_type": "client",
            "delivery_status": None, "attachments": {"surface": "portal_ticket_bridge"}}


def test_default_off_does_not_read_or_write(monkeypatch):
    monkeypatch.delenv("AGENT_HELD_CLIENT_TICKET_RECONCILE_ENABLED", raising=False)
    bus = FakeBus(messages=[inbound()])

    result = R.run(bus=bus, now=NOW)

    assert result == {"ok": True, "queued": [], "reason": "disabled"}
    assert bus.available_calls == 0
    assert bus.inserted == []


def test_stale_eligible_hold_gets_internal_deterministic_row_and_stays_held():
    bus = FakeBus(messages=[inbound()])

    first = R.run(bus=bus, now=NOW, enabled=True)
    second = R.run(bus=bus, now=NOW, enabled=True)

    assert first == {"ok": True, "queued": [TICKET_ID], "duplicates": []}
    assert second == {"ok": True, "queued": [], "duplicates": [TICKET_ID]}
    assert len(bus.inserted) == 1
    row = bus.inserted[0]
    assert row["direction"] == "outbound"
    assert row["author_type"] == "system"
    assert row["delivery_status"] == "ready"
    assert row["attachments"]["kind"] == "escalation"
    assert row["attachments"]["identity"] == "echo"
    assert row["attachments"]["ticket_id"] == TICKET_ID
    assert row["attachments"]["request_version"] == 2
    assert "raw" not in row["body"].lower()
    assert bus.ticket_row["status"] == "hold"
    assert bus.ticket_row["resolved_at"] is None


def test_latest_inbound_resets_wait_window():
    bus = FakeBus(messages=[inbound(NOW - timedelta(minutes=30))])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == []
    assert bus.inserted == []


def test_null_author_inbound_is_conservatively_treated_as_requester_activity():
    null_author = {**inbound(NOW - timedelta(hours=1)), "id": "inbound-null",
                   "author_type": None}
    bus = FakeBus(messages=[inbound(NOW - timedelta(hours=8)), null_author])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == []
    assert bus.inserted == []


def test_posted_client_visible_reply_resets_wait_window():
    recent_reply = {"id": "reply-1", "ticket_id": TICKET_ID,
                    "created_at": (NOW - timedelta(minutes=20)).isoformat(),
                    "direction": "outbound", "author_type": "echo",
                    "delivery_status": "posted", "attachments": {"kind": "status"},
                    "slack_ts": str((NOW - timedelta(minutes=20)).timestamp())}
    bus = FakeBus(messages=[inbound(NOW - timedelta(hours=8)), recent_reply])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == []
    assert bus.inserted == []


def test_delayed_post_uses_delivery_time_not_enqueue_created_at():
    delayed_reply = {"id": "reply-delayed", "ticket_id": TICKET_ID,
                     "created_at": (NOW - timedelta(hours=7)).isoformat(),
                     "direction": "outbound", "author_type": "echo",
                     "delivery_status": "posted", "attachments": {"kind": "status"},
                     "slack_ts": str((NOW - timedelta(minutes=20)).timestamp())}
    bus = FakeBus(messages=[inbound(NOW - timedelta(hours=8)), delayed_reply])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == []
    assert bus.inserted == []


def test_portal_delivery_anchor_uses_persisted_delivery_time():
    portal_reply = {"id": "reply-portal", "ticket_id": TICKET_ID,
                    "created_at": (NOW - timedelta(hours=7)).isoformat(),
                    "direction": "outbound", "author_type": "echo",
                    "delivery_status": "posted",
                    "attachments": {"kind": "status", "delivered_via": "portal_thread",
                                    "delivered_at": (NOW - timedelta(minutes=15)).isoformat()}}
    bus = FakeBus(messages=[inbound(NOW - timedelta(hours=8)), portal_reply])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == []
    assert bus.inserted == []


def test_portal_delivery_anchor_uses_persisted_delivery_time():
    portal_reply = {"id": "reply-portal", "ticket_id": TICKET_ID,
                    "created_at": (NOW - timedelta(hours=7)).isoformat(),
                    "direction": "outbound", "author_type": "echo",
                    "delivery_status": "posted",
                    "attachments": {"kind": "status", "delivered_via": "portal_thread",
                                    "delivered_at": (NOW - timedelta(minutes=15)).isoformat()}}
    bus = FakeBus(messages=[inbound(NOW - timedelta(hours=8)), portal_reply])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == []
    assert bus.inserted == []


def test_posted_visible_reply_without_authoritative_delivery_time_fails_closed():
    ambiguous_reply = {"id": "reply-no-time", "ticket_id": TICKET_ID,
                       "created_at": (NOW - timedelta(minutes=20)).isoformat(),
                       "direction": "outbound", "author_type": "echo",
                       "delivery_status": "posted", "attachments": {"kind": "status"}}
    bus = FakeBus(messages=[inbound(NOW - timedelta(hours=8)), ambiguous_reply])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == []
    assert bus.inserted == []


def test_internal_post_does_not_reset_wait_window():
    internal = {"id": "internal-1", "ticket_id": TICKET_ID,
                "created_at": (NOW - timedelta(minutes=20)).isoformat(),
                "direction": "outbound", "author_type": "system",
                "delivery_status": "posted", "attachments": {"kind": "escalation"}}
    bus = FakeBus(messages=[inbound(), internal])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == [TICKET_ID]


def test_internal_hold_notice_and_staff_inbound_are_not_client_completion():
    staff_note = {"id": "staff-1", "ticket_id": TICKET_ID,
                  "created_at": (NOW - timedelta(minutes=20)).isoformat(),
                  "direction": "inbound", "author_type": "staff",
                  "delivery_status": None, "attachments": {"surface": "thread_reply"}}
    hold_notice = {"id": "hold-1", "ticket_id": TICKET_ID,
                   "created_at": (NOW - timedelta(minutes=10)).isoformat(),
                   "direction": "outbound", "author_type": "system",
                   "delivery_status": "posted", "attachments": {"kind": "hold_notice"}}
    bus = FakeBus(messages=[inbound(), staff_note, hold_notice])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["queued"] == [TICKET_ID]


def test_untrusted_duplicate_id_conflict_is_not_accepted():
    expected = R._notice_identity(ticket(), NOW)
    forged = dict(expected, body="different message")
    forged["created_at"] = (NOW - timedelta(minutes=1)).isoformat()
    bus = FakeBus(messages=[forged, inbound()])

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result == {"ok": True, "queued": [], "duplicates": []}
    assert bus.inserted == []


def test_uncertain_insert_accepts_only_exact_readback():
    class CommitThenTimeout(FakeBus):
        def _insert(self, table, row):
            result = super()._insert(table, row)
            raise TimeoutError("response lost")

    bus = CommitThenTimeout(messages=[inbound()])
    result = R.run(bus=bus, now=NOW, enabled=True)
    assert result == {"ok": True, "queued": [], "duplicates": [TICKET_ID]}
    assert len(bus.inserted) == 1


def test_uncertain_insert_without_exact_readback_fails_closed():
    class TimeoutWithoutCommit(FakeBus):
        def _insert(self, _table, _row):
            raise TimeoutError("response lost")

    bus = TimeoutWithoutCommit(messages=[inbound()])
    result = R.run(bus=bus, now=NOW, enabled=True)
    assert result == {"ok": True, "queued": [], "duplicates": []}
    assert bus.inserted == []


def test_duplicate_readback_allows_only_known_outbox_claim_metadata():
    expected = R._notice_identity(ticket(), NOW)
    stored = dict(expected)
    stored["delivery_status"] = "posted"
    stored["attachments"] = {**expected["attachments"],
                             "claimed_at": (NOW - timedelta(minutes=1)).isoformat(),
                             "reclaimed_stale_posting": True}
    assert R._same_claim(stored, expected)
    stored["attachments"]["untrusted"] = "value"
    assert not R._same_claim(stored, expected)


def test_only_exact_echo_client_bound_sources_qualify():
    rejected = [
        ticket(source="ops_fix"), ticket(source="slack_conversation", client_id=None),
        ticket(bot_identity="scout"), ticket(is_test=True),
        ticket(status="resolved"), ticket(escalated=False),
        ticket(request_version=True),
        ticket(source="slack_conversation", identity_kind="staff",
               slack_channel_id="G1", slack_thread_ts="1.0"),
    ]
    assert not any(R._valid_ticket(row) for row in rejected)
    assert R._valid_ticket(ticket())
    assert R._valid_ticket(ticket(source="slack_conversation", identity_kind="client",
                                  slack_channel_id="G1", slack_thread_ts="1.0"))
    assert R._valid_ticket(ticket(source="slack_conversation", identity_kind="coach",
                                  slack_channel_id="G1", slack_thread_ts="1.0"))


def test_incomplete_ticket_scan_fails_closed(monkeypatch):
    class Broken(FakeBus):
        def _get(self, table, params):
            if table == "support_tickets":
                raise RuntimeError("read failed")
            return super()._get(table, params)

    bus = Broken(messages=[inbound()])
    result = R.run(bus=bus, now=NOW, enabled=True, log=lambda _message: None)
    assert result == {"ok": False, "queued": [], "reason": "ticket_scan_incomplete"}
    assert bus.inserted == []


def _dispatch_fixture(monkeypatch, *, mutate_on_claim=None):
    monkeypatch.setenv("AGENT_HELD_CLIENT_TICKET_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER_INTERNAL")
    slack_ticket = ticket(source="slack_conversation", identity_kind="client",
                          slack_channel_id="G_CLIENT", slack_thread_ts="1.0")
    reminder = R._notice_identity(slack_ticket, NOW)
    reminder["created_at"] = (NOW - timedelta(minutes=1)).isoformat()
    bus = FakeBus(ticket_row=slack_ticket, messages=[inbound(), reminder])
    if mutate_on_claim:
        original_claim = bus.claim_message

        def claim_and_mutate(message_id):
            claimed = original_claim(message_id)
            mutate_on_claim(bus.ticket_row)
            return claimed

        bus.claim_message = claim_and_mutate
    posted = []
    summary = {"posted": 0, "held": 0, "suppressed": 0, "failed": 0,
               "skipped": 0, "resolved": 0, "reclaimed": 0}

    return bus, posted, summary, reminder


def test_outbox_suppresses_ready_reminder_after_ticket_resolves(monkeypatch):
    bus, posted, summary, reminder = _dispatch_fixture(monkeypatch)
    bus.ticket_row["status"] = "resolved"

    O._dispatch_one(bus, lambda *args, **kwargs: posted.append(args), reminder,
                    identity=IDENTITIES["echo"], log=lambda _msg: None,
                    summary=summary, now=NOW)

    assert posted == []
    assert summary["suppressed"] == 1
    assert bus.message(reminder["id"])["delivery_status"] == "suppressed"


def test_outbox_rechecks_ticket_after_claim_before_post(monkeypatch):
    bus, posted, summary, reminder = _dispatch_fixture(
        monkeypatch, mutate_on_claim=lambda current: current.update(request_version=3))

    O._dispatch_one(bus, lambda *args, **kwargs: posted.append(args), reminder,
                    identity=IDENTITIES["echo"], log=lambda _msg: None,
                    summary=summary, now=NOW)

    assert posted == []
    assert summary["suppressed"] == 1
    assert bus.message(reminder["id"])["delivery_status"] == "suppressed"


def test_transient_dispatch_read_failure_requeues_and_healthy_retry_posts(monkeypatch):
    bus, posted, summary, reminder = _dispatch_fixture(monkeypatch)
    original_ticket = bus.ticket
    calls = {"count": 0}

    def fail_post_claim_check_once(ticket_id):
        calls["count"] += 1
        if calls["count"] == 4:
            raise RuntimeError("temporary database read failure")
        return original_ticket(ticket_id)

    bus.ticket = fail_post_claim_check_once
    O._dispatch_one(bus, lambda *args, **kwargs: posted.append(args), reminder,
                    identity=IDENTITIES["echo"], log=lambda _msg: None,
                    summary=summary, now=NOW)

    deferred = bus.message(reminder["id"])
    assert posted == []
    assert summary["suppressed"] == 0
    assert deferred["delivery_status"] == "ready"
    assert deferred["attachments"]["held_reconcile_retry_reason"] == "dispatch_check_failed"
    retry_after = R._parse_ts(deferred["attachments"]["held_reconcile_retry_after"])
    assert retry_after and retry_after > NOW

    # Before the durable retry deadline, skip without suppressing or reading ticket state.
    calls_before = calls["count"]
    O._dispatch_one(bus, lambda *args, **kwargs: posted.append(args), deferred,
                    identity=IDENTITIES["echo"], log=lambda _msg: None,
                    summary=summary, now=NOW + timedelta(seconds=30))
    assert calls["count"] == calls_before
    assert deferred["delivery_status"] == "ready"
    assert summary["suppressed"] == 0

    bus.ticket = original_ticket
    retried = bus.message(reminder["id"])
    O._dispatch_one(bus, lambda *args, **kwargs: posted.append(args), retried,
                    identity=IDENTITIES["echo"], log=lambda _msg: None,
                    summary=summary, now=retry_after + timedelta(seconds=1))
    assert len(posted) == 1
    assert summary["posted"] == 1


def test_initial_outbox_ticket_read_failure_keeps_row_ready(monkeypatch):
    bus, posted, summary, reminder = _dispatch_fixture(monkeypatch)
    original_ticket = bus.ticket
    calls = {"count": 0}

    def fail_once(ticket_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary database read failure")
        return original_ticket(ticket_id)

    bus.ticket = fail_once
    O._dispatch_one(bus, lambda *args, **kwargs: posted.append(args), reminder,
                    identity=IDENTITIES["echo"], log=lambda _msg: None,
                    summary=summary, now=NOW)

    deferred = bus.message(reminder["id"])
    assert posted == []
    assert summary["failed"] == 0
    assert summary["suppressed"] == 0
    assert deferred["delivery_status"] == "ready"
    assert deferred["attachments"]["held_reconcile_retry_reason"] == "dispatch_check_failed"


def test_missing_delivery_timestamp_is_retryable_not_terminal(monkeypatch):
    monkeypatch.setenv("AGENT_HELD_CLIENT_TICKET_RECONCILE_ENABLED", "true")
    slack_ticket = ticket(source="slack_conversation", identity_kind="client",
                          slack_channel_id="G_CLIENT", slack_thread_ts="1.0")
    row = R._notice_identity(slack_ticket, NOW)
    visible_without_delivery = {"id": "unknown-delivery", "ticket_id": TICKET_ID,
                                "created_at": (NOW - timedelta(minutes=5)).isoformat(),
                                "direction": "outbound", "author_type": "echo",
                                "delivery_status": "posted",
                                "attachments": {"kind": "status"}}
    bus = FakeBus(ticket_row=slack_ticket,
                  messages=[inbound(NOW - timedelta(hours=8)), visible_without_delivery])

    eligible, reason, retry_after = R.dispatch_eligibility(
        bus, row, now=NOW)

    assert eligible is None
    assert reason == "requester_activity_unverified"
    assert retry_after and retry_after > NOW


def test_keyset_pagination_probes_past_a_full_page():
    page = [{"id": f"{index:03d}", "created_at":
             (NOW - timedelta(days=1) + timedelta(seconds=index)).isoformat()}
            for index in range(R.PAGE_SIZE)]

    class Paged:
        calls = []

        def _get(self, table, params):
            self.calls.append((table, params))
            return page if len(self.calls) == 1 else []

    bus = Paged()
    result = R._read_all(bus, "support_tickets", {}, max_pages=2)
    assert len(result) == R.PAGE_SIZE
    assert len(bus.calls) == 2
    assert "or" in bus.calls[1][1]


def test_keyset_pagination_ceiling_is_incomplete_not_success():
    page = [{"id": f"{index:03d}", "created_at":
             (NOW - timedelta(days=1) + timedelta(seconds=index)).isoformat()}
            for index in range(R.PAGE_SIZE)]

    class Paged:
        def _get(self, _table, _params):
            return page

    with pytest.raises(BusError, match="pagination ceiling"):
        R._read_all(Paged(), "support_tickets", {}, max_pages=1)


def test_keyset_pagination_rejects_unordered_rows_inside_page():
    later = {"id": "b", "created_at": (NOW - timedelta(days=1)).isoformat()}
    earlier = {"id": "a", "created_at": (NOW - timedelta(days=2)).isoformat()}

    class Unordered:
        def _get(self, _table, _params):
            return [later, earlier]

    with pytest.raises(BusError, match="unordered or duplicate"):
        R._read_all(Unordered(), "support_tickets", {}, max_pages=1)
