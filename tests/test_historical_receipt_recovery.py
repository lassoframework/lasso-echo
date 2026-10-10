"""Portal 0640 historical receipt recovery: narrow resolved-notice admission.

An already-resolved Chateau-shaped answerable_question can receive its reserved
resolution receipt ONLY when the row is explicitly stamped for historical
receipt recovery AND the portal 0640 service-role validator agrees at the
pre-POST boundary inside _post_support_resolution. Ordinary resolved status
notices, a changed same-version requester transcript, and a missing or
dissenting validator all fail closed with no Slack POST.
"""
import hashlib
import os
import uuid
import pytest

os.environ.setdefault("RAILWAY_DEPLOYMENT_ID", "test-deployment")
os.environ.setdefault("RAILWAY_GIT_COMMIT_SHA", "test-build-sha")
os.environ.setdefault("AGENT_SLACK_BOT_USER_ID", "U_ECHO_BOT")

from agent.slack_convo import adapter as A  # noqa: E402
from agent.slack_convo import identities as IDS  # noqa: E402
from agent.slack_convo import outbox as OB  # noqa: E402
from tests.test_slack_convo import FakeBus, _posted  # noqa: E402

MENTION = "<@U_CLIENT>"
RESERVATION = "0640-reservation-chateau-20261009"


def _case(bus, *, stamped=True):
    tid = str(uuid.uuid4())
    bus.tickets[tid] = {
        "id": tid, "product": "echo", "source": "slack_conversation",
        "status": "resolved", "classification": "answerable_question",
        "client_id": "gym-chateau", "bot_identity": "echo",
        "identity_kind": "client", "slack_user_id": "U_CLIENT",
        "slack_channel_id": "C_CLIENT", "slack_thread_ts": "1.0",
        "escalated": False, "hold_tier": None, "verification_after": None,
        "request_version": 0, "raw_text": "Why did my post not publish?",
        "created_at": "2026-10-09T10:00:00Z",
    }
    bus.record_inbound(ticket_id=tid, author_type="client",
                       body="Why did my post not publish?")
    bus.tickets[tid]["status"] = "resolved"  # resolved AFTER the request landed
    key = OB._current_fixer_request_key(bus, bus.ticket(tid))
    scope = "the answer to your publishing question"
    body = OB.HISTORICAL_PRECLOSE_BODY.format(recipient="U_CLIENT", scope=scope)
    meta = {"identity": "echo", "recipient_kind": "client", "fixer": True,
            "released_by": "fixer", "resolve_notice": True,
            "request_key": key,
            "request_version": bus.tickets[tid]["request_version"],
            "historical_receipt_verified_scope": scope,
            "delivery_identity_fence": True,
            "delivery_expected_source": "slack_conversation",
            "delivery_expected_product": "echo",
            "delivery_expected_client_id": "gym-chateau",
            "delivery_expected_status": "resolved",
            "delivery_expected_classification": "answerable_question",
            "delivery_expected_bot_identity": "echo",
            "delivery_expected_slack_user_id": "U_CLIENT",
            "delivery_expected_slack_channel_id": "C_CLIENT",
            "delivery_expected_slack_thread_ts": "1.0"}
    notice = bus.record_outbound(ticket_id=tid, author_type="echo", body=body,
                                 delivery_status="ready", kind=A.KIND_STATUS,
                                 meta=meta)
    if stamped:
        bus.mark_message(notice["id"], "ready", meta_update={
            "historical_receipt_recovery": True,
            "historical_receipt_notice_id": notice["id"],
            "historical_receipt_reservation": RESERVATION,
            "historical_receipt_body_sha256":
                hashlib.sha256(body.encode("utf-8")).hexdigest()})
    return tid, notice, key


def _validator(bus, result):
    seen = []

    def validate(payload):
        seen.append(payload)
        if result is True:
            return {"ok": True, "notice_id": payload["notice_id"],
                    "reservation": payload["reservation"]}
        return result
    bus.fixer_validate_historical_receipt_dispatch = validate
    return seen


def _run(bus, *, exact_message_id=None):
    post, calls = _posted()
    summary = OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None,
                          member_check=lambda channel, user: True,
                          exact_message_id=exact_message_id)
    return calls, summary


def test_answerable_resolved_receipt_posts_with_validator(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    tid, notice, key = _case(bus)
    seen = _validator(bus, True)
    calls, summary = _run(bus)
    assert bus.message(notice["id"])["delivery_status"] == "posted"
    assert any(c["channel"] == "C_CLIENT" for c in calls)
    assert len(seen) == 1
    payload = seen[0]
    assert payload["notice_id"] == notice["id"]
    assert payload["reservation"] == RESERVATION
    assert payload["ticket_id"] == tid
    assert payload["request_key"] == key
    assert payload["channel"] == "C_CLIENT"
    assert payload["source"] == "slack_conversation"
    assert payload["product"] == "echo"
    assert payload["client_id"] == "gym-chateau"
    assert payload["bot_identity"] == "echo"
    assert payload["slack_user_id"] == "U_CLIENT"


def test_exact_historical_dispatch_sends_only_selected_client_body(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    _validator(bus, True)
    monkeypatch.setattr(bus, "outbox", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("broad outbox sweep")))
    calls, summary = _run(bus, exact_message_id=notice["id"])
    assert summary["posted"] == 1
    assert len(calls) == 1
    assert calls[0]["text"] == notice["body"]
    assert "<@U_CLIENT>" in calls[0]["text"]
    assert f"<@{OB.config.APPROVER_SLACK_ID}>" not in calls[0]["text"]


def test_historical_dispatch_rejects_blake_mention_instead_of_requester(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    wrong = OB.HISTORICAL_PRECLOSE_BODY.format(
        recipient=OB.config.APPROVER_SLACK_ID,
        scope="the answer to your publishing question")
    bus.set_message_body_if_posting = lambda *a, **k: None
    row = next(m for m in bus.msgs if m["id"] == notice["id"])
    row["body"] = wrong
    row["attachments"]["historical_receipt_body_sha256"] = hashlib.sha256(
        wrong.encode()).hexdigest()
    _validator(bus, True)
    calls, summary = _run(bus)
    assert not calls
    assert summary["posted"] == 0


def test_historical_staff_stamp_cannot_bypass_validator(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    bus.mark_message(notice["id"], "ready", meta_update={"recipient_kind": "staff"})
    seen = _validator(bus, False)
    calls, summary = _run(bus, exact_message_id=notice["id"])
    assert not calls
    assert not seen
    assert summary["posted"] == 0


def test_historical_staff_ticket_cannot_prepare(monkeypatch):
    from agent.support_historical_closeout import HistoricalCloseoutError
    from tests.test_support_historical_closeout import Fake, prep
    bus = Fake()
    bus.t["identity_kind"] = "staff"
    with pytest.raises(HistoricalCloseoutError):
        prep(bus)


def test_same_version_correction_before_admission_suppresses(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _case(bus)
    # Same request_version, changed requester transcript: mutate the stored
    # inbound body directly so the full inbound hash no longer matches.
    inbound = next(m for m in bus.msgs if m["direction"] == "inbound")
    inbound["body"] = "Actually, never mind the post -- refund me instead"
    _validator(bus, True)
    calls, summary = _run(bus)
    assert not calls
    assert summary["posted"] == 0


def test_same_version_correction_during_admission_blocks_post(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    _validator(bus, True)
    original_rpc = bus.post

    def corrupting_post(url, **kwargs):
        result = original_rpc(url, **kwargs)
        if url.rsplit("/", 1)[-1] == "support_admission_acquire_lane":
            # A requester correction lands after durable admission, before POST.
            inbound = next(m for m in bus.msgs if m["direction"] == "inbound")
            inbound["body"] = "Wait -- I still need help with this"
        return result
    bus.post = corrupting_post
    calls, summary = _run(bus)
    assert not calls
    assert bus.message(notice["id"])["delivery_status"] != "posted"
    assert summary["posted"] == 0


def test_ordinary_resolved_notice_without_stamp_denied(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _case(bus, stamped=False)
    _validator(bus, True)
    calls, summary = _run(bus)
    assert not calls
    assert summary["posted"] == 0


def test_stamped_resolved_notice_without_fixer_denied(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    bus.mark_message(notice["id"], "ready", meta_update={"fixer": False})
    _validator(bus, True)
    calls, summary = _run(bus)
    assert not calls
    assert summary["posted"] == 0


def test_historical_receipt_without_delivery_fence_denied(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    bus.mark_message(notice["id"], "ready", meta_update={
        "delivery_identity_fence": False})
    _validator(bus, True)
    calls, summary = _run(bus)
    assert not calls
    assert summary["posted"] == 0


def test_historical_receipt_with_wrong_tenant_fence_denied(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    bus.mark_message(notice["id"], "ready", meta_update={
        "delivery_expected_client_id": "another-gym"})
    _validator(bus, True)
    calls, summary = _run(bus)
    assert not calls
    assert summary["posted"] == 0


def test_same_version_correction_during_validator_blocks_post(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)

    def correct_during_validation(payload):
        inbound = next(m for m in bus.msgs if m["direction"] == "inbound")
        inbound["body"] = "Correction: I still need help"
        return {"ok": True, "notice_id": payload["notice_id"],
                "reservation": payload["reservation"]}

    bus.fixer_validate_historical_receipt_dispatch = correct_during_validation
    calls, summary = _run(bus)
    assert not calls
    assert bus.message(notice["id"])["delivery_status"] != "posted"
    assert summary["posted"] == 0


def test_route_change_during_validator_blocks_post(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    tid, notice, _ = _case(bus)

    def move_during_validation(payload):
        bus.tickets[tid]["slack_channel_id"] = "C_OTHER_GYM"
        return {"ok": True, "notice_id": payload["notice_id"],
                "reservation": payload["reservation"]}

    bus.fixer_validate_historical_receipt_dispatch = move_during_validation
    calls, summary = _run(bus)
    assert not calls
    assert bus.message(notice["id"])["delivery_status"] != "posted"
    assert summary["posted"] == 0


def test_notice_change_during_validator_blocks_post(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)

    def change_notice_during_validation(payload):
        for message in bus.msgs:
            if message["id"] == notice["id"]:
                message["body"] = "Changed while validation was in flight"
                break
        return {"ok": True, "notice_id": payload["notice_id"],
                "reservation": payload["reservation"]}

    bus.fixer_validate_historical_receipt_dispatch = change_notice_during_validation
    calls, summary = _run(bus)
    assert not calls
    assert bus.message(notice["id"])["delivery_status"] != "posted"
    assert summary["posted"] == 0


def test_validator_absent_fails_closed(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    assert not callable(getattr(bus, "fixer_validate_historical_receipt_dispatch", None))
    calls, summary = _run(bus)
    assert not calls
    assert bus.message(notice["id"])["delivery_status"] != "posted"
    assert summary["posted"] == 0


def test_validator_denies_no_post(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    _, notice, _ = _case(bus)
    _validator(bus, {"ok": False})
    calls, summary = _run(bus)
    assert not calls
    assert bus.message(notice["id"])["delivery_status"] != "posted"
    assert summary["posted"] == 0
