"""Focused tests for the read-only client support exception classifier.

Fixtures reflect the real portal fields at main (SHA 4cdbcdf9):
delivery_request_version is a TOP-LEVEL message column; attachments carry
delivery_expected_* identity keys, the predecessor status and the identity
fence.
"""
from __future__ import annotations

import pytest

from agent.jobs import client_support_reconciler as r

# The test gym: client_id is a UUID; zz-test-gym is its SLUG, never its ID.
TEST_GYM_ID = "ca397eec-519a-4524-b666-d048199c76b2"
TEST_GYM = {"id": TEST_GYM_ID, "slug": "zz-test-gym"}
REAL_GYM = {"id": "9f1c2f68-2f2a-4f6e-9d3b-9a3f2b1c0a01", "slug": "crossfit-eng"}
DALE_GYM = {"id": "6ee04ee4-13a5-47db-8416-7b8ee3e61ab8", "slug": "eng"}


def _gym_for(*gyms):
    by_id = {g["id"]: g for g in gyms}
    return by_id.get


def _ticket(**over):
    base = {
        "id": "t-1",
        "product": "echo",
        "source": "website_tab",
        "status": "resolved",
        "resolved_at": "2026-10-08T00:00:00Z",
        "request_version": 3,
        "client_id": REAL_GYM["id"],
        "bot_identity": "echo",
        "slack_user_id": "U123",
        "classification": "answerable_question",
        "is_test": False,
        # Guarded is the default; explicit False opts out.
        "client_delivery_guard_required": True,
    }
    base.update(over)
    return base


def _visible_msg(kind="answer", *, ticket_id="t-1", version=None, **attach):
    attachments = {"kind": kind}
    attachments.update(attach)
    msg = {"ticket_id": ticket_id, "direction": "outbound", "delivery_status": "posted",
           "attachments": attachments}
    if version is not None:
        msg["delivery_request_version"] = version
    return msg


def _guarded_msg(ticket, *, version=None, **attach):
    base = {
        "delivery_expected_product": ticket["product"],
        "delivery_expected_client_id": ticket["client_id"],
        "delivery_expected_bot_identity": ticket["bot_identity"],
        "delivery_expected_slack_user_id": ticket["slack_user_id"],
        "delivery_expected_status": "verification",
        "delivery_expected_classification": ticket["classification"],
        "delivery_identity_fence": True,
    }
    base.update(attach)
    return _visible_msg(ticket_id=ticket["id"], version=ticket["request_version"] if version is None
                        else version, **base)


# --- guarded receipt parity -------------------------------------------------

def test_guarded_valid_receipt():
    t = _ticket()
    assert r.classify_ticket(t, [_guarded_msg(t)], gym=REAL_GYM)["category"] == r.SATISFIED


def test_guard_default_when_flag_absent_or_null():
    # Absent and null flags are GUARDED: a plain posted outbound is not enough.
    t = _ticket()
    del t["client_delivery_guard_required"]
    res = r.classify_ticket(t, [_visible_msg(version=t["request_version"])],
                            gym=REAL_GYM)
    assert res["category"] == r.EXCEPTION
    assert res["reason"] == "terminal_missing_completion_receipt"
    t = _ticket(client_delivery_guard_required=None)
    res = r.classify_ticket(t, [_visible_msg(version=t["request_version"])],
                            gym=REAL_GYM)
    assert res["category"] == r.EXCEPTION
    # Only an explicit False opts out.
    t = _ticket(client_delivery_guard_required=False)
    res = r.classify_ticket(t, [_visible_msg()], gym=REAL_GYM)
    assert res["category"] == r.SATISFIED


def test_delivery_request_version_is_top_level_not_attachments():
    t = _ticket()
    msg = _guarded_msg(t)
    # Move the version into attachments: must NOT qualify.
    msg["attachments"]["delivery_request_version"] = t["request_version"]
    del msg["delivery_request_version"]
    assert r.qualifying_completion_receipt(t, [msg]) is None
    # Stale top-level version rejected.
    assert r.qualifying_completion_receipt(
        t, [_guarded_msg(t, version=2)]) is None


def test_guarded_status_resolve_notice_receipt():
    t = _ticket()
    msg = _guarded_msg(t, kind="status", resolve_notice=True)
    assert r.classify_ticket(t, [msg], gym=REAL_GYM)["category"] == r.SATISFIED


def test_wrong_expected_identity_rejected():
    t = _ticket()
    msg = _guarded_msg(t, delivery_expected_client_id="gym-other")
    assert r.qualifying_completion_receipt(t, [msg]) is None


def test_both_null_identity_fields_match():
    # sameNullableText: both null is a MATCH.
    t = _ticket(slack_user_id=None)
    msg = _guarded_msg(t, delivery_expected_slack_user_id=None)
    assert r.qualifying_completion_receipt(t, [msg]) is not None


def test_one_null_identity_field_never_matches():
    t = _ticket(slack_user_id=None)
    msg = _guarded_msg(t, delivery_expected_slack_user_id="U999")
    assert r.qualifying_completion_receipt(t, [msg]) is None
    t2 = _ticket()
    msg2 = _guarded_msg(t2, delivery_expected_slack_user_id=None)
    assert r.qualifying_completion_receipt(t2, [msg2]) is None


def test_missing_expected_key_fails():
    t = _ticket(slack_user_id=None)
    msg = _guarded_msg(t)
    del msg["attachments"]["delivery_expected_slack_user_id"]
    assert r.qualifying_completion_receipt(t, [msg]) is None
    msg2 = _guarded_msg(t)
    del msg2["attachments"]["delivery_expected_classification"]
    assert r.qualifying_completion_receipt(t, [msg2]) is None


def test_expected_classification_must_equal_ticket_classification():
    t = _ticket()
    msg = _guarded_msg(t, delivery_expected_classification="code_fix",
                       delivery_expected_status="resolved")
    assert r.qualifying_completion_receipt(t, [msg]) is None


def test_expected_status_is_predecessor_whitelisted():
    t = _ticket()
    # Predecessor (verification, answerable_question) qualifies.
    assert r.qualifying_completion_receipt(t, [_guarded_msg(t)]) is not None
    # A disallowed predecessor pair does not.
    msg = _guarded_msg(t, delivery_expected_status="merged",
                       delivery_expected_classification="answerable_question")
    assert r.qualifying_completion_receipt(t, [msg]) is None


def test_missing_identity_fence_rejected():
    t = _ticket()
    assert r.qualifying_completion_receipt(
        t, [_guarded_msg(t, delivery_identity_fence=None)]) is None
    assert r.qualifying_completion_receipt(
        t, [_guarded_msg(t, delivery_identity_fence=False)]) is None


def test_internal_posted_kind_does_not_qualify():
    t = _ticket(client_delivery_guard_required=False)
    for kind in ("escalation", "fixer_request", "hold_notice"):
        assert r.qualifying_completion_receipt(t, [_visible_msg(kind)]) is None
    res = r.classify_ticket(t, [_visible_msg("escalation")], gym=REAL_GYM)
    assert res["category"] == r.EXCEPTION


def test_unposted_or_inbound_does_not_qualify():
    t = _ticket(client_delivery_guard_required=False)
    inbound = {"direction": "inbound", "delivery_status": "posted",
               "attachments": {"kind": "answer"}}
    ready = {"direction": "outbound", "delivery_status": "ready",
             "attachments": {"kind": "answer"}}
    assert r.qualifying_completion_receipt(t, [inbound, ready]) is None


# --- gym verification --------------------------------------------------------

def test_gym_unverified_when_no_mapping():
    t = _ticket()
    res = r.classify_ticket(t, [_guarded_msg(t)])
    assert res["category"] == r.ANOMALY
    assert res["reason"] == "gym_unverified"
    assert res["client_visible_working"] is False


def test_gym_mismatch_is_anomaly():
    t = _ticket()
    other = {"id": "someone-else", "slug": "crossfit-eng"}
    res = r.classify_ticket(t, [_guarded_msg(t)], gym=other)
    assert res["category"] == r.ANOMALY
    assert res["reason"] == "gym_mismatch"


def test_test_gym_uuid_with_test_slug_is_out_of_scope_not_real_client():
    # client_id is the UUID; zz-test-gym is the SLUG. Never compare id to slug.
    t = _ticket(client_id=TEST_GYM_ID, is_test=False)
    res = r.classify_ticket(t, [_guarded_msg(t)], gym=TEST_GYM)
    assert res["category"] == r.OUT_OF_SCOPE
    assert res["reason"] == "test_gym"


def test_any_test_or_demo_slug_is_out_of_scope():
    for slug in ("zz-test-gym", "zz-test-2", "demo-west"):
        gym = {"id": REAL_GYM["id"], "slug": slug}
        t = _ticket()
        res = r.classify_ticket(t, [_guarded_msg(t)], gym=gym)
        assert res["category"] == r.OUT_OF_SCOPE, slug


def test_legitimate_slug_with_test_syllable_is_not_excluded():
    t = _ticket()
    gym = {"id": REAL_GYM["id"], "slug": "contest-fitness"}
    assert r.classify_ticket(t, [_guarded_msg(t)], gym=gym)["category"] == r.SATISFIED


def test_missing_gym_slug_is_not_verified_as_real_client():
    t = _ticket()
    gym = {"id": REAL_GYM["id"]}
    result = r.classify_ticket(t, [_guarded_msg(t)], gym=gym)
    assert result["reason"] == "gym_slug_unverified"
    assert result["client_visible_working"] is False


def test_null_client_id_is_separate_anomaly():
    t = _ticket(client_id=None)
    res = r.classify_ticket(t, [_guarded_msg(t)], gym=None)
    assert res["category"] == r.ANOMALY
    assert res["reason"] == "null_client_id"


def test_is_test_true_out_of_scope():
    t = _ticket(is_test=True)
    assert r.classify_ticket(t, [], gym=REAL_GYM)["category"] == r.OUT_OF_SCOPE


# --- status behavior ---------------------------------------------------------

def test_merged_without_resolved_at_is_anomaly_and_shows_working():
    t = _ticket(status="merged", resolved_at=None)
    res = r.classify_ticket(t, [_guarded_msg(t)], gym=REAL_GYM)
    assert res["category"] == r.ANOMALY
    assert res["reason"] == "merged_missing_resolved_at"
    assert res["client_visible_working"] is True


def test_merged_with_resolved_at_and_receipt_satisfied():
    t = _ticket(status="merged")
    assert r.classify_ticket(t, [_guarded_msg(t)], gym=REAL_GYM)["category"] == r.SATISFIED


def test_resolved_without_receipt_is_exception_and_working():
    t = _ticket()
    res = r.classify_ticket(t, [], gym=REAL_GYM)
    assert res["category"] == r.EXCEPTION
    assert res["reason"] == "terminal_missing_completion_receipt"
    assert res["client_visible_working"] is True


def test_resolved_null_timestamp_with_receipt_is_anomaly_but_not_working():
    # Receipt present: the portal displays done despite the null timestamp.
    t = _ticket(status="resolved", resolved_at=None)
    res = r.classify_ticket(t, [_guarded_msg(t)], gym=REAL_GYM)
    assert res["category"] == r.ANOMALY
    assert res["reason"] == "resolved_missing_resolved_at"
    assert res["client_visible_working"] is False


def test_resolved_null_timestamp_without_receipt_shows_working():
    t = _ticket(status="resolved", resolved_at=None)
    res = r.classify_ticket(t, [], gym=REAL_GYM)
    assert res["category"] == r.ANOMALY
    assert res["reason"] == "resolved_missing_resolved_at"
    assert res["client_visible_working"] is True


def test_done_needs_receipt_only():
    t = _ticket(status="done", resolved_at=None)
    res = r.classify_ticket(t, [_guarded_msg(t)], gym=REAL_GYM)
    assert res["category"] == r.SATISFIED
    res2 = r.classify_ticket(t, [], gym=REAL_GYM)
    assert res2["category"] == r.EXCEPTION
    assert res2["client_visible_working"] is True


def test_working_statuses_show_received_or_working():
    for status in ("new", "triage", "working", "fixing", "verification", "hold"):
        t = _ticket(status=status, resolved_at=None)
        res = r.classify_ticket(t, [], gym=REAL_GYM)
        assert res["category"] == r.EXCEPTION, status
        assert res["reason"] == "client_request_open"
        assert res["client_visible_working"] is (status != "new"), status


def test_approved_displays_approved_not_working():
    t = _ticket(status="approved", resolved_at=None)
    res = r.classify_ticket(t, [], gym=REAL_GYM)
    assert res["category"] == r.EXCEPTION
    assert res["reason"] == "client_approved_pending_action"
    assert res["client_visible_working"] is False


def test_failed_displays_refused_not_working():
    t = _ticket(status="failed", resolved_at=None)
    res = r.classify_ticket(t, [], gym=REAL_GYM)
    assert res["category"] == r.EXCEPTION
    assert res["reason"] == "client_sees_refused"
    assert res["client_visible_working"] is False


def test_unknown_status_is_exception_not_success():
    t = _ticket(status="zorp", resolved_at=None)
    res = r.classify_ticket(t, [_guarded_msg(t)], gym=REAL_GYM)
    assert res["category"] == r.EXCEPTION
    assert res["reason"] == "unknown_status"


def test_client_facing_sources_guarded():
    for source in ("website_tab", "slack_conversation", "coach_portal"):
        t = _ticket(status="hold", resolved_at=None, source=source)
        res = r.classify_ticket(t, [], gym=REAL_GYM)
        assert res["category"] == r.EXCEPTION
        assert res["client_visible_working"] is True


def test_non_client_facing_source_out_of_scope():
    t = _ticket(source="ops_internal", status="hold", resolved_at=None)
    assert r.classify_ticket(t, [], gym=REAL_GYM)["category"] == r.OUT_OF_SCOPE


# --- Dale's four production ticket state shapes -----------------------------

def test_dale_four_statuses_fixture():
    dale = dict(client_id=DALE_GYM["id"])
    items = [
        (_ticket(id="35e066d0-d9bc-40e6-aef8-86719a010590", status="resolved",
                 resolved_at=None, client_delivery_guard_required=False, **dale), []),
        (_ticket(id="cd08b049-1bf6-4b71-bb80-35d42d9d9de2", status="verification",
                 resolved_at=None, **dale), []),
        (_ticket(id="a328078e-3f1d-4d83-8b0c-312fece6e227", status="verification",
                 resolved_at=None, **dale), []),
        (_ticket(id="554ac760-b0b3-413a-919f-4e13cff6d3fc", status="merged",
                 resolved_at=None, classification="code_fix", **dale), []),
    ]
    summary = r.reconcile(items, gym_for=_gym_for(DALE_GYM))
    by_id = {x["ticket_id"]: x
             for bucket in summary.values() for x in bucket}
    assert set(by_id) == {ticket["id"] for ticket, _ in items}
    assert all(row["client_visible_working"] is True for row in by_id.values())
    assert by_id["35e066d0-d9bc-40e6-aef8-86719a010590"]["reason"] == "resolved_missing_resolved_at"
    assert by_id["cd08b049-1bf6-4b71-bb80-35d42d9d9de2"]["reason"] == "client_request_open"
    assert by_id["a328078e-3f1d-4d83-8b0c-312fece6e227"]["reason"] == "client_request_open"
    assert by_id["554ac760-b0b3-413a-919f-4e13cff6d3fc"]["reason"] == "merged_missing_resolved_at"


def test_receipt_for_another_ticket_cannot_close_this_one():
    ticket = _ticket(id="ticket-A")
    wrong_ticket_receipt = _guarded_msg(ticket)
    wrong_ticket_receipt["ticket_id"] = "ticket-B"
    result = r.classify_ticket(ticket, [wrong_ticket_receipt], gym=REAL_GYM)
    assert result["reason"] == "terminal_missing_completion_receipt"
    assert result["client_visible_working"] is True


def test_unguarded_receipt_for_another_ticket_cannot_close_this_one():
    ticket = _ticket(id="ticket-A", client_delivery_guard_required=False)
    result = r.classify_ticket(ticket, [_visible_msg(ticket_id="ticket-B")], gym=REAL_GYM)
    assert result["reason"] == "terminal_missing_completion_receipt"


def test_request_version_must_be_js_safe_integer():
    ticket = _ticket(request_version=2**53)
    receipt = _guarded_msg(ticket)
    assert r.qualifying_completion_receipt(ticket, [receipt]) is None
    ticket = _ticket(request_version=1)
    receipt = _guarded_msg(ticket)
    receipt["delivery_request_version"] = True
    assert r.qualifying_completion_receipt(ticket, [receipt]) is None


# --- generic invariants ------------------------------------------------------

def test_malformed_ticket_does_not_claim_completion():
    res = r.classify_ticket(None, [])
    assert res["category"] == r.ANOMALY
    assert res["client_visible_working"] is False
    t = _ticket(status=None)
    assert r.classify_ticket(t, [], gym=REAL_GYM)["reason"] == "missing_status"


def test_results_never_include_raw_text():
    t = _ticket(raw_text="SECRET CLIENT TEXT")
    res = r.classify_ticket(t, [], gym=REAL_GYM)
    assert "SECRET" not in str(res)


def test_reconcile_summary_buckets():
    items = [
        (_ticket(id="a"), [_guarded_msg(_ticket(id="a"))]),
        (_ticket(id="b", status="hold", resolved_at=None), []),
        (_ticket(id="c", client_id=None), []),
        (_ticket(id="d", is_test=True), []),
    ]
    summary = r.reconcile(items, gym_for=_gym_for(REAL_GYM))
    assert [x["ticket_id"] for x in summary[r.SATISFIED]] == ["a"]
    assert [x["ticket_id"] for x in summary[r.EXCEPTION]] == ["b"]
    assert [x["ticket_id"] for x in summary[r.ANOMALY]] == ["c"]
    assert [x["ticket_id"] for x in summary[r.OUT_OF_SCOPE]] == ["d"]


# --- keyset pagination -------------------------------------------------------

def _rows(n, prefix="t", start=0):
    return [{"id": f"{prefix}-{i:06d}",
             "created_at": f"2026-10-08T00:{(i // 60) % 60:02d}:{i % 60:02d}Z"}
            for i in range(start, start + n)]


def _keyset_fetch(rows):
    def fetch_page(cursor, page_size):
        start = 0
        if cursor is not None:
            keys = [(r["created_at"], r["id"]) for r in rows]
            start = keys.index(tuple(cursor)) + 1
        return rows[start:start + page_size]
    return fetch_page


def test_fetch_pages_multiple_pages_keyset():
    rows = _rows(1200)
    got = r.fetch_pages(_keyset_fetch(rows), page_size=500)
    assert got == rows


def test_fetch_pages_beyond_200_pages():
    rows = _rows(2050, start=0)
    got = r.fetch_pages(_keyset_fetch(rows), page_size=10, max_pages=300)
    assert len(got) == 2050


def test_fetch_pages_exactly_full_final_page_is_not_assumed_end():
    rows = _rows(1000)
    calls = []

    def fetch_page(cursor, page_size):
        calls.append(cursor)
        return _keyset_fetch(rows)(cursor, page_size)

    got = r.fetch_pages(fetch_page, page_size=500)
    assert len(got) == 1000
    assert len(calls) == 3  # required a third (empty) page read


def test_fetch_pages_rejects_unordered_rows():
    rows = _rows(4)
    rows[1], rows[2] = rows[2], rows[1]
    with pytest.raises(r.PaginationError):
        r.fetch_pages(_keyset_fetch(rows), page_size=10)


def test_fetch_pages_rejects_duplicate_and_malformed():
    with pytest.raises(r.PaginationError):
        r.fetch_pages(lambda c, s: [{"id": "x", "created_at": "2026-10-08T00:00:00Z"},
                                    {"id": "x", "created_at": "2026-10-08T00:00:01Z"}],
                      page_size=2, max_pages=2)
    with pytest.raises(r.PaginationError):
        r.fetch_pages(lambda c, s: [{"id": "x"}, "nope"], page_size=2, max_pages=2)
    with pytest.raises(r.PaginationError):
        r.fetch_pages(lambda c, s: [{"id": "x", "created_at": "not-a-ts"}],
                      page_size=1, max_pages=1)
    with pytest.raises(r.PaginationError):
        r.fetch_pages(lambda c, s: [{"no_id": 1, "created_at": "2026-10-08T00:00:00Z"}],
                      page_size=1, max_pages=1)


def test_fetch_pages_changing_page_is_no_false_success():
    # A backend that ignores the cursor replays the first page: duplicates.
    rows = _rows(20)
    with pytest.raises(r.PaginationError):
        r.fetch_pages(lambda c, s: rows[:s], page_size=10)


def test_fetch_pages_ceiling_fails_closed():
    rows = _rows(10_000)
    with pytest.raises(r.PaginationError):
        r.fetch_pages(_keyset_fetch(rows), page_size=10, max_pages=3)


# --- consumed posted-notice adoption proof (Portal 0645) ---------------------

ADOPTION_TICKET_ID = "cd08b049-1bf6-4b71-bb80-35d42d9d9de2"
ADOPTION_NOTICE_ID = "20adfae9-986b-4391-a74c-671e9d807d3e"
_ADOPTION_RESOLVED_AT = "2026-10-09T21:30:00+00:00"


def _adoption_ticket(**over):
    base = _ticket(
        id=ADOPTION_TICKET_ID,
        status="resolved",
        resolved_at=_ADOPTION_RESOLVED_AT,
        request_version=1,
        client_id=DALE_GYM["id"],
        slack_user_id="U06P23E3Y2Y",
    )
    base.update(over)
    return base


def _adoption_message(**over):
    msg = {
        "id": ADOPTION_NOTICE_ID,
        "ticket_id": ADOPTION_TICKET_ID,
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
    }
    msg.update(over)
    return msg


def _adoption_row(**over):
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


def _adoption_result(ticket=None, messages=None, adoptions="default"):
    t = ticket or _adoption_ticket()
    msgs = [_adoption_message()] if messages is None else messages
    rows = [_adoption_row()] if adoptions == "default" else adoptions
    return r.classify_ticket(t, msgs, gym=DALE_GYM, adoptions=rows)


def test_consumed_adoption_proof_satisfies_terminal_ticket():
    res = _adoption_result()
    assert res["category"] == r.SATISFIED
    assert res["reason"] == "consumed_posted_notice_adoption"
    assert res["client_visible_working"] is False


def test_no_adoption_rows_still_exception():
    res = _adoption_result(adoptions=None)
    assert res["reason"] == "terminal_missing_completion_receipt"
    res = _adoption_result(adoptions=[])
    assert res["reason"] == "terminal_missing_completion_receipt"


def test_multiple_adoption_rows_rejected():
    res = _adoption_result(adoptions=[_adoption_row(), _adoption_row()])
    assert res["reason"] == "terminal_missing_completion_receipt"


def test_unconsumed_adoption_rejected():
    res = _adoption_result(adoptions=[_adoption_row(consumed_at=None)])
    assert res["reason"] == "terminal_missing_completion_receipt"


def test_consumed_at_must_equal_resolved_at():
    res = _adoption_result(
        adoptions=[_adoption_row(consumed_at="2026-10-09T21:31:00+00:00")])
    assert res["reason"] == "terminal_missing_completion_receipt"


def test_adoption_pinned_to_exact_ticket_and_message():
    # A different ticket can never use the proof, even with a row that lies.
    other = _adoption_ticket(id="t-9")
    res = _adoption_result(ticket=other,
                           adoptions=[_adoption_row(ticket_id="t-9")])
    assert res["reason"] == "terminal_missing_completion_receipt"
    # Wrong notice message id in the proof.
    res = _adoption_result(adoptions=[_adoption_row(notice_message_id="m-x")])
    assert res["reason"] == "terminal_missing_completion_receipt"
    # Wrong ticket id in the proof row.
    res = _adoption_result(adoptions=[_adoption_row(ticket_id="t-9")])
    assert res["reason"] == "terminal_missing_completion_receipt"


def test_adoption_version_drift_rejected():
    # Snapshot from a newer request version.
    row = _adoption_row()
    row["ticket_snapshot"] = dict(row["ticket_snapshot"], request_version=2)
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    # Message posted under a newer delivery request version.
    assert _adoption_result(
        messages=[_adoption_message(delivery_request_version=2)]
    )["reason"] == "terminal_missing_completion_receipt"
    # Ticket itself no longer v1.
    assert _adoption_result(ticket=_adoption_ticket(request_version=2))[
        "reason"] == "terminal_missing_completion_receipt"


def test_adoption_snapshot_must_be_predecessor_verification_unresolved():
    row = _adoption_row()
    row["ticket_snapshot"] = dict(row["ticket_snapshot"], status="resolved")
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    row = _adoption_row()
    row["ticket_snapshot"] = dict(row["ticket_snapshot"],
                                  resolved_at=_ADOPTION_RESOLVED_AT)
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    row = _adoption_row()
    row["ticket_snapshot"] = dict(row["ticket_snapshot"],
                                  slack_user_id="U999")
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")


def test_adoption_current_identity_must_match_snapshot():
    # Current ticket identity drift from the pinned v1 snapshot fails.
    assert _adoption_result(ticket=_adoption_ticket(slack_user_id="U999"))[
        "reason"] == "terminal_missing_completion_receipt"
    assert _adoption_result(
        ticket=_adoption_ticket(classification="code_fix"))[
        "reason"] == "terminal_missing_completion_receipt"


def test_adoption_malformed_proof_fields_rejected():
    assert _adoption_result(adoptions=["nope"])["reason"] == (
        "terminal_missing_completion_receipt")
    assert _adoption_result(adoptions=[_adoption_row(
        transcript_sha256="zzzz")])["reason"] == (
        "terminal_missing_completion_receipt")
    assert _adoption_result(adoptions=[_adoption_row(
        ticket_snapshot=None)])["reason"] == (
        "terminal_missing_completion_receipt")
    assert _adoption_result(adoptions=[_adoption_row(
        receipt="nope")])["reason"] == "terminal_missing_completion_receipt"
    assert _adoption_result(adoptions=[_adoption_row(
        independent_review=None)])["reason"] == (
        "terminal_missing_completion_receipt")


def test_adoption_receipt_and_review_binding_required():
    # Receipt not verified / wrong method / wrong notice binding.
    row = _adoption_row()
    row["receipt"] = dict(row["receipt"], verified=False)
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    row = _adoption_row()
    row["receipt"] = dict(row["receipt"], notice_message_id="m-other")
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    # Review not passed.
    row = _adoption_row()
    row["independent_review"] = dict(row["independent_review"], passed=False)
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    # Reviewer must be independent of the author.
    row = _adoption_row()
    row["independent_review"] = dict(row["independent_review"],
                                     reviewer="echo-fixer")
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    # Review must bind this exact ticket / notice / receipt.
    row = _adoption_row()
    row["independent_review"] = dict(row["independent_review"],
                                     ticket_id="t-other")
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    row = _adoption_row()
    row["independent_review"] = dict(row["independent_review"], receipt={})
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")
    row = _adoption_row()
    row["receipt"] = dict(row["receipt"], body_sha256="a" * 64)
    row["independent_review"] = dict(row["independent_review"],
                                      receipt=row["receipt"], body_sha256="a" * 64)
    assert _adoption_result(adoptions=[row])["reason"] == (
        "terminal_missing_completion_receipt")


def test_adoption_notice_message_must_be_posted_with_readback():
    # Message missing entirely.
    assert _adoption_result(messages=[])["reason"] == (
        "terminal_missing_completion_receipt")
    # Not actually posted.
    assert _adoption_result(messages=[_adoption_message(
        delivery_status="ready")])["reason"] == (
        "terminal_missing_completion_receipt")
    # Inbound or wrong ticket.
    assert _adoption_result(messages=[_adoption_message(
        direction="inbound")])["reason"] == (
        "terminal_missing_completion_receipt")
    assert _adoption_result(messages=[_adoption_message(
        ticket_id="t-other")])["reason"] == (
        "terminal_missing_completion_receipt")
    # Readback metadata absent: attachments alone are never trusted.
    msg = _adoption_message()
    msg["attachments"] = {"kind": "status"}
    assert _adoption_result(messages=[msg])["reason"] == (
        "terminal_missing_completion_receipt")
    for changed in ({"author_type": "system"},
                    {"slack_ts": "1791573331.865260"}):
        assert _adoption_result(messages=[_adoption_message(**changed)])["reason"] == (
            "terminal_missing_completion_receipt")
    msg = _adoption_message()
    msg["attachments"] = dict(msg["attachments"],
                               delivery_readback_channel="wrong-channel")
    assert _adoption_result(messages=[msg])["reason"] == (
        "terminal_missing_completion_receipt")


def test_post_close_acknowledgement_is_not_adoption():
    # A separate posted post-close acknowledgement (different message id)
    # never substitutes for the adopted client disposition message.
    ack = _adoption_message(id="ack-1")
    assert _adoption_result(messages=[ack])["reason"] == (
        "terminal_missing_completion_receipt")


def test_adoption_reconcile_plumbing():
    t = _adoption_ticket()
    rows = {t["id"]: [_adoption_row()]}
    summary = r.reconcile([(t, [_adoption_message()])],
                          gym_for=_gym_for(DALE_GYM),
                          adoption_for=rows.get)
    assert [x["ticket_id"] for x in summary[r.SATISFIED]] == [t["id"]]
