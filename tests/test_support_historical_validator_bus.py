"""0640 reservation validation is exact and never inferred from an HTTP error."""

import json
import uuid

import pytest

from agent.slack_convo.bus import Bus, BusError


class Response:
    def __init__(self, value, status=200):
        self.value = value
        self.status_code = status
        self.text = ""

    def json(self):
        return self.value


class Http:
    def __init__(self, value=True, status=200):
        self.value = value
        self.status = status
        self.calls = []

    def post(self, url, *, data, headers, timeout):
        self.calls.append((url, json.loads(data), headers, timeout))
        return Response(self.value, self.status)


def expected():
    return {
        "ticket_id": str(uuid.uuid4()), "request_version": 2,
        "notice_id": str(uuid.uuid4()), "request_key": "a" * 64,
        "body_sha256": "b" * 64, "source": "slack_conversation",
        "product": "social", "client_id": "gym-1", "bot_identity": "scout",
        "slack_user_id": "U_CLIENT", "channel": "C_CLIENT",
        "thread_ts": None, "reservation": "ticket-v2",
    }


def test_validator_sends_complete_snapshot_to_private_rpc():
    http = Http()
    bus = Bus(url="https://bus.invalid", service_key="service-test", http=http)
    e = expected()
    assert bus.fixer_validate_historical_receipt_dispatch(e) == {
        "ok": True, "notice_id": e["notice_id"], "reservation": e["reservation"]}
    url, body, _, timeout = http.calls[0]
    assert url.endswith("/rpc/fixer_validate_historical_receipt_reservation")
    assert body == {
        "p_ticket_id": e["ticket_id"], "p_request_version": 2,
        "p_notice_message_id": e["notice_id"], "p_request_key": "a" * 64,
        "p_intended_body_sha256": "b" * 64, "p_source": "slack_conversation",
        "p_product": "social", "p_client_id": "gym-1",
        "p_bot_identity": "scout", "p_slack_user_id": "U_CLIENT",
        "p_slack_channel_id": "C_CLIENT", "p_slack_thread_ts": None,
    }
    assert timeout == 30


def test_validator_refuses_missing_identity_and_nontrue_rpc_result():
    for missing in ("client_id", "source", "notice_id", "request_key", "body_sha256"):
        http = Http()
        bus = Bus(url="https://bus.invalid", service_key="service-test", http=http)
        e = expected()
        del e[missing]
        assert bus.fixer_validate_historical_receipt_dispatch(e) is None
        assert http.calls == []
    for response in (False, None, {}, [True], "true"):
        http = Http(response)
        bus = Bus(url="https://bus.invalid", service_key="service-test", http=http)
        assert bus.fixer_validate_historical_receipt_dispatch(expected()) is None


def test_uninstalled_validator_rpc_is_never_treated_as_approval():
    http = Http(status=404)
    bus = Bus(url="https://bus.invalid", service_key="service-test", http=http)
    with pytest.raises(BusError):
        bus.fixer_validate_historical_receipt_dispatch(expected())
