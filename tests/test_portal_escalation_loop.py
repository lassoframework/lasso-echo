"""
tests/test_portal_escalation_loop.py -- D48 (Blake, 2026-09-05): an escalated portal
ticket must never be a dead end for the person who wrote it.

Found live on three real portal tickets (cb7b385a / 063bc73d / af01f3ea, ZZ Test Gym):
each one escalated correctly into #fixer, and each one left its submitter with nothing.
No "we got it" when it escalated, no word when it was dealt with. Two holes, both
covered here:

  1. the bridge escalated and returned without ever writing a client-facing row;
  2. even if it had, outbox.py's gate 7 marked every conversational row on a ticket with
     no slack_channel_id 'failed' -- and a portal ticket has no Slack channel until a
     group DM is opened, which for an unresolved identity never happens.
"""
from datetime import datetime, timezone

import pytest

from agent import echo_ticket_worker as W
from agent.slack_convo import adapter as A
from agent.slack_convo import identities as IDS
from agent.slack_convo import outbox as OB

ECHO = IDS.IDENTITIES["echo"]


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    # armed live on the Railway echo service since 2026-09-03 ([[slack-convo-adapter-armed]]).
    # The master and identity switches are set too: M1 (2026-09-05 audit 2) made the portal
    # bridge's client-facing DMs obey them, so a fixture that armed only CLIENT_REPLY was
    # describing a state that cannot exist -- client_reply is meaningless with the identity
    # off, and config.slack_convo_client_reply_armed's own callers now check both.
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    yield


class Bus:
    """Enough of bus.py for both halves: the worker's writes and the outbox's dispatch."""

    def __init__(self, tickets=()):
        self.tickets = {t["id"]: dict(t) for t in tickets}
        self.msgs = []

    # tickets
    def ticket(self, tid):
        return dict(self.tickets[tid]) if tid in self.tickets else None

    def set_ticket(self, tid, **fields):
        self.tickets[tid].update(fields)
        return dict(self.tickets[tid])

    def find_new_tickets(self, *, product, source, limit=20):
        return [dict(t) for t in self.tickets.values()
                if t.get("product") == product and t.get("source") == source
                and t.get("status") == "new" and t.get("classification") is None]

    def find_fixing_tickets(self, *, product, limit=20):
        return [dict(t) for t in self.tickets.values() if t.get("status") == "fixing"]

    # messages
    def record_inbound(self, **kw):
        m = {"id": f"in-{len(self.msgs)}", "direction": "inbound",
             "attachments": kw.get("meta") or {}, **kw}
        self.msgs.append(m)
        return m, False

    def inbound_count(self, tid):
        return len([m for m in self.msgs
                    if m.get("ticket_id") == tid and m.get("direction") == "inbound"])

    def record_outbound(self, *, ticket_id, author_type, body, delivery_status, kind,
                        meta=None):
        att = dict(meta or {})
        att["kind"] = kind
        m = {"id": f"out-{len(self.msgs)}", "ticket_id": ticket_id, "direction": "outbound",
             "author_type": author_type, "body": body, "delivery_status": delivery_status,
             "attachments": att, "created_at": datetime.now(timezone.utc).isoformat()}
        self.msgs.append(m)
        return m

    def count_outbound_kind_since(self, tid, kind, since_iso):
        return len([m for m in self.msgs
                    if m.get("ticket_id") == tid and m.get("direction") == "outbound"
                    and (m.get("attachments") or {}).get("kind") == kind])

    def messages(self, tid, limit=40):
        return [m for m in self.msgs if m.get("ticket_id") == tid]

    def message(self, mid):
        return next((dict(m) for m in self.msgs if m["id"] == mid), None)

    def outbox(self, status="ready", limit=50, identity=None):
        return [dict(m) for m in self.msgs
                if m.get("direction") == "outbound" and m.get("delivery_status") == status]

    def claim_message(self, mid):
        return True

    def mark_message(self, mid, delivery_status, slack_ts=None, meta_update=None):
        for m in self.msgs:
            if m["id"] == mid:
                m["delivery_status"] = delivery_status
                if slack_ts:
                    m["slack_ts"] = slack_ts
                if meta_update:
                    m["attachments"] = {**(m.get("attachments") or {}), **meta_update}
                return m
        return None

    # helpers for assertions
    def outbound_kinds(self, tid=None):
        return [(m["attachments"].get("kind"), m["delivery_status"])
                for m in self.msgs if m.get("direction") == "outbound"
                and (tid is None or m["ticket_id"] == tid)]

    def of_kind(self, kind):
        return [m for m in self.msgs if m.get("direction") == "outbound"
                and (m.get("attachments") or {}).get("kind") == kind]


def _ticket(**over):
    row = {"id": "t-1", "product": "echo", "source": "website_tab", "client_id": "g-1",
           "reporter": "owner@gym.com", "raw_text": "is my instagram connected?",
           "status": "new", "classification": None, "bot_identity": "echo"}
    row.update(over)
    return row


def _directory(users):
    by_email = {u["email"]: uid for uid, u in users.items()}

    def slack_lookup_email(email):
        return by_email.get(email)

    def slack_user_info(uid):
        u = users.get(uid) or {}
        return {"id": uid, "is_bot": False, "email": u.get("email", ""),
                "real_name": "Test User", "is_restricted": False,
                "is_ultra_restricted": False}

    def portal_lookup(email):
        uid = by_email.get(email)
        return None if not uid else {"role": users[uid]["role"],
                                     "gyms": users[uid].get("gyms", [])}

    return dict(slack_lookup_email=slack_lookup_email, slack_user_info=slack_user_info,
                portal_lookup=portal_lookup, operator_ids=())


def _client_deps(gym_id="g-1", account_key=""):
    return _directory({"U_CLIENT": {"email": "owner@gym.com", "role": "client",
                                    "gyms": [{"gym_id": gym_id,
                                              "relationship": "client_owner",
                                              "account_key": account_key}]}})


def _stranger_deps():
    return dict(slack_lookup_email=lambda e: None,
                slack_user_info=lambda u: {"is_bot": False, "email": ""},
                portal_lookup=lambda e: None, operator_ids=())


def _slack_calls():
    seen = {"opened": [], "posted": []}

    def open_group_dm(user_ids):
        seen["opened"].append(list(user_ids))
        return {"ok": True, "channel_id": "G_DM"}

    def post_first_message(channel_id, text):
        seen["posted"].append((channel_id, text))
        return {"ok": True, "ts": "1.1"}

    return seen, open_group_dm, post_first_message


def _run_intake(bus, deps, **over):
    seen, open_dm, post = _slack_calls()
    kwargs = dict(open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=lambda **kw: {"id": "card"},
                  stamp_ticket=lambda tid, **kw: bus.set_ticket(tid, **kw),
                  mark_message=bus.mark_message, claim_message=bus.claim_message,
                  llm=lambda *a, **k: A.NO_ANSWER if hasattr(A, "NO_ANSWER") else "NO_ANSWER",
                  fetch_state=lambda t, w: {})
    kwargs.update(over)
    return seen, W.intake_pass(bus, **deps, **kwargs)


# ---- hole 1: the submitter now always hears something -------------------------------

def test_unresolved_identity_still_gets_an_acknowledgement():
    bus = Bus([_ticket()])
    seen, _ = _run_intake(bus, _stranger_deps())
    kinds = dict(bus.outbound_kinds("t-1"))
    assert A.KIND_ESCALATION in kinds, "the #fixer card must still be written"
    acks = bus.of_kind(A.KIND_ACK)
    assert len(acks) == 1
    assert acks[0]["body"] == A.TEMPLATE_NO_ANSWER_YET
    assert acks[0]["delivery_status"] == "ready"
    assert seen["opened"] == [], "no Slack DM is possible for an unresolved identity"


def test_a_known_client_gets_the_acknowledgement_as_a_group_dm():
    bus = Bus([_ticket()])
    seen, _ = _run_intake(bus, _client_deps())
    assert seen["opened"] == [[W._out.BLAKE_SLACK_USER_ID, "U_CLIENT"]]
    assert seen["posted"] and A.TEMPLATE_NO_ANSWER_YET in seen["posted"][0][1]
    assert len(bus.of_kind(A.KIND_ACK)) == 1


def test_the_acknowledgement_is_written_exactly_once_per_ticket():
    bus = Bus([_ticket()])
    W.acknowledge_submitter(bus, bus.ticket("t-1"), identity_name="echo")
    W.acknowledge_submitter(bus, bus.ticket("t-1"), identity_name="echo")
    assert len(bus.of_kind(A.KIND_ACK)) == 1


def test_a_bus_read_fault_never_licenses_a_second_acknowledgement():
    class Faulty(Bus):
        def count_outbound_kind_since(self, *a, **k):
            raise RuntimeError("bus down")

        def messages(self, *a, **k):
            raise RuntimeError("bus down")

    bus = Faulty([_ticket()])
    assert W.acknowledge_submitter(bus, bus.ticket("t-1"), identity_name="echo",
                                   log=lambda *a: None) is True
    assert bus.of_kind(A.KIND_ACK) == []


# ---- hole 2: the portal support thread is a real delivery surface -------------------

def _posts():
    sent = []

    def post(channel, text, thread_ts=None, blocks=None):
        sent.append({"channel": channel, "text": text, "blocks": blocks})
        return "111.1"

    return sent, post


def test_a_conversational_row_on_a_portal_ticket_is_delivered_to_the_portal_thread():
    bus = Bus([_ticket(status="hold")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="hi", meta={})
    bus.record_outbound(ticket_id="t-1", author_type="echo", body=A.TEMPLATE_NO_ANSWER_YET,
                        delivery_status="ready", kind=A.KIND_ACK,
                        meta={"identity": "echo", "recipient_kind": "client"})
    sent, post = _posts()
    summary = OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert summary["posted"] == 1 and summary["failed"] == 0
    row = bus.of_kind(A.KIND_ACK)[0]
    assert row["delivery_status"] == "posted"
    assert row["attachments"]["delivered_via"] == "portal_thread"
    assert sent == [], "nothing is posted to Slack for a portal thread delivery"


def test_a_ticket_with_no_slack_channel_and_no_portal_thread_still_fails():
    bus = Bus([_ticket(status="hold", source="engage_tenant_event")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="x", body="hi", meta={})
    bus.record_outbound(ticket_id="t-1", author_type="echo", body="hello",
                        delivery_status="ready", kind=A.KIND_ACK,
                        meta={"identity": "echo", "recipient_kind": "client"})
    sent, post = _posts()
    summary = OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert summary["failed"] == 1 and summary["posted"] == 0


def test_portal_deliverable_needs_a_client_id():
    assert OB.portal_deliverable({"source": "website_tab", "client_id": "g-1"})
    assert not OB.portal_deliverable({"source": "website_tab", "client_id": ""})
    assert not OB.portal_deliverable({"source": "slack_conversation", "client_id": "g-1"})


# ---- the loop closes: Blake's tap on the escalation card ----------------------------

def test_the_escalation_card_carries_a_resolve_button():
    bus = Bus([_ticket(status="hold")])
    row = bus.record_outbound(ticket_id="t-1", author_type="system", body="escalated",
                              delivery_status="ready", kind=A.KIND_ESCALATION,
                              meta={"identity": "echo",
                                    "surface": "portal_ticket_bridge"})
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert sent and sent[0]["channel"] == "C_FIXER"
    actions = [b for b in sent[0]["blocks"] if b["type"] == "actions"]
    assert actions, "the escalation card must offer the resolve tap"
    button = actions[0]["elements"][0]
    assert button["action_id"] == OB.RESOLVE_ACTION_ID
    assert button["value"] == "t-1"


def test_no_resolve_button_when_there_is_nowhere_to_send_the_notice():
    bus = Bus([_ticket(status="hold", source="engage_tenant_event")])
    bus.record_outbound(ticket_id="t-1", author_type="system", body="escalated",
                        delivery_status="ready", kind=A.KIND_ESCALATION,
                        meta={"identity": "echo"})
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert [b for b in sent[0]["blocks"] if b["type"] == "actions"] == []


def test_resolve_and_notify_writes_the_person_a_notice_and_closes_the_ticket():
    bus = Bus([_ticket(status="hold", escalated=True)])
    assert OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                 log=lambda *a: None) is True
    notice = bus.of_kind(A.KIND_STATUS)
    assert len(notice) == 1
    assert notice[0]["body"] == OB.RESOLVED_NOTICE
    assert notice[0]["delivery_status"] == "ready"
    assert bus.ticket("t-1")["status"] == "resolved"
    assert bus.ticket("t-1")["approved_by"] == "U_BLAKE"


def test_resolve_and_notify_is_idempotent():
    bus = Bus([_ticket(status="hold")])
    OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                          log=lambda *a: None)
    assert OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                 log=lambda *a: None) is False
    assert len(bus.of_kind(A.KIND_STATUS)) == 1


def test_resolve_and_notify_refuses_another_bots_ticket():
    bus = Bus([_ticket(status="hold", bot_identity="ranger")])
    assert OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                 log=lambda *a: None) is False
    assert bus.of_kind(A.KIND_STATUS) == []


def test_the_resolve_notice_reaches_the_portal_thread_end_to_end():
    bus = Bus([_ticket(status="hold")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="hi", meta={})
    OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                          log=lambda *a: None)
    sent, post = _posts()
    summary = OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert summary["posted"] == 1
    assert bus.of_kind(A.KIND_STATUS)[0]["delivery_status"] == "posted"


# =========================================================================================
# C2 (2026-09-05 audit, CRITICAL): the portal bridge bypassed EVERY D54 gate
# =========================================================================================
#
# The bridge's QUESTION branch sends through outreach.initiate, not through
# outbox._dispatch_one, so none of the trust ladder, the AUTO_ANSWER flag or the hard lines
# applied to it. The auditor reproduced a model-written answer posting to a client's group
# DM with CLIENT_REPLY off, AUTO_ANSWER off, on a hard-line topic ("Can we add our group
# sessions schedule to the website?"). These tests are that reproduction, kept.

def _answering_worker(bus, seen_deps, *, answer_body="Yes, both are connected."):
    """Run intake_pass with an answer lane that always grounds, so the QUESTION branch is
    the one under test rather than the escalation branch."""
    import agent.echo_ticket_worker as WW
    cards = []
    WW.intake_pass(
        bus, open_group_dm=seen_deps[1], post_first_message=seen_deps[2],
        write_hold_notice=lambda **kw: cards.append(kw),
        fetch_state=lambda t, w: {"social_status": {"instagram": "connected"}},
        llm=lambda system, user, model=None: answer_body,
        classify_llm=None, mark_message=bus.mark_message, claim_message=bus.claim_message,
        stamp_ticket=lambda *a, **k: None, log=lambda *a, **k: None,
        **_client_deps())
    return cards


def test_portal_bridge_never_auto_answers_a_client_with_auto_answer_off(monkeypatch):
    monkeypatch.delenv("SLACK_CONVO_ECHO_AUTO_ANSWER", raising=False)
    bus = Bus([_ticket()])
    seen, open_dm, post = _slack_calls()
    cards = _answering_worker(bus, (seen, open_dm, post))
    assert seen["posted"] == [], "no client-facing send without the narrower permission"
    assert bus.tickets["t-1"]["status"] == "hold"
    held = [m for m in bus.of_kind(A.KIND_ANSWER) if m["delivery_status"] == "held"]
    assert held, "the drafted answer is kept, held, for a tap"
    assert cards, "a held answer always gets a card in #fixer"


def test_portal_bridge_never_auto_answers_a_hard_line_even_when_armed(monkeypatch):
    """The real a9efa713 text. 'group sessions schedule' is a gym schedule question."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    bus = Bus([_ticket(raw_text="Can we add our group sessions schedule to the website?")])
    seen, open_dm, post = _slack_calls()
    cards = _answering_worker(bus, (seen, open_dm, post),
                              answer_body="Yes, we can add your group sessions schedule.")
    assert seen["posted"] == [], "a hard line never auto answers, whatever the flags say"
    assert bus.tickets["t-1"]["status"] == "hold"
    assert any("hard line" in (c.get("why") or "") for c in cards)


def test_portal_bridge_does_auto_answer_when_fully_armed_and_writes_a_receipt(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    bus = Bus([_ticket()])
    seen, open_dm, post = _slack_calls()
    _answering_worker(bus, (seen, open_dm, post))
    assert seen["posted"], "fully armed, the grounded answer does go out"
    assert bus.tickets["t-1"]["status"] == "resolved"
    # M1: the one path that sends with no tap at all must be visible in #fixer
    receipts = [m for m in bus.of_kind(A.KIND_ESCALATION)
                if (m["attachments"] or {}).get("receipt")]
    assert receipts, "an unattended send must leave a receipt"
    assert "SENT AUTOMATICALLY (no tap)" in receipts[0]["body"]
    assert "Yes, both are connected." in receipts[0]["body"]
    assert receipts[0]["attachments"]["kind"] in A.CLIENT_INVISIBLE_KINDS
