"""Focused tests for the default-OFF client support scan reminder queue.

Covers the 19-row synthetic census (including a Dale anomaly row), exact
insert/readback dedupe, UUID collision identity mismatch, fail-closed scan
behavior, unarmed/invalid identity skips, and the no-writes guarantee while
disabled. The job never mutates tickets and never calls Slack.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent.jobs import client_support_scan_reminder as R

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
DAY = "2026-10-09"


@pytest.fixture(autouse=True)
def echo_only_owner_route(monkeypatch):
    monkeypatch.setattr(R, "_active_internal_identities",
                        lambda: frozenset({"echo"}))

TEST_GYM_ID = "ca397eec-519a-4524-b666-d048199c76b2"
TEST_GYM = {"id": TEST_GYM_ID, "slug": "zz-test-gym"}
REAL_GYM = {"id": "9f1c2f68-2f2a-4f6e-9d3b-9a3f2b1c0a01", "slug": "crossfit-eng"}
DALE_GYM = {"id": "6ee04ee4-13a5-47db-8416-7b8ee3e61ab8", "slug": "eng"}
GYMS = {g["id"]: dict(g) for g in (TEST_GYM, REAL_GYM, DALE_GYM)}

_BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _ts(n):
    return (_BASE + timedelta(seconds=n)).isoformat()


def _ticket(n, **over):
    base = {
        "id": f"t-{n:04d}",
        "product": "echo",
        "source": "website_tab",
        "status": "resolved",
        "resolved_at": _ts(n),
        "request_version": 1,
        "client_id": REAL_GYM["id"],
        "bot_identity": "echo",
        "slack_user_id": "U1",
        "classification": "answerable_question",
        "is_test": False,
        "client_delivery_guard_required": True,
        "created_at": _ts(n),
        "raw_text": "SECRET TICKET TEXT",
        "body": "SECRET BODY",
    }
    base.update(over)
    return base


def _census():
    """The 19-row client-visible-working synthetic set + excluded rows."""
    tickets = []
    for n in range(1, 10):
        tickets.append(_ticket(n, status="hold", resolved_at=None))
    for n in range(10, 13):
        tickets.append(_ticket(n, status="verification", resolved_at=None))
    tickets.append(_ticket(13, status="merged", resolved_at=None,
                           classification="code_fix"))
    for n in range(14, 20):
        tickets.append(_ticket(n, status="resolved", resolved_at=None))
    # Dale anomaly row: resolved with a null resolved_at.
    tickets[13] = _ticket(14, client_id=DALE_GYM["id"], status="resolved",
                          resolved_at=None, request_version=4)
    # Out of scope / non-visible rows that must never queue.
    tickets.append(_ticket(20, client_id=TEST_GYM_ID, is_test=False,
                           status="resolved", resolved_at=None))
    tickets.append(_ticket(21, client_id=None, status="resolved",
                           resolved_at=None))
    # Actionable by the classifier but owned by an unarmed identity.
    tickets.append(_ticket(22, product="portal", bot_identity="scout", status="working",
                           resolved_at=None))
    return tickets


class FakeBus:
    """Read/write fake honoring the PostgREST params the scan sends and the
    deterministic insert/readback contract the reminder job needs."""

    def __init__(self, tickets, gyms=None, messages=None, fail_table=None):
        self._tickets = [dict(t) for t in tickets]
        self._gyms = dict(gyms or {})
        self.message_rows = list(messages or [])
        self._fail_table = fail_table
        self.inserted = []
        self.calls = []
        self.available_calls = 0

    def available(self):
        self.available_calls += 1
        return True

    # Ticket mutation surfaces must never be touched by this job.
    def set_ticket(self, *a, **k):
        raise AssertionError("ticket mutation attempted by reminder job")

    patch_ticket_if_current = set_ticket

    def _patch(self, *a, **k):
        raise AssertionError("patch attempted by reminder job")

    _post = _delete = _upsert = _patch

    def _get(self, table, params):
        self.calls.append((table, dict(params)))
        if table == self._fail_table:
            raise RuntimeError("boom -- raw low-level text that must not leak")
        select = params.get("select", "")
        assert "body" not in select and "raw_text" not in select
        rows = {"support_tickets": self._tickets,
                "gyms": list(self._gyms.values()),
                "support_messages": self.message_rows}[table]

        def keep(row):
            src = params.get("source")
            if src is not None:
                assert src.startswith("in.(") and src.endswith(")")
                if row.get("source") not in src[4:-1].split(","):
                    return False
            for key in ("id", "ticket_id"):
                flt = params.get(key)
                if flt is not None:
                    assert flt.startswith("eq."), flt
                    if row.get(key) != flt[3:]:
                        return False
            ors = params.get("or")
            if ors is not None:
                ca = re.search(r"created_at\.gt\.([^,)]+)", ors).group(1)
                rid = re.search(r"id\.gt\.([^,)]+)", ors).group(1)
                if not (row.get("created_at"), row.get("id")) > (ca, rid):
                    return False
            return True

        rows = [r for r in rows if keep(r)]
        if "order" in params:
            assert params["order"] == "created_at.asc,id.asc"
            rows.sort(key=lambda r: (r["created_at"], r["id"]))
        limit = params.get("limit")
        if limit is not None:
            rows = rows[:int(limit)]
        return rows

    def _insert(self, table, row):
        assert table == "support_messages"
        existing = next((m for m in self.message_rows
                         if m["id"] == row["id"]), None)
        if existing:
            return None, True
        stored = dict(row)
        stored["created_at"] = NOW.isoformat()
        self.message_rows.append(stored)
        self.inserted.append(stored)
        return stored, False

    def message(self, message_id):
        return next((dict(m) for m in self.message_rows
                     if m["id"] == message_id), None)


def _expected_id(ticket_id, version, day=DAY):
    return str(uuid5(NAMESPACE_URL,
                     f"lasso:client-support-scan-reminder:"
                     f"{ticket_id}:{version}:{day}"))


def _reminder_for(bus, ticket_id, version=1, day=DAY):
    return bus.message(_expected_id(ticket_id, version, day))


# --- default OFF ---------------------------------------------------------------

def test_disabled_does_not_read_or_write(monkeypatch):
    monkeypatch.delenv("AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED",
                       raising=False)
    bus = FakeBus(_census(), GYMS)

    result = R.run(bus=bus, now=NOW)

    assert result == {"ok": True, "queued": [], "reason": "disabled"}
    assert bus.available_calls == 0
    assert bus.calls == []
    assert bus.inserted == []


def test_env_flag_arms_the_job(monkeypatch):
    monkeypatch.setenv("AGENT_CLIENT_SUPPORT_SCAN_REMINDER_ENABLED", "true")
    bus = FakeBus(_census(), GYMS)
    result = R.run(bus=bus, now=NOW)
    assert result["ok"]
    assert len(result["queued"]) == 19
    assert len(bus.inserted) == 19


# --- 19-row census: queue shape -------------------------------------------------

def test_census_queues_19_deterministic_internal_reminders():
    bus = FakeBus(_census(), GYMS)
    tickets_before = [dict(t) for t in bus._tickets]

    result = R.run(bus=bus, now=NOW, enabled=True)

    assert result["ok"]
    assert sorted(result["queued"]) == [f"t-{n:04d}" for n in range(1, 20)]
    assert result["duplicates"] == []
    # t-0022 (scout identity) skipped; test-gym and null-client rows never
    # reached the actionable list at all.
    assert result["skipped"] == ["t-0022"]
    assert len(bus.inserted) == 19

    dale = _reminder_for(bus, "t-0014", version=4)
    assert dale is not None
    assert dale["direction"] == "outbound"
    assert dale["author_type"] == "system"
    assert dale["author_id"] is None
    assert dale["delivery_status"] == "ready"
    # Body carries the ticket ID and reason token only -- no client text.
    assert dale["body"] == (
        "CLIENT SUPPORT SCAN REMINDER: ticket t-0014 still displays working "
        "to the client (reason: resolved_missing_resolved_at). This is an "
        "internal reminder; the ticket is unchanged.")
    att = dale["attachments"]
    assert att["kind"] == "escalation"
    assert att["identity"] == "echo"
    assert att["surface"] == "client_support_scan_reminder"
    assert att["contract"] == R.CONTRACT
    assert att["ticket_id"] == "t-0014"
    assert att["request_version"] == 4
    assert att["reason"] == "resolved_missing_resolved_at"
    assert att["notice_day"] == DAY
    assert att["client_id"] == DALE_GYM["id"]
    assert att["source"] == "website_tab"
    assert att["product"] == "echo"
    assert att["status"] == "resolved"
    blob = str(bus.inserted)
    for forbidden in ("SECRET", "zz-test-gym", "crossfit-eng", "raw_text"):
        assert forbidden not in blob, forbidden
    # No ticket mutation: stored tickets are byte-identical to the pre-run set.
    assert bus._tickets == tickets_before


def test_second_run_same_day_is_exact_duplicate_no_new_writes():
    bus = FakeBus(_census(), GYMS)

    first = R.run(bus=bus, now=NOW, enabled=True)
    second = R.run(bus=bus, now=NOW, enabled=True)

    assert len(first["queued"]) == 19
    assert second["queued"] == []
    assert sorted(second["duplicates"]) == [f"t-{n:04d}" for n in range(1, 20)]
    assert len(bus.inserted) == 19


def test_new_request_alerts_only_after_intake_grace_and_deduplicates():
    ticket = _ticket(30, status="new", classification=None, resolved_at=None,
                     created_at=(NOW - timedelta(minutes=31)).isoformat())
    bus = FakeBus([ticket], GYMS)
    report = R.scan.scan_support_bus(bus)
    assert report["client_visible_working"] == 0
    assert len(report["actionable"]) == 1
    assert report["actionable"][0]["created_at"] == ticket["created_at"]
    result = R.run(bus=bus, now=NOW, enabled=True)
    assert result["queued"] == [ticket["id"]]
    notice = _reminder_for(bus, ticket["id"], 1)
    assert "received and untriaged" in notice["body"]
    assert notice["attachments"]["created_at"] == ticket["created_at"]
    again = R.run(bus=bus, now=NOW, enabled=True)
    assert again["duplicates"] == [ticket["id"]]
    assert len(bus.inserted) == 1


def test_fresh_new_request_is_not_alerted_yet():
    ticket = _ticket(31, status="new", classification=None, resolved_at=None,
                     created_at=(NOW - timedelta(minutes=29)).isoformat())
    bus = FakeBus([ticket], GYMS)
    result = R.run(bus=bus, now=NOW, enabled=True)
    assert result["queued"] == []
    assert result["skipped"] == []
    assert bus.inserted == []


def test_new_utc_day_or_version_queues_a_new_reminder():
    bus = FakeBus(_census(), GYMS)
    R.run(bus=bus, now=NOW, enabled=True)
    next_day = R.run(bus=bus, now=NOW + timedelta(days=1), enabled=True)
    assert sorted(next_day["queued"]) == [f"t-{n:04d}" for n in range(1, 20)]
    assert len(bus.inserted) == 38
    assert (_reminder_for(bus, "t-0001", 1, DAY)["id"]
            != _reminder_for(bus, "t-0001", 1, "2026-10-10")["id"])


# --- collision / dedupe integrity ----------------------------------------------

def test_uuid_collision_with_different_body_is_skipped_not_claimed():
    tickets = _census()
    collision_id = _expected_id("t-0001", 1)
    foreign = {"id": collision_id, "ticket_id": "t-0001",
               "author_type": "system", "author_id": None,
               "body": "SOME OTHER ROW THAT HAPPENS TO SHARE THE UUID",
               "attachments": {"kind": "escalation", "surface": "other"},
               "direction": "outbound", "delivery_status": "ready",
               "created_at": _ts(5000)}
    logs = []
    bus = FakeBus(tickets, GYMS, messages=[foreign])

    result = R.run(bus=bus, now=NOW, enabled=True, log=logs.append)

    assert result["ok"]
    assert "t-0001" not in result["queued"]
    assert "t-0001" not in result["duplicates"]
    assert "t-0001" in result["skipped"]
    assert any("t-0001" in line for line in logs)
    # The foreign row was not overwritten and the other 18 still queued.
    assert bus.message(collision_id)["body"] == foreign["body"]
    assert len(bus.inserted) == 18


def test_exact_stored_row_is_accepted_as_duplicate():
    bus = FakeBus(_census(), GYMS)
    R.run(bus=bus, now=NOW, enabled=True)
    again = R.run(bus=bus, now=NOW, enabled=True)
    assert again["queued"] == []
    assert len(again["duplicates"]) == 19


# --- fail closed -----------------------------------------------------------------

def test_scan_failure_means_no_writes():
    bus = FakeBus(_census(), GYMS, fail_table="support_tickets")
    result = R.run(bus=bus, now=NOW, enabled=True)
    assert result == {"ok": False, "queued": [], "reason": "scan_failed"}
    assert bus.inserted == []
    assert "boom" not in str(result)


def test_message_read_failure_means_no_writes():
    bus = FakeBus(_census(), GYMS, fail_table="support_messages")
    result = R.run(bus=bus, now=NOW, enabled=True)
    assert result == {"ok": False, "queued": [], "reason": "scan_failed"}
    assert bus.inserted == []


def test_naive_clock_refused():
    bus = FakeBus(_census(), GYMS)
    result = R.run(bus=bus, now=datetime(2026, 10, 9, 12, 0), enabled=True)
    assert result == {"ok": False, "queued": [], "reason": "naive_clock"}
    assert bus.inserted == []


def test_bus_unavailable():
    class Down(FakeBus):
        def available(self):
            return False
    result = R.run(bus=Down(_census(), GYMS), now=NOW, enabled=True)
    assert result == {"ok": False, "queued": [], "reason": "bus_unavailable"}


# --- identity / entry hygiene -----------------------------------------------------

def test_invalid_and_unarmed_entries_skip_safely():
    assert not R._valid_entry(None)
    assert not R._valid_entry({})
    assert not R._valid_entry({"ticket_id": "t-1", "reason": "bogus_reason",
                               "product": "echo", "source": "website_tab",
                               "bot_identity": "echo", "client_id": "g",
                               "request_version": 1, "status": "hold"})
    assert not R._valid_entry({"ticket_id": "t-1",
                               "reason": "client_request_open",
                               "product": "portal", "source": "website_tab",
                               "bot_identity": "scout", "client_id": "g",
                               "request_version": 1, "status": "hold"})
    assert not R._valid_entry({"ticket_id": "t-1",
                               "reason": "client_request_open",
                               "product": "echo", "source": "website_tab",
                               "bot_identity": "echo", "client_id": "g",
                               "request_version": True, "status": "hold"})
    assert R._valid_entry({"ticket_id": "t-1", "reason": "client_request_open",
                           "product": "echo", "source": "website_tab",
                           "bot_identity": "echo", "client_id": "g",
                           "request_version": 0, "status": "hold"})


def test_scout_owner_route_queues_its_own_internal_notice(monkeypatch):
    monkeypatch.setattr(R, "_active_internal_identities",
                        lambda: frozenset({"echo", "scout"}))
    bus = FakeBus(_census(), GYMS)
    result = R.run(bus=bus, now=NOW, enabled=True)
    assert result["ok"] and len(result["queued"]) == 20
    scout = _reminder_for(bus, "t-0022", version=1)
    assert scout["attachments"]["identity"] == "scout"
    assert scout["attachments"]["product"] == "portal"
