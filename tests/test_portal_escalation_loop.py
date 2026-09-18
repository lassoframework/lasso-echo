"""
tests/test_portal_escalation_loop.py -- portal escalation and delivery contracts.

Found live on three real portal tickets (cb7b385a / 063bc73d / af01f3ea, ZZ Test Gym):
each one escalated correctly into #fixer, and each one left its submitter with nothing.
The earlier D48 path tried to solve silence with a pre-verification ACK. The
current customer-contact rule keeps escalations internal until the fix is merged,
deployed and verified and Blake is included. Portal-thread delivery is still
tested here for messages that are authorized later.

Outbox gate 7 previously marked conversational rows on portal tickets with no
slack_channel_id as failed; a portal ticket has no Slack channel until a group DM
is opened.
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

    def set_message_body_if_posting(self, mid, body):
        for m in self.msgs:
            if m["id"] == mid and m.get("delivery_status") == "posting":
                m["body"] = body
                return dict(m)
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


# ---- escalation stays internal before verification ----------------------------------

def test_unresolved_identity_stays_internal_until_verification():
    bus = Bus([_ticket()])
    seen, _ = _run_intake(bus, _stranger_deps())
    kinds = dict(bus.outbound_kinds("t-1"))
    assert A.KIND_ESCALATION in kinds, "the #fixer card must still be written"
    assert bus.of_kind(A.KIND_ACK) == []
    assert bus.of_kind(A.KIND_TEMPLATE) == []
    assert seen["opened"] == [], "no Slack DM is possible for an unresolved identity"


def test_a_known_client_does_not_get_an_escalation_dm():
    bus = Bus([_ticket()])
    seen, _ = _run_intake(bus, _client_deps())
    assert seen["opened"] == []
    assert seen["posted"] == []
    assert bus.of_kind(A.KIND_ESCALATION)
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


def _fix_proof():
    return {"exit_code": 0, "incomplete": False, "fixer": {
        "merged_sha": "abc123", "deployment_check": {"verified": True, "sha": "abc123"}}}


def _verify_business(bus):
    release = bus.tickets["t-1"]["verification_after"]["fixer"]
    request_key = OB._current_fixer_request_key(bus, bus.ticket("t-1"))
    release["request_key"] = request_key
    release["business_postcondition"] = {
        "source": "independent_business_check", "verified": True,
        "symptom_resolved": True, "check_id": "business-check-1",
        "evidence": "Observed the reported symptom resolved for this request",
        "request_key": request_key, "merged_sha": release["merged_sha"]}


def test_healthy_deployment_with_unresolved_business_symptom_cannot_notify():
    bus = Bus([_ticket(classification="code_fix", status="merged",
                       fix_pr_url="https://example.test/pr/1",
                       verification_after=_fix_proof(), slack_channel_id="G_CLIENT")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    _verify_business(bus)
    release = bus.tickets["t-1"]["verification_after"]["fixer"]
    release["business_postcondition"]["symptom_resolved"] = False
    assert not OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                     log=lambda *a: None)
    assert [m for m in bus.of_kind(A.KIND_STATUS)
            if (m.get("attachments") or {}).get("recipient_kind") == "client"] == []

    # A stale queued notice must also be suppressed by the dispatch-time read.
    row = bus.record_outbound(
        ticket_id="t-1", author_type="echo", body=OB.RESOLVED_NOTICE,
        delivery_status="ready", kind=A.KIND_STATUS,
        meta={"identity": "echo", "recipient_kind": "client", "fixer": True,
              "resolve_notice": True, "request_key": release["request_key"],
              "pr_url": bus.ticket("t-1")["fix_pr_url"]})
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert not any(item["channel"] == "G_CLIENT" for item in sent)


def test_business_evidence_must_match_request_and_merged_sha():
    bus = Bus([_ticket(classification="code_fix", status="merged",
                       fix_pr_url="https://example.test/pr/1",
                       verification_after=_fix_proof(), slack_channel_id="G_CLIENT")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    _verify_business(bus)
    business = bus.tickets["t-1"]["verification_after"]["fixer"]["business_postcondition"]
    for field, stale in (("request_key", "old-request"), ("merged_sha", "old-sha"),
                         ("evidence", ""), ("check_id", ""),
                         ("source", "deployment_check")):
        original = business[field]
        business[field] = stale
        assert not OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                         log=lambda *a: None), field
        business[field] = original
    assert bus.of_kind(A.KIND_STATUS) == []


@pytest.mark.parametrize("bad", [True, 42, ["observed"], {"proof": "yes"}, "  "])
@pytest.mark.parametrize("field", ["check_id", "evidence"])
def test_business_evidence_requires_text_at_manual_and_dispatch(field, bad):
    bus = Bus([_ticket(classification="code_fix", status="merged",
                       fix_pr_url="https://example.test/pr/1",
                       verification_after=_fix_proof(), slack_channel_id="G_CLIENT")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    _verify_business(bus)
    release = bus.tickets["t-1"]["verification_after"]["fixer"]
    release["business_postcondition"][field] = bad
    assert not OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                     log=lambda *a: None)
    row = bus.record_outbound(
        ticket_id="t-1", author_type="echo", body=OB.RESOLVED_NOTICE,
        delivery_status="ready", kind=A.KIND_STATUS,
        meta={"identity": "echo", "recipient_kind": "client", "fixer": True,
              "resolve_notice": True, "request_key": release["request_key"],
              "pr_url": bus.ticket("t-1")["fix_pr_url"]})
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert not any(item["channel"] == "G_CLIENT" for item in sent)


def test_legacy_untagged_code_fix_ack_never_reaches_portal_thread():
    bus = Bus([_ticket(classification="code_fix", status="hold")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    row = bus.record_outbound(ticket_id="t-1", author_type="echo", body=A.ACK_CODE_FIX,
                              delivery_status="ready", kind=A.KIND_ACK,
                              meta={"identity": "echo", "recipient_kind": "client"})
    sent, post = _posts()
    summary = OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert summary["posted"] == 0
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert sent == []


def test_escalated_portal_ticket_with_cleared_classification_holds_legacy_rows():
    bus = Bus([_ticket(classification=None, status="hold", escalated=True,
                       hold_tier="routine", verification_after={
                           "source": "grounding", "hold": {"reason": "needs_review"}})])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    ack = bus.record_outbound(ticket_id="t-1", author_type="echo", body=A.ACK_CODE_FIX,
                              delivery_status="ready", kind=A.KIND_ACK,
                              meta={"identity": "echo", "recipient_kind": "client"})
    answer = bus.record_outbound(ticket_id="t-1", author_type="echo", body="Handled.",
                                 delivery_status="ready", kind=A.KIND_ANSWER,
                                 meta={"identity": "echo", "recipient_kind": "client",
                                       "released_by": "U_BLAKE"})
    assert not OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                     log=lambda *a: None)
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert bus.message(ack["id"])["delivery_status"] == "suppressed"
    assert bus.message(answer["id"])["delivery_status"] == "suppressed"
    assert bus.of_kind(A.KIND_STATUS) == []
    assert all(item["channel"] == "C_FIXER" for item in sent)


def test_released_grounded_answer_cannot_bypass_code_fix_release(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    bus = Bus([_ticket(classification="code_fix", status="hold",
                       verification_after={"source": "grounding"})])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    row = bus.record_outbound(ticket_id="t-1", author_type="echo", body="It is fixed.",
                              delivery_status="ready", kind=A.KIND_ANSWER,
                              meta={"identity": "echo", "recipient_kind": "client",
                                    "released_by": "U_BLAKE"})
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert sent == []


def test_escalated_question_hold_overrides_question_exception(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    bus = Bus([_ticket(classification="answerable_question", status="hold",
                       escalated=True, hold_tier="routine",
                       raw_text="Can we add our group sessions schedule to the website?",
                       verification_after={"source": "grounding",
                                           "hold": {"tier": "org_floor"}})])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="question", meta={})
    row = bus.record_outbound(ticket_id="t-1", author_type="echo",
                              body="Yes, we can add your group sessions schedule.",
                              delivery_status="ready", kind=A.KIND_ANSWER,
                              meta={"identity": "echo", "recipient_kind": "client",
                                    "released_by": "U_BLAKE"})
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)
    assert bus.message(row["id"])["delivery_status"] == "suppressed"
    assert sent == []


def test_code_fix_resolve_requires_release_and_blake_in_conversation():
    bus = Bus([_ticket(classification="code_fix", status="hold")])
    assert not OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                     log=lambda *a: None)
    assert bus.of_kind(A.KIND_STATUS) == []
    bus.set_ticket("t-1", status="merged", fix_pr_url="https://example.test/pr/1",
                   verification_after=_fix_proof())
    assert not OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                     log=lambda *a: None)
    bus.set_ticket("t-1", slack_channel_id="G_CLIENT")
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    _verify_business(bus)
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com",
                       body="Correction before the resolve tap", meta={})
    assert not OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                     log=lambda *a: None)
    _verify_business(bus)
    assert OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                 log=lambda *a: None)
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None,
                member_check=lambda channel, user: False)
    assert bus.of_kind(A.KIND_STATUS)[0]["delivery_status"] == "suppressed"
    assert bus.ticket("t-1")["status"] == "merged"
    assert all(item["channel"] == "C_FIXER" for item in sent)


def test_verified_fix_notice_names_blake_in_group_dm():
    bus = Bus([_ticket(classification="code_fix", status="merged",
                       fix_pr_url="https://example.test/pr/1",
                       verification_after=_fix_proof(), slack_channel_id="G_CLIENT")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com", body="broken", meta={})
    _verify_business(bus)
    assert OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                 log=lambda *a: None)
    sent, post = _posts()
    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None,
                member_check=lambda channel, user: channel == "G_CLIENT" and bool(user))
    assert len(sent) == 1, bus.outbound_kinds()
    assert f"<@{OB.config.APPROVER_SLACK_ID}>" in sent[0]["text"]
    assert bus.ticket("t-1")["status"] == "resolved"


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


def test_post_time_portal_answer_hold_does_not_queue_customer_template():
    bus = Bus([_ticket(status="verification", verification_after={"source": "test"},
                       raw_text="Can we add our group sessions schedule to the website?")])
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com",
                       body="Can we add our group sessions schedule to the website?", meta={})
    answer = bus.record_outbound(
        ticket_id="t-1", author_type="echo",
        body="Yes, we can add your group sessions schedule.",
        delivery_status="ready", kind=A.KIND_ANSWER,
        meta={"identity": "echo", "recipient_kind": "client",
              "surface": "portal_ticket_bridge"})
    sent, post = _posts()

    OB.run_once(bus, post, identity=ECHO, log=lambda *a: None)

    assert bus.message(answer["id"])["delivery_status"] == "held"
    assert bus.of_kind(A.KIND_TEMPLATE) == []
    assert sent == []


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
    bus = Bus([_ticket(status="verification", escalated=False,
                       classification="answerable_question",
                       verification_after={"source": "grounding"})])
    # the human's own message, which every real ticket has and which outbox gate 1 (first
    # contact: the bot never speaks first) requires before anything can post
    bus.record_inbound(ticket_id="t-1", slack_event_id=None, slack_ts=None,
                       author_type="client", author_id="owner@gym.com",
                       body="is my instagram connected?",
                       meta={"surface": "portal_ticket_bridge"})
    assert OB.resolve_and_notify(bus, "t-1", approved_by="U_BLAKE", identity=ECHO,
                                 log=lambda *a: None) is True
    notice = bus.of_kind(A.KIND_STATUS)
    assert len(notice) == 1
    assert notice[0]["body"] == OB.RESOLVED_NOTICE
    assert notice[0]["delivery_status"] == "ready"
    # Audit 7, MINOR 5: the ticket closes when the person HAS the notice, not when the tap is
    # registered -- a post failure must never leave a ticket asserting it was resolved. The
    # approval is stamped at tap time; the status follows the delivery.
    assert bus.ticket("t-1")["approved_by"] == "U_BLAKE"
    assert bus.ticket("t-1")["status"] != "resolved"
    # This ticket's delivery surface is the portal support thread (D48), so "delivered"
    # means the row reached delivery_status='posted' and migration 0310 now lets the client
    # read it -- no Slack call is involved. Either way the ticket closes only after that.
    OB.run_once(bus, lambda ch, text, thread_ts=None, blocks=None: "1",
                identity=ECHO, log=lambda *a: None)
    assert bus.of_kind(A.KIND_STATUS)[0]["delivery_status"] == "posted"
    assert bus.ticket("t-1")["status"] == "resolved"


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
    monkeypatch.setenv("SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE", "true")
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
    monkeypatch.setenv("SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE", "true")
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
