"""Authenticated, read-only transport for fixer_business_evidence.observe."""
import json
import hashlib
from datetime import datetime, timezone

import pytest

from agent import fixer_ops as FO

SECRET = "observer-secret"
TICKET_ID = "11111111-1111-4111-8111-111111111111"
CLIENT_ID = "22222222-2222-4222-8222-222222222222"
ECHO_KEY = "chateau123"
MESSAGE = {"id": "message-1", "ticket_id": TICKET_ID,
           "created_at": "2026-09-19T11:00:00+00:00", "body": "Please fix it",
           "author_type": "client", "direction": "inbound", "attachments": {}}
REQUEST_KEY = hashlib.sha256(json.dumps(
    [[MESSAGE["id"], MESSAGE["created_at"], MESSAGE["body"]]],
    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
MERGED_SHA = "b" * 40
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setenv(FO.SECRET_ENV, SECRET)


def headers(secret=SECRET):
    return lambda key, default=None: {FO.HEADER: secret}.get(key, default)


def payload(**patch):
    value = {
        "schema_version": 1,
        "contract_version": FO.BUSINESS_EVIDENCE_CONTRACT,
        "ticket_id": TICKET_ID,
        "client_id": CLIENT_ID,
        "request_key": REQUEST_KEY,
        "merged_sha": MERGED_SHA,
        "check_id": "calendar_row_status",
        "params": {"row_id": "calendar_row_99", "expected_status": "published"},
    }
    value.update(patch)
    return value


_DEFAULT = object()


class Reader:
    def __init__(self, *, ticket=_DEFAULT, messages=None, calendar=None, fail=False):
        self.ticket = ticket if ticket is not _DEFAULT else {
            "id": TICKET_ID, "product": "echo", "client_id": CLIENT_ID}
        self.calendar = calendar if calendar is not None else [{
            "id": "calendar_row_99", "gym_id": ECHO_KEY, "status": "published"}]
        self.messages = messages if messages is not None else [dict(MESSAGE)]
        self.fail = fail
        self.calls = []
        self.writes = 0

    def __call__(self, table, params):
        self.calls.append((table, dict(params)))
        if self.fail:
            raise RuntimeError("database unavailable")
        if table == "support_tickets":
            return [] if self.ticket is None else [dict(self.ticket)]
        if table == "echo_intake_tokens":
            return [{"gym_id": CLIENT_ID, "echo_account_key": ECHO_KEY}]
        if table == "support_messages":
            return [dict(row) for row in self.messages]
        if table == "content_calendar":
            return [dict(row) for row in self.calendar]
        raise AssertionError(f"unexpected table {table}")


def post(value, reader, *, secret=SECRET, now=NOW):
    return FO.handle(
        "POST", FO.BUSINESS_EVIDENCE_PATH, headers(secret),
        json.dumps(value).encode(), deps={"business_evidence": {"read": reader}},
        now=now, log=lambda *_: None)


def test_success_returns_only_canonical_fresh_bound_record_and_performs_no_write():
    reader = Reader()
    status, body = post(payload(), reader)
    assert status == 200
    assert set(body) == {
        "schema_version", "source", "check_id", "gym_key", "request_key",
        "merged_sha", "captured_at", "outcome", "verified",
        "symptom_resolved", "evidence", "reason", "params"}
    assert body == {
        "schema_version": 1,
        "source": "independent_business_check",
        "check_id": "calendar_row_status",
        "gym_key": CLIENT_ID,
        "request_key": REQUEST_KEY,
        "merged_sha": MERGED_SHA,
        "captured_at": NOW.isoformat(),
        "outcome": "verified",
        "verified": True,
        "symptom_resolved": True,
        "evidence": "calendar_row:calendar_row_99:published",
        "reason": "",
        "params": {"row_id": "calendar_row_99", "expected_status": "published"},
    }
    assert [table for table, _ in reader.calls] == [
        "support_tickets", "support_messages", "echo_intake_tokens",
        "echo_intake_tokens", "content_calendar"]
    assert reader.writes == 0


def test_production_composition_uses_bounded_http_reader_without_injected_deps(
        monkeypatch):
    """The live route must compose from config plus HTTP, without a test-only bus."""
    reader = Reader()
    calls = []

    class Response:
        status_code = 200

        def __init__(self, rows):
            self._body = json.dumps(rows).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_content(self, _chunk_size):
            yield self._body

    def get(url, *, params, headers, timeout, stream, allow_redirects):
        table = url.rsplit("/", 1)[-1]
        calls.append((table, dict(params), dict(headers), timeout, stream,
                      allow_redirects))
        return Response(reader(table, params))

    monkeypatch.setattr("agent.config.supabase_url", lambda: "https://db.example")
    monkeypatch.setattr("agent.config.supabase_service_key", lambda: "service-key")
    monkeypatch.setattr("requests.get", get)

    status, body = FO.handle(
        "POST", FO.BUSINESS_EVIDENCE_PATH, headers(),
        json.dumps(payload()).encode(), deps={}, now=NOW, log=lambda *_: None)

    assert status == 200 and body["verified"] is True
    assert [table for table, *_ in calls] == [
        "support_tickets", "support_messages", "echo_intake_tokens",
        "echo_intake_tokens", "content_calendar"]
    assert all(call[2]["apikey"] == "service-key" for call in calls)
    assert all(call[4] is True and call[5] is False for call in calls)


def test_valid_negative_observation_is_canonical_200_never_coerced_to_success():
    reader = Reader(calendar=[{
        "id": "calendar_row_99", "gym_id": ECHO_KEY, "status": "approved"}])
    status, body = post(payload(), reader)
    assert status == 200
    assert body["outcome"] == "unverified"
    assert body["verified"] is False
    assert body["symptom_resolved"] is False
    assert body["reason"] == "status_mismatch"


@pytest.mark.parametrize("patch", [
    {"schema_version": 2},
    {"contract_version": "future"},
    {"ticket_id": "not-a-uuid"},
    {"client_id": "22222222-2222-4222-8222-22222222222A"},
    {"request_key": "a" * 63},
    {"merged_sha": "B" * 40},
    {"check_id": "run_anything"},
    {"params": {"row_id": "short", "expected_status": "published"}},
    {"params": {"row_id": "calendar_row_99", "expected_status": "published",
                "command": "run"}},
])
def test_strict_contract_refuses_bad_identity_unknown_checks_and_extra_params(patch):
    reader = Reader()
    status, body = post(payload(**patch), reader)
    assert status == 400 and body["error"] == "bad_request"
    assert reader.calls == []


def test_missing_or_extra_top_level_fields_are_refused_before_reads():
    reader = Reader()
    missing = payload()
    del missing["request_key"]
    extra = payload(extra="not allowed")
    for value in (missing, extra, [], "text"):
        status, body = post(value, reader)
        assert status == 400 and body["error"] == "bad_request"
    assert reader.calls == []


def test_ticket_and_client_binding_is_re_read_before_observer():
    cases = [
        (None, 404, "ticket_not_found"),
        ({"id": TICKET_ID, "product": "portal", "client_id": CLIENT_ID},
         409, "ticket_tenant_mismatch"),
        ({"id": TICKET_ID, "product": "echo",
          "client_id": "33333333-3333-4333-8333-333333333333"},
         409, "ticket_tenant_mismatch"),
    ]
    for ticket, expected_status, error in cases:
        reader = Reader(ticket=ticket)
        status, body = post(payload(), reader)
        assert (status, body["error"]) == (expected_status, error)
        assert [table for table, _ in reader.calls] == ["support_tickets"]


def test_current_request_sha_is_recomputed_and_stale_or_unreadable_threads_fail_closed():
    reader = Reader()
    status, body = post(payload(request_key="a" * 64), reader)
    assert (status, body["error"]) == (409, "request_identity_mismatch")
    assert [table for table, _ in reader.calls] == ["support_tickets", "support_messages"]

    unreadable = Reader(messages=[{**MESSAGE, "attachments": "not-an-object"}])
    status, body = post(payload(), unreadable)
    assert (status, body["error"]) == (503, "evidence_unavailable")

    truncated = Reader(messages=[dict(MESSAGE) for _ in range(1000)])
    status, body = post(payload(), truncated)
    assert (status, body["error"]) == (503, "evidence_unavailable")


def test_reader_failure_and_ambiguous_ticket_fail_closed():
    reader = Reader(fail=True)
    assert post(payload(), reader) == (503, {"error": "evidence_unavailable"})

    class Ambiguous(Reader):
        def __call__(self, table, params):
            rows = super().__call__(table, params)
            return rows * 2 if table == "support_tickets" else rows
    status, body = post(payload(), Ambiguous())
    assert (status, body["error"]) == (409, "ticket_identity_unconfirmed")


def test_existing_auth_and_method_gates_apply_before_observation():
    reader = Reader()
    status, body = post(payload(), reader, secret="wrong")
    assert (status, body["error"]) == (401, "unauthorized")
    assert reader.calls == []
    status, body = FO.handle(
        "GET", FO.BUSINESS_EVIDENCE_PATH, headers(), b"",
        deps={"business_evidence": {"read": reader}})
    assert (status, body["error"]) == (405, "method_not_allowed")
    assert reader.calls == []


@pytest.mark.parametrize("check_id,params,terminal_table", [
    ("forward_book_grade_at_least", {"min_total": 80}, "gym_social_grades"),
    ("media_source_active", {"folder_id": "drive_folder_123"}, "media_source"),
])
def test_other_registered_checks_reach_only_their_fixed_reader(check_id, params, terminal_table):
    class RegisteredReader(Reader):
        def __call__(self, table, query):
            if table == "gym_social_grades":
                self.calls.append((table, dict(query)))
                return [{"gym_id": ECHO_KEY, "total": 90,
                         "graded_at": NOW.isoformat()}]
            if table == "media_source":
                self.calls.append((table, dict(query)))
                return [{"id": "source-1", "gym_id": ECHO_KEY,
                         "folder_id": "drive_folder_123", "active": True,
                         "revoked_externally": False, "sync_status": "ready"}]
            return super().__call__(table, query)
    reader = RegisteredReader()
    status, body = post(payload(check_id=check_id, params=params), reader)
    assert status == 200 and body["verified"] is True
    assert [table for table, _ in reader.calls][-1] == terminal_table
