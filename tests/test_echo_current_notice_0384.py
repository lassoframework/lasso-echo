"""Focused caller contract for portal 0384's single-message late route bind."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from types import SimpleNamespace
import json
import time

import pytest
import requests

from agent import echo_ticket_worker as worker
from agent import support_sender_fence as sender_fence
from agent.slack_convo import outbox, outreach
from agent.slack_convo.bus import Bus, BusError, _suppressed_current_notice_alert_id


NOTICE_TOKEN = "0b9c3b7a-4077-4a45-82c7-d351d766beef"
TICKET_ID = "30205455-1555-4150-a69a-247a0b4c91ab"


@pytest.fixture(autouse=True)
def arm_current_notice_for_contract_tests(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", "true")
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "dep-test")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "build-test")


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
        self.admission = {"generation": 7, "unresolved": 0, "paused": False,
                          "acquire_denied": False, "finish_fails": False,
                          "finishes": [], "calls": []}

    # -- portal 0624 support-resolution-send admission transport ----------------
    def _client(self):
        bus = self

        class AdmissionClient:
            def post(self, url, data=None, headers=None, timeout=None):
                body = json.loads(data or "{}")
                name = url.rsplit("rpc/", 1)[-1]
                bus.admission["calls"].append(name)

                class Resp:
                    status_code = 200

                    def json(self):
                        if name == "support_admission_status_lane":
                            return {"lane": "support-resolution-send",
                                    "generation": bus.admission["generation"],
                                    "unresolved": bus.admission["unresolved"],
                                    "paused": bus.admission["paused"],
                                    "drained": bus.admission["paused"]
                                    and bus.admission["unresolved"] == 0,
                                    "operation_id": "op-test"}
                        if name == "support_admission_acquire_lane":
                            if bus.admission["acquire_denied"]:
                                return {"admitted": False,
                                        "lane": "support-resolution-send",
                                        "invocation_id": body["p_invocation_id"],
                                        "generation": body["p_expected_generation"]}
                            return {"admitted": True,
                                    "lane": "support-resolution-send",
                                    "invocation_id": body["p_invocation_id"],
                                    "generation": body["p_expected_generation"]}
                        if name == "support_admission_finish_lane":
                            bus.admission["finishes"].append(body["p_outcome"])
                            if bus.admission["finish_fails"]:
                                return {"recorded": False,
                                        "lane": "support-resolution-send",
                                        "invocation_id": body["p_invocation_id"]}
                            return {"recorded": True,
                                    "lane": "support-resolution-send",
                                    "invocation_id": body["p_invocation_id"]}
                        raise AssertionError("unexpected rpc " + name)

                return Resp()

        return AdmissionClient()

    def _rest(self, table):
        return "http://test.local/rest/v1/" + table

    def _headers(self, extra=None):
        return dict(extra or {})

    def _patch(self, table, match, fields):
        # Exact support_messages CAS: refuse unless every match term holds.
        assert table == "support_messages"
        row = self.row
        for key, spec in match.items():
            current = row.get(key)
            if spec == "is.null":
                if current is not None:
                    return None
                continue
            assert spec.startswith("eq.")
            want = spec[3:]
            if key == "attachments":
                encoded = (None if current is None else json.dumps(
                    current, sort_keys=True, separators=(",", ":")))
                if encoded != want:
                    return None
            elif key == "delivery_request_version":
                if str(current) != want:
                    return None
            elif current != want:
                return None
        if isinstance(fields.get("attachments"), dict):
            row["attachments"] = fields["attachments"]
        if "slack_ts" in fields:
            row["slack_ts"] = fields["slack_ts"]
        if "delivery_status" in fields:
            row["delivery_status"] = fields["delivery_status"]
        return deepcopy(row)

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

    def ensure_suppressed_current_notice_alert(self, mid, identity):
        return self.record_outbound(
            ticket_id=self.current["id"], author_type="system", body="Review the ticket.",
            delivery_status="ready", kind="escalation",
            meta={"identity": identity, "suppressed_message_id": mid})

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


def _send(bus, *, post=None, member_check=None):
    snapshot = bus.ticket(TICKET_ID)
    identity = SimpleNamespace(name="echo", bot_user_id=lambda: "U_ECHO")
    who = SimpleNamespace(slack_user_id="U_CLIENT", kind="client")
    posts = []

    def do_post(channel, body):
        posts.append((channel, body))
        bus.events.append("slack_post")
        return post(channel, body) if post else {"ok": True, "ts": bus.ts}

    do_post.verify_sender = lambda: {"ok": True, "user_id": "U_ECHO"}

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
        member_check=member_check or (lambda _channel, _user: True))
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



def test_admitted_send_records_lease_before_post_and_completion_after():
    """The successful path: durable 0624 admission wraps the exact one POST."""
    bus = NoticeBus()
    _snapshot, result, posts = _send(bus)
    assert result.delivered and len(posts) == 1
    calls = bus.admission["calls"]
    assert calls[0] == "support_admission_status_lane"
    assert calls.index("support_admission_acquire_lane") < bus.events.index("slack_post")
    assert calls.index("support_admission_acquire_lane") < calls.index(
        "support_admission_finish_lane")
    assert bus.admission["finishes"] == ["completed"]
    att = bus.row["attachments"]
    lease = att["support_resolution_send_admission"]
    assert lease["lane"] == "support-resolution-send"
    assert lease["deployment"] == "dep-test" and lease["build"] == "build-test"
    assert lease["binding"]["sender"] == "U_ECHO"
    assert lease["binding"]["ticket"]["id"] == TICKET_ID
    assert lease["binding"]["ticket"]["slack_channel_id"] is None
    assert lease["binding"]["channel"] == "G_CLIENT"
    assert att["support_resolution_send_completion"]["recorded"] is True
    assert att["support_resolution_send_completion"]["generation"] == 7
    assert att["support_resolution_send_completion"]["outcome"] == "completed"
    assert sender_fence._trusted_admission_completion(bus.row)


def test_paused_admission_lane_zero_posts_and_no_reservation():
    """A paused 0624 lane refuses before reservation: no row, no notice, no POST."""
    bus = NoticeBus()
    bus.admission["paused"] = True
    _snapshot, result, posts = _send(bus)
    assert posts == [] and bus.row is None
    assert not result.opened or result.reason == "current_notice_admission_unavailable"
    assert result.reason == "current_notice_admission_unavailable"
    assert result.notice_id == "" and result.attempt_token == ""
    assert bus.events == [] and bus.admission["calls"] == ["support_admission_status_lane"]


def test_wrong_sender_zero_posts_and_no_reservation():
    """auth.test proof that is not this identity's bot user refuses pre-reservation."""
    bus = NoticeBus()
    identity = SimpleNamespace(name="echo", bot_user_id=lambda: "U_ECHO")
    who = SimpleNamespace(slack_user_id="U_CLIENT", kind="client")
    posts = []

    def do_post(channel, body):
        posts.append((channel, body))
        return {"ok": True, "ts": bus.ts}

    do_post.verify_sender = lambda: {"ok": True, "user_id": "U_INTRUDER"}
    result = outreach._send(
        bus.ticket(TICKET_ID), who, identity,
        open_group_dm=lambda _users: {"ok": True, "channel_id": "G_CLIENT"},
        post_first_message=do_post, record_outbound=bus.record_outbound,
        message_text="Your account is connected.", completion=True,
        claim_message=bus.claim_message, ticket_lookup=bus.ticket,
        current_notice_bus=bus,
        readback=lambda channel, **kw: {"ok": True, "messages": []},
        member_check=lambda _channel, _user: True)
    assert posts == [] and bus.row is None
    assert result.reason == "current_notice_sender_mismatch"
    assert bus.events == [] and bus.admission["calls"] == []


def test_missing_sender_verifier_refuses_before_reservation():
    bus = NoticeBus()
    identity = SimpleNamespace(name="echo", bot_user_id=lambda: "U_ECHO")
    who = SimpleNamespace(slack_user_id="U_CLIENT", kind="client")
    posts = []
    result = outreach._send(
        bus.ticket(TICKET_ID), who, identity,
        open_group_dm=lambda _users: {"ok": True, "channel_id": "G_CLIENT"},
        post_first_message=lambda channel, body: posts.append((channel, body))
        or {"ok": True, "ts": bus.ts},
        record_outbound=bus.record_outbound,
        message_text="Your account is connected.", completion=True,
        claim_message=bus.claim_message, ticket_lookup=bus.ticket,
        current_notice_bus=bus,
        readback=lambda channel, **kw: {"ok": True, "messages": []},
        member_check=lambda _channel, _user: True)
    assert posts == [] and bus.row is None
    assert result.reason == "current_notice_preflight_unavailable"
    assert bus.events == [] and bus.admission["calls"] == []


def test_missing_deployment_env_refuses_before_reservation(monkeypatch):
    monkeypatch.delenv("RAILWAY_DEPLOYMENT_ID")
    bus = NoticeBus()
    _snapshot, result, posts = _send(bus)
    assert posts == [] and bus.row is None
    assert result.reason == "current_notice_admission_unavailable"
    assert bus.events == [] and bus.admission["calls"] == []


def test_denied_acquisition_holds_uncertain_finishes_unknown_and_never_posts():
    """A denied 0624 acquisition: zero POSTs, lease finished unknown, row held."""
    bus = NoticeBus()
    bus.admission["acquire_denied"] = True
    _snapshot, result, posts = _send(bus)
    assert posts == []
    assert not result.delivered and result.reason == "current_notice_uncertain"
    assert bus.row["delivery_status"] == "held"
    assert bus.row["attachments"]["fixer_route_uncertain"] is True
    assert bus.admission["finishes"] == ["unknown"]
    assert "bind" not in bus.events and "resolve" not in bus.events


def test_failed_post_finishes_lease_unknown_and_never_replays():
    bus = NoticeBus()
    _snapshot, result, posts = _send(bus, post=lambda _c, _b: {"ok": False})
    assert len(posts) == 1 and not result.delivered
    assert bus.row["delivery_status"] == "held"
    assert bus.admission["finishes"] == ["unknown"]
    att = bus.row["attachments"]
    assert "support_resolution_send_completion" not in att
    assert "bind" not in bus.events


def test_unconfirmed_completion_ack_holds_without_replay():
    """The send + readback succeeded but the completion ACK is unconfirmed: the
    row is held for reconciliation, the lease is finished unknown, never reposted."""
    bus = NoticeBus()
    bus.admission["finish_fails"] = True
    _snapshot, result, posts = _send(bus)
    assert len(posts) == 1 and not result.delivered
    assert result.reason == "current_notice_uncertain"
    assert bus.row["delivery_status"] == "held"
    # The completed ACK was attempted and refused; the lease then closes unknown.
    assert bus.admission["finishes"] == ["completed", "unknown"]
    assert "bind" not in bus.events and "posted" not in bus.events


def test_sender_change_after_acquisition_never_posts():
    """The exact captured poster is re-authenticated after admission; a changed
    sender finishes the lease unknown with zero POSTs and the row held."""
    bus = NoticeBus()
    proofs = iter([{"ok": True, "user_id": "U_ECHO"},
                   {"ok": True, "user_id": "U_SWAPPED"}])
    identity = SimpleNamespace(name="echo", bot_user_id=lambda: "U_ECHO")
    who = SimpleNamespace(slack_user_id="U_CLIENT", kind="client")
    posts = []

    def do_post(channel, body):
        posts.append((channel, body))
        return {"ok": True, "ts": bus.ts}

    do_post.verify_sender = lambda: next(proofs)
    result = outreach._send(
        bus.ticket(TICKET_ID), who, identity,
        open_group_dm=lambda _users: {"ok": True, "channel_id": "G_CLIENT"},
        post_first_message=do_post, record_outbound=bus.record_outbound,
        message_text="Your account is connected.", completion=True,
        claim_message=bus.claim_message, ticket_lookup=bus.ticket,
        current_notice_bus=bus,
        readback=lambda channel, **kw: {"ok": True, "messages": []},
        member_check=lambda _channel, _user: True)
    assert posts == [] and not result.delivered
    assert result.reason == "current_notice_uncertain"
    assert bus.row["delivery_status"] == "held"
    assert bus.admission["finishes"] == ["unknown"]


@pytest.mark.parametrize("mode", ["revoked", "unreadable"])
def test_membership_change_after_acquisition_never_posts(mode):
    """P1 reproduction: after the 0624 acquire and the second auth.test, Blake's
    exact channel membership is rechecked immediately before the one POST. A
    revoked or unreadable membership finishes the lease 'unknown', holds the row
    and never posts, binds or replays."""
    bus = NoticeBus()

    def member_check(_channel, _user):
        if "support_admission_acquire_lane" in bus.admission["calls"]:
            if mode == "unreadable":
                raise RuntimeError("conversations.members unavailable")
            return False
        return True

    _snapshot, result, posts = _send(bus, member_check=member_check)
    assert posts == []
    assert result.reason == "current_notice_uncertain"
    assert bus.admission["finishes"] == ["unknown"]
    assert "slack_post" not in bus.events
    assert "bind" not in bus.events and "posted" not in bus.events
    assert bus.row["delivery_status"] == "held"


def test_request_change_during_membership_recheck_never_posts():
    """The post-acquire membership network call cannot stale the final request."""
    bus = NoticeBus()

    def member_check(_channel, _user):
        if "support_admission_acquire_lane" in bus.admission["calls"]:
            bus.current["request_version"] = 4
        return True

    _snapshot, result, posts = _send(bus, member_check=member_check)
    assert posts == []
    assert result.reason == "current_notice_uncertain"
    assert bus.current["request_version"] == 4
    assert bus.admission["finishes"] == ["unknown"]
    assert bus.row["delivery_status"] == "held"
    assert "bind" not in bus.events and "posted" not in bus.events


def _pending_route_recovery(receipt):
    now = datetime.now(timezone.utc)
    ts = str(now.timestamp() + 5)
    intent = {"channel": "G_CLIENT", "thread_ts": None, "body": "Exact body",
              "sender": "U_ECHO", "claimed_at": now.isoformat(),
              "not_before": now.isoformat(), "request_key": None,
              "request_version": 3}
    row = {"id": "70164909-16b5-43df-b6a3-8d7499d51485", "ticket_id": TICKET_ID,
           "delivery_status": "held", "slack_ts": ts,
           "delivery_request_version": 3, "body": "Exact body",
           "attachments": {"kind": "status",
                           "fixer_current_attempt_token": NOTICE_TOKEN,
                           "fixer_route_pending": True, "fixer_route_uncertain": True,
                           "fixer_slack_delivery_intent": intent,
                           "delivery_expected_status": "verification"}}
    if receipt is not None:  # admitted rows carry the durable 0624 lease
        row["attachments"][outbox.SUPPORT_SEND_ADMISSION_KEY] = {
            "lane": outbox.SUPPORT_SEND_LANE, "invocation_id": "inv-1",
            "generation": 7, "deployment": "dep-test", "build": "build-test",
            "binding": {"ticket": {"id": TICKET_ID, "request_version": 3},
                        "message_id": row["id"], "sender": "U_ECHO",
                        "channel": "G_CLIENT", "thread_ts": None}}
    if receipt in ("completed", "bad"):
        row["attachments"]["support_resolution_send_completion"] = {
            "recorded": receipt == "completed", "lane": outbox.SUPPORT_SEND_LANE,
            "invocation_id": "inv-1", "generation": 7}

    class RecoveryBus:
        def __init__(self):
            self.events = []
            self.current = {"id": TICKET_ID, "request_version": 3,
                            "status": "verification"}

        def record_held_current_notice_readback(self, mid, proof, *,
                                                expected_intent, expected_ts):
            assert row["delivery_status"] == "held" and row["slack_ts"] == expected_ts
            row["attachments"].update(proof)
            self.events.append("readback")
            return deepcopy(row)

        def transition_fixer_delivery(self, mid, status, *, slack_ts=None,
                                      meta_update=None, expected_intent=None,
                                      expected_ts=None):
            assert row["delivery_status"] == "posting"
            assert row["slack_ts"] == expected_ts == slack_ts
            row["attachments"].update(meta_update or {})
            row["delivery_status"] = status
            self.events.append(status)
            return deepcopy(row)

        def ticket(self, _tid):
            return deepcopy(self.current)

        def bind_current_notice_route(self, _tid, _version, _mid, _token,
                                      _channel, _ts):
            assert row["attachments"]["delivery_readback_verified"] is True
            row["attachments"].pop("fixer_route_pending")
            row["attachments"].pop("fixer_route_uncertain", None)
            row["delivery_status"] = "posting"
            self.events.append("bind")
            return True

        def resolve_current_notice(self, _ticket, _mid, _token, _expected):
            assert row["delivery_status"] == "posted"
            self.current["status"] = "resolved"
            self.events.append("resolve")
            return deepcopy(self.current)

        def finalize_fixer_delivery(self, _mid, reason):
            assert reason == "resolved_after_verified_slack"
            self.events.append("finalize")

    bus = RecoveryBus()
    proof = {"delivery_readback_verified": True, "delivery_readback_ts": ts}
    summary = {"resolved": 0}
    finished = outbox._finish_pending_route_notice(
        bus, deepcopy(row), proof, SimpleNamespace(name="echo"),
        lambda _msg: None, summary)
    return finished, bus, row, summary


def test_pending_route_recovery_requires_confirmed_completion_receipt():
    """An admitted held notice closes only with the confirmed durable receipt
    persisted before binding; exact readback alone never binds on uncertainty."""
    finished, bus, row, summary = _pending_route_recovery("completed")
    assert finished and summary["resolved"] == 1
    assert bus.events == ["readback", "bind", "posted", "resolve", "finalize"]
    assert row["delivery_status"] == "posted"


@pytest.mark.parametrize("receipt", ["missing", "bad"])
def test_pending_route_recovery_no_bind_on_unknown_or_bad_receipt(receipt):
    """P1 reproduction: absent or unrecorded 0624 completion receipts stay held;
    recovery never binds, promotes, resolves or resends on uncertainty."""
    finished, bus, row, summary = _pending_route_recovery(receipt)
    assert not finished and summary["resolved"] == 0
    assert bus.events == ["readback"]
    assert row["delivery_status"] == "held"
    assert row["attachments"]["fixer_route_pending"] is True


def test_pending_route_recovery_legacy_proof_only_row_unchanged():
    """Rows predating the durable admission contract keep proof-only recovery."""
    finished, bus, row, summary = _pending_route_recovery(None)
    assert finished and summary["resolved"] == 1
    assert bus.events == ["readback", "bind", "posted", "resolve", "finalize"]
    assert row["delivery_status"] == "posted"


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


@pytest.mark.parametrize("enabled", [False, True])
def test_human_fix_resolve_refuses_disabled_or_rejected_current_notice(monkeypatch, enabled):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", str(enabled))
    ticket = NoticeBus().current
    ticket.update(status="merged", classification="code_fix", slack_channel_id="G123",
                  fix_pr_url="https://github.com/example/echo/pull/317",
                  verification_after={"fixer": {"request_key": "current-request"}})
    before = deepcopy(ticket)
    bus = Bus(url="https://example.test", service_key="test")
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(bus, "recent_messages", lambda *_a, **_kw: [])
    monkeypatch.setattr(bus, "begin_current_notice", lambda *_a, **_kw: None)
    monkeypatch.setattr(bus, "_insert", lambda *_a: pytest.fail("refusal wrote a notice"))
    monkeypatch.setattr(bus, "set_ticket", lambda *_a, **_kw: pytest.fail("refusal changed ticket"))
    monkeypatch.setattr(outbox, "_recipient_armed", lambda *_a: True)
    monkeypatch.setattr(outbox, "_verified_fix_notice", lambda *_a, **_kw: True)
    monkeypatch.setattr(outbox, "_current_fixer_request_key", lambda *_a: "current-request")
    logs = []

    assert outbox.resolve_and_notify(
        bus, TICKET_ID, approved_by="U_BLAKE", identity=SimpleNamespace(name="echo"),
        log=logs.append) is False
    assert ticket == before
    assert any("resolve refused" in entry for entry in logs)


@pytest.mark.parametrize("failed_scan", ["route", "legacy", None])
def test_uncertain_outreach_scans_fail_independently(monkeypatch, failed_scan):
    legacy = {"id": "legacy", "ticket_id": TICKET_ID,
              "attachments": {"outreach_delivery_uncertain": True}}
    pending = {"id": "pending", "ticket_id": TICKET_ID,
               "attachments": {"fixer_route_uncertain": True}}
    bus = Bus(url="https://example.test", service_key="test")
    alerts, marked, logs = [], [], []

    def legacy_scan(*_a, **_kw):
        if failed_scan == "legacy":
            raise BusError(503, "legacy scan unavailable")
        return [deepcopy(legacy)]

    def route_scan(identity, marker, **_kw):
        assert identity == "echo" and marker == "fixer_route_uncertain"
        if failed_scan == "route":
            raise BusError(400, "invalid FIXER hold marker")
        return [deepcopy(pending)]

    monkeypatch.setattr(bus, "outbox", legacy_scan)
    monkeypatch.setattr(bus, "pending_fixer_holds", route_scan)
    monkeypatch.setattr(bus, "ticket", lambda _tid: {"bot_identity": "echo"})
    monkeypatch.setattr(bus, "uncertain_outreach_alert_exists", lambda *_a: False)
    monkeypatch.setattr(bus, "record_outbound", lambda **kw: alerts.append(kw))
    monkeypatch.setattr(bus, "mark_uncertain_outreach_alerted", marked.append)
    outbox._report_uncertain_outreach(bus, SimpleNamespace(name="echo"), logs.append)

    expected = {"route": {"legacy"}, "legacy": {"pending"},
                None: {"legacy", "pending"}}[failed_scan]
    assert {row["meta"]["outreach_uncertain_row_id"] for row in alerts} == expected
    assert marked == (["legacy"] if failed_scan != "legacy" else [])
    assert legacy["attachments"] == {"outreach_delivery_uncertain": True}
    assert pending["attachments"] == {"fixer_route_uncertain": True}
    assert all(row["kind"] == "escalation" and row["meta"]["identity"] == "echo"
               for row in alerts)
    if failed_scan:
        assert any("scan failed" in entry for entry in logs)


@pytest.mark.parametrize("identity", ["echo", "scout"])
def test_route_uncertain_marker_query_preserves_identity_and_held_bounds(monkeypatch, identity):
    bus = Bus(url="https://example.test", service_key="test")
    calls = []
    monkeypatch.setattr(bus, "_get", lambda table, params: calls.append((table, params)) or [])
    after = {"created_at": "2026-10-07T00:00:00+00:00", "id": TICKET_ID}
    assert bus.pending_fixer_holds(identity, "fixer_route_uncertain", limit=20, after=after) == []
    assert len(calls) == 1
    table, params = calls[0]
    assert table == "support_messages"
    assert params["direction"] == "eq.outbound"
    assert params["delivery_status"] == "eq.held"
    assert params["attachments->>identity"] == f"eq.{identity}"
    assert params["attachments->>fixer_route_uncertain"] == "eq.true"
    assert params["limit"] == "20" and params["order"] == "created_at.asc,id.asc"
    assert after["id"] in params["or"] and after["created_at"] in params["or"]
    with pytest.raises(BusError, match="invalid FIXER hold marker"):
        bus.pending_fixer_holds(identity, "unknown_marker", limit=20)
    assert len(calls) == 1


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


@pytest.mark.parametrize("overrides", [
    {"product": "portal"}, {"source": "slack"}, {"status": "hold"},
    {"escalated": True}, {"hold_tier": "human"}, None,
])
def test_noneligible_completion_retains_ordinary_row_without_reservation(monkeypatch, overrides):
    ticket = NoticeBus().current if overrides is not None else None
    if ticket is not None:
        ticket.update(overrides)
    bus = Bus(url="https://example.test", service_key="test")
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(bus, "begin_current_notice",
                        lambda *_a, **_kw: pytest.fail("ineligible reservation"))
    monkeypatch.setattr(bus, "_insert", lambda _table, row: (deepcopy(row), False))
    row = bus.record_outbound(
        ticket_id=TICKET_ID, author_type="echo", body="The issue is fixed.",
        delivery_status="ready", kind="status",
        meta={"fixer": True, "resolve_notice": True, "identity": "echo"})
    assert "id" not in row
    assert "fixer_current_attempt_token" not in row["attachments"]


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


@pytest.mark.parametrize("mode", ["route_pending", "stale_cycle"])
def test_immutable_held_readback_waits_one_minute_without_row_mutation(monkeypatch, mode):
    now = datetime.now(timezone.utc)
    clock = [100.0]
    monkeypatch.setattr(outbox.time, "monotonic", lambda: clock[0])
    row = {
        "id": "immutable-held", "ticket_id": TICKET_ID,
        "created_at": now.isoformat(), "delivery_status": "held",
        "slack_ts": str(now.timestamp() + 5), "delivery_request_version": 3,
        "attachments": {
            "fixer_current_attempt_token": NOTICE_TOKEN,
            "fixer_slack_delivery_uncertain": True,
            "delivery_expected_status": "verification",
            "fixer_slack_delivery_intent": {
                "channel": "G_CLIENT", "thread_ts": "1.0", "body": "Exact body",
                "sender": "U_ECHO", "not_before": now.isoformat(),
                "request_key": None, "request_version": 3,
            },
        },
    }
    if mode == "route_pending":
        row["attachments"]["fixer_route_pending"] = True
    original = deepcopy(row)
    reads, proofs, finishes = [], [], []

    class HeldBus:
        def pending_held_fixer_delivery(self, _identity, **_kw):
            return [deepcopy(row)]

        def ticket(self, _tid):
            return {"request_version": 4 if mode == "stale_cycle" else 3,
                    "status": "verification"}

        def defer_held_fixer_reconcile(self, *_a):
            pytest.fail("immutable rows must never persist a retry timestamp")

        def record_held_fixer_readback(self, _mid, proof, **_kw):
            proofs.append(proof)
            return deepcopy(row)

        def reconcile_held_fixer_delivery(self, *_a, **_kw):
            pytest.fail("stale rows must not become posted")

    bus = HeldBus()

    def readback(channel, **_kw):
        reads.append(clock[0])
        messages = [] if clock[0] < 160 else [{
            "ts": row["slack_ts"], "text": "Exact body", "user": "U_ECHO",
            "thread_ts": "1.0",
        }]
        return {"ok": True, "channel": channel, "messages": messages}

    def finish(*_a):
        finishes.append(True)
        return True

    monkeypatch.setattr(outbox, "_finish_pending_route_notice", finish)
    summary = {"reconciled_posted": 0}
    for seconds in (100, 105, 159, 160, 165):
        clock[0] = seconds
        outbox._reconcile_held_fixer(
            bus, SimpleNamespace(name="echo"), readback, lambda _msg: None, summary)
    assert reads == [100, 160]
    assert row == original
    assert len(finishes) == (1 if mode == "route_pending" else 0)
    assert len(proofs) == (1 if mode == "stale_cycle" else 0)
    assert summary["reconciled_posted"] == (1 if mode == "route_pending" else 0)


def test_immutable_readback_throttle_is_bounded_and_scoped_by_identity_and_row(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(outbox.time, "monotonic", lambda: clock[0])
    bus = SimpleNamespace()
    echo, other = SimpleNamespace(name="echo"), SimpleNamespace(name="other")
    reserve = outbox._reserve_immutable_fixer_readback
    assert reserve(bus, echo, {"id": "one"})
    assert not reserve(bus, echo, {"id": "one"})
    assert reserve(bus, other, {"id": "one"})
    assert reserve(bus, echo, {"id": "two"})
    for index in range(1100):
        assert reserve(bus, echo, {"id": f"row-{index}"})
    assert len(bus._fixer_immutable_readback_retries) == 1024
    clock[0] = 160
    assert reserve(bus, echo, {"id": "one"})
    assert bus._fixer_immutable_readback_retries == {("echo", "one"): 220}


@pytest.mark.parametrize("outcome", ["success", "lost_cas", "wrong_identity", "error"])
def test_designated_suppression_logs_only_after_successful_cas(outcome):
    row = {"id": "notice", "attachments": {
        "fixer_current_attempt_token": NOTICE_TOKEN,
        "identity": "other" if outcome == "wrong_identity" else "echo",
    }}
    logs, events = [], []

    class SuppressionBus:
        def suppress_unattempted_current_notice(self, *_a, **_kw):
            events.append("cas")
            if outcome == "error":
                raise RuntimeError("CAS unavailable")
            return {"delivery_status": "suppressed"} if outcome == "success" else None

        def ensure_suppressed_current_notice_alert(self, *_a):
            events.append("alert")

    def log(message):
        logs.append(message)
        if "SUPPRESSED row" in message:
            assert events == ["cas"]

    summary = {"suppressed": 0, "skipped": 0}
    outbox._suppress(SuppressionBus(), row, None, SimpleNamespace(name="echo"),
                     "test reason", log, summary)
    assert any("SUPPRESSED row" in message for message in logs) == (outcome == "success")
    assert summary["suppressed"] == (1 if outcome == "success" else 0)


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

        def ensure_suppressed_current_notice_alert(self, _mid, _identity):
            self.events.append("alert")

    bus = StaleBus()
    summary = {}
    assert outbox._recover_stale_claims(
        bus, SimpleNamespace(name="echo"), lambda _msg: None,
        now=datetime.now(timezone.utc), summary=summary) == 1
    assert bus.events == ["suppressed", "alert"]
    assert summary["suppressed"] == 1


def _reserved_ready_row(now):
    return {"id": "70164909-16b5-43df-b6a3-8d7499d51485",
            "ticket_id": TICKET_ID, "delivery_status": "ready",
            "direction": "outbound", "author_type": "echo",
            "created_at": (now - timedelta(minutes=5)).isoformat(),
            "slack_ts": None, "slack_event_id": None,
            "attachments": {"fixer": True, "identity": "echo",
                            "fixer_route_pending": True,
                            "fixer_current_attempt_token": NOTICE_TOKEN}}


@pytest.mark.parametrize("initial_failure", [False, True])
def test_crashed_ready_reservation_recovered_by_sweep_without_send(
        monkeypatch, initial_failure):
    now = datetime.now(timezone.utc)
    row = _reserved_ready_row(now)
    bus = Bus(url="https://example.test", service_key="test")
    alerts = []
    attempts = []
    monkeypatch.setattr(bus, "message", lambda mid: deepcopy(row) if mid == row["id"] else None)
    monkeypatch.setattr(bus, "_get", lambda *_a: [])

    def patch(_table, match, fields):
        attempts.append(match)
        assert match["delivery_status"] == "eq.ready"
        assert match["slack_ts"] == match["slack_event_id"] == "is.null"
        assert json.loads(match["attachments"][3:]) == row["attachments"]
        if initial_failure and len(attempts) == 1:
            raise TimeoutError("compensation database unavailable")
        row.update(deepcopy(fields))
        return deepcopy(row)

    monkeypatch.setattr(bus, "_patch", patch)
    def record_alert(**kw):
        alerts.append(kw)
        return {"id": kw["message_id"], "ticket_id": kw["ticket_id"],
                "author_type": "system", "direction": "outbound", "body": kw["body"],
                "attachments": {"kind": kw["kind"], **kw["meta"]}}

    monkeypatch.setattr(bus, "record_outbound", record_alert)
    snapshot = deepcopy(row)
    summary = {"skipped": 0, "suppressed": 0}
    post = lambda *_a, **_kw: pytest.fail("reserved row must never post")
    for _ in range(3):
        outbox._dispatch_one(bus, post, snapshot,
                             identity=SimpleNamespace(name="echo"),
                             log=lambda _msg: None, summary=summary, now=now)
    assert row["delivery_status"] == "suppressed"
    assert summary["suppressed"] == 1
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "escalation"
    assert alerts[0]["meta"]["suppressed_message_id"] == row["id"]


@pytest.mark.parametrize("race", ["claim", "intent", "receipt", "attachment"])
def test_ready_recovery_cas_loses_to_concurrent_owner(monkeypatch, race):
    now = datetime.now(timezone.utc)
    row = _reserved_ready_row(now)
    snapshot = deepcopy(row)
    bus = Bus(url="https://example.test", service_key="test")
    monkeypatch.setattr(bus, "message", lambda _mid: deepcopy(row))

    def patch(_table, match, _fields):
        if race == "claim":
            row["delivery_status"] = "posting"
        elif race == "intent":
            row["attachments"]["fixer_slack_delivery_intent"] = {"channel": "G"}
        elif race == "receipt":
            row["slack_ts"] = "1.2"
        else:
            row["attachments"]["owner_marker"] = "changed"
        assert (match["delivery_status"] != f"eq.{row['delivery_status']}"
                or json.loads(match["attachments"][3:]) != row["attachments"]
                or row["slack_ts"] is not None)
        return None

    monkeypatch.setattr(bus, "_patch", patch)
    monkeypatch.setattr(bus, "record_outbound", lambda **_kw: pytest.fail("lost CAS alert"))
    summary = {"skipped": 0, "suppressed": 0}
    outbox._dispatch_one(bus, lambda *_a, **_kw: pytest.fail("duplicate send"),
                         snapshot, identity=SimpleNamespace(name="echo"),
                         log=lambda _msg: None, summary=summary, now=now)
    assert row["delivery_status"] != "suppressed"
    assert summary == {"skipped": 1, "suppressed": 0}


def test_recent_or_other_identity_ready_reservation_is_untouched(monkeypatch):
    now = datetime.now(timezone.utc)
    bus = Bus(url="https://example.test", service_key="test")
    monkeypatch.setattr(bus, "message", lambda _mid: pytest.fail("active owner read"))
    row = _reserved_ready_row(now)
    for created, identity in [(now, "echo"), (now - timedelta(minutes=5), "scout")]:
        row["created_at"] = created.isoformat()
        summary = {"skipped": 0}
        outbox._dispatch_one(bus, lambda *_a, **_kw: pytest.fail("duplicate send"),
                             row, identity=SimpleNamespace(name=identity),
                             log=lambda _msg: None, summary=summary, now=now)
        assert summary["skipped"] == 1


@pytest.mark.parametrize("suppress_wins", [True, False])
def test_identity_changed_first_contact_uses_claimed_cas_only(suppress_wins):
    class ChangedBus(NoticeBus):
        def claim_message(self, mid):
            result = super().claim_message(mid)
            self.current["request_version"] += 1
            return result

        def suppress_unattempted_current_notice(self, mid, reason):
            assert self.row["id"] == mid and self.row["delivery_status"] == "posting"
            assert self.row["attachments"].get("fixer_slack_delivery_intent") is None
            assert "identity changed" in reason
            self.events.append("suppress_cas")
            if not suppress_wins:
                return None
            self.row["delivery_status"] = "suppressed"
            return deepcopy(self.row)

        def record_outbound(self, **kw):
            if kw["kind"] == "escalation":
                self.events.append("staff_alert")
                return {"id": "staff-alert"}
            return super().record_outbound(**kw)

    bus = ChangedBus()
    _snapshot, result, posts = _send(bus)
    assert result.reason == "delivery_identity_changed"
    assert posts == []
    assert bus.events == ["begin", "insert", "claim", "suppress_cas"] + (
        ["staff_alert"] if suppress_wins else [])
    assert bus.row["delivery_status"] == ("suppressed" if suppress_wins else "posting")


def test_current_notice_flag_defaults_off_and_refuses_new_paths(monkeypatch):
    monkeypatch.delenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED")
    assert outbox.config.slack_convo_echo_current_notice_enabled() is False
    bus = NoticeBus()
    _snapshot, result, posts = _send(bus)
    assert result.reason == "current_notice_disabled" and not result.opened
    assert bus.events == [] and bus.row is None and posts == []

    real_bus = Bus(url="https://example.test", service_key="test")
    monkeypatch.setattr(real_bus, "_current_notice_rpc", lambda *_a: pytest.fail("flag OFF RPC"))
    monkeypatch.setattr(real_bus, "_insert", lambda *_a: pytest.fail("flag OFF insert"))
    monkeypatch.setattr(real_bus, "ticket", lambda _tid: deepcopy(bus.current))
    mid = "70164909-16b5-43df-b6a3-8d7499d51485"
    assert real_bus.begin_current_notice(bus.current, mid) is None
    assert not real_bus.bind_current_notice_route(TICKET_ID, 3, mid, NOTICE_TOKEN, "G", "1.2")
    assert real_bus.resolve_current_notice(bus.current, mid, NOTICE_TOKEN, "verification") is None
    for meta, message_id in [({"fixer": True, "resolve_notice": True}, None),
                             ({"fixer_current_attempt_token": NOTICE_TOKEN}, mid)]:
        with pytest.raises(BusError, match="capability disabled"):
            real_bus.record_outbound(ticket_id=TICKET_ID, author_type="echo", body="Fixed.",
                                     delivery_status="ready", kind="status",
                                     meta=meta, message_id=message_id)


def test_flag_off_never_dispatches_existing_token_notice(monkeypatch):
    monkeypatch.delenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED")
    row = _reserved_ready_row(datetime.now(timezone.utc))
    row["attachments"].pop("fixer_route_pending")
    summary = {"skipped": 0}
    outbox._dispatch_one(None, lambda *_a: pytest.fail("flag OFF post"), row,
                         identity=SimpleNamespace(name="echo"),
                         log=lambda _msg: None, summary=summary)
    assert summary["skipped"] == 1


def test_released_non_fixer_answer_cannot_resolve_unverified_website_ticket():
    class AnswerBus(NoticeBus):
        def set_ticket(self, *_a, **_kw):
            pytest.fail("ordinary answer cannot certify a fix")

    bus = AnswerBus()
    row = {"id": "70164909-16b5-43df-b6a3-8d7499d51485",
           "ticket_id": TICKET_ID, "delivery_status": "posted",
           "attachments": {"kind": "answer", "released_by": "U_BLAKE"}}
    summary = {"resolved": 0}
    outbox._resolve_on_answer(bus, bus.current, row, "answer", summary,
                              att=row["attachments"], body="Your account is connected.")
    assert bus.current["status"] == "verification"
    assert summary["resolved"] == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_real_bus_released_answer_uses_actual_notice_flag(monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", "true")
    else:
        monkeypatch.delenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED")
    bus = Bus(url="https://example.test", service_key="test")
    ticket = NoticeBus().current
    row = {"id": "70164909-16b5-43df-b6a3-8d7499d51485", "ticket_id": TICKET_ID,
           "body": "Your account is connected.", "delivery_status": "held",
           "attachments": {"identity": "echo", "kind": "answer"}}
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(bus, "message", lambda _mid: deepcopy(row))
    monkeypatch.setattr(bus, "set_ticket", lambda _tid, **fields: ticket.update(fields))
    monkeypatch.setattr(bus, "mark_message", lambda _mid, status, meta_update:
                        (row.update(delivery_status=status),
                         row["attachments"].update(meta_update)))
    monkeypatch.setattr(bus, "_client", lambda: pytest.fail("unexpected current-notice RPC"))
    # These real Bus methods exist in both flag states. Their presence is not arming.
    assert callable(bus.resolve_current_notice) and callable(bus.begin_current_notice)
    identity = SimpleNamespace(name="echo")
    assert outbox.release_held(bus, row["id"], approved_by="U_BLAKE", identity=identity)
    assert ticket["status"] == "verification" and row["delivery_status"] == "ready"
    row["delivery_status"] = "posted"  # simulate the confirmed successful client send
    summary = {"resolved": 0}
    outbox._after_answer_posted(bus, ticket, row, "answer", summary,
                               row["attachments"], identity)
    assert ticket["status"] == ("verification" if enabled else "resolved")
    assert summary["resolved"] == (0 if enabled else 1)


@pytest.mark.parametrize("enabled,token", [(False, None), (True, None),
                                           (True, NOTICE_TOKEN), (False, NOTICE_TOKEN)])
def test_real_bus_fixer_answer_selects_armed_close_contract(monkeypatch, enabled, token):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", str(enabled))
    bus = Bus(url="https://example.test", service_key="test")
    ticket = NoticeBus().current
    ticket.update(slack_channel_id="G_CLIENT", slack_thread_ts="1.2")
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(outbox, "_current_fixer_request_key", lambda *_a: "current-request")
    monkeypatch.setattr(bus, "set_ticket", lambda *_a, **_kw:
                        pytest.fail("FIXER answer bypassed atomic close"))
    calls = []

    def post(url, *, data, **_kw):
        payload = json.loads(data)
        calls.append((url, payload))
        assert payload["p_ticket_id"] == TICKET_ID
        assert payload["p_expected_request_version"] == 3
        assert payload["p_expected_slack_channel_id"] == "G_CLIENT"
        assert payload["p_expected_slack_thread_ts"] == "1.2"
        ticket["status"] = "resolved"
        return SimpleNamespace(status_code=200, json=lambda: [deepcopy(ticket)])

    monkeypatch.setattr(bus, "_client", lambda: SimpleNamespace(post=post))
    meta = {"fixer": True, "kind": "answer", "released_by": "U_BLAKE",
            "request_key": "current-request", "request_version": 3,
            "delivery_expected_status": "verification"}
    if token:
        meta["fixer_current_attempt_token"] = token
    row = {"id": "70164909-16b5-43df-b6a3-8d7499d51485", "ticket_id": TICKET_ID,
           "delivery_status": "posted", "attachments": meta}
    # Closeout now re-reads the persisted outbound row before using the
    # atomic FIXER resolver. Model that durable read alongside the RPC.
    monkeypatch.setattr(bus, "message", lambda mid: deepcopy(row)
                        if mid == row["id"] else None)
    summary = {"resolved": 0}
    outbox._resolve_on_answer(bus, deepcopy(ticket), row, "answer", summary,
                              att=meta, body="Your account is connected.")
    closes = enabled == bool(token)
    assert summary["resolved"] == int(closes)
    assert ticket["status"] == ("resolved" if closes else "verification")
    assert len(calls) == int(closes)
    if closes:
        rpc = "fixer_resolve_current_notice" if enabled else "fixer_resolve_current_delivery"
        assert calls[0][0].endswith("/" + rpc)
        if enabled:
            assert calls[0][1]["p_attempt_token"] == NOTICE_TOKEN
            assert calls[0][1]["p_notice_message_id"] == row["id"]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("recipient,escalated,hold_tier", [("staff", True, "human"),
                                                        ("client", False, None)])
def test_real_bus_generic_resolve_tap_uses_actual_notice_flag(
        monkeypatch, enabled, recipient, escalated, hold_tier):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", str(enabled))
    bus = Bus(url="https://example.test", service_key="test")
    ticket = NoticeBus().current
    ticket.update(status="hold", slack_channel_id="G_CLIENT", identity_kind=recipient,
                  escalated=escalated, hold_tier=hold_tier)
    # Staff escalation has no customer fix proof exemption. It retains the
    # existing generic notice path when the current-notice capability is OFF.
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    written = []
    monkeypatch.setattr(bus, "recent_messages", lambda *_a, **_kw: deepcopy(written))

    def insert(_table, row):
        # PostgREST supplies the durable row ID on INSERT; downstream closeout
        # now reads that exact persisted row before resolving.
        persisted = {"id": "70164909-16b5-43df-b6a3-8d7499d51485",
                     **deepcopy(row)}
        written.append(persisted)
        return deepcopy(persisted), False

    monkeypatch.setattr(bus, "_insert", insert)
    monkeypatch.setattr(bus, "set_ticket", lambda _tid, **fields: ticket.update(fields))
    monkeypatch.setattr(bus, "_client", lambda: pytest.fail("generic tap used notice RPC"))
    monkeypatch.setattr(outbox, "_recipient_armed", lambda *_a: True)
    identity = SimpleNamespace(name="echo")
    accepted = outbox.resolve_and_notify(bus, TICKET_ID, approved_by="U_BLAKE",
                                         identity=identity, log=lambda _msg: None)
    assert accepted is (not enabled)
    assert ticket["status"] == "hold"  # queueing a notice never closes the ticket
    assert len(written) == 1
    if enabled:
        assert written[0]["attachments"]["kind"] == "escalation"
        assert written[0]["attachments"]["resolve_refused"] is True
        assert not written[0]["attachments"].get("resolve_notice")
        assert "requires current notice or held release proof" in written[0]["body"]
        assert "guarded FIXER workflow" in written[0]["body"]
        assert "approved_by" not in ticket
    if not enabled:
        assert written[0]["attachments"]["resolve_notice"] is True
        assert ticket["approved_by"] == "U_BLAKE"
        assert not outbox.resolve_and_notify(bus, TICKET_ID, approved_by="U_BLAKE",
                                             identity=identity, log=lambda _msg: None)
        row = {**written[0], "delivery_status": "posted"}
        monkeypatch.setattr(bus, "message", lambda mid: deepcopy(row)
                            if mid == row["id"] else None)
        summary = {"resolved": 0}
        outbox._resolve_on_answer(bus, ticket, row, "status", summary,
                                  att=row["attachments"], body=row["body"])
        assert ticket["status"] == "resolved" and summary["resolved"] == 1


def test_real_bus_resolve_tap_contains_reservation_transport_failure(monkeypatch):
    bus = Bus(url="https://example.test", service_key="test")
    ticket = NoticeBus().current
    ticket.update(status="merged", classification="code_fix", slack_channel_id="G_CLIENT",
                  fix_pr_url="https://github.com/example/echo/pull/317",
                  verification_after={"fixer": {"request_key": "current-request"}})
    before = deepcopy(ticket)
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(bus, "recent_messages", lambda *_a, **_kw: [])
    monkeypatch.setattr(bus, "_insert", lambda *_a: pytest.fail("failed reservation inserted notice"))
    monkeypatch.setattr(bus, "set_ticket", lambda *_a, **_kw:
                        pytest.fail("uncertain reservation stamped approval"))
    monkeypatch.setattr(outbox, "_recipient_armed", lambda *_a: True)
    monkeypatch.setattr(outbox, "_verified_fix_notice", lambda *_a, **_kw: True)
    monkeypatch.setattr(outbox, "_current_fixer_request_key", lambda *_a: "current-request")
    attempts = []

    def post(url, **_kw):
        attempts.append(url)
        raise requests.exceptions.Timeout("lost reservation response")

    monkeypatch.setattr(bus, "_client", lambda: SimpleNamespace(post=post))
    logs = []
    assert not outbox.resolve_and_notify(bus, TICKET_ID, approved_by="U_BLAKE",
                                         identity=SimpleNamespace(name="echo"), log=logs.append)
    assert len(attempts) == 2  # the existing idempotent RPC retry, no new tap retry
    assert ticket == before
    assert any("failed: Timeout" in entry for entry in logs)


def _alert_store(monkeypatch, *, initial=None, fail_alert_inserts=0,
                 lost_response_status=None):
    """Real Bus alert/INSERT contracts over a conditional in-memory transport."""
    bus = Bus(url="https://example.test", service_key="test")
    rows = {initial["id"]: deepcopy(initial)} if initial else {}
    lock = Lock()
    alert_attempts = []

    def message(mid):
        with lock:
            return deepcopy(rows.get(mid))

    def get(_table, params):
        with lock:
            candidates = list(rows.values())
            for key, value in params.items():
                if key in {"select", "order", "limit", "or"}:
                    continue
                def actual(row):
                    if key.startswith("attachments->>"):
                        return (row.get("attachments") or {}).get(key.split("->>")[1])
                    return row.get(key)
                if value == "not.is.null":
                    candidates = [r for r in candidates if actual(r) is not None]
                elif value == "is.null":
                    candidates = [r for r in candidates if actual(r) is None]
                elif value.startswith("eq."):
                    expected = True if value == "eq.true" else value[3:]
                    candidates = [r for r in candidates if actual(r) == expected]
            return deepcopy(candidates[:int(params.get("limit", 20))])

    def insert(_table, row):
        with lock:
            if row["attachments"]["kind"] == "escalation":
                alert_attempts.append(row["id"])
                if len(alert_attempts) <= fail_alert_inserts:
                    raise TimeoutError("alert INSERT unavailable after committed suppression")
            if row["id"] in rows:
                return None, True
            saved = deepcopy(row)
            saved.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            rows[row["id"]] = saved
            if row["attachments"]["kind"] == "escalation" and lost_response_status:
                saved["delivery_status"] = lost_response_status
                if lost_response_status == "posted":
                    saved["slack_ts"] = "1700000000.000123"
                    saved["attachments"]["claimed_at"] = saved["created_at"]
                raise TimeoutError("committed alert INSERT response lost")
            return deepcopy(saved), False

    def patch(_table, match, fields):
        with lock:
            row = rows.get(match["id"][3:])
            if not row or ("delivery_status" in match
                           and match["delivery_status"] != f"eq.{row['delivery_status']}"):
                return None
            if match.get("attachments") and json.loads(match["attachments"][3:]) != row["attachments"]:
                return None
            if any(match.get(k) == "is.null" and row.get(k) is not None
                   for k in ("slack_ts", "slack_event_id")):
                return None
            row.update(deepcopy(fields))
            return deepcopy(row)

    monkeypatch.setattr(bus, "message", message)
    monkeypatch.setattr(bus, "_get", get)
    monkeypatch.setattr(bus, "_insert", insert)
    monkeypatch.setattr(bus, "_patch", patch)
    return bus, rows, alert_attempts


@pytest.mark.parametrize("path", ["stale_ready", "stale_posting", "lost_claim", "changed_identity"])
def test_failed_suppression_alert_insert_reconciles_from_terminal_source(monkeypatch, path):
    now = datetime.now(timezone.utc)
    original = _reserved_ready_row(now) if path.startswith("stale") else None
    if path == "stale_posting":
        original["delivery_status"] = "posting"
    bus, rows, attempts = _alert_store(monkeypatch, initial=original, fail_alert_inserts=1)
    identity = SimpleNamespace(name="echo", bot_user_id=lambda: "U_ECHO")
    posts = []
    if path == "stale_ready":
        outbox._dispatch_one(bus, lambda *_a, **_kw: posts.append("customer"), original,
                             identity=identity, log=lambda _msg: None,
                             summary={"skipped": 0, "suppressed": 0}, now=now)
    elif path == "stale_posting":
        monkeypatch.setattr(bus, "outbox", lambda *_a, **_kw: [deepcopy(original)])
        assert outbox._recover_stale_claims(bus, identity, lambda _msg: None,
                                           now=now, summary={}) == 1
    else:
        current = NoticeBus().current
        monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(current))
        monkeypatch.setattr(bus, "begin_current_notice", lambda *_a, **_kw: NOTICE_TOKEN)

        def claim(mid):
            if path == "lost_claim":
                return False
            rows[mid]["delivery_status"] = "posting"
            current["request_version"] += 1
            return True

        monkeypatch.setattr(bus, "claim_message", claim)
        # These two refusal paths never reach the in-flight 0624 admission; the
        # durable lane status preflight is stubbed so the real Bus needs no HTTP.
        monkeypatch.setattr(outreach, "_current_notice_admission_status",
                            lambda _bus: {"generation": 7})
        _snapshot, result, posts = _send(bus)
        assert result.reason == ("lost_claim" if path == "lost_claim" else "delivery_identity_changed")
    notice = next(r for r in rows.values() if r["attachments"].get("fixer") is True)
    assert notice["delivery_status"] == "suppressed"
    frozen_notice = deepcopy(notice)
    alert_id = _suppressed_current_notice_alert_id(notice["id"])
    assert alert_id not in rows and attempts == [alert_id]
    # OFF still allows staff reconciliation; it never rearms the customer capability.
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", "false")
    monkeypatch.setattr(bus, "outbox", lambda *_a, **_kw: [])
    outbox.run_once(bus, lambda *_a, **_kw: posts.append("customer"), identity=identity,
                    log=lambda _msg: None, now=now, member_check=lambda *_a: False)
    outbox._report_suppressed_current_notices(bus, identity, lambda _msg: None)
    assert attempts == [alert_id, alert_id]
    assert rows[alert_id]["delivery_status"] == "ready"
    assert rows[notice["id"]] == frozen_notice
    assert posts == []


@pytest.mark.parametrize("delivered_state", ["ready", "posted"])
def test_lost_alert_insert_response_recovers_same_row_without_rearm(monkeypatch, delivered_state):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    bus, rows, attempts = _alert_store(monkeypatch, initial=notice,
                                       lost_response_status=delivered_state)
    returned = bus.ensure_suppressed_current_notice_alert(notice["id"], "echo")
    frozen_alert = deepcopy(returned)
    for _ in range(3):
        assert bus.ensure_suppressed_current_notice_alert(notice["id"], "echo") == frozen_alert
    assert returned["delivery_status"] == delivered_state
    assert attempts == [returned["id"]]
    assert rows[notice["id"]] == notice


def test_concurrent_suppressed_notice_reconcilers_insert_one_stable_alert(monkeypatch):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    bus, rows, attempts = _alert_store(monkeypatch, initial=notice)
    original_message = bus.message
    alert_id = _suppressed_current_notice_alert_id(notice["id"])
    barrier = Barrier(2)

    def simultaneous_read(mid):
        value = original_message(mid)
        if mid == alert_id and value is None:
            barrier.wait(timeout=5)
        return value

    monkeypatch.setattr(bus, "message", simultaneous_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: bus.ensure_suppressed_current_notice_alert(
            notice["id"], "echo"), range(2)))
    assert all(r["id"] == alert_id for r in results)
    assert 1 <= len(attempts) <= 2 and set(attempts) == {alert_id}
    assert len(rows) == 2
    assert rows[notice["id"]] == notice


def test_existing_posted_legacy_suppression_alert_is_preserved(monkeypatch):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    bus, rows, attempts = _alert_store(monkeypatch, initial=notice)
    legacy_id = "1c44f89a-8838-4efe-81fb-920e569d934d"
    legacy = {"id": legacy_id, "ticket_id": TICKET_ID, "author_type": "system",
              "direction": "outbound", "delivery_status": "posted", "slack_ts": "1.23",
              "body": (f"FIXER first-contact notice {notice['id']} on ticket {TICKET_ID} "
                       "was canceled before Slack delivery. "
                       "Review the ticket before opening another notice."),
              "attachments": {"kind": "escalation", "identity": "echo",
                              "suppressed_message_id": notice["id"], "claimed_at": "then"}}
    rows[legacy_id] = deepcopy(legacy)
    assert bus.ensure_suppressed_current_notice_alert(notice["id"], "echo") == legacy
    assert attempts == [] and rows[legacy_id] == legacy


@pytest.mark.parametrize("ambiguous", ["posting", "intent", "receipt", "verified", "owner"])
def test_alert_retry_refuses_uncertain_or_other_owner_notice(monkeypatch, ambiguous):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    if ambiguous == "posting":
        notice["delivery_status"] = "posting"
    elif ambiguous == "intent":
        notice["attachments"]["fixer_slack_delivery_intent"] = {"channel": "G"}
    elif ambiguous == "receipt":
        notice["slack_ts"] = "1.2"
    elif ambiguous == "verified":
        notice["attachments"]["delivery_readback_verified"] = True
    else:
        notice["attachments"]["identity"] = "scout"
    bus, rows, attempts = _alert_store(monkeypatch, initial=notice)
    assert bus.ensure_suppressed_current_notice_alert(notice["id"], "echo") is None
    assert attempts == [] and rows[notice["id"]] == notice


def test_suppressed_notice_scan_is_scoped_bounded_and_advances_past_existing_alerts(monkeypatch):
    bus = Bus(url="https://example.test", service_key="test")
    queries = []
    monkeypatch.setattr(bus, "_get", lambda table, params: queries.append(params) or [])
    bus.suppressed_unattempted_current_notices(
        "echo", limit=1000, after={"created_at": "2026-10-07T01:00:00Z", "id": TICKET_ID})
    query = queries[0]
    assert query["limit"] == "20" and query["delivery_status"] == "eq.suppressed"
    assert query["attachments->>identity"] == "eq.echo"
    assert query["attachments->>fixer_current_attempt_token"] == "not.is.null"
    assert query["attachments->>fixer_slack_delivery_intent"] == "is.null"
    assert query["slack_ts"] == query["slack_event_id"] == "is.null"
    assert 'created_at.gt."2026-10-07T01:00:00Z"' in query["or"]
    assert query["order"] == "created_at.asc,id.asc"

    class PagedBus:
        def __init__(self):
            self.seen = []

        def suppressed_unattempted_current_notices(self, identity, *, limit, after):
            assert identity == "echo" and limit == 20
            start = int(after["id"]) + 1 if after else 0
            return [{"id": str(n), "created_at": "then"} for n in range(start, min(45, start + limit))]

        def ensure_suppressed_current_notice_alert(self, mid, identity):
            self.seen.append(mid)
            if self.seen == ["0"]:
                raise TimeoutError("one missing alert INSERT")

    paged = PagedBus()
    for expected_count in (20, 40, 45, 65):
        outbox._report_suppressed_current_notices(paged, SimpleNamespace(name="echo"), lambda _msg: None)
        assert len(paged.seen) == expected_count
    assert paged.seen.count("0") == 2 and paged.seen.count("44") == 1


@pytest.mark.parametrize("initial_status", ["ready", "posting"])
@pytest.mark.parametrize("insert_outcome", ["failed", "lost_posted_response"])
def test_generic_suppression_uses_terminal_cas_and_durable_alert(
        monkeypatch, initial_status, insert_outcome):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["attachments"].pop("fixer_route_pending")
    notice["delivery_status"] = initial_status
    bus, rows, attempts = _alert_store(
        monkeypatch, initial=notice, fail_alert_inserts=int(insert_outcome == "failed"),
        lost_response_status="posted" if insert_outcome == "lost_posted_response" else None)
    summary = {"suppressed": 0, "skipped": 0}
    outbox._suppress(bus, notice, {"id": TICKET_ID}, SimpleNamespace(name="echo"),
                     "FIXER requester identity changed before Slack delivery",
                     lambda _msg: None, summary)
    terminal = deepcopy(rows[notice["id"]])
    assert terminal["delivery_status"] == "suppressed" and summary["suppressed"] == 1
    # The same stale dispatcher cannot rewrite terminal state or create another alert.
    outbox._suppress(bus, notice, {"id": TICKET_ID}, SimpleNamespace(name="echo"),
                     "another suppression", lambda _msg: None, summary)
    outbox._report_suppressed_current_notices(bus, SimpleNamespace(name="echo"), lambda _msg: None)
    alert_id = _suppressed_current_notice_alert_id(notice["id"])
    assert rows[notice["id"]] == terminal
    assert set(attempts) == {alert_id} and len(rows) == 2
    assert rows[alert_id]["delivery_status"] == ("ready" if insert_outcome == "failed" else "posted")


@pytest.mark.parametrize("conflict", ["intent", "receipt", "token", "owner"])
def test_generic_suppression_refuses_changed_or_attempted_source(monkeypatch, conflict):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    bus, rows, attempts = _alert_store(monkeypatch, initial=notice)
    current = rows[notice["id"]]
    if conflict == "intent":
        current["attachments"]["fixer_slack_delivery_intent"] = {"channel": "G_CLIENT"}
    elif conflict == "receipt":
        current["slack_ts"] = "1.2"
    elif conflict == "token":
        current["attachments"]["fixer_current_attempt_token"] = TICKET_ID
    else:
        current["attachments"]["identity"] = "scout"
    frozen = deepcopy(current)
    summary = {"suppressed": 0, "skipped": 0}
    outbox._suppress(bus, notice, {"id": TICKET_ID}, SimpleNamespace(name="echo"),
                     "identity changed", lambda _msg: None, summary)
    assert rows[notice["id"]] == frozen and attempts == []
    assert summary == {"suppressed": 0, "skipped": 1}


def _generic_legacy_alert(notice, state):
    reason = notice["attachments"]["suppressed_why"]
    return {"id": "1c44f89a-8838-4efe-81fb-920e569d934d", "ticket_id": notice["ticket_id"],
            "author_type": "system", "direction": "outbound", "delivery_status": state,
            "body": (f"SUPPRESSED reply on ticket {notice['ticket_id']} (echo): {reason}. "
                     "Nothing was posted; a person should look."),
            "attachments": {"kind": "escalation", "identity": "echo",
                            "suppressed_message_id": notice["id"], "recipient_kind": "client"}}


@pytest.mark.parametrize("state", ["ready", "posting", "posted"])
def test_generic_source_linked_legacy_alert_is_not_duplicated(monkeypatch, state):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    notice["attachments"]["suppressed_why"] = "FIXER requester identity changed before Slack delivery"
    bus, rows, attempts = _alert_store(monkeypatch, initial=notice)
    alert = _generic_legacy_alert(notice, state)
    rows[alert["id"]] = deepcopy(alert)
    assert bus.ensure_suppressed_current_notice_alert(notice["id"], "echo") == alert
    assert attempts == [] and len(rows) == 2
    assert rows[alert["id"]] == alert and rows[notice["id"]] == notice


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("owner_changes_before_insert", [False, True])
def test_verified_staff_notice_survives_owner_change_without_buttons_or_customer_send(
        monkeypatch, legacy, owner_changes_before_insert):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    notice["attachments"]["suppressed_why"] = "identity changed"
    bus, rows, _attempts = _alert_store(monkeypatch, initial=notice)
    ticket = NoticeBus().current
    if owner_changes_before_insert:
        ticket["bot_identity"] = "scout"
    if legacy:
        alert = _generic_legacy_alert(notice, "ready")
        rows[alert["id"]] = deepcopy(alert)
    else:
        alert = bus.ensure_suppressed_current_notice_alert(notice["id"], "echo")
    ticket["bot_identity"] = "scout"
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(bus, "set_ticket", lambda *_a, **_kw: pytest.fail("staff alert must not close ticket"))
    monkeypatch.setattr(outbox, "_channel_for", lambda *_a: "C_STAFF")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", "false")

    def claim(mid):
        if rows[mid]["delivery_status"] != "ready":
            return False
        rows[mid]["delivery_status"] = "posting"
        return True

    monkeypatch.setattr(bus, "claim_message", claim)
    posts = []

    def post(channel, body, *, thread_ts, blocks):
        assert channel == "C_STAFF" and thread_ts is None and blocks is None
        assert body == alert["body"]
        posts.append(channel)
        return "1700000000.123456"

    summary = {"posted": 0, "skipped": 0, "failed": 0}
    outbox._dispatch_one(bus, post, deepcopy(alert), identity=SimpleNamespace(name="echo"),
                         log=lambda _msg: None, summary=summary)
    assert posts == ["C_STAFF"] and summary["posted"] == 1
    assert rows[alert["id"]]["delivery_status"] == "posted"
    assert rows[notice["id"]] == notice and ticket["status"] == "verification"
    # The capability remains disabled for the actual customer row.
    customer = deepcopy(notice)
    customer["attachments"].pop("fixer_route_pending")
    customer["delivery_status"] = "ready"
    outbox._dispatch_one(bus, post, customer, identity=SimpleNamespace(name="echo"),
                         log=lambda _msg: None, summary=summary)
    assert posts == ["C_STAFF"]


@pytest.mark.parametrize("forgery", ["body", "source", "source_owner", "ticket", "author", "kind", "row_id"])
def test_forged_staff_alert_marker_cannot_bypass_owner_fence(monkeypatch, forgery):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    bus, rows, _attempts = _alert_store(monkeypatch, initial=notice)
    alert = bus.ensure_suppressed_current_notice_alert(notice["id"], "echo")
    if forgery == "body":
        alert["body"] = "Unrelated message"
    elif forgery == "source":
        alert["attachments"]["suppressed_message_id"] = TICKET_ID
    elif forgery == "source_owner":
        rows[notice["id"]]["attachments"]["identity"] = "scout"
    elif forgery == "ticket":
        alert["ticket_id"] = "1c44f89a-8838-4efe-81fb-920e569d934d"
    elif forgery == "author":
        alert["author_type"] = "echo"
    elif forgery == "kind":
        alert["attachments"]["kind"] = "answer"
    else:
        alert["id"] = "1c44f89a-8838-4efe-81fb-920e569d934d"
    ticket = NoticeBus().current
    ticket["bot_identity"] = "scout"
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(bus, "claim_message", lambda _mid: pytest.fail("forged alert claim"))
    summary = {"skipped": 0}
    outbox._dispatch_one(bus, lambda *_a, **_kw: pytest.fail("forged alert send"), alert,
                         identity=SimpleNamespace(name="echo"), log=lambda _msg: None,
                         summary=summary)
    assert summary == {"skipped": 1}


def test_staff_alert_source_link_is_rechecked_after_claim(monkeypatch):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    bus, rows, _attempts = _alert_store(monkeypatch, initial=notice)
    alert = bus.ensure_suppressed_current_notice_alert(notice["id"], "echo")
    ticket = NoticeBus().current
    ticket["bot_identity"] = "scout"
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    monkeypatch.setattr(outbox, "_channel_for", lambda *_a: "C_STAFF")

    def claim(mid):
        rows[mid]["delivery_status"] = "posting"
        rows[mid]["attachments"]["suppressed_message_id"] = TICKET_ID
        return True

    monkeypatch.setattr(bus, "claim_message", claim)
    summary = {"skipped": 0}
    outbox._dispatch_one(bus, lambda *_a, **_kw: pytest.fail("changed alert link sent"), alert,
                         identity=SimpleNamespace(name="echo"), log=lambda _msg: None,
                         summary=summary)
    assert summary == {"skipped": 1} and rows[notice["id"]] == notice


def test_transient_staff_alert_source_read_preserves_ready_alert(monkeypatch):
    notice = _reserved_ready_row(datetime.now(timezone.utc))
    notice["delivery_status"] = "suppressed"
    bus, rows, _attempts = _alert_store(monkeypatch, initial=notice)
    alert = bus.ensure_suppressed_current_notice_alert(notice["id"], "echo")
    ticket = NoticeBus().current
    ticket["bot_identity"] = "scout"
    monkeypatch.setattr(bus, "ticket", lambda _tid: deepcopy(ticket))
    def fail_read(_mid):
        raise TimeoutError("DB read failed")

    monkeypatch.setattr(bus, "message", fail_read)
    monkeypatch.setattr(bus, "claim_message", lambda _mid: pytest.fail("unverified claim"))
    summary = {"skipped": 0}
    outbox._dispatch_one(bus, lambda *_a, **_kw: pytest.fail("unverified send"), alert,
                         identity=SimpleNamespace(name="echo"), log=lambda _msg: None,
                         summary=summary)
    assert summary == {"skipped": 1} and rows[alert["id"]] == alert
    assert rows[notice["id"]] == notice


@pytest.mark.parametrize("guard", ["escalated", "held"])
def test_customer_fix_resolve_tap_explains_current_notice_refusal(monkeypatch, guard):
    ticket = NoticeBus().current
    ticket.update(status="merged", classification="code_fix", slack_channel_id="G_CLIENT",
                  fix_pr_url="https://github.com/example/echo/pull/317")
    ticket.update({"escalated": True} if guard == "escalated" else {"hold_tier": "human"})
    before = deepcopy(ticket)
    written = []
    bus = SimpleNamespace(ticket=lambda _tid: deepcopy(ticket),
                          record_outbound=lambda **kw: written.append(kw))
    assert not outbox.resolve_and_notify(bus, TICKET_ID, approved_by="U_BLAKE",
                                         identity=SimpleNamespace(name="echo"), log=lambda _m: None)
    assert ticket == before
    assert len(written) == 1 and written[0]["kind"] == "escalation"
    assert written[0]["meta"]["resolve_refused"] is True
    assert "The ticket is unchanged" in written[0]["body"]
