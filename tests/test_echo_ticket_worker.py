"""
tests/test_echo_ticket_worker.py -- D46 (Blake, 2026-09-04): the portal-Echo-ticket
bridge. "with echo if someone submits a support echo should receive that then echo
should fix it verify the fix and then send slack message with them and me in the
message."
"""
import os

import pytest

from agent import echo_ticket_worker as W
from agent.slack_convo import adapter as A
from agent.slack_convo import classifier as C


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "true")
    yield


def _off(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "false")


class FakeBus:
    def __init__(self, tickets):
        self.tickets = {t["id"]: dict(t) for t in tickets}
        self.inbound = []
        self.outbound = []
        self.patches = []

    def find_new_tickets(self, *, product, source, limit=20):
        return [t for t in self.tickets.values()
               if t.get("product") == product and t.get("source") == source
               and t.get("status") == "new" and t.get("classification") is None]

    def find_fixing_tickets(self, *, product, limit=20):
        return [t for t in self.tickets.values()
               if t.get("product") == product and t.get("status") == "fixing"]

    def record_inbound(self, **kwargs):
        self.inbound.append(kwargs)
        return ({"id": f"in-{len(self.inbound)}"}, False)

    def record_outbound(self, **kwargs):
        row = {"id": f"out-{len(self.outbound)}", **kwargs}
        self.outbound.append(row)
        return row

    def set_ticket(self, ticket_id, **fields):
        self.patches.append((ticket_id, fields))
        self.tickets[ticket_id].update(fields)


def _ticket(**over):
    row = {
        "id": "t-1", "product": "echo", "source": "website_tab",
        "client_id": "g-1", "reporter": "owner@gym.com",
        "raw_text": "my Instagram posts stopped going out",
        "status": "new", "classification": None,
    }
    row.update(over)
    return row


def _calls():
    log = {"opened": [], "posted": []}

    def open_group_dm(user_ids):
        log["opened"].append(list(user_ids))
        return {"ok": True, "channel_id": "G123"}

    def post_first_message(channel_id, text):
        log["posted"].append((channel_id, text))
        return {"ok": True, "ts": "9999.1"}

    return log, open_group_dm, post_first_message


def _notices():
    calls = []

    def write_hold_notice(**kwargs):
        calls.append(kwargs)
        return {"id": "card-1"}

    return calls, write_hold_notice


# ---- config gate -------------------------------------------------------------------

def test_intake_pass_is_a_full_noop_when_the_flag_is_off(monkeypatch):
    _off(monkeypatch)
    bus = FakeBus([_ticket()])
    log, open_dm, post = _calls()
    _notices_calls, notice = _notices()
    result = W.intake_pass(bus, slack_lookup_email=lambda e: "U_CLIENT",
                           account_key_for_gym=lambda g: "crossfitlocal",
                           open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice)
    assert result == {"processed": 0}
    assert bus.inbound == [] and bus.outbound == []


def test_fixed_pass_is_a_full_noop_when_the_flag_is_off(monkeypatch):
    _off(monkeypatch)
    bus = FakeBus([_ticket(status="fixing", verification_after={"fix_pr_url": "x"})])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result == {"notified": 0}
    assert log["opened"] == []


# ---- identity resolution -------------------------------------------------------------

def test_resolve_client_identity_succeeds():
    who = W.resolve_client_identity(
        _ticket(), slack_lookup_email=lambda e: "U_CLIENT",
        account_key_for_gym=lambda g: "crossfitlocal")
    assert who.kind == "client"
    assert who.slack_user_id == "U_CLIENT"
    assert who.account_key == "crossfitlocal"


def test_resolve_client_identity_unknown_when_no_slack_account():
    who = W.resolve_client_identity(
        _ticket(), slack_lookup_email=lambda e: None,
        account_key_for_gym=lambda g: "crossfitlocal")
    assert who.kind == "unknown"


def test_resolve_client_identity_unknown_when_lookup_raises():
    def boom(e):
        raise RuntimeError("slack down")
    who = W.resolve_client_identity(_ticket(), slack_lookup_email=boom,
                                    account_key_for_gym=lambda g: "x")
    assert who.kind == "unknown"


def test_resolve_client_identity_unknown_when_ticket_missing_reporter():
    who = W.resolve_client_identity(_ticket(reporter=""),
                                    slack_lookup_email=lambda e: "U_X",
                                    account_key_for_gym=lambda g: "x")
    assert who.kind == "unknown"


# ---- intake_pass: unresolved identity ------------------------------------------------

def test_intake_pass_escalates_an_unresolved_identity_never_dispatches():
    bus = FakeBus([_ticket()])
    log, open_dm, post = _calls()
    _, notice = _notices()
    W.intake_pass(bus, slack_lookup_email=lambda e: None,
                 account_key_for_gym=lambda g: "x", open_group_dm=open_dm,
                 post_first_message=post, write_hold_notice=notice)
    assert bus.tickets["t-1"]["status"] == "escalated"
    assert log["opened"] == []
    assert any(o["kind"] == A.KIND_ESCALATION for o in bus.outbound)


# ---- intake_pass: question ------------------------------------------------------------

def test_intake_pass_answers_a_grounded_question_and_sends_outreach():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?")])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        return {"social_status": "connected"}

    def llm(system, user):
        return "Yes, your Instagram is connected right now."

    result = W.intake_pass(bus, slack_lookup_email=lambda e: "U_CLIENT",
                           account_key_for_gym=lambda g: "crossfitlocal",
                           open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, fetch_state=fetch_state, llm=llm)
    assert result == {"processed": 1}
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert len(log["opened"]) == 1
    assert "connected" in log["posted"][0][1]
    # Never a generic "I'm on it" placeholder -- the VERIFIED answer is the first
    # message, per Blake's ruling: fix/answer, verify, THEN send.
    assert "I am on it" not in log["posted"][0][1]


def test_intake_pass_escalates_a_question_that_cannot_be_grounded():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?")])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        raise RuntimeError("seam down")

    W.intake_pass(bus, slack_lookup_email=lambda e: "U_CLIENT",
                 account_key_for_gym=lambda g: "crossfitlocal", open_group_dm=open_dm,
                 post_first_message=post, write_hold_notice=notice,
                 fetch_state=fetch_state, llm=lambda s, u: "irrelevant")
    assert bus.tickets["t-1"]["status"] == "escalated"
    assert log["opened"] == []


# ---- intake_pass: code_fix -- D14's hold gate is untouched ---------------------------

def test_intake_pass_holds_a_code_fix_behind_the_fixer_tap_same_as_any_other():
    bus = FakeBus([_ticket(raw_text="my instagram posting is broken and errors out")])
    log, open_dm, post = _calls()
    notices, notice = _notices()
    result = W.intake_pass(bus, slack_lookup_email=lambda e: "U_CLIENT",
                           account_key_for_gym=lambda g: "crossfitlocal",
                           open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice)
    assert result == {"processed": 1}
    assert bus.tickets["t-1"]["status"] == "fixing"
    assert bus.tickets["t-1"]["slack_user_id"] == "U_CLIENT"
    fixer_rows = [o for o in bus.outbound if o["kind"] == A.KIND_FIXER_REQUEST]
    assert len(fixer_rows) == 1
    assert fixer_rows[0]["delivery_status"] == "held", (
        "a client's code_fix must ALWAYS be held behind Blake's tap, D14 unchanged")
    assert len(notices) == 1
    assert notices[0]["kind"] == A.KIND_FIXER_REQUEST
    # No autonomous client message for a code_fix -- nothing is verified yet.
    assert log["opened"] == []


# ---- fixed_pass -------------------------------------------------------------------

def test_fixed_pass_notifies_once_verification_after_is_present():
    bus = FakeBus([_ticket(status="fixing", slack_user_id="U_CLIENT",
                          verification_after={"fix_pr_url": "https://github.com/x/y/pull/1"})])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result == {"notified": 1}
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert "Fixed it" in log["posted"][0][1]
    assert "https://github.com/x/y/pull/1" in log["posted"][0][1]


def test_fixed_pass_leaves_an_unverified_ticket_alone():
    bus = FakeBus([_ticket(status="fixing", slack_user_id="U_CLIENT",
                          verification_after=None)])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result == {"notified": 0}
    assert bus.tickets["t-1"]["status"] == "fixing"
    assert log["opened"] == []


def test_fixed_pass_escalates_if_slack_user_id_was_never_persisted():
    bus = FakeBus([_ticket(status="fixing",
                          verification_after={"fix_pr_url": "x"})])
    log, open_dm, post = _calls()
    W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert bus.tickets["t-1"]["status"] == "escalated"
    assert log["opened"] == []
