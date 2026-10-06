"""Portal progress updates stay in the original ticket and never route to Slack."""

from datetime import datetime, timedelta, timezone

import pytest

from agent.slack_convo import adapter as A
from agent.slack_convo import outbox as O
from tests.test_portal_escalation_loop import Bus, ECHO, _ticket


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")


def _progress_bus(*, ticket_changes=None, meta_changes=None):
    fields = {"status": "merged", "classification": "code_fix",
              "slack_channel_id": None, "slack_thread_ts": None,
              **(ticket_changes or {})}
    ticket = _ticket(**fields)
    bus = Bus([ticket])
    bus.record_inbound(ticket_id=ticket["id"], author_type="client",
                       author_id=ticket["reporter"], body=ticket["raw_text"],
                       meta={"surface": "portal_ticket_bridge"})
    current = bus.ticket(ticket["id"])
    meta = {"identity": "echo", "recipient_kind": "client",
            "surface": "portal_ticket_bridge", "fixer": True,
            "portal_progress_status": True,
            "request_version": current["request_version"],
            "request_key": O._current_fixer_request_key(bus, current)}
    meta.update(meta_changes or {})
    row = bus.record_outbound(ticket_id=ticket["id"], author_type="echo",
                              body="The team is checking this request.",
                              delivery_status="ready", kind=A.KIND_STATUS, meta=meta)
    return bus, row


def _dispatch(bus):
    calls = []

    def post(channel, text, thread_ts=None, blocks=None):
        calls.append((channel, text))
        return "slack-ts"

    result = O.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    return result, calls


@pytest.mark.parametrize("status", ["merged", "hold"])
def test_current_progress_is_posted_only_to_portal_and_keeps_ticket_open(status):
    bus, row = _progress_bus(ticket_changes={"status": status})
    result, calls = _dispatch(bus)
    posted = bus.message(row["id"])
    assert result["posted"] == 1
    assert posted["delivery_status"] == "posted"
    assert posted["attachments"]["delivered_via"] == "portal_thread"
    assert calls == []
    assert bus.ticket("t-1")["status"] == status


@pytest.mark.parametrize("ticket_changes,meta_changes", [
    ({"slack_channel_id": "G_CLIENT"}, {}),
    ({"slack_thread_ts": "123.45"}, {}),
    ({"client_id": ""}, {}),
    ({"source": "slack_conversation"}, {}),
    ({"status": "resolved"}, {}),
    ({"bot_identity": "scout"}, {}),
    ({}, {"resolve_notice": True}),
    ({}, {"request_version": True}),
    ({}, {"request_version": 999}),
    ({}, {"request_key": "old-request"}),
])
def test_mismatched_progress_suppresses_before_claim_or_slack(
        ticket_changes, meta_changes):
    bus, row = _progress_bus(ticket_changes=ticket_changes,
                             meta_changes=meta_changes)
    result, calls = _dispatch(bus)
    assert result["suppressed"] == 1
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert calls == []


def test_held_progress_rechecks_after_human_release(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "false")
    bus, row = _progress_bus()
    first, _ = _dispatch(bus)
    assert first["held"] == 1
    assert bus.message(row["id"])["delivery_status"] == "held"
    assert O.release_held(bus, row["id"], approved_by="U_OPERATOR", identity=ECHO)
    bus.set_ticket("t-1", request_version=2)
    second, calls = _dispatch(bus)
    assert second["suppressed"] == 1
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert all(channel != "G_CLIENT" for channel, _ in calls)
    assert bus.ticket("t-1")["status"] == "merged"


def test_human_released_current_progress_reaches_portal_without_arming(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "false")
    bus, row = _progress_bus()
    _dispatch(bus)
    assert bus.message(row["id"])["delivery_status"] == "held"
    assert O.release_held(bus, row["id"], approved_by="U_OPERATOR", identity=ECHO)
    _, calls = _dispatch(bus)
    posted = bus.message(row["id"])
    assert posted["delivery_status"] == "posted"
    assert posted["attachments"]["delivered_via"] == "portal_thread"
    assert all(channel != "G_CLIENT" for channel, _ in calls)
    assert bus.ticket("t-1")["status"] == "merged"


def test_stale_portal_progress_claim_is_retried_not_quarantined_as_uncertain_fixer():
    bus, row = _progress_bus()
    assert bus.claim_message(row["id"])
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    bus.mark_message(row["id"], "posting", meta_update={"claimed_at": stale})

    result, calls = _dispatch(bus)
    posted = bus.message(row["id"])
    assert result["reclaimed"] == 1
    assert posted["delivery_status"] == "posted"
    assert posted["attachments"]["delivered_via"] == "portal_thread"
    assert not posted["attachments"].get("fixer_slack_delivery_uncertain")
    assert calls == []


def test_progress_marker_on_an_internal_kind_is_suppressed_before_internal_post():
    bus, row = _progress_bus()
    row["attachments"]["kind"] = A.KIND_ESCALATION
    result, calls = _dispatch(bus)
    assert result["suppressed"] == 1
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert calls == []


@pytest.mark.parametrize("change", ["surface", "author", "duplicate"])
def test_progress_requires_exact_original_portal_inbound(change):
    bus, row = _progress_bus()
    inbound = bus.msgs[0]
    if change == "surface":
        inbound["attachments"]["surface"] = "mpim"
    elif change == "author":
        inbound["author_id"] = "someone-else@gym.com"
    else:
        bus.msgs.append({**inbound, "id": "second-original"})
    result, calls = _dispatch(bus)
    assert result["suppressed"] == 1
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert calls == []


def test_scout_and_echo_request_keys_match_for_a_portal_client_inbound():
    # Expected key was produced by Scout's sources.requestKey for this exact row.
    bus = Bus([_ticket(id="t-1", created_at="2026-10-03T00:00:00Z")])
    bus.record_inbound(ticket_id="t-1", author_type="client",
                       author_id="owner@gym.com", body="is my instagram connected?",
                       created_at="2026-10-03T00:00:01Z",
                       meta={"surface": "portal_ticket_bridge"})
    assert O._current_fixer_request_key(bus, bus.ticket("t-1")) == (
        "4cee09e2f73efe8a1c4bb0bed4058cf7e24c0db3b2b4cad4925029088e0741cb")


def test_progress_rechecks_a_correction_during_claim():
    class RacingBus(Bus):
        def claim_message(self, mid):
            claimed = super().claim_message(mid)
            if claimed:
                self.set_ticket("t-1", request_version=2)
            return claimed

    initial, row = _progress_bus()
    bus = RacingBus(initial.tickets.values())
    bus.msgs = initial.msgs
    result, calls = _dispatch(bus)
    assert result["suppressed"] == 1
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert calls == []
