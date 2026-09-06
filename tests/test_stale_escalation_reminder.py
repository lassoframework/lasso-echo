"""
STALE ESCALATION REMINDER: a held, escalated ticket must nag while it stays
unresolved, not go quiet after one post.

Checked against production 2026-09-06 before writing anything: Dean's real
ticket (4941e162-2923-495f-8efb-d2554dea5aec) shows the ORIGINAL escalation
worked -- posted to #fixer and acked within 5 seconds of ticket creation. What
read as "parked" was silence for hours AFTER that single post. This module is
the re-fire, not a classifier fix -- see the module docstring for the full
production evidence.

Fully offline. AGENT_DB_PATH is per-test (conftest), so kv dedup is isolated.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, db  # noqa: E402
from agent.jobs.stale_escalation_reminder import (  # noqa: E402
    reminder_body,
    run,
    stale_tickets,
)
from agent.slack_convo import adapter as _a  # noqa: E402

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _ticket(**over):
    row = {
        "id": "t-1", "status": "hold", "escalated": True, "resolved_at": None,
        "created_at": (NOW - timedelta(hours=6)).isoformat(),
        "raw_text": "All the captions are almost the same as one another.",
        "bot_identity": "echo",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# stale_tickets (pure)
# ---------------------------------------------------------------------------

def test_stale_tickets_finds_old_held_escalated_ticket():
    tickets = [_ticket()]
    found = stale_tickets(tickets, now=NOW, stale_hours=4)
    assert [t["id"] for t in found] == ["t-1"]


def test_stale_tickets_ignores_recent_ticket():
    tickets = [_ticket(created_at=(NOW - timedelta(hours=1)).isoformat())]
    assert stale_tickets(tickets, now=NOW, stale_hours=4) == []


def test_stale_tickets_ignores_resolved_ticket():
    tickets = [_ticket(resolved_at=NOW.isoformat())]
    assert stale_tickets(tickets, now=NOW, stale_hours=4) == []


def test_stale_tickets_ignores_non_hold_status():
    tickets = [_ticket(status="resolved")]
    assert stale_tickets(tickets, now=NOW, stale_hours=4) == []


def test_stale_tickets_ignores_non_escalated():
    tickets = [_ticket(escalated=False)]
    assert stale_tickets(tickets, now=NOW, stale_hours=4) == []


def test_stale_tickets_boundary_is_inclusive():
    tickets = [_ticket(created_at=(NOW - timedelta(hours=4)).isoformat())]
    assert len(stale_tickets(tickets, now=NOW, stale_hours=4)) == 1


def test_stale_tickets_ignores_malformed_row_shapes():
    tickets = ["not-a-dict", None, {}, _ticket()]
    found = stale_tickets(tickets, now=NOW, stale_hours=4)
    assert [t["id"] for t in found] == ["t-1"]


# ---------------------------------------------------------------------------
# reminder_body (pure) -- names the age and the original ask
# ---------------------------------------------------------------------------

def test_reminder_body_names_age_and_ticket_id():
    t = _ticket(created_at=(NOW - timedelta(hours=6, minutes=30)).isoformat())
    body = reminder_body(t, now=NOW)
    assert "t-1" in body
    assert "6.5h" in body
    assert "captions are almost the same" in body


# ---------------------------------------------------------------------------
# FakeBus, schema-faithful to agent/slack_convo/bus.py's public surface
# ---------------------------------------------------------------------------

class FakeBus:
    def __init__(self, tickets):
        self._tickets = list(tickets)
        self.outbound = []

    def available(self):
        return True

    def _get(self, table, params):
        assert table == "support_tickets"
        assert params.get("status") == "eq.hold"
        assert params.get("escalated") == "eq.true"
        assert params.get("resolved_at") == "is.null"
        return list(self._tickets)

    def record_outbound(self, **kwargs):
        row = {"id": f"out-{len(self.outbound)}", **kwargs}
        self.outbound.append(row)
        return row


# ---------------------------------------------------------------------------
# run() -- the I/O wrapper: dedup, delivery kind, escape hatch
# ---------------------------------------------------------------------------

def test_run_reminds_a_stale_ticket_once(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    bus = FakeBus([_ticket()])
    result = run(bus=bus, now=NOW, stale_hours=4, enabled=True)
    assert result["ok"] is True
    assert result["reminded"] == ["t-1"]
    assert len(bus.outbound) == 1
    assert bus.outbound[0]["kind"] == _a.KIND_ESCALATION
    assert bus.outbound[0]["ticket_id"] == "t-1"


def test_run_is_deduped_same_day(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    bus = FakeBus([_ticket()])
    run(bus=bus, now=NOW, stale_hours=4, enabled=True)
    result2 = run(bus=bus, now=NOW, stale_hours=4, enabled=True)
    assert result2["reminded"] == []
    assert len(bus.outbound) == 1  # not two


def test_run_re_fires_the_next_day(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    bus = FakeBus([_ticket()])
    run(bus=bus, now=NOW, stale_hours=4, enabled=True)
    tomorrow = NOW + timedelta(days=1)
    result = run(bus=bus, now=tomorrow, stale_hours=4, enabled=True)
    assert result["reminded"] == ["t-1"]
    assert len(bus.outbound) == 2  # re-fired, doctrine: alerts must re-fire while unresolved


def test_run_skips_non_stale_ticket(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    bus = FakeBus([_ticket(created_at=(NOW - timedelta(hours=1)).isoformat())])
    result = run(bus=bus, now=NOW, stale_hours=4, enabled=True)
    assert result["reminded"] == []
    assert bus.outbound == []


def test_run_escape_hatch_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    bus = FakeBus([_ticket()])
    result = run(bus=bus, now=NOW, stale_hours=4, enabled=False)
    assert result == {"ok": True, "reminded": [], "reason": "disabled"}
    assert bus.outbound == []


def test_run_one_bad_ticket_never_sinks_the_rest(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))

    class _FailOnFirstBus(FakeBus):
        def record_outbound(self, **kwargs):
            if kwargs.get("ticket_id") == "t-1":
                raise RuntimeError("bus down for this row")
            return super().record_outbound(**kwargs)

    bus = _FailOnFirstBus([_ticket(id="t-1"), _ticket(id="t-2")])
    result = run(bus=bus, now=NOW, stale_hours=4, enabled=True)
    assert result["reminded"] == ["t-2"]


def test_run_bus_unavailable_reports_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))

    class _DownBus(FakeBus):
        def available(self):
            return False

    result = run(bus=_DownBus([_ticket()]), now=NOW, stale_hours=4, enabled=True)
    assert result == {"ok": False, "reminded": [], "reason": "bus_unavailable"}


# ---------------------------------------------------------------------------
# config flags: default ON / 4.0, escape hatches work
# ---------------------------------------------------------------------------

def test_config_reminder_default_on(monkeypatch):
    monkeypatch.delenv("AGENT_STALE_ESCALATION_REMINDER", raising=False)
    assert config.stale_escalation_reminder_enabled() is True


def test_config_reminder_escape_hatch(monkeypatch):
    monkeypatch.setenv("AGENT_STALE_ESCALATION_REMINDER", "false")
    assert config.stale_escalation_reminder_enabled() is False


def test_config_stale_hold_hours_default(monkeypatch):
    monkeypatch.delenv("STALE_HOLD_HOURS", raising=False)
    assert config.stale_hold_hours() == 4.0


def test_config_stale_hold_hours_override(monkeypatch):
    monkeypatch.setenv("STALE_HOLD_HOURS", "2.5")
    assert config.stale_hold_hours() == 2.5


def test_config_stale_hold_hours_malformed_falls_back(monkeypatch):
    monkeypatch.setenv("STALE_HOLD_HOURS", "not-a-number")
    assert config.stale_hold_hours() == 4.0


# ---------------------------------------------------------------------------
# VERIFY BY DELETION: prove the dedup key is actually load-bearing
# ---------------------------------------------------------------------------

def test_deletion_proof_dedup_key_is_load_bearing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "t.db"))
    # If dedup used the WRONG key (e.g. ticket id alone, no date), a same-day
    # re-run and a next-day re-run would be indistinguishable. Confirm the
    # actual stored key carries today's date.
    bus = FakeBus([_ticket()])
    run(bus=bus, now=NOW, stale_hours=4, enabled=True)
    assert db.kv_get(f"stale_escalation_reminder_t-1_{NOW.date().isoformat()}", "")
