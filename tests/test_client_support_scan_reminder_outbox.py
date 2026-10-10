"""Offline dispatch tests for internal scan reminders, no external calls."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.jobs import client_support_scan_reminder as reminder
from agent.slack_convo import outbox

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
IDENTITY = SimpleNamespace(name="echo", fixer_channel=lambda: "CINTERNAL",
                           bot_user_id=lambda: "UBOT")


class Bus:
    def __init__(self):
        self.ticket_row = {"id": "ticket-1", "request_version": 2,
                           "client_id": "gym-1", "source": "website_tab",
                           "product": "echo", "bot_identity": "echo",
                           "status": "working", "is_test": False}
        self.row = reminder._notice_identity({
            **self.ticket_row, "ticket_id": "ticket-1",
            "reason": "client_request_open"}, NOW)
        self.gym = {"id": "gym-1", "slug": "real-gym"}
        self.messages = []
        self.on_claim = None
        self.broken = False

    def _get(self, table, params):
        if self.broken:
            raise TimeoutError()
        if table == "support_tickets":
            assert params["id"] == "eq.ticket-1"
            assert "body" not in params["select"]
            return [deepcopy(self.ticket_row)]
        if table == "gyms":
            assert params["id"] == "eq.gym-1"
            return [deepcopy(self.gym)]
        assert table == "support_messages"
        assert params["ticket_id"] == "eq.ticket-1"
        assert "body" not in params["select"] and "raw_text" not in params["select"]
        return deepcopy(self.messages)

    def claim_message(self, mid):
        assert mid == self.row["id"]
        self.row["delivery_status"] = "posting"
        if self.on_claim:
            self.on_claim(self)
        return deepcopy(self.row)

    def mark_message(self, mid, status, meta_update=None, **kwargs):
        assert mid == self.row["id"]
        self.row["delivery_status"] = status
        self.row["attachments"].update(meta_update or {})
        if "slack_ts" in kwargs:
            self.row["slack_ts"] = kwargs["slack_ts"]

    def message(self, mid):
        return deepcopy(self.row)

    def record_outbound(self, **kwargs):
        pytest.fail("stale reminders must not create escalations")


def dispatch(monkeypatch, bus):
    monkeypatch.setenv("AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED", "true")
    calls = []
    summary = {"posted": 0, "skipped": 0, "suppressed": 0}
    outbox._dispatch_one(bus, lambda *a, **kw: calls.append((a, kw)) or str(datetime.now(timezone.utc).timestamp()),
                         deepcopy(bus.row), identity=IDENTITY, log=lambda *a: None,
                         summary=summary, now=NOW, readback=lambda channel, **kw: {
                             "ok": True, "channel": channel, "messages": [{
                                 "ts": kw["ts"], "user": "UBOT", "text": bus.row["body"]}]})
    return calls, summary


def test_current_reminder_posts_informational_only(monkeypatch):
    bus = Bus()
    calls, summary = dispatch(monkeypatch, bus)
    assert summary["posted"] == 1 and bus.row["delivery_status"] == "posted"
    assert calls[0][0][0] == "CINTERNAL"
    assert all(b["type"] == "section" for b in calls[0][1]["blocks"])
    assert bus.ticket_row["status"] == "working"


def test_missing_configured_sender_uses_authenticated_bot_identity(monkeypatch):
    from agent import slack_surface

    calls = []
    class AuthPoster:
        def __init__(self, *, token):
            assert token == "scout-token"
        def _send(self, url, payload):
            calls.append((url, payload))
            return {"ok": True, "user_id": "WSCOUT", "bot_id": "BSCOUT"}
    monkeypatch.setattr(slack_surface, "SlackPoster", AuthPoster)
    identity = SimpleNamespace(bot_user_id=lambda: "", bot_token_env="SCOUT_TOKEN",
                               env=lambda name: "scout-token")
    assert outbox._scan_reminder_sender(identity) == "WSCOUT"
    assert calls == [("https://slack.com/api/auth.test", {})]


def test_missing_sender_auth_failure_refuses_post(monkeypatch):
    from agent import slack_surface

    class BadAuthPoster:
        def __init__(self, *, token):
            pass
        def _send(self, url, payload):
            return {"ok": False, "error": "invalid_auth"}
    monkeypatch.setattr(slack_surface, "SlackPoster", BadAuthPoster)
    identity = SimpleNamespace(bot_user_id=lambda: "", bot_token_env="SCOUT_TOKEN",
                               env=lambda name: "scout-token")
    assert outbox._scan_reminder_sender(identity) is None


@pytest.mark.parametrize("status,reason", [
    ("approved", "client_approved_pending_action"),
    ("failed", "client_sees_refused"),
])
def test_nonworking_client_exception_revalidated_before_internal_post(monkeypatch,
                                                                       status, reason):
    bus = Bus()
    bus.ticket_row["status"] = status
    bus.row = reminder._notice_identity({**bus.ticket_row,
                                         "ticket_id": "ticket-1",
                                         "reason": reason}, NOW)
    calls, summary = dispatch(monkeypatch, bus)
    assert summary["posted"] == 1
    assert len(calls) == 1 and calls[0][0][0] == "CINTERNAL"
    assert bus.row["delivery_status"] == "posted"


@pytest.mark.parametrize("field,value", [
    ("request_version", 3), ("request_version", True), ("client_id", "other"),
    ("bot_identity", "scout"), ("source", "ops_fix"), ("product", "portal"),
    ("status", "resolved")])
def test_changed_identity_or_status_suppresses(monkeypatch, field, value):
    bus = Bus()
    bus.ticket_row[field] = value
    calls, summary = dispatch(monkeypatch, bus)
    assert not calls and summary["suppressed"] == 1
    assert bus.row["delivery_status"] == "suppressed"


def test_changed_after_claim_suppresses(monkeypatch):
    bus = Bus()
    bus.on_claim = lambda b: b.ticket_row.update(request_version=3)
    calls, summary = dispatch(monkeypatch, bus)
    assert not calls and summary["suppressed"] == 1


@pytest.mark.parametrize("after_claim", [False, True])
def test_transient_failure_remains_retryable(monkeypatch, after_claim):
    bus = Bus()
    if after_claim:
        bus.on_claim = lambda b: setattr(b, "broken", True)
    else:
        bus.broken = True
    calls, summary = dispatch(monkeypatch, bus)
    assert not calls and summary["skipped"] == 1
    assert bus.row["delivery_status"] == "ready"


@pytest.mark.parametrize("slug", ["demo", "zz-test-gym", ""])
def test_unverified_or_test_gym_suppresses(monkeypatch, slug):
    bus = Bus()
    bus.gym["slug"] = slug
    calls, summary = dispatch(monkeypatch, bus)
    assert not calls and summary["suppressed"] == 1


@pytest.mark.parametrize("mutation", [
    lambda b: b.row["attachments"].update(contract="wrong"),
    lambda b: b.row["attachments"].update(kind="answer"),
    lambda b: b.row.update(body="forged body"),
    lambda b: b.row.update(id="forged-id"),
    lambda b: b.row["attachments"].update(fixer=True),
])
def test_malformed_contract_suppresses_without_escalation(monkeypatch, mutation):
    bus = Bus()
    mutation(bus)
    calls, summary = dispatch(monkeypatch, bus)
    assert not calls and summary["suppressed"] == 1


def test_disabled_dispatch_keeps_row_ready(monkeypatch):
    monkeypatch.delenv("AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED", raising=False)
    bus = Bus()
    calls = []
    summary = {"skipped": 0}
    outbox._dispatch_scan_reminder(bus, lambda *a, **kw: calls.append(a),
                                  bus.row, IDENTITY, lambda *a: None, summary)
    assert not calls and bus.row["delivery_status"] == "ready"


def test_blocks_never_offer_resolution_even_for_malformed_reminder():
    bus = Bus()
    bus.row["attachments"]["contract"] = "wrong"
    blocks = outbox.escalation_blocks(bus.row, {**bus.ticket_row,
                                              "slack_channel_id": "CCLIENT"})
    assert all(b["type"] == "section" for b in blocks)


def test_prior_utc_day_suppresses(monkeypatch):
    bus = Bus()
    bus.row = reminder._notice_identity({
        **bus.ticket_row, "ticket_id": "ticket-1", "reason": "client_request_open"},
        datetime(2026, 10, 8, tzinfo=timezone.utc))
    calls, summary = dispatch(monkeypatch, bus)
    assert not calls and summary["suppressed"] == 1


def attempt(monkeypatch, bus, post, readback=None):
    monkeypatch.setenv("AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED", "true")
    summary = {"posted": 0, "skipped": 0, "suppressed": 0, "held": 0}
    outbox._dispatch_scan_reminder(bus, post, deepcopy(bus.row), IDENTITY,
                                  lambda *a: None, summary, now=NOW,
                                  readback=readback or (lambda channel, **kw: {
                                      "ok": True, "channel": channel, "messages": [{
                                          "ts": kw["ts"], "user": "UBOT",
                                          "text": bus.row["body"]}]}))
    return summary


def test_accepted_post_then_mark_failure_never_replays(monkeypatch):
    class MarkFailure(Bus):
        def mark_message(self, mid, status, **kw):
            if kw.get("slack_ts"):
                raise TimeoutError()
            return super().mark_message(mid, status, **kw)
    bus = MarkFailure()
    calls = []
    summary = attempt(monkeypatch, bus, lambda *a, **kw: calls.append(a) or
                      str(datetime.now(timezone.utc).timestamp()))
    assert len(calls) == 1 and summary["held"] == 1
    attempt(monkeypatch, bus, lambda *a, **kw: calls.append(a))
    assert len(calls) == 1 and bus.row["delivery_status"] == "held"


def test_post_timeout_after_acceptance_quarantines(monkeypatch):
    bus = Bus()
    calls = []
    def post(*args, **kwargs):
        calls.append(args)
        raise TimeoutError()
    summary = attempt(monkeypatch, bus, post)
    assert summary["held"] == 1 and len(calls) == 1
    attempt(monkeypatch, bus, post)
    assert len(calls) == 1


def test_confirmed_prepost_failure_safely_requeues(monkeypatch):
    class Once(Bus):
        def message(self, mid):
            if self.row["attachments"].get("scan_reminder_slack_intent") and not getattr(self, "raised", False):
                self.raised = True
                raise TimeoutError()
            return super().message(mid)
    bus = Once()
    calls = []
    summary = attempt(monkeypatch, bus, lambda *a, **kw: calls.append(a))
    assert not calls and summary["skipped"] == 1
    assert bus.row["delivery_status"] == "ready"
    assert bus.row["attachments"]["scan_reminder_no_post"] is True
    summary = attempt(monkeypatch, bus, lambda *a, **kw: calls.append(a) or
                      str(datetime.now(timezone.utc).timestamp()))
    assert len(calls) == 1 and summary["posted"] == 1


def test_unknown_recovery_readback_never_requeues(monkeypatch):
    bus = Bus()
    attempt(monkeypatch, bus, lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    summary = {"skipped": 0, "held": 0}
    outbox._recover_scan_reminder(bus, bus.row, IDENTITY, None, lambda *a: None, summary)
    assert bus.row["delivery_status"] == "held"


def test_persisted_timestamp_recovers_exact_receipt(monkeypatch):
    class FailPosted(Bus):
        def mark_message(self, mid, status, **kw):
            if status == "posted" and not getattr(self, "failed_once", False):
                self.failed_once = True
                raise TimeoutError()
            return super().mark_message(mid, status, **kw)
    bus = FailPosted()
    calls = []
    attempt(monkeypatch, bus, lambda *a, **kw: calls.append(a) or
            str(datetime.now(timezone.utc).timestamp()))
    assert bus.row["delivery_status"] == "held"
    summary = attempt(monkeypatch, bus, lambda *a, **kw: calls.append(a))
    assert len(calls) == 1 and summary["reconciled_posted"] == 1
    assert bus.row["delivery_status"] == "posted"


def test_generic_stale_recovery_never_requeues_attempt(monkeypatch):
    bus = Bus()
    attempt(monkeypatch, bus, lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    bus.row['delivery_status'] = 'posting'
    bus.row['attachments']['claimed_at'] = '2026-10-08T00:00:00+00:00'
    bus.outbox = lambda *a, **kw: [deepcopy(bus.row)]
    summary = {'skipped': 0}
    outbox._recover_stale_claims(bus, IDENTITY, lambda *a: None,
                                now=NOW, readback=None, summary=summary)
    assert bus.row['delivery_status'] == 'held'
    assert summary.get('requeued_ready', 0) == 0


@pytest.mark.parametrize("age,expected", [(29, False), (30, True), (90, True)])
def test_new_ticket_reminder_requires_age(monkeypatch, age, expected):
    from datetime import timedelta
    bus = Bus()
    bus.ticket_row.update(status="new", created_at=(NOW-timedelta(minutes=age)).isoformat())
    entry = {**bus.ticket_row, "ticket_id": "ticket-1", "reason": "client_request_open"}
    bus.row = reminder._notice_identity(entry, NOW)
    calls, summary = dispatch(monkeypatch, bus)
    assert bool(calls) is expected


def test_new_ticket_creation_drift_suppresses(monkeypatch):
    bus = Bus()
    bus.ticket_row.update(status="new", created_at="2026-10-08T20:00:00+00:00")
    bus.row = reminder._notice_identity({**bus.ticket_row, "ticket_id": "ticket-1",
                                        "reason": "client_request_open"}, NOW)
    bus.ticket_row['created_at'] = "2026-10-08T21:00:00+00:00"
    calls, summary = dispatch(monkeypatch, bus)
    assert not calls and summary['suppressed'] == 1


def test_held_recovery_queries_only_reminders_with_keyset(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED", "true")
    bus = Bus()
    calls = []
    def get(table, params):
        calls.append((table, params))
        return []
    bus._get = get
    outbox._reconcile_held_scan_reminders(bus, IDENTITY, None, lambda *a: None, {})
    assert calls[0][1]['attachments->>surface'] == 'eq.client_support_scan_reminder'
    assert calls[0][1]['attachments->>identity'] == 'eq.echo'
    assert calls[0][1]['delivery_status'] == 'eq.held'
    assert calls[0][1]['order'] == 'created_at.asc,id.asc'


def test_held_recovery_does_not_query_bus_while_lane_disabled(monkeypatch):
    monkeypatch.delenv("AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED", raising=False)
    bus = Bus()
    bus._get = lambda *_: pytest.fail("default-off lane must not scan held rows")
    outbox._reconcile_held_scan_reminders(bus, IDENTITY, None, lambda *a: None, {})
