"""Portal draft #869: held requester-success resolve-notice dispatch exception.

The service_role RPC fixer_held_requester_success_dispatch_eligible is the ONLY
authority that may waive the verified-fix PR gate, and only for the exact notice
shape (kind=status, resolve_notice, fixer_requester_success, byte-exact body).
Everything else -- fresh requester/route/hold, membership, replay, immutable
send admission, one-shot ts/readback, no-resend-after-uncertainty -- is
unchanged. These tests use a fake transport; no live RPC or Slack call is made.
"""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import sys
import time
import uuid

import pytest

from agent.slack_convo import outbox as OB
from agent.slack_convo import adapter as _a
from agent.slack_convo.bus import Bus

TICKET_ID = str(uuid.uuid4())
ROW_ID = str(uuid.uuid4())
BODY = OB.HELD_REQUESTER_SUCCESS_NOTICE_BODY
NOW_TS = str(time.time() + 120)

INBOUND = {
    "id": str(uuid.uuid4()), "ticket_id": TICKET_ID, "direction": "inbound",
    "author_type": "client", "created_at": "2026-10-10T00:00:00+00:00",
    "body": "yes, this is fixed", "attachments": {"surface": "portal_ticket_bridge"},
}


def _ticket(**over):
    ticket = {
        "id": TICKET_ID, "product": "portal", "source": "slack_conversation",
        "client_id": "gym-held-1", "bot_identity": "scout",
        "slack_user_id": "U_CLIENT", "slack_channel_id": "G_CLIENT",
        "slack_thread_ts": "1700.0001", "request_version": 5,
        "status": "hold", "lane": "hold", "hold_tier": "routine",
        "classification": "answerable_question", "escalated": False,
        "identity_kind": "client", "is_test": False,
        "raw_text": "the schedule is wrong",
        "created_at": "2026-10-09T00:00:00+00:00",
        # The whole point of the exception: no PR, no release proof.
        "fix_pr_url": None, "verification_after": None,
    }
    ticket.update(over)
    return ticket


def _att(bus, ticket, **over):
    key = OB._current_fixer_request_key(bus, ticket)
    att = {
        "kind": _a.KIND_STATUS, "identity": "scout", "recipient_kind": "client",
        "surface": "portal_ticket_bridge", "fixer": True,
        "resolve_notice": True, "fixer_requester_success": True,
        "released_by": "operator", "request_key": key,
        "request_version": ticket["request_version"],
    }
    att.update(over)
    return att


class _Resp:
    def __init__(self, status_code, value):
        self.status_code = status_code
        self._value = value

    def json(self):
        return self._value


class HeldBus:
    """Minimal durable store + HTTP transport for the #869 dispatch path."""

    def __init__(self, ticket, row):
        self._ticket = ticket
        self.row = row
        self.rpc_calls = []
        self.rpc_events = []  # ordered with Slack post events
        self.marked = []
        self.resolved_with = []
        self.attest_result = True
        self.attest_status = 200
        self.attest_raises = False
        self.logs = []

    # -- store reads ------------------------------------------------------
    def ticket(self, _tid):
        return deepcopy(self._ticket)

    def message(self, _mid):
        return deepcopy(self.row)

    def messages(self, _tid, limit=1000):
        return [deepcopy(INBOUND)]

    def inbound_count(self, _tid):
        return 1

    # -- mutations --------------------------------------------------------
    def claim_message(self, _mid):
        self.row["delivery_status"] = "posting"
        return deepcopy(self.row)

    def prepare_fixer_delivery(self, mid, intent, expected_attachments=None):
        assert mid == self.row["id"]
        assert expected_attachments is None or \
            all(self.row["attachments"].get(k) == v
                for k, v in expected_attachments.items())
        self.row["attachments"]["fixer_slack_delivery_intent"] = dict(intent)
        return deepcopy(self.row)

    def record_fixer_delivery_timestamp(self, mid, intent, ts):
        self.row["slack_ts"] = ts
        return deepcopy(self.row)

    def transition_fixer_delivery(self, mid, status, slack_ts=None,
                                  meta_update=None, **_kw):
        self.row["delivery_status"] = status
        if slack_ts:
            self.row["slack_ts"] = slack_ts
        if meta_update:
            self.row["attachments"].update(meta_update)
        return deepcopy(self.row)

    def record_outbound(self, **kw):
        self.marked.append(("alert", kw.get("body")))

    def mark_message(self, mid, status, meta_update=None, slack_ts=None):
        self.marked.append((mid, status, meta_update))
        self.row["delivery_status"] = status
        return deepcopy(self.row)

    def resolve_current_delivery(self, *args):
        self.resolved_with.append(args)
        resolved = deepcopy(self._ticket)
        resolved.update(status="resolved", escalated=False, hold_tier=None)
        return resolved

    # -- HTTP transport for the attestation RPC ----------------------------
    def _client(self):
        bus = self

        class Client:
            def post(self, url, data=None, headers=None, timeout=None):
                name = url.rsplit("rpc/", 1)[-1]
                body = json.loads(data or "{}")
                assert name == "fixer_held_requester_success_dispatch_eligible"
                bus.rpc_calls.append(body)
                bus.rpc_events.append("rpc")
                if bus.attest_raises:
                    raise TimeoutError("attestation transport lost")
                if bus.attest_status >= 400:
                    return _Resp(bus.attest_status, None)
                return _Resp(200, bus.attest_result)

        return Client()

    def _rest(self, path):
        return f"https://held.test/rest/v1/{path}"

    def _headers(self):
        return {"apikey": "synthetic"}


def _row(bus, ticket, att=None, body=BODY, **over):
    row = {
        "id": ROW_ID, "ticket_id": TICKET_ID, "direction": "outbound",
        "author_type": "scout", "body": body, "delivery_status": "ready",
        "delivery_request_version": ticket["request_version"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "attachments": att if att is not None else _att(bus, ticket),
    }
    row.update(over)
    return row


def _dispatch(bus, row, posts, monkeypatch):
    # The full suite can take fifteen minutes after this module is imported.
    # Slack readback must use a timestamp newer than this dispatch's intent.
    post_ts = str(time.time() + 120)
    monkeypatch.setattr(OB, "_post_support_resolution",
                        lambda *a, **kw: (posts.append(("post", a[5])),
                                          bus.rpc_events.append("post"),
                                          post_ts)[-1])
    identity = SimpleNamespace(name="scout", bot_user_id=lambda: "U_SCOUT")

    def readback(channel, **_kw):
        return {"ok": True, "channel": channel,
                "messages": [{"ts": post_ts, "text": posts[-1][1], "user": "U_SCOUT"}]}

    summary = {"posted": 0, "held": 0, "suppressed": 0, "skipped": 0, "resolved": 0}
    OB._dispatch_one(bus, lambda *a, **kw: post_ts, deepcopy(row),
                     identity=identity, log=bus.logs.append, summary=summary,
                     member_check=lambda _c, _u: True, readback=readback)
    return summary


# --------------------------------------------------------------------------
# helper-level gates
# --------------------------------------------------------------------------

def test_candidate_requires_exact_shape():
    att = {"fixer": True, "fixer_requester_success": True, "resolve_notice": True,
           "recipient_kind": "client"}
    assert OB._held_requester_success_candidate(att, _a.KIND_STATUS, BODY)
    assert not OB._held_requester_success_candidate(att, _a.KIND_ANSWER, BODY)
    assert not OB._held_requester_success_candidate(
        att, _a.KIND_STATUS, BODY + " ")
    assert not OB._held_requester_success_candidate(
        att, _a.KIND_STATUS, BODY.replace("closing", "CLOSING"))
    assert not OB._held_requester_success_candidate(
        {**att, "resolve_notice": False}, _a.KIND_STATUS, BODY)
    assert not OB._held_requester_success_candidate(
        {**att, "recipient_kind": "staff"}, _a.KIND_STATUS, BODY)
    assert not OB._held_requester_success_candidate(
        {**att, "fixer": False}, _a.KIND_STATUS, BODY)
    assert not OB._held_requester_success_candidate(
        {**att, "fixer_requester_success": "true"}, _a.KIND_STATUS, BODY)
    assert not OB._held_requester_success_candidate(
        {"resolve_notice": True}, _a.KIND_STATUS, BODY)
    assert not OB._held_requester_success_candidate(
        None, _a.KIND_STATUS, BODY)


def test_posted_finalization_walks_past_a_stuck_first_page(monkeypatch):
    rows = [{"id": str(uuid.uuid4()), "ticket_id": TICKET_ID,
             "created_at":
                 f"2026-10-10T{index // 60:02d}:{index % 60:02d}:00+00:00"}
            for index in range(101)]

    class PagingBus:
        def pending_fixer_finalization(self, _identity, limit=100, after=None):
            remaining = [row for row in rows if not after or
                         (row["created_at"], row["id"]) >
                         (after["created_at"], after["id"])]
            return remaining[:limit]

        def ticket(self, _ticket_id):
            return {"id": TICKET_ID}

    seen = []
    monkeypatch.setattr(OB, "_finalize_fixer_post",
                        lambda _bus, _ticket, row, *_args: seen.append(row["id"]))
    bus = PagingBus()
    identity = SimpleNamespace(name="scout")
    OB._reconcile_posted_fixer(bus, identity, lambda _msg: None, {})
    assert len(seen) == 100
    OB._reconcile_posted_fixer(bus, identity, lambda _msg: None, {})
    assert len(seen) == 101
    assert seen[-1] == rows[-1]["id"]


def test_posted_finalization_query_uses_keyset_cursor(monkeypatch):
    bus = Bus.__new__(Bus)
    captured = []
    monkeypatch.setattr(bus, "_get", lambda _table, params: captured.append(params) or [])
    bus.pending_fixer_finalization("scout", after={
        "created_at": "2026-10-10T00:00:00+00:00", "id": ROW_ID})
    assert captured[0]["order"] == "created_at.asc,id.asc"
    assert "created_at.gt" in captured[0]["or"]
    assert f'id.gt."{ROW_ID}"' in captured[0]["or"]


def test_attestation_payload_is_exact_and_current():
    ticket = _ticket()
    bus = HeldBus(ticket, _row(HeldBus(ticket, {}), ticket))
    assert OB._held_requester_success_attested(bus, ticket, ROW_ID, BODY)
    assert bus.rpc_calls == [{
        "p_ticket_id": TICKET_ID,
        "p_request_version": 5,
        "p_notice_message_id": ROW_ID,
        "p_body": BODY,
    }]


@pytest.mark.parametrize("mode", ["raise", "http500", "false", "dict", "str_true"])
def test_attestation_fails_closed(mode):
    ticket = _ticket()
    bus = HeldBus(ticket, {})
    if mode == "raise":
        bus.attest_raises = True
    elif mode == "http500":
        bus.attest_status = 500
    elif mode == "false":
        bus.attest_result = False
    elif mode == "dict":
        bus.attest_result = {"eligible": True}
    else:
        bus.attest_result = "true"
    assert not OB._held_requester_success_attested(bus, ticket, ROW_ID, BODY)


def test_attestation_rejects_bad_version_and_notice():
    ticket = _ticket()
    bus = HeldBus(ticket, {})
    assert not OB._held_requester_success_attested(
        bus, _ticket(request_version="5"), ROW_ID, BODY)
    assert not OB._held_requester_success_attested(
        bus, _ticket(request_version=-1), ROW_ID, BODY)
    assert not OB._held_requester_success_attested(bus, ticket, None, BODY)
    assert not OB._held_requester_success_attested(bus, None, ROW_ID, BODY)
    assert bus.rpc_calls == []


def test_normal_code_fix_notice_never_calls_attestation(monkeypatch):
    """The exception must not change any other FIXER notice's gate."""
    ticket = _ticket(status="merged", hold_tier=None,
                     fix_pr_url="https://github.com/example/echo/pull/317",
                     verification_after={"fixer": {"request_key": "rk"}})
    bus = HeldBus(ticket, {})
    att = {"kind": _a.KIND_STATUS, "resolve_notice": True, "request_key": "rk",
           "pr_url": ticket["fix_pr_url"]}
    monkeypatch.setattr(OB, "_verified_fix_notice", lambda *_a, **_kw: True)
    assert OB._customer_fix_release_ok(bus, ticket, att, _a.KIND_STATUS,
                                       "handled", notice_id=ROW_ID)
    assert bus.rpc_calls == []
    # Release-key binding still enforced for the normal path.
    monkeypatch.setattr(OB, "_verified_fix_notice", lambda *_a, **_kw: True)
    assert not OB._customer_fix_release_ok(
        bus, _ticket(status="merged", hold_tier=None,
                     verification_after={"fixer": {"request_key": "other"}}),
        att, _a.KIND_STATUS, "handled", notice_id=ROW_ID)
    monkeypatch.setattr(OB, "_verified_fix_notice", lambda *_a, **_kw: False)
    assert not OB._customer_fix_release_ok(bus, ticket, att, _a.KIND_STATUS,
                                           "handled", notice_id=ROW_ID)
    assert bus.rpc_calls == []


def test_forged_flag_without_attestation_is_not_an_exception():
    """fixer_requester_success: true alone never bypasses the verified gate."""
    ticket = _ticket()
    bus = HeldBus(ticket, {})
    bus.attest_result = False
    att = {"fixer_requester_success": True, "resolve_notice": True}
    assert not OB._customer_fix_release_ok(bus, ticket, att, _a.KIND_STATUS,
                                           BODY, notice_id=ROW_ID)


def test_malformed_marked_notice_cannot_borrow_code_fix_proof(monkeypatch):
    ticket = _ticket(status="merged", hold_tier=None,
                     verification_after={"fixer": {"request_key": "rk"}})
    bus = HeldBus(ticket, {})
    att = {"fixer_requester_success": True, "resolve_notice": True,
           "request_key": "rk"}
    monkeypatch.setattr(OB, "_verified_fix_notice", lambda *_a, **_kw: True)
    assert not OB._customer_fix_release_ok(bus, ticket, att, _a.KIND_STATUS,
                                           BODY + " extra", notice_id=ROW_ID)
    assert bus.rpc_calls == []


@pytest.mark.parametrize("recipient,marker", [
    ("staff", True), ("coach", True), ("client", "true"), ("client", False),
])
def test_malformed_marked_notice_cannot_use_ordinary_or_code_fix_path(
        monkeypatch, recipient, marker):
    ticket = _ticket(status="merged", hold_tier=None,
                     verification_after={"fixer": {"request_key": "rk"}})
    bus = HeldBus(ticket, {})
    att = {"fixer_requester_success": marker, "recipient_kind": recipient,
           "resolve_notice": True, "request_key": "rk"}
    monkeypatch.setattr(OB, "_verified_fix_notice", lambda *_a, **_kw: True)
    assert OB._customer_fix_reply(ticket, att, BODY)
    assert not OB._customer_fix_release_ok(
        bus, ticket, att, _a.KIND_STATUS, BODY, notice_id=ROW_ID)
    assert bus.rpc_calls == []


# --------------------------------------------------------------------------
# dispatch-level gates
# --------------------------------------------------------------------------

def test_attested_held_success_notice_dispatches_exact_body(monkeypatch):
    """Positive exact case: held ticket, NO PR, RPC attests -> exact post."""
    monkeypatch.setattr(sys.modules[__name__], "NOW_TS", "1")
    ticket = _ticket()
    bus = HeldBus(ticket, None)
    bus.row = _row(bus, ticket)
    posts = []
    summary = _dispatch(bus, bus.row, posts, monkeypatch)
    assert summary["posted"] == 1
    assert posts == [("post", BODY)]  # no Blake mention, no mutation
    # Attested on every dispatch gate AND immediately before the Slack POST.
    assert len(bus.rpc_calls) >= 3
    assert all(call["p_notice_message_id"] == ROW_ID and call["p_body"] == BODY
               and call["p_request_version"] == 5
               for call in bus.rpc_calls)
    post_index = bus.rpc_events.index("post")
    assert bus.rpc_events[post_index - 1] == "rpc"  # attested immediately pre-POST


def test_rpc_unavailable_suppresses_without_send(monkeypatch):
    ticket = _ticket()
    bus = HeldBus(ticket, None)
    bus.row = _row(bus, ticket)
    bus.attest_raises = True
    posts = []
    summary = _dispatch(bus, bus.row, posts, monkeypatch)
    assert posts == []
    assert summary["posted"] == 0
    assert summary["suppressed"] == 1


def test_stale_request_version_fails_closed(monkeypatch):
    """A newer requester correction makes the attestation stale: no send."""
    ticket = _ticket()
    bus = HeldBus(ticket, None)
    bus.row = _row(bus, ticket)
    posts = []

    def stale(*_a, **_kw):
        t = deepcopy(bus._ticket)
        t["request_version"] = 6  # corrected after the row was stamped
        return t

    monkeypatch.setattr(bus, "ticket", stale)
    summary = _dispatch(bus, bus.row, posts, monkeypatch)
    assert posts == []
    assert summary["posted"] == 0
    assert summary["suppressed"] == 1


def test_body_mismatch_uses_normal_gate_and_never_posts(monkeypatch):
    ticket = _ticket()
    bus = HeldBus(ticket, None)
    bus.row = _row(bus, ticket, body=BODY + " (updated)")
    posts = []
    summary = _dispatch(bus, bus.row, posts, monkeypatch)
    assert posts == []
    assert summary["suppressed"] == 1
    # Not a candidate: the attestation RPC must never be consulted.
    assert bus.rpc_calls == []


def test_route_change_after_attestation_fails_closed(monkeypatch):
    ticket = _ticket()
    bus = HeldBus(ticket, None)
    bus.row = _row(bus, ticket)
    posts = []
    reads = {"n": 0}

    def reroute(_tid):
        reads["n"] += 1
        t = deepcopy(bus._ticket)
        if reads["n"] > 2:
            t["slack_channel_id"] = "G_OTHER"  # destination moved mid-dispatch
        return t

    monkeypatch.setattr(bus, "ticket", reroute)
    summary = _dispatch(bus, bus.row, posts, monkeypatch)
    assert posts == []
    assert summary["posted"] == 0


def test_held_success_notice_never_uses_generic_resolver():
    """Scout's posted/readback exact close RPC alone may close this ticket."""
    ticket = _ticket()
    bus = HeldBus(ticket, None)
    att = _att(bus, ticket)
    bus.row = _row(bus, ticket, att=att, delivery_status="posted",
                   slack_ts=NOW_TS)
    ticket["verification_after"] = {"fixer": {"request_key": att["request_key"]}}
    summary = {"resolved": 0}
    OB._after_answer_posted(bus, ticket, bus.row, _a.KIND_STATUS, summary,
                            att, SimpleNamespace(name="scout"), lambda _m: None)
    assert summary["resolved"] == 0
    assert bus.resolved_with == []
    assert bus.rpc_calls == []


def test_held_success_generic_close_stays_skipped_without_binding():
    ticket = _ticket()
    bus = HeldBus(ticket, None)
    bus.attest_result = False
    att = _att(bus, ticket)
    bus.row = _row(bus, ticket, att=att, delivery_status="posted",
                   slack_ts=NOW_TS)
    summary = {"resolved": 0}
    OB._after_answer_posted(bus, ticket, bus.row, _a.KIND_STATUS, summary,
                            att, SimpleNamespace(name="scout"), lambda _m: None)
    assert summary["resolved"] == 0
    assert bus.resolved_with == []
