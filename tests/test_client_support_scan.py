"""Focused tests for the read-only support bus scan shell.

Covers the bus contract only (server-side source filter before limit, exact
tenant gym lookup, exact ticket_id message filter, keyset paging, fail-closed
reads, no-write guarantee, report shape). Classification parity itself is
covered by test_client_support_reconciler.py and is not duplicated here.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from agent.jobs import client_support_scan as s

TEST_GYM_ID = "ca397eec-519a-4524-b666-d048199c76b2"
TEST_GYM = {"id": TEST_GYM_ID, "slug": "zz-test-gym"}
REAL_GYM = {"id": "9f1c2f68-2f2a-4f6e-9d3b-9a3f2b1c0a01", "slug": "crossfit-eng"}
DALE_GYM = {"id": "6ee04ee4-13a5-47db-8416-7b8ee3e61ab8", "slug": "eng"}
GYMS = {g["id"]: dict(g) for g in (TEST_GYM, REAL_GYM, DALE_GYM)}

_BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
_TICKET_SEQ = [0]


def _ts(n):
    return (_BASE + timedelta(seconds=n)).isoformat()


class FakeBus:
    """Read-only fake honoring the PostgREST params the scan must send.

    Applies the source IN / exact id / exact ticket_id / keyset `or` filters
    and the limit itself, records every call, and explodes on any write or on
    a select that asks for raw text.
    """

    def __init__(self, tickets, gyms=None, messages=None, fail_table=None,
                 adoptions=None):
        self._tickets = list(tickets)
        self._gyms = dict(gyms or {})
        self._messages = list(messages or [])
        self._adoptions = list(adoptions or [])
        self._fail_table = fail_table
        self.calls = []

    # Any write surface must never be touched.
    def _insert(self, *a, **k):
        raise AssertionError("write method called on read-only scan")

    _patch = _post = _delete = _upsert = _insert

    def _get(self, table, params):
        self.calls.append((table, dict(params)))
        if table == self._fail_table:
            raise RuntimeError("boom -- raw low-level text that must not leak")
        select = params.get("select", "")
        assert "body" not in select and "raw_text" not in select
        rows = {"support_tickets": self._tickets, "gyms": list(self._gyms.values()),
                "support_messages": self._messages,
                "fixer_posted_notice_adoptions": self._adoptions}[table]

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
                key = (row.get("created_at"), row.get("id"))
                if not key > (ca, rid):
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
        "raw_text": "SECRET TICKET TEXT",  # present in store; never selected
        "body": "SECRET BODY",
    }
    base.update(over)
    return base


def _guarded_receipt(ticket, n, **over):
    msg = {
        "id": f"m-{n:04d}",
        "ticket_id": ticket["id"],
        "created_at": _ts(n) ,
        "direction": "outbound",
        "delivery_status": "posted",
        "delivery_request_version": ticket["request_version"],
        "attachments": {
            "kind": "answer",
            "delivery_expected_product": ticket["product"],
            "delivery_expected_client_id": ticket["client_id"],
            "delivery_expected_bot_identity": ticket["bot_identity"],
            "delivery_expected_slack_user_id": ticket["slack_user_id"],
            "delivery_expected_status": "verification",
            "delivery_expected_classification": ticket["classification"],
            "delivery_identity_fence": True,
        },
        "body": "SECRET MESSAGE BODY",
    }
    msg.update(over)
    return msg


# --- query contract -----------------------------------------------------------

def test_source_filter_server_side_and_precedes_limit():
    tickets = [_ticket(1), _ticket(2, source="ops_internal")]
    bus = FakeBus(tickets, GYMS)
    report = s.scan_support_bus(bus, page_size=500)
    assert report["ok"] and report["scanned"] == 1
    ticket_calls = [p for t, p in bus.calls if t == "support_tickets"]
    assert ticket_calls
    for params in ticket_calls:
        keys = list(params)
        assert keys.index("source") < keys.index("limit")
        assert params["source"] == "in.(coach_portal,slack_conversation,website_tab)"
        assert "raw_text" not in params["select"] and "body" not in params["select"]


def test_exact_tenant_gym_lookup_cached():
    tickets = [_ticket(1), _ticket(2)]  # same client
    bus = FakeBus(tickets, GYMS)
    assert s.scan_support_bus(bus)["ok"]
    gym_calls = [p for t, p in bus.calls if t == "gyms"]
    assert len(gym_calls) == 1  # cached per client_id
    assert gym_calls[0]["id"] == f"eq.{REAL_GYM['id']}"
    assert gym_calls[0]["select"] == "id,slug"
    assert gym_calls[0]["limit"] == "1"


def test_messages_filtered_by_exact_ticket_id():
    t = _ticket(1)
    other = _ticket(2)
    msgs = [_guarded_receipt(t, 1), _guarded_receipt(other, 2)]
    bus = FakeBus([t], GYMS, msgs)
    report = s.scan_support_bus(bus)
    assert report["ok"]
    msg_calls = [p for t2, p in bus.calls if t2 == "support_messages"]
    assert msg_calls and all(p["ticket_id"] == f"eq.{t['id']}" for p in msg_calls)
    assert report["counts"]["satisfied"] == 1


def test_more_than_200_tickets_paged_without_starvation():
    tickets = [_ticket(n, status="working", resolved_at=None) for n in range(1, 231)]
    bus = FakeBus(tickets, GYMS)
    report = s.scan_support_bus(bus, page_size=50)
    assert report["ok"]
    assert report["scanned"] == 230
    assert report["counts"]["exception"] == 230
    # full pages of 50 until the final short page: no offset starvation
    assert len([c for c in bus.calls if c[0] == "support_tickets"]) == 5


def test_keyset_cursor_advances_between_pages():
    tickets = [_ticket(n, status="working", resolved_at=None) for n in range(1, 6)]
    bus = FakeBus(tickets, GYMS)
    report = s.scan_support_bus(bus, page_size=2)
    assert report["ok"] and report["scanned"] == 5
    pages = [p for t, p in bus.calls if t == "support_tickets"]
    assert "or" not in pages[0]
    assert "created_at.gt." in pages[1]["or"] and "id.gt." in pages[1]["or"]


# --- classification integration via the scan ----------------------------------

def test_test_gym_excluded_despite_is_test_false():
    t = _ticket(1, client_id=TEST_GYM_ID, is_test=False, status="working",
                resolved_at=None)
    bus = FakeBus([t], GYMS)
    report = s.scan_support_bus(bus)
    assert report["ok"]
    assert report["out_of_scope"] == 1
    assert report["counts"]["exception"] == 0
    assert report["client_visible_working"] == 0


def test_null_client_is_anomaly_and_needs_no_gym_fetch():
    t = _ticket(1, client_id=None, status="working", resolved_at=None)
    bus = FakeBus([t], GYMS)
    report = s.scan_support_bus(bus)
    assert report["ok"]
    assert report["anomalies"] == [{"ticket_id": t["id"], "reason": "null_client_id"}]
    assert not [c for c in bus.calls if c[0] == "gyms"]


def test_four_dale_id_status_shapes():
    # Production state shapes, not a claim that the business fixes are done.
    tickets = [
        _ticket(1, id="35e066d0-d9bc-40e6-aef8-86719a010590",
                client_id=DALE_GYM["id"], status="resolved", resolved_at=None,
                client_delivery_guard_required=False),
        _ticket(2, id="cd08b049-1bf6-4b71-bb80-35d42d9d9de2",
                client_id=DALE_GYM["id"], status="verification", resolved_at=None),
        _ticket(3, id="a328078e-3f1d-4d83-8b0c-312fece6e227",
                client_id=DALE_GYM["id"], status="verification", resolved_at=None),
        _ticket(4, id="554ac760-b0b3-413a-919f-4e13cff6d3fc",
                client_id=DALE_GYM["id"], status="merged", resolved_at=None,
                classification="code_fix"),
    ]
    bus = FakeBus(tickets, GYMS, [])
    report = s.scan_support_bus(bus)
    assert report["ok"]
    assert report["counts"]["satisfied"] == 0
    reasons = {(e["ticket_id"], e["reason"]) for e in report["exceptions"]}
    assert reasons == {
        (tickets[1]["id"], "client_request_open"),
        (tickets[2]["id"], "client_request_open"),
    }
    anomalies = {(a["ticket_id"], a["reason"]) for a in report["anomalies"]}
    assert anomalies == {
        (tickets[0]["id"], "resolved_missing_resolved_at"),
        (tickets[3]["id"], "merged_missing_resolved_at"),
    }
    assert report["client_visible_working"] == 4


def test_current_census_shape_has_19_real_client_working_rows():
    tickets = []
    for n in range(1, 10):
        tickets.append(_ticket(n, status="hold", resolved_at=None))
    for n in range(10, 13):
        tickets.append(_ticket(n, status="verification", resolved_at=None))
    tickets.append(_ticket(13, status="merged", resolved_at=None,
                           classification="code_fix"))
    for n in range(14, 20):
        tickets.append(_ticket(n, status="resolved", resolved_at=None))
    tickets.append(_ticket(20, client_id=TEST_GYM_ID, is_test=False,
                           status="resolved", resolved_at=None))
    tickets.append(_ticket(21, client_id=None, status="resolved", resolved_at=None))
    report = s.scan_support_bus(FakeBus(tickets, GYMS), page_size=7)
    assert report["ok"]
    assert report["scanned"] == 21
    assert report["client_visible_working"] == 19
    assert report["out_of_scope"] == 1
    assert report["anomalies"][-1] == {"ticket_id": "t-0021", "reason": "null_client_id"}
    # Actionable list: every one of the 19 client-visible-working rows,
    # including anomaly rows (resolved/merged missing resolved_at), each with
    # bounded metadata and no client text.
    actionable = report["actionable"]
    assert len(actionable) == 19
    ids = {e["ticket_id"] for e in actionable}
    assert ids == {f"t-{n:04d}" for n in range(1, 20)}
    by_id = {t["id"]: t for t in tickets}
    for entry in actionable:
        assert set(entry) == {"ticket_id", "reason", "product", "source",
                              "bot_identity", "client_id", "request_version",
                              "status"}
        t = by_id[entry["ticket_id"]]
        assert entry["product"] == t["product"]
        assert entry["source"] == t["source"]
        assert entry["bot_identity"] == t["bot_identity"]
        assert entry["client_id"] == t["client_id"]
        assert entry["request_version"] == t["request_version"]
        assert entry["status"] == t["status"]
    # The Dale-style resolved anomaly is actionable with its reason carried.
    resolved_anomalies = [e for e in actionable
                          if e["reason"] == "resolved_missing_resolved_at"]
    assert len(resolved_anomalies) == 6
    # The null-client anomaly is NOT client-visible-working and never appears.
    assert "t-0021" not in ids and "t-0020" not in ids
    assert "SECRET" not in str(actionable)
    assert "zz-test-gym" not in str(actionable)


def test_actionable_includes_dale_merged_anomaly_metadata():
    tickets = [
        _ticket(1, id="554ac760-b0b3-413a-919f-4e13cff6d3fc",
                client_id=DALE_GYM["id"], status="merged", resolved_at=None,
                classification="code_fix", request_version=3),
    ]
    report = s.scan_support_bus(FakeBus(tickets, GYMS))
    assert report["ok"]
    assert report["actionable"] == [{
        "ticket_id": tickets[0]["id"],
        "reason": "merged_missing_resolved_at",
        "product": "echo",
        "source": "website_tab",
        "bot_identity": "echo",
        "client_id": DALE_GYM["id"],
        "request_version": 3,
        "status": "merged",
    }]


def test_satisfied_ticket_not_actionable():
    t = _ticket(1)
    report = s.scan_support_bus(FakeBus([t], GYMS, [_guarded_receipt(t, 1)]))
    assert report["ok"]
    assert report["actionable"] == []


def test_valid_current_cycle_receipt_suppresses_visible_working():
    t = _ticket(1)
    bus = FakeBus([t], GYMS, [_guarded_receipt(t, 1)])
    report = s.scan_support_bus(bus)
    assert report["ok"]
    assert report["counts"]["satisfied"] == 1
    assert report["client_visible_working"] == 0
    assert report["exceptions"] == []


# --- fail closed ----------------------------------------------------------------

@pytest.mark.parametrize("fail_table,fixed_reason", [
    ("support_tickets", s.FAIL_TICKET_READ),
    ("gyms", s.FAIL_GYM_READ),
    ("support_messages", s.FAIL_MESSAGE_READ),
])
def test_read_failure_returns_no_partial_report(fail_table, fixed_reason):
    t = _ticket(1)
    bus = FakeBus([t], GYMS, [_guarded_receipt(t, 1)], fail_table=fail_table)
    report = s.scan_support_bus(bus)
    assert report == {"ok": False, "error": fixed_reason}
    assert "boom" not in str(report) and "SECRET" not in str(report)


def test_malformed_page_fails_closed():
    class BadBus(FakeBus):
        def _get(self, table, params):
            if table == "support_tickets":
                return [{"id": "x"}]  # no created_at: malformed keyset row
            return super()._get(table, params)
    report = s.scan_support_bus(BadBus([_ticket(1)], GYMS))
    assert report == {"ok": False, "error": s.FAIL_TICKET_READ}


def test_bus_without_get_fails_closed():
    assert s.scan_support_bus(bus=object()) == {"ok": False,
                                                "error": s.FAIL_BUS_UNAVAILABLE}


# --- report hygiene / no writes -------------------------------------------------

def test_report_has_no_text_slugs_attachments_or_error_strings():
    tickets = [_ticket(1), _ticket(2, status="working", resolved_at=None),
               _ticket(3, client_id=None, status="working", resolved_at=None),
               _ticket(4, client_id=TEST_GYM_ID)]
    bus = FakeBus(tickets, GYMS, [_guarded_receipt(tickets[0], 1)])
    report = s.scan_support_bus(bus)
    assert report["ok"]
    blob = str(report)
    for forbidden in ("SECRET", "zz-test-gym", "crossfit-eng", "eng",
                      "attachments", "delivery_expected", "Traceback"):
        assert forbidden not in blob, forbidden
    assert set(report) == {"ok", "scanned", "counts", "client_visible_working",
                           "exceptions", "anomalies", "out_of_scope",
                           "actionable"}
    for entry in report["exceptions"] + report["anomalies"]:
        assert set(entry) == {"ticket_id", "reason"}
    for entry in report["actionable"]:
        assert set(entry) == {"ticket_id", "reason", "product", "source",
                              "bot_identity", "client_id", "request_version",
                              "status"}


def test_no_write_method_ever_called():
    tickets = [_ticket(n, status="working", resolved_at=None) for n in range(1, 4)]
    bus = FakeBus(tickets, GYMS)
    assert s.scan_support_bus(bus, page_size=2)["ok"]
    # Every recorded bus interaction is a _get call with a params dict.
    assert bus.calls and all(isinstance(c[1], dict) for c in bus.calls)
    # FakeBus write stubs (_insert/_patch/_post/_delete/_upsert) raise
    # AssertionError; reaching this point proves none of them fired.


# --- consumed posted-notice adoption (Portal 0645) -----------------------------

ADOPTION_TICKET_ID = "cd08b049-1bf6-4b71-bb80-35d42d9d9de2"
ADOPTION_NOTICE_ID = "20adfae9-986b-4391-a74c-671e9d807d3e"
_ADOPTION_RESOLVED_AT = "2026-10-09T21:30:00+00:00"


def _eng_grow_ticket(**over):
    base = {
        "id": ADOPTION_TICKET_ID,
        "product": "echo",
        "source": "website_tab",
        "status": "resolved",
        "resolved_at": _ADOPTION_RESOLVED_AT,
        "request_version": 1,
        "client_id": DALE_GYM["id"],
        "bot_identity": "echo",
        "slack_user_id": "U06P23E3Y2Y",
        "classification": "answerable_question",
        "is_test": False,
        "client_delivery_guard_required": True,
        "created_at": "2026-10-09T20:00:00+00:00",
        "raw_text": "SECRET TICKET TEXT",
    }
    base.update(over)
    return base


def _adoption_notice(ticket, **over):
    msg = {
        "id": ADOPTION_NOTICE_ID,
        "ticket_id": ticket["id"],
        "created_at": "2026-10-09T21:00:00+00:00",
        "direction": "outbound",
        "author_type": "echo",
        "slack_ts": "1791573331.865259",
        "delivery_status": "posted",
        "delivery_request_version": 1,
        "attachments": {
            "kind": "status",
            "operator_disposition": "provider limitation, no technical fix",
            "delivery_readback_verified": True,
            "delivery_readback_ts": "1791573331.865259",
            "delivery_readback_channel": "C0BUJKCCX6C",
            "delivery_readback_sender": "U0BE39F02KV",
        },
        "body": "SECRET MESSAGE BODY",
    }
    msg.update(over)
    return msg


def _adoption_proof(ticket, **over):
    snapshot = {
        "id": ADOPTION_TICKET_ID,
        "product": "echo",
        "source": "website_tab",
        "status": "verification",
        "resolved_at": None,
        "request_version": 1,
        "client_id": DALE_GYM["id"],
        "bot_identity": "echo",
        "slack_user_id": "U06P23E3Y2Y",
        "classification": "answerable_question",
    }
    body_sha = "b98d25c16a4ea90b045cab49aa1b3560c569ca661cc8c078f71c5ccabdada23d"
    receipt = {
        "identity_binding": "exact_historical_notice",
        "notice_message_id": ADOPTION_NOTICE_ID,
        "history_match_count": 1,
        "client_msg_id": None,
        "channel": "C0BUJKCCX6C",
        "sender": "U0BE39F02KV",
        "ts": "1791573331.865259",
        "thread_ts": None,
        "recipient_user_id": "U06P23E3Y2Y",
        "recipient_membership_verified": True,
        "body_sha256": body_sha,
        "verified": True,
        "method": "slack_api_readback",
        "observed_at": "2026-10-09T21:05:00+00:00",
        "evidence_ref": "evidence/adoption.json",
        "evidence_sha256": "c" * 64,
    }
    review = {
        "passed": True,
        "scope": "provider limitation disposition; no Grow connection",
        "reviewer": "independent-reviewer",
        "author_identity": "echo-fixer",
        "evidence_ref": "evidence/review.json",
        "evidence_sha256": "d" * 64,
        "ticket_id": ADOPTION_TICKET_ID,
        "notice_message_id": ADOPTION_NOTICE_ID,
        "transcript_sha256": "a" * 64,
        "ticket_snapshot": snapshot,
        "receipt": receipt,
        "body_sha256": body_sha,
    }
    row = {
        "ticket_id": ADOPTION_TICKET_ID,
        "notice_message_id": ADOPTION_NOTICE_ID,
        "ticket_snapshot": snapshot,
        "transcript_sha256": "a" * 64,
        "receipt": receipt,
        "independent_review": review,
        "created_at": "2026-10-09T21:10:00+00:00",
        "consumed_at": _ADOPTION_RESOLVED_AT,
    }
    row.update(over)
    return row


def test_consumed_adoption_satisfies_eng_grow_terminal_ticket():
    t = _eng_grow_ticket()
    bus = FakeBus([t], GYMS, [_adoption_notice(t)],
                  adoptions=[_adoption_proof(t)])
    report = s.scan_support_bus(bus)
    assert report["ok"]
    assert report["counts"]["satisfied"] == 1
    assert report["counts"]["exception"] == 0
    assert report["client_visible_working"] == 0
    assert report["actionable"] == []
    # The proof table was queried by exact ticket_id, selecting no body.
    adoption_calls = [c for c in bus.calls
                      if c[0] == "fixer_posted_notice_adoptions"]
    assert len(adoption_calls) == 1
    assert adoption_calls[0][1]["ticket_id"] == f"eq.{ADOPTION_TICKET_ID}"
    assert "body" not in adoption_calls[0][1]["select"]


def test_unconsumed_adoption_stays_actionable():
    t = _eng_grow_ticket()
    bus = FakeBus([t], GYMS, [_adoption_notice(t)],
                  adoptions=[_adoption_proof(t, consumed_at=None)])
    report = s.scan_support_bus(bus)
    assert report["ok"]
    assert report["counts"]["exception"] == 1
    assert report["exceptions"] == [{
        "ticket_id": ADOPTION_TICKET_ID,
        "reason": "terminal_missing_completion_receipt",
    }]
    assert report["actionable"][0]["reason"] == (
        "terminal_missing_completion_receipt")


def test_adoption_read_failure_fails_closed():
    t = _eng_grow_ticket()
    bus = FakeBus([t], GYMS, [_adoption_notice(t)],
                  fail_table="fixer_posted_notice_adoptions")
    report = s.scan_support_bus(bus)
    assert report == {"ok": False, "error": s.FAIL_ADOPTION_READ}
    assert "boom" not in str(report) and "SECRET" not in str(report)


def test_malformed_adoption_page_fails_closed():
    t = _eng_grow_ticket()

    class BadBus(FakeBus):
        def _get(self, table, params):
            if table == "fixer_posted_notice_adoptions":
                return "not-a-list"
            return super()._get(table, params)

    report = s.scan_support_bus(
        BadBus([t], GYMS, [_adoption_notice(t)]))
    assert report == {"ok": False, "error": s.FAIL_ADOPTION_READ}


def test_non_terminal_tickets_never_read_adoption_table():
    tickets = [_ticket(1, status="working", resolved_at=None)]
    bus = FakeBus(tickets, GYMS)
    report = s.scan_support_bus(bus)
    assert report["ok"]
    assert all(c[0] != "fixer_posted_notice_adoptions" for c in bus.calls)


def test_other_terminal_tickets_never_read_special_adoption_table():
    bus = FakeBus([_ticket(1)], GYMS)
    assert s.scan_support_bus(bus)["ok"]
    assert all(c[0] != "fixer_posted_notice_adoptions" for c in bus.calls)
