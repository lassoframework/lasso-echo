"""Focused caller contract for portal 0384's single-message late route bind."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time

from agent import echo_ticket_worker as worker
from agent.slack_convo import outbox, outreach
from agent.slack_convo.bus import Bus


NOTICE_TOKEN = "0b9c3b7a-4077-4a45-82c7-d351d766beef"
TICKET_ID = "30205455-1555-4150-a69a-247a0b4c91ab"


class NoticeBus:
    def __init__(self):
        self.ts = str(time.time() + 5)
        self.current = {
            "id": TICKET_ID, "product": "echo", "source": "website_tab",
            "request_version": 3, "status": "verification",
            "classification": "answerable_question", "client_id": "gym-1",
            "bot_identity": "echo", "slack_user_id": "U_CLIENT",
            "slack_channel_id": None, "slack_thread_ts": None,
            "escalated": False, "hold_tier": None,
        }
        self.row = None
        self.events = []
        self.receipts = {}
        self.receipt_available = True

    def record_fixer_receipt_once(self, **kw):
        if not self.receipt_available:
            raise TimeoutError("receipt unavailable")
        source = kw["source_message_id"]
        if source not in self.receipts:
            self.receipts[source] = deepcopy(kw)
            self.events.append("receipt")
        return deepcopy(self.receipts[source])

    def fixer_receipt_exists(self, mid, tid, kind):
        row = self.receipts.get(mid) or {}
        return row.get("ticket_id") == tid and row.get("kind") == kind

    def ticket(self, _tid):
        return deepcopy(self.current)

    def begin_current_notice(self, ticket, notice_id, *, unrouted=False):
        assert ticket == self.current and unrouted
        self.events.append("begin")
        return NOTICE_TOKEN

    def record_outbound(self, **kw):
        assert self.events == ["begin"]
        self.events.append("insert")
        self.row = {
            "id": kw["message_id"], "ticket_id": kw["ticket_id"],
            "body": kw["body"], "delivery_status": "ready",
            "delivery_request_version": kw["expected_request_version"],
            "direction": "outbound", "attachments": {
                "kind": kw["kind"], **kw["meta"]},
        }
        assert self.row["attachments"]["fixer_route_pending"] is True
        assert self.row["attachments"]["delivery_expected_slack_thread_ts"] is None
        return deepcopy(self.row)

    def claim_message(self, _mid):
        assert self.row["delivery_status"] == "ready"
        self.row["delivery_status"] = "posting"
        self.events.append("claim")
        return True

    def message(self, _mid):
        return deepcopy(self.row)

    def prepare_fixer_delivery(self, _mid, intent, *, expected_attachments):
        assert self.row["delivery_status"] == "posting"
        assert self.row["attachments"] == expected_attachments
        self.row["attachments"]["fixer_slack_delivery_intent"] = deepcopy(intent)
        self.events.append("intent")
        return deepcopy(self.row)

    def record_fixer_delivery_timestamp(self, _mid, intent, ts):
        assert self.row["attachments"]["fixer_slack_delivery_intent"] == intent
        self.row["slack_ts"] = ts
        self.events.append("timestamp")
        return deepcopy(self.row)

    def transition_fixer_delivery(self, _mid, status, *, slack_ts=None,
                                  meta_update=None, expected_intent=None,
                                  expected_ts=None):
        assert self.row["delivery_status"] == "posting"
        assert self.row["slack_ts"] == expected_ts == slack_ts
        assert self.row["attachments"]["fixer_slack_delivery_intent"] == expected_intent
        if status == "posted":
            assert self.row["attachments"].get("fixer_route_pending") is None
        self.row["attachments"].update(meta_update or {})
        self.row["delivery_status"] = status
        self.events.append(status)
        return deepcopy(self.row)

    def record_held_current_notice_readback(self, _mid, proof, *,
                                            expected_intent, expected_ts):
        assert self.row["delivery_status"] == "held"
        assert self.row["slack_ts"] == expected_ts
        self.row["attachments"].update(proof)
        self.events.append("held_readback")
        return deepcopy(self.row)

    def bind_current_notice_route(self, _tid, _version, _mid, _token, channel, ts):
        assert self.row["attachments"]["delivery_readback_verified"] is True
        assert self.row["slack_ts"] == ts
        assert self.row["delivery_status"] in {"posting", "held"}
        self.current.update(slack_channel_id=channel, slack_thread_ts=ts)
        self.row["delivery_status"] = "posting"
        self.row["attachments"].pop("fixer_route_pending")
        self.row["attachments"].pop("fixer_route_uncertain", None)
        self.row["attachments"].update(
            delivery_expected_slack_channel_id=channel,
            delivery_expected_slack_thread_ts=ts)
        self.events.append("bind")
        return True

    def hold_uncertain_fixer_delivery(self, _mid, reason):
        self.row["delivery_status"] = "held"
        self.row["attachments"]["fixer_route_uncertain"] = True
        self.events.append("held")
        return deepcopy(self.row)

    def resolve_current_notice(self, ticket, _mid, token, expected_status):
        assert token == NOTICE_TOKEN
        assert ticket == self.current and expected_status == "verification"
        assert self.row["delivery_status"] == "posted"
        self.current["status"] = "resolved"
        self.events.append("resolve")
        return deepcopy(self.current)

    def finalize_fixer_delivery(self, _mid, reason):
        assert reason == "resolved_after_verified_slack"
        self.events.append("finalize")


def _send(bus, *, post=None):
    snapshot = bus.ticket(TICKET_ID)
    identity = SimpleNamespace(name="echo", bot_user_id=lambda: "U_ECHO")
    who = SimpleNamespace(slack_user_id="U_CLIENT", kind="client")
    posts = []

    def do_post(channel, body):
        posts.append((channel, body))
        bus.events.append("slack_post")
        return post(channel, body) if post else {"ok": True, "ts": bus.ts}

    def readback(channel, **_kwargs):
        bus.events.append("readback")
        return {"ok": True, "channel": channel, "messages": [{
            "ts": bus.ts, "text": posts[-1][1], "user": "U_ECHO"}]}

    result = outreach._send(
        snapshot, who, identity,
        open_group_dm=lambda _users: {"ok": True, "channel_id": "G_CLIENT"},
        post_first_message=do_post, record_outbound=bus.record_outbound,
        message_text="Your account is connected.", completion=True,
        claim_message=bus.claim_message, ticket_lookup=bus.ticket,
        current_notice_bus=bus, readback=readback,
        member_check=lambda _channel, _user: True)
    return snapshot, result, posts


def test_single_post_binds_after_exact_readback_then_resolves():
    bus = NoticeBus()
    snapshot, result, posts = _send(bus)
    assert len(posts) == 1
    assert result.delivered and result.completion_posted and result.ticket_stamped
    assert bus.events.index("begin") < bus.events.index("insert")
    assert bus.events.index("slack_post") < bus.events.index("readback")
    assert bus.events.index("readback") < bus.events.index("bind")
    assert bus.events.index("bind") < bus.events.index("posted")
    assert worker._resolve_delivered(bus, snapshot, result, log=lambda _msg: None)
    assert bus.current["status"] == "resolved"
    assert bus.events.index("receipt") < bus.events.index("resolve")
    assert len(bus.receipts) == 1


def test_receipt_failure_leaves_exact_notice_pending_then_retries_without_send():
    bus = NoticeBus()
    snapshot, result, posts = _send(bus)
    bus.receipt_available = False
    assert not worker._resolve_delivered(bus, snapshot, result, log=lambda _msg: None)
    assert bus.current["status"] == "verification"
    assert bus.row["delivery_status"] == "posted"
    assert "resolve" not in bus.events and "finalize" not in bus.events
    bus.receipt_available = True
    # A lost resolver response may retry the receipt step. Its source-message
    # uniqueness boundary must retain one receipt for the one Slack post.
    bus.record_fixer_receipt_once(
        source_message_id=result.notice_id, ticket_id=TICKET_ID,
        kind="escalation", body="durable", meta={})
    assert worker._resolve_delivered(bus, snapshot, result, log=lambda _msg: None)
    assert len(posts) == len(bus.receipts) == 1
    assert bus.events.count("receipt") == 1
    assert bus.events.index("receipt") < bus.events.index("resolve")


def test_uncertain_first_post_is_held_and_never_bound():
    bus = NoticeBus()
    _snapshot, result, posts = _send(bus, post=lambda _c, _b: {"ok": False})
    assert len(posts) == 1
    assert not result.delivered and not result.completion_posted
    assert bus.row["delivery_status"] == "held"
    assert bus.row["attachments"]["fixer_route_uncertain"] is True
    assert "bind" not in bus.events and "resolve" not in bus.events


def test_slow_root_post_after_hold_recovers_same_notice_without_repost():
    class SlowBus(NoticeBus):
        def __init__(self):
            super().__init__()
            self.timestamp_attempts = 0

        def record_fixer_delivery_timestamp(self, mid, intent, ts):
            self.timestamp_attempts += 1
            if self.timestamp_attempts == 1:
                return None  # a sweep parked posting while Slack answered
            return super().record_fixer_delivery_timestamp(mid, intent, ts)

        def transition_fixer_delivery(self, mid, status, **kw):
            if self.row["delivery_status"] == "held":
                return None
            return super().transition_fixer_delivery(mid, status, **kw)

    bus = SlowBus()
    _snapshot, result, posts = _send(bus)
    assert len(posts) == 1
    assert bus.timestamp_attempts == 2
    assert result.completion_posted
    assert bus.events.index("held") < bus.events.index("timestamp")
    assert bus.events.index("held_readback") < bus.events.index("bind")
    assert bus.row["delivery_status"] == "posted"


def test_root_claim_failure_cancels_exact_ready_notice_and_alerts():
    class ClaimFailureBus(NoticeBus):
        def record_outbound(self, **kw):
            if kw["kind"] == "escalation":
                self.events.append("staff_alert")
                return {"id": "staff-alert"}
            return super().record_outbound(**kw)

        def claim_message(self, _mid):
            self.events.append("claim_failed")
            return False

        def suppress_unclaimed_current_notice(self, mid, reason):
            assert mid == self.row["id"]
            assert self.row["delivery_status"] == "ready"
            assert "claim" in reason
            self.row["delivery_status"] = "suppressed"
            self.row["attachments"]["suppressed_why"] = reason
            self.events.append("suppressed")
            return deepcopy(self.row)

    bus = ClaimFailureBus()
    _snapshot, result, posts = _send(bus)
    assert not posts
    assert result.reason == "lost_claim"
    assert bus.row["delivery_status"] == "suppressed"
    assert bus.events == ["begin", "insert", "claim_failed", "suppressed", "staff_alert"]


def test_normal_outbox_skips_reserved_first_contact():
    bus = NoticeBus()
    _snapshot, _result, posts = _send(bus, post=lambda _c, _b: {"ok": False})
    bus.row["delivery_status"] = "ready"  # simulate the insert/claim gap
    summary = {"skipped": 0}
    outbox._dispatch_one(bus, lambda *_a, **_k: posts.append("duplicate"),
                         deepcopy(bus.row), identity=SimpleNamespace(name="echo"),
                         log=lambda _msg: None, summary=summary)
    assert summary["skipped"] == 1
    assert len(posts) == 1


def test_human_tap_cannot_close_unreserved_website_ticket():
    bus = NoticeBus()
    assert outbox.resolve_and_notify(
        bus, TICKET_ID, approved_by="U_BLAKE",
        identity=SimpleNamespace(name="echo"), log=lambda _msg: None) is False
    assert bus.row is None
    assert bus.current["status"] == "verification"


def test_held_exact_readback_binds_without_reposting():
    bus = NoticeBus()
    _snapshot, _result, posts = _send(bus)
    bus.row["delivery_status"] = "held"
    bus.row["attachments"]["fixer_route_pending"] = True
    bus.row["attachments"]["fixer_route_uncertain"] = True
    bus.current["slack_channel_id"] = None
    bus.current["slack_thread_ts"] = None
    proof = {"delivery_readback_verified": True, "delivery_readback_ts": bus.ts}
    summary = {"resolved": 0}
    assert outbox._finish_pending_route_notice(
        bus, deepcopy(bus.row), proof, SimpleNamespace(name="echo"),
        lambda _msg: None, summary)
    assert len(posts) == 1
    assert bus.current["status"] == "resolved"
    assert bus.events[-2:] == ["resolve", "finalize"]
    assert bus.events.index("receipt") < bus.events.index("resolve")
    assert len(bus.receipts) == 1


def test_held_route_receipt_outage_keeps_posted_notice_for_finalization():
    bus = NoticeBus()
    _snapshot, _result, posts = _send(bus)
    bus.row["delivery_status"] = "held"
    bus.row["attachments"]["fixer_route_pending"] = True
    bus.row["attachments"]["fixer_route_uncertain"] = True
    bus.current["slack_channel_id"] = None
    bus.current["slack_thread_ts"] = None
    bus.receipt_available = False
    proof = {"delivery_readback_verified": True, "delivery_readback_ts": bus.ts}
    summary = {"resolved": 0}
    assert outbox._finish_pending_route_notice(
        bus, deepcopy(bus.row), proof, SimpleNamespace(name="echo"),
        lambda _msg: None, summary)
    assert bus.row["delivery_status"] == "posted"
    assert bus.current["status"] == "verification"
    assert "resolve" not in bus.events and "finalize" not in bus.events
    bus.receipt_available = True
    outbox._finalize_fixer_post(
        bus, bus.ticket(TICKET_ID), deepcopy(bus.row), SimpleNamespace(name="echo"),
        lambda _msg: None, summary)
    assert bus.current["status"] == "resolved"
    assert len(posts) == len(bus.receipts) == 1
    assert bus.events.index("receipt") < bus.events.index("resolve")


def test_known_route_reserves_id_and_token_before_insert(monkeypatch):
    ticket = NoticeBus().current
    ticket.update(slack_channel_id="G_CLIENT", slack_thread_ts="45.67")
    bus = Bus(url="https://example.test", service_key="test")
    events = []
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))

    def begin(_ticket, notice_id, *, unrouted=False):
        assert not unrouted and notice_id
        events.append("begin")
        return NOTICE_TOKEN

    def insert(_table, row):
        events.append("insert")
        return deepcopy(row), False

    monkeypatch.setattr(bus, "begin_current_notice", begin)
    monkeypatch.setattr(bus, "_insert", insert)
    row = bus.record_outbound(
        ticket_id=TICKET_ID, author_type="echo", body="The issue is fixed.",
        delivery_status="ready", kind="status",
        meta={"fixer": True, "resolve_notice": True, "identity": "echo",
              "request_key": "key"})
    att = row["attachments"]
    assert events == ["begin", "insert"]
    assert att["fixer_current_attempt_token"] == NOTICE_TOKEN
    assert att["delivery_expected_slack_channel_id"] == "G_CLIENT"
    assert att["delivery_expected_slack_thread_ts"] == "45.67"
    assert row["delivery_request_version"] == ticket["request_version"]
    assert row["body"].startswith(f"<@{outbox.config.APPROVER_SLACK_ID}> ")


def test_lost_insert_response_reads_only_designated_id(monkeypatch):
    bus = Bus(url="https://example.test", service_key="test")
    inserted = {}

    def insert(_table, row):
        inserted.update(deepcopy(row))
        raise TimeoutError("response lost after insert")

    monkeypatch.setattr(bus, "_insert", insert)
    monkeypatch.setattr(bus, "message", lambda mid: deepcopy(inserted)
                        if mid == inserted.get("id") else None)
    row = bus.record_outbound(
        ticket_id=TICKET_ID, author_type="echo", body="Exact body",
        delivery_status="ready", kind="status",
        message_id="70164909-16b5-43df-b6a3-8d7499d51485",
        expected_request_version=3, meta={"identity": "echo"})
    assert row == inserted


def test_pending_route_held_timestamp_accepts_only_exact_reserved_intent(monkeypatch):
    bus = Bus(url="https://example.test", service_key="test")
    intent = {"channel": "G_CLIENT", "body": "Exact body", "sender": "U_ECHO"}
    row = {
        "id": "70164909-16b5-43df-b6a3-8d7499d51485",
        "delivery_status": "held", "slack_ts": None,
        "attachments": {
            "fixer_current_attempt_token": NOTICE_TOKEN,
            "fixer_route_pending": True, "fixer_route_uncertain": True,
            "fixer_slack_delivery_intent": intent,
        },
    }
    monkeypatch.setattr(bus, "message", lambda _mid: deepcopy(row))

    def patch(_table, match, fields):
        assert match["delivery_status"] == "eq.held"
        assert match["slack_ts"] == "is.null"
        assert fields == {"slack_ts": "1700000000.000001"}
        row.update(fields)
        return deepcopy(row)

    monkeypatch.setattr(bus, "_patch", patch)
    stamped = bus.record_fixer_delivery_timestamp(
        row["id"], intent, "1700000000.000001")
    assert stamped["slack_ts"] == "1700000000.000001"
    assert row["attachments"].get("fixer_slack_delivery_uncertain") is None
    row["slack_ts"] = None
    row["attachments"].pop("fixer_route_uncertain")
    assert bus.record_fixer_delivery_timestamp(
        row["id"], intent, "1700000000.000001") is None


def test_known_route_held_stages_proof_before_posted(monkeypatch):
    now = datetime.now(timezone.utc)
    ts = str(now.timestamp() + 5)
    intent = {"channel": "G_CLIENT", "thread_ts": "1.0", "body": "Exact body",
              "sender": "U_ECHO", "not_before": now.isoformat(),
              "request_key": None, "request_version": 3}
    row = {"id": "70164909-16b5-43df-b6a3-8d7499d51485",
           "ticket_id": TICKET_ID, "delivery_status": "held", "slack_ts": ts,
           "delivery_request_version": 3, "created_at": now.isoformat(),
           "attachments": {"fixer_current_attempt_token": NOTICE_TOKEN,
                           "fixer_slack_delivery_uncertain": True,
                           "fixer_slack_delivery_intent": intent,
                           "delivery_expected_status": "verification"}}

    class HeldBus:
        def __init__(self):
            self.events = []
            self.status = "verification"

        def pending_held_fixer_delivery(self, _identity, **_kw):
            return [deepcopy(row)]

        def ticket(self, _tid):
            return {"request_version": 3, "status": self.status}

        def defer_held_fixer_reconcile(self, _mid, _next_at):
            self.events.append("defer")
            return deepcopy(row)

        def record_held_fixer_readback(self, _mid, proof, **_kw):
            self.events.append("proof")
            row["attachments"].update(proof)
            return deepcopy(row)

        def reconcile_held_fixer_delivery(self, _mid, _proof, **_kw):
            assert row["attachments"]["delivery_readback_verified"] is True
            self.events.append("posted")
            row["delivery_status"] = "posted"
            return deepcopy(row)

    bus = HeldBus()
    readback = lambda channel, **_kw: {
        "ok": True, "channel": channel, "messages": [{
            "ts": ts, "text": "Exact body", "user": "U_ECHO", "thread_ts": "1.0"}]}
    monkeypatch.setattr(outbox, "_finalize_fixer_post", lambda *_a, **_kw: None)
    outbox._reconcile_held_fixer(
        bus, SimpleNamespace(name="echo"), readback,
        lambda _msg: None, {"reconciled_posted": 0})
    assert bus.events == ["defer", "proof", "posted"]

    row["delivery_status"] = "held"
    row["attachments"].pop("delivery_readback_verified")
    bus.events.clear()
    bus.status = "resolved"
    outbox._reconcile_held_fixer(
        bus, SimpleNamespace(name="echo"), readback,
        lambda _msg: None, {"reconciled_posted": 0})
    assert bus.events == ["proof"]
    assert row["delivery_status"] == "held"


def test_expired_token_claim_before_intent_is_suppressed_not_requeued():
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = {"id": "70164909-16b5-43df-b6a3-8d7499d51485",
           "ticket_id": TICKET_ID, "created_at": old.isoformat(),
           "delivery_status": "posting", "attachments": {
               "fixer": True, "identity": "echo",
               "fixer_current_attempt_token": NOTICE_TOKEN,
               "fixer_route_pending": True,
               "fixer_slack_delivery_protocol": outbox.FIXER_DELIVERY_PROTOCOL}}

    class StaleBus:
        def __init__(self):
            self.events = []

        def outbox(self, status, **_kw):
            assert status == "posting"
            return [deepcopy(row)]

        def suppress_unattempted_current_notice(self, _mid, reason):
            assert "before durable Slack intent" in reason
            self.events.append("suppressed")
            return {**row, "delivery_status": "suppressed"}

        def requeue_unattempted_fixer_delivery(self, *_args):
            raise AssertionError("tokenized notice must never requeue")

        def record_outbound(self, **kw):
            assert kw["kind"] == "escalation"
            self.events.append("alert")

    bus = StaleBus()
    summary = {}
    assert outbox._recover_stale_claims(
        bus, SimpleNamespace(name="echo"), lambda _msg: None,
        now=datetime.now(timezone.utc), summary=summary) == 1
    assert bus.events == ["suppressed", "alert"]
    assert summary["suppressed"] == 1
