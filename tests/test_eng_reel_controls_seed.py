"""Adversarial contract tests for the one-ticket producer; all transport is fake."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from agent import eng_reel_controls_seed as seed
from agent.slack_convo.outbox import _current_fixer_request_key

NOW = datetime(2026, 10, 10, 18, 0, 0, 123456, tzinfo=timezone.utc)
JOB = {"request_id": seed.REQUEST_ID, "status": "exhausted",
       "reason": seed.PORTRAIT_HOLD_REASON, "next_attempt_at": None}


def ticket_row(**overrides):
    return {"id": seed.TICKET_ID, "product": "echo", "source": "website_tab",
            "classification": "code_fix", "status": "merged", "request_version": 1,
            "client_id": seed.CLIENT_ID, "slack_channel_id": "C123",
            "slack_thread_ts": "1700000000.000001", "raw_text": "reel controls broken",
            "created_at": "2026-10-09T12:00:00+00:00", "verification_before": {},
            **overrides}


class FakeBus:
    def __init__(self, ticket=None):
        self.row = ticket or ticket_row()
        self.ticket_reads = 0
        self.rows = [{"id": "m1", "direction": "inbound", "author_type": "client",
                      "body": "reel controls broken", "created_at": "2026-10-09T12:00:00Z",
                      "attachments": {}}]

    def ticket(self, ticket_id):
        assert ticket_id == seed.TICKET_ID
        self.ticket_reads += 1
        return copy.deepcopy(self.row)

    def messages(self, ticket_id, limit=40):
        return copy.deepcopy(self.rows)

    def inbound_count(self, ticket_id):
        return len(self.rows)


def gym_status_ok(gym):
    assert gym == "eng"
    return {"ok": True, "gym": "eng", "status": "exhausted", "exhausted_count": 1,
            "jobs": [copy.deepcopy(JOB)]}


def run_kwargs(bus, **overrides):
    return {"bus": bus, "client_resolver": lambda gym: seed.CLIENT_ID,
            "gym_status": gym_status_ok, "secret": "test-secret", "now": NOW, **overrides}


def response(status, value):
    return SimpleNamespace(status_code=status, content=json.dumps(value).encode())


def binder(bus):
    def post(url, *, headers, body, timeout):
        assert url == seed.DEFAULT_BASE_URL + seed.SEED_PATH
        assert headers["X-Fixer-Ops-Secret"] == "test-secret"
        envelope = json.loads(body)
        assert set(envelope) == {"seed"}
        transport = envelope["seed"]
        key = _current_fixer_request_key(bus, bus.row)
        assert transport == seed.build_plan(NOW, expected_request_key=key)
        # Actual Scout binder shape, NOT a copy of the transport seed.
        bus.row["verification_before"] = {"fixer": {"business_check": {
            "schema_version": 1, "contract_version": "echo-business-evidence-v1",
            "ticket_id": seed.TICKET_ID, "client_id": seed.CLIENT_ID,
            "request_key": key, "check_id": "automatic_reel_controls_repaired",
            "params": {"request_id": seed.REQUEST_ID}}}}
        return response(200, {"ok": True, "request_key": key})
    return post


def test_dry_run_exact_scout_seed_and_no_post():
    result = seed.run(**run_kwargs(FakeBus(), post=lambda *a, **k: pytest.fail("POST")))
    assert result["seed"] == {"expected_request_key": result["request_key"], "schema_version": 1, "contract_version": "echo-business-evidence-v1",
                              "ticket_id": seed.TICKET_ID, "client_id": seed.CLIENT_ID,
                              "source": "website_tab", "issued_at": "2026-10-10T18:00:00.123Z",
                              "check_id": "automatic_reel_controls_repaired",
                              "params": {"request_id": seed.REQUEST_ID}}
    assert set(result["plan"]) == seed._PLAN_KEYS
    assert "issued_at" not in result["plan"] and "source" not in result["plan"]


def test_utc_millisecond_clock_and_naive_refusal():
    local = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert seed.build_plan(local, expected_request_key="a" * 64) == seed.build_plan(NOW, expected_request_key="a" * 64)
    with pytest.raises(seed.ReelSeedError):
        seed.build_plan(NOW.replace(tzinfo=None), expected_request_key="a" * 64)


def test_write_actual_scout_contract_and_readback():
    bus = FakeBus()
    result = seed.run(**run_kwargs(bus, write=True, post=binder(bus)))
    assert result["persisted"] is True and bus.ticket_reads == 3
    assert result["plan"] == bus.row["verification_before"]["fixer"]["business_check"]
    # An existing matching durable plan remains valid, with a fresh issued_at.
    assert seed.run(**run_kwargs(bus))["plan"] == result["plan"]


@pytest.mark.parametrize("overrides", [
    {"status": "fixing"}, {"source": "slack_conversation"}, {"classification": "question"},
    {"request_version": 2}, {"request_version": True}, {"product": "portal"},
    {"client_id": "foreign"}, {"id": "foreign"},
])
def test_ticket_identity_refusal(overrides):
    with pytest.raises(seed.ReelSeedError):
        seed.run(**run_kwargs(FakeBus(ticket_row(**overrides))))


@pytest.mark.parametrize("status", [
    {"ok": False}, {"ok": True, "gym": "foreign", "jobs": [JOB]},
    {"ok": True, "gym": "eng", "jobs": []},
    {"ok": True, "gym": "eng", "jobs": [JOB, JOB]},
    {"ok": True, "gym": "eng", "jobs": [{**JOB, "request_id": "foreign"}]},
    {"ok": True, "gym": "eng", "jobs": [{**JOB, "status": "held"}]},
    {"ok": True, "gym": "eng", "jobs": [{**JOB, "status": "running"}]},
    {"ok": True, "gym": "eng", "jobs": [{**JOB, "next_attempt_at": "tomorrow"}]},
    {"ok": True, "gym": "eng", "jobs": [{**JOB, "reason": "any portrait error"}]},
])
def test_worker_state_refusal(status):
    with pytest.raises(seed.ReelSeedError):
        seed.run(**run_kwargs(FakeBus(), gym_status=lambda gym: status))


def test_exhausted_worker_allows_only_elapsed_legacy_retry_timestamp():
    elapsed = datetime.now(timezone.utc).timestamp() - 60
    status = {"ok": True, "gym": "eng", "jobs": [{**JOB, "next_attempt_at": elapsed}]}
    assert seed._validate_worker_state(status)["request_id"] == seed.REQUEST_ID
    for value in (datetime.now(timezone.utc).timestamp() + 60, float("nan"), -1, True):
        status["jobs"][0]["next_attempt_at"] = value
        with pytest.raises(seed.ReelSeedError, match="original_job_not_terminal_hold"):
            seed._validate_worker_state(status)


def test_default_worker_reader_refuses_nondurable_store(monkeypatch):
    from agent import auto_reels
    monkeypatch.setattr(auto_reels.db, "kv_is_durable", lambda: False)
    with pytest.raises(seed.ReelSeedError, match="worker_status_unavailable"):
        seed.run(**run_kwargs(FakeBus(), gym_status=None))


def test_foreign_tenant_and_incomplete_request_reads_refused():
    with pytest.raises(seed.ReelSeedError, match="tenant_mismatch"):
        seed.run(**run_kwargs(FakeBus(), client_resolver=lambda gym: "99999999-9999-4999-8999-999999999999"))
    bus = FakeBus()
    bus.inbound_count = lambda ticket_id: 2
    with pytest.raises(seed.ReelSeedError, match="current_request_unavailable"):
        seed.run(**run_kwargs(bus))


@pytest.mark.parametrize("existing", ["bad", {}, {"source": "website_tab"}])
def test_malformed_existing_plan_refused(existing):
    bus = FakeBus(ticket_row(verification_before={"fixer": {"business_check": existing}}))
    with pytest.raises(seed.ReelSeedError, match="different_business_plan"):
        seed.run(**run_kwargs(bus))


def test_stale_existing_plan_refused():
    bus = FakeBus()
    bus.row["verification_before"] = {"fixer": {"business_check": seed._bound_plan(seed.build_plan(NOW, expected_request_key="a" * 64), "a" * 64)}}
    with pytest.raises(seed.ReelSeedError, match="different_business_plan"):
        seed.run(**run_kwargs(bus))


@pytest.mark.parametrize("origin", ["http://wrangler-production.up.railway.app", "https://evil.example",
    seed.DEFAULT_BASE_URL + "/evil", seed.DEFAULT_BASE_URL + "?x=1",
    "https://wrangler-production.up.railway.app@evil.example", seed.DEFAULT_BASE_URL + ":443"])
def test_fixed_origin_refuses_secret_exfiltration(origin):
    with pytest.raises(seed.ReelSeedError, match="fixer_service_unavailable"):
        seed.run(**run_kwargs(FakeBus(), write=True, base_url=origin,
                             post=lambda *a, **k: pytest.fail("secret sent")))


@pytest.mark.parametrize("status,value,reason", [
    (200, {"ok": True, "request_key": "a" * 64}, "binder_request_mismatch"),
    (200, {"ok": True, "plan": {}}, "invalid_response"),
    (200, {"ok": False}, "invalid_response"),
    (201, {}, "fixer_service_unavailable"), (302, {}, "fixer_service_unavailable"),
    (403, {}, "seed_rejected"),
])
def test_http_response_refusal(status, value, reason):
    with pytest.raises(seed.ReelSeedError, match=reason):
        seed.run(**run_kwargs(FakeBus(), write=True, post=lambda *a, **k: response(status, value)))


def test_missing_secret_and_transport_exception_never_leak(capsys, monkeypatch):
    with pytest.raises(seed.ReelSeedError, match="missing_secret"):
        seed.run(**run_kwargs(FakeBus(), write=True, secret="", post=lambda *a, **k: pytest.fail("POST")))
    def fail(*a, **k):
        raise RuntimeError("secret test-secret")
    with pytest.raises(seed.ReelSeedError, match="fixer_service_unavailable"):
        seed.run(**run_kwargs(FakeBus(), write=True, post=fail))
    monkeypatch.setattr(seed, "run", lambda **k: (_ for _ in ()).throw(RuntimeError("test-secret")))
    assert seed.main(["--write"]) == 2
    assert "test-secret" not in capsys.readouterr().out


def test_request_changed_before_post_refused():
    bus = FakeBus()
    def status(gym):
        bus.rows[0]["body"] = "new request"
        return gym_status_ok(gym)
    with pytest.raises(seed.ReelSeedError, match="ticket_changed_during_write"):
        seed.run(**run_kwargs(bus, write=True, gym_status=status,
                             post=lambda *a, **k: pytest.fail("POST")))


@pytest.mark.parametrize("field", ["body", "slack_thread_ts", "bot_identity"])
def test_changed_binding_after_post_refused(field):
    bus = FakeBus()
    real_post = binder(bus)
    def post(*a, **k):
        result = real_post(*a, **k)
        if field == "body":
            bus.rows[0][field] = "new request"
        else:
            bus.row[field] = "changed"
        return result
    with pytest.raises(seed.ReelSeedError, match="ticket_changed_during_write"):
        seed.run(**run_kwargs(bus, write=True, post=post))


def test_ack_without_exact_persisted_plan_refused():
    bus = FakeBus()
    def post(*a, **k):
        return response(200, {"ok": True, "request_key": _current_fixer_request_key(bus, bus.row)})
    with pytest.raises(seed.ReelSeedError, match="persisted_plan_mismatch"):
        seed.run(**run_kwargs(bus, write=True, post=post))


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b"{}", b"[]", b"x" * (seed.MAX_RESPONSE_BYTES + 1)])
def test_malformed_or_oversized_http_refused(raw):
    with pytest.raises(seed.ReelSeedError, match="invalid_response"):
        seed.run(**run_kwargs(FakeBus(), write=True,
                             post=lambda *a, **k: SimpleNamespace(status_code=200, content=raw)))


@pytest.mark.parametrize("field,value", [
    ("source", "website_tab"), ("issued_at", "2026-10-10T18:00:00.123Z"),
    ("request_key", "a" * 64), ("client_id", "foreign"),
    ("params", {"request_id": "foreign"}),
])
def test_readback_refuses_extra_or_foreign_binding(field, value):
    bus = FakeBus()
    real_post = binder(bus)
    def post(*a, **k):
        result = real_post(*a, **k)
        bus.row["verification_before"]["fixer"]["business_check"][field] = value
        return result
    with pytest.raises(seed.ReelSeedError, match="persisted_plan_mismatch"):
        seed.run(**run_kwargs(bus, write=True, post=post))


def test_scope_is_exact_production_ticket_tenant_and_original_job():
    assert seed.TICKET_ID == "554ac760-b0b3-413a-919f-4e13cff6d3fc"
    assert seed.CLIENT_ID == "6ee04ee4-13a5-47db-8416-7b8ee3e61ab8"
    assert seed.REQUEST_ID == "b185a4e4-425b-5bc3-8ec6-7c9145426918"
    assert seed.GYM_KEY == "eng"
    assert JOB["status"] == "exhausted"
    assert JOB["reason"] == "Automatic reel held: The complete athlete will not fit a portrait crop"


@pytest.mark.parametrize("key", [None, 123, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"])
def test_expected_request_key_exact_format_required(key):
    with pytest.raises(seed.ReelSeedError, match="current_request_unavailable"):
        seed.build_plan(NOW, expected_request_key=key)


@pytest.mark.parametrize("mutation", ["missing", "extra", "foreign"])
def test_transport_seed_refuses_wrong_expected_binding(mutation):
    key = "a" * 64
    transport = seed.build_plan(NOW, expected_request_key=key)
    if mutation == "missing":
        del transport["expected_request_key"]
    elif mutation == "extra":
        transport["extra"] = True
    else:
        transport["expected_request_key"] = "b" * 64
    with pytest.raises(seed.ReelSeedError, match="invalid_seed"):
        seed.send_plan(transport, request_key=key, secret="test-secret",
                       post=lambda *a, **k: pytest.fail("POST"))


def test_request_racing_precheck_is_refused_by_binder_without_metadata():
    bus = FakeBus()
    posted = []
    def post(url, *, headers, body, timeout):
        incoming = json.loads(body)["seed"]
        posted.append(incoming)
        # Request arrives after Echo's last read, before Scout's binder read.
        bus.rows[0]["body"] = "Use a different original reel"
        current_key = _current_fixer_request_key(bus, bus.row)
        assert incoming["expected_request_key"] != current_key
        return response(409, {"ok": False, "reason": "business_check_seed_request_mismatch"})
    with pytest.raises(seed.ReelSeedError, match="seed_rejected"):
        seed.run(**run_kwargs(bus, write=True, post=post))
    assert len(posted) == 1
    assert bus.row["verification_before"] == {}
