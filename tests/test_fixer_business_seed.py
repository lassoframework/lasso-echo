"""Trusted grade-drop seed -> service-authenticated FIXER bus coverage."""
import hashlib
import json
import os
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import fixer_business_seed as FBS, ops_alerts  # noqa: E402
from agent.jobs import grade_sweep  # noqa: E402
from agent.slack_convo.bus import Bus, BusError  # noqa: E402


CLIENT_ID = "22222222-2222-4222-8222-222222222222"


class MemoryBus(Bus):
    def __init__(self):
        self.rows = {}
        self.tokens = [{"gym_id": CLIENT_ID, "echo_account_key": "chateau123"}]

    def _get(self, table, params):
        if table == "echo_intake_tokens":
            key = str(params.get("echo_account_key") or "").removeprefix("eq.")
            return [deepcopy(row) for row in self.tokens
                    if row["echo_account_key"] == key]
        if table == "support_tickets":
            ticket_id = str(params.get("id") or "").removeprefix("eq.")
            return [deepcopy(self.rows[ticket_id])] if ticket_id in self.rows else []
        raise AssertionError(table)

    def _insert(self, table, row):
        assert table == "support_tickets"
        if row["id"] in self.rows:
            return None, True
        self.rows[row["id"]] = deepcopy(row)
        return deepcopy(row), False


def _seed(bus=None):
    return FBS.prepare_grade_drop_seed(
        gym_key="chateau123", min_total=82, observed_total=61,
        source_day="2026-09-19", bus=bus or MemoryBus())


def test_grade_drop_seed_uses_exact_portal_uuid_pre_drop_total_and_event_identity():
    seed = _seed()
    assert seed == {
        "schema_version": 1,
        "source": "echo.grade_sweep.forward_book_drop",
        "occurred_at": "2026-09-19T00:00:00+00:00",
        "gym_key": "chateau123",
        "client_id": CLIENT_ID,
        "previous_total": 82,
        "observed_total": 61,
        "source_event_id": seed["source_event_id"],
        "check_id": "forward_book_grade_at_least",
        "params": {"min_total": 82},
    }
    assert len(seed["source_event_id"]) == 64
    assert seed == _seed(), "the same producer event must have one stable identity"


def test_tenant_resolution_refuses_missing_ambiguous_and_non_uuid_mappings():
    for rows in (
        [],
        [{"gym_id": CLIENT_ID, "echo_account_key": "chateau123"},
         {"gym_id": "33333333-3333-4333-8333-333333333333",
          "echo_account_key": "chateau123"}],
        [{"gym_id": "chateau123", "echo_account_key": "chateau123"}],
    ):
        bus = MemoryBus()
        bus.tokens = rows
        with pytest.raises(FBS.SeedError, match="portal client identity unconfirmed"):
            FBS.resolve_portal_client_id("chateau123", bus=bus)


def test_materialized_ticket_matches_scout_request_key_contract_and_is_idempotent():
    bus = MemoryBus()
    seed = _seed(bus)
    text = "calendar grade DROPPED: chateau123 forward book went 82 -> 61 (D)"
    row = FBS.ticket_row(seed, text)
    identity = json.dumps(
        [row["id"], row["created_at"], row["raw_text"]],
        ensure_ascii=False, separators=(",", ":"))
    expected_key = hashlib.sha256(identity.encode()).hexdigest()
    plan = row["verification_before"]["fixer"]["business_check"]
    assert plan == {
        "schema_version": 1,
        "contract_version": "echo-business-evidence-v1",
        "ticket_id": row["id"],
        "client_id": CLIENT_ID,
        "request_key": expected_key,
        "check_id": "forward_book_grade_at_least",
        "params": {"min_total": 82},
    }
    first = FBS.persist(seed, text, bus=bus)
    second = FBS.persist(seed, text, bus=bus)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert len(bus.rows) == 1


def test_duplicate_primary_key_must_read_back_the_same_tenant_event_and_plan():
    bus = MemoryBus()
    seed = _seed(bus)
    text = "calendar grade DROPPED: chateau123 forward book went 82 -> 61 (D)"
    FBS.persist(seed, text, bus=bus)
    ticket_id = next(iter(bus.rows))
    bus.rows[ticket_id]["verification_before"]["fixer"]["business_check"]["params"] = {
        "min_total": 80}
    with pytest.raises(BusError, match="readback identity mismatch"):
        FBS.persist(seed, text, bus=bus)


class Poster:
    def __init__(self):
        self.notices = []
        self.chat_posts = []

    def post_notice(self, text):
        self.notices.append(text)
        return {"ok": True}

    def _chat_post(self, **kwargs):
        self.chat_posts.append(kwargs)
        return {"ok": True}


def test_alert_writes_structured_row_first_and_does_not_create_a_slack_prose_duplicate(
        monkeypatch):
    bus = MemoryBus()
    poster = Poster()
    monkeypatch.setattr(ops_alerts.config, "ops_fix_triage_enabled", lambda: True)
    monkeypatch.setattr(ops_alerts.config, "support_channel_id", lambda: "C_SUPPORT")
    result = ops_alerts.alert(
        "calendar grade DROPPED: chateau123 forward book went 82 -> 61 (D)",
        poster=poster, force=True, business_seed=_seed(bus), seed_bus=bus)
    assert result == {"ok": True}
    assert len(bus.rows) == 1
    assert len(poster.notices) == 1
    assert poster.chat_posts == [], "confirmed bus write replaces Slack-prose intake"


def test_seed_write_failure_keeps_the_existing_ops_fix_slack_fallback(monkeypatch):
    class BrokenBus(MemoryBus):
        def record_seeded_ops_fix(self, row):
            raise BusError(503, "store unavailable")

    poster = Poster()
    monkeypatch.setattr(ops_alerts.config, "ops_fix_triage_enabled", lambda: True)
    monkeypatch.setattr(ops_alerts.config, "support_channel_id", lambda: "C_SUPPORT")
    ops_alerts.alert(
        "calendar grade DROPPED: chateau123 forward book went 82 -> 61 (D)",
        poster=poster, force=True, business_seed=_seed(), seed_bus=BrokenBus())
    assert len(poster.notices) == 1
    assert len(poster.chat_posts) == 1
    assert poster.chat_posts[0]["text"].startswith("OPS-FIX REQUEST: ECHO ALERT:")


def test_sweep_passes_actual_previous_grade_to_structured_seed(monkeypatch):
    class Store:
        def __init__(self):
            self.grades = []

        def rows_in_range(self, gym_id, start, end):
            return [{"id": "row-1"}]

        def latest_grade(self, gym_id, window):
            return {"total": 82}

        def insert_grade(self, record):
            self.grades.append(record)

    grades = iter([
        SimpleNamespace(total=90, letter="A", scores={}, defects=[], exempt={}),
        SimpleNamespace(total=61, letter="D", scores={}, defects=[
            ("caption", "row-1", "missing approved ask")], exempt={}),
    ])
    import agent.calendar_grade as calendar_grade
    import agent.real_month_planner as planner
    from agent import config
    monkeypatch.setattr(calendar_grade, "grade_month", lambda *a, **k: next(grades))
    monkeypatch.setattr(planner, "_profile_for", lambda gym: "GYM")
    monkeypatch.setattr(config, "calendar_grade_enabled_for", lambda gym: True)
    monkeypatch.setattr(config, "grade_self_fix_enabled", lambda: False)
    monkeypatch.setattr(grade_sweep, "_should_alert_drop", lambda *a, **k: True)
    seed_calls, alerts = [], []

    def seed_fn(**kwargs):
        seed_calls.append(kwargs)
        return {"trusted": True}

    def alert_fn(message, business_seed=None):
        alerts.append((message, business_seed))

    grade_sweep.run(
        gyms=["chateau123"], store=Store(), now="2026-09-19",
        alert_fn=alert_fn, business_seed_fn=seed_fn)
    drop = [(message, seed) for message, seed in alerts if "DROPPED" in message]
    assert seed_calls == [{
        "gym_key": "chateau123", "min_total": 82,
        "observed_total": 61, "source_day": "2026-09-19"}]
    assert drop and drop[0][1] == {"trusted": True}
