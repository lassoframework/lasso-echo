"""Live-shaped coverage for Echo's authenticated row-first FIXER client."""
import json
from types import SimpleNamespace

import pytest

from agent import fixer_business_seed_client as client


CLIENT = "22222222-2222-4222-8222-222222222222"
TICKET = "33333333-3333-4333-8333-333333333333"
SUBMISSION = "11111111-1111-4111-8111-111111111111"
REQUEST_KEY = "a" * 64


class Poster:
    def __init__(self, status=201, value=None):
        self.status = status
        self.value = value if value is not None else {
            "ok": True, "outcome": "created", "ticket_id": TICKET,
            "request_key": REQUEST_KEY,
        }
        self.calls = []

    def __call__(self, url, *, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        return SimpleNamespace(status_code=self.status,
                               content=json.dumps(self.value).encode())


class MappingBus:
    def __init__(self, value=CLIENT):
        self.value = value
        self.calls = []

    def portal_client_id(self, gym_key):
        self.calls.append(gym_key)
        return self.value


def test_calendar_event_posts_exact_row_first_contract():
    event = client.calendar_row_event(
        submission_key=SUBMISSION, client_id=CLIENT,
        row_id="calendar_row_123", expected_status="published")
    poster = Poster()
    result = client.send(event, base_url="https://fixer.example.test/",
                         secret="shared", post=poster, timeout=3)
    assert result == client.SendResult(
        True, "", TICKET, REQUEST_KEY, "created")
    url, headers, body, timeout = poster.calls[0]
    assert url == "https://fixer.example.test/fixer/business-ticket"
    assert headers == {
        "X-Fixer-Ops-Secret": "shared", "Content-Type": "application/json",
        "Accept": "application/json"}
    assert json.loads(body) == {"event": {
        "schema_version": 1,
        "contract_version": "echo-business-evidence-v1",
        "submission_key": SUBMISSION,
        "client_id": CLIENT,
        "source": "ops_fix",
        "check_id": "calendar_row_status",
        "params": {"row_id": "calendar_row_123", "expected_status": "published"},
    }}
    assert timeout == 3


def test_media_incident_resolves_exact_portal_client_and_is_uuid5_idempotent():
    bus = MappingBus()
    values = dict(
        gym_key="crossfitchateau813e78", folder_id="Drive_Folder-123",
        incident_kind="existing_source_sync_queue_failed", source_id="source_123",
        prior_sync_status="failed",
        prior_sync_requested_at="2026-09-19T15:04:05+00:00", bus=bus)
    first = client.media_incident_event(**values)
    second = client.media_incident_event(**values)
    assert first == second
    assert first["client_id"] == CLIENT
    assert first["params"] == {"folder_id": "Drive_Folder-123"}
    assert first["check_id"] == "media_source_active"
    assert bus.calls == ["crossfitchateau813e78", "crossfitchateau813e78"]

    changed = client.media_incident_event(**{
        **values, "prior_sync_requested_at": "2026-09-19T16:00:00+00:00"})
    assert changed["submission_key"] != first["submission_key"]


def test_production_mapping_is_bounded_and_requires_both_exact_directions(monkeypatch):
    monkeypatch.setattr("agent.config.supabase_url", lambda: "https://db.example.test")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "service-secret")
    calls = []

    def get(url, *, params, headers, timeout):
        calls.append((url, params, headers, timeout))
        if "echo_account_key" in params:
            rows = [{"gym_id": CLIENT,
                     "echo_account_key": "crossfitchateau813e78"}]
        else:
            rows = [{"gym_id": CLIENT,
                     "echo_account_key": "crossfitchateau813e78"}]
        return SimpleNamespace(status_code=200, json=lambda: rows)

    assert client.resolve_portal_client_id(
        "crossfitchateau813e78", get=get) == CLIENT
    assert len(calls) == 2
    assert all(call[3] == 2 for call in calls)
    assert all(call[2]["Authorization"] == "Bearer service-secret" for call in calls)

    def ambiguous(url, *, params, headers, timeout):
        rows = [{"gym_id": CLIENT, "echo_account_key": "crossfitchateau813e78"},
                {"gym_id": "44444444-4444-4444-8444-444444444444",
                 "echo_account_key": "crossfitchateau813e78"}]
        return SimpleNamespace(status_code=200, json=lambda: rows)

    with pytest.raises(client.BusinessSeedError,
                       match="portal client identity unconfirmed"):
        client.resolve_portal_client_id("crossfitchateau813e78", get=ambiguous)


@pytest.mark.parametrize("mapped", [None, "", "not-a-uuid"])
def test_media_incident_refuses_missing_ambiguous_or_malformed_tenant(mapped):
    with pytest.raises(client.BusinessSeedError,
                       match="portal client identity unconfirmed"):
        client.media_incident_event(
            gym_key="crossfitchateau813e78", folder_id="DriveFolder123",
            incident_kind="existing_source_sync_queue_failed",
            bus=MappingBus(mapped))


def test_media_event_rejects_hostile_identity_fields_before_network():
    with pytest.raises(client.BusinessSeedError, match="gym key"):
        client.media_incident_event(
            gym_key="other tenant/../../", folder_id="DriveFolder123",
            incident_kind="existing_source_sync_queue_failed", bus=MappingBus())
    with pytest.raises(client.BusinessSeedError, match="incident kind"):
        client.media_incident_submission_key(
            gym_key="chateau123", folder_id="DriveFolder123",
            incident_kind="client said it is broken")
    with pytest.raises(client.BusinessSeedError, match="incident identity"):
        client.media_incident_submission_key(
            gym_key="chateau123", folder_id="DriveFolder123",
            incident_kind="source_persist_unconfirmed",
            source_id="source id from prose")


def test_sender_refuses_raw_ticket_text_and_unknown_fields_without_network():
    event = client.media_source_seed(
        submission_key=SUBMISSION, client_id=CLIENT,
        folder_id="DriveFolder123")
    event["raw_text"] = "the folder in my Slack message"
    poster = Poster()
    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=poster) == client.SendResult(False, "invalid_seed")
    assert poster.calls == []


def test_sender_accepts_only_matching_201_created_or_200_existing_receipts():
    event = client.media_source_seed(
        submission_key=SUBMISSION, client_id=CLIENT,
        folder_id="DriveFolder123")
    existing = Poster(status=200, value={
        "ok": True, "outcome": "existing", "ticket_id": TICKET,
        "request_key": REQUEST_KEY})
    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=existing) == client.SendResult(
                           True, "", TICKET, REQUEST_KEY, "existing")
    mismatch = Poster(status=200)
    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=mismatch) == client.SendResult(False, "invalid_response")


def test_sender_fails_closed_for_redirect_rejection_and_unconfirmed_response():
    event = client.media_source_seed(
        submission_key=SUBMISSION, client_id=CLIENT,
        folder_id="DriveFolder123")
    assert client.send(event, base_url="https://fixer.example.test", secret="") \
        == client.SendResult(False, "unavailable")
    assert client.send(event, base_url="http://fixer.example.test", secret="shared") \
        == client.SendResult(False, "unavailable")
    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=Poster(status=307)) == client.SendResult(False, "unavailable")
    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=Poster(status=409)) == client.SendResult(False, "rejected")
    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=Poster(value={"ok": True, "request_key": "short"})) \
        == client.SendResult(False, "invalid_response")


def test_sender_has_bounded_timeout_and_transport_failure_is_never_success():
    event = client.media_source_seed(
        submission_key=SUBMISSION, client_id=CLIENT,
        folder_id="DriveFolder123")

    def broken(*args, **kwargs):
        raise TimeoutError("provider unavailable")

    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=broken, timeout=5) == client.SendResult(False, "unavailable")
    assert client.send(event, base_url="https://fixer.example.test", secret="shared",
                       post=broken, timeout=16) == client.SendResult(False, "invalid_timeout")
