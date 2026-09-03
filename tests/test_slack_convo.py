"""
Slack Conversational Adapter (agent/slack_convo) — Blake's TESTS list, verbatim, plus the
gates behind each one:

  thread_ts to ticket mapping survives listener restart
  duplicate event_id creates no second row
  unknown user gets the templated reply and no worker runs
  follow-up in thread attaches to the correct open ticket
  reply never posts without verification_after populated
  client reply held when client-reply flag is off
  bot never posts in a thread with no prior human message
  config-only onboarding of a second bot identity
  flags off equals today

Everything runs against FakeBus, which emulates the two unique indexes migration 0309 adds
(thread -> ticket, slack_event_id) in memory, so the DB-enforced guarantees are the thing
under test, not adapter memory. No network anywhere.
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.slack_convo import adapter as A  # noqa: E402
from agent.slack_convo import classifier as C  # noqa: E402
from agent.slack_convo import identities as IDS  # noqa: E402
from agent.slack_convo import identity_gate as IG  # noqa: E402
from agent.slack_convo import outbox as OB  # noqa: E402
from agent.slack_convo.bus import BusError  # noqa: E402


# ---- fakes ------------------------------------------------------------------------------

class FakeBus:
    """In-memory support_tickets / support_messages with BOTH unique indexes emulated."""

    def __init__(self):
        self.tickets = {}
        self.msgs = []
        self.calls = []

    # tickets
    def find_ticket_by_thread(self, channel_id, thread_ts):
        self.calls.append("find_ticket_by_thread")
        for t in self.tickets.values():
            if t["slack_channel_id"] == channel_id and t["slack_thread_ts"] == thread_ts:
                return dict(t)
        return None

    def find_open_ticket_in_conversation(self, channel_id, within_days):
        self.calls.append("find_open")
        opens = [t for t in self.tickets.values()
                 if t["slack_channel_id"] == channel_id
                 and t["status"] in ("new", "triage", "fixing", "verification", "hold", "approved")]
        return dict(opens[-1]) if opens else None

    def get_or_create_ticket(self, **kw):
        self.calls.append("get_or_create")
        # emulate uq_support_tickets_slack_thread
        ex = self.find_ticket_by_thread(kw["channel_id"], kw["thread_ts"])
        if ex:
            return ex, False
        t = {"id": str(uuid.uuid4()), "product": kw["product"], "source": "slack_conversation",
             "client_id": kw.get("client_id"), "reporter": kw.get("reporter"),
             "raw_text": kw["raw_text"], "status": "new",
             "slack_channel_id": kw["channel_id"], "slack_thread_ts": kw["thread_ts"],
             "slack_user_id": kw["slack_user_id"], "identity_kind": kw["identity_kind"],
             "bot_identity": kw["bot_identity"],
             "classification": kw.get("classification"), "request_type": kw.get("request_type"),
             "verification_before": None, "verification_after": None, "escalated": False,
             "lane": None, "hold_tier": None}
        self.tickets[t["id"]] = t
        return dict(t), True

    def ticket(self, tid):
        return dict(self.tickets[tid]) if tid in self.tickets else None

    def set_ticket(self, tid, **fields):
        self.calls.append("set_ticket")
        self.tickets[tid].update(fields)
        return dict(self.tickets[tid])

    def count_tickets_for_user_today(self, slack_user_id):
        return sum(1 for t in self.tickets.values() if t["slack_user_id"] == slack_user_id)

    # messages
    def record_inbound(self, **kw):
        self.calls.append("record_inbound")
        # emulate uq_support_messages_slack_event
        if kw.get("slack_event_id") and any(
                m.get("slack_event_id") == kw["slack_event_id"] for m in self.msgs):
            return None, True
        m = {"id": str(uuid.uuid4()), "direction": "inbound", "delivery_status": None, **kw}
        self.msgs.append(m)
        return dict(m), False

    def record_outbound(self, **kw):
        self.calls.append("record_outbound")
        att = {"kind": kw["kind"]}
        att.update(kw.get("meta") or {})
        m = {"id": str(uuid.uuid4()), "direction": "outbound", "ticket_id": kw["ticket_id"],
             "author_type": kw["author_type"], "body": kw["body"],
             "delivery_status": kw["delivery_status"], "attachments": att, "slack_ts": None}
        self.msgs.append(m)
        return dict(m)

    def inbound_count(self, tid):
        return sum(1 for m in self.msgs if m["ticket_id"] == tid and m["direction"] == "inbound")

    def messages_for(self, tid):
        return [dict(m) for m in self.msgs if m["ticket_id"] == tid]

    def messages(self, tid, limit=40):  # adapter calls bus.messages(tid)
        return self.messages_for(tid)

    def outbox(self, status="ready", limit=50):
        return [dict(m) for m in self.msgs
                if m["direction"] == "outbound" and m["delivery_status"] == status]

    def mark_message(self, mid, delivery_status, slack_ts=None):
        for m in self.msgs:
            if m["id"] == mid:
                m["delivery_status"] = delivery_status
                if slack_ts:
                    m["slack_ts"] = slack_ts
                return dict(m)
        return None

    def _get(self, table, params):  # used by outbox.release_held
        if table == "support_messages":
            mid = params["id"].replace("eq.", "")
            return [dict(m) for m in self.msgs if m["id"] == mid]
        return []

    # test helpers
    def outbound_kinds(self, tid):
        return [m["attachments"]["kind"] for m in self.msgs
                if m["ticket_id"] == tid and m["direction"] == "outbound"]


def _who(kind, uid="U_CLIENT", account_key="crossfitlocal", gym_id="g-1"):
    if kind == IG.CLIENT:
        return IG.Identity(IG.CLIENT, uid, email="chad@x.com", display="Chad",
                           account_key=account_key, gym_id=gym_id, reason="test")
    if kind == IG.STAFF:
        return IG.Identity(IG.STAFF, uid, email="blake@x.com", display="Blake", reason="test")
    if kind == IG.UNKNOWN:
        return IG.Identity(IG.UNKNOWN, uid, reason="no portal user")
    if kind == IG.BOT:
        return IG.Identity(IG.BOT, uid, reason="bot")
    raise ValueError(kind)


def _deps(bus, *, who=IG.CLIENT, identity="echo", enabled=True, client_armed=False,
          staff_armed=True, cap=10, answer=None):
    ident = IDS.get(identity)
    return A.Deps(bus=bus, identity=ident,
                  resolve_identity=lambda uid: _who(who, uid),
                  identity_enabled=lambda: enabled,
                  client_reply_armed=lambda: client_armed,
                  staff_reply_armed=lambda: staff_armed,
                  daily_cap=lambda: cap, open_window_days=lambda: 7,
                  answer=answer, classify_llm=None, log=lambda *a, **k: None)


def _ev(text, *, channel="G0MPIM", ts="1.001", channel_type="mpim", user="U_CLIENT",
        thread_ts=None, etype="message", **extra):
    e = {"type": etype, "channel": channel, "channel_type": channel_type, "user": user,
         "text": text, "ts": ts}
    if thread_ts:
        e["thread_ts"] = thread_ts
    e.update(extra)
    return e


@pytest.fixture(autouse=True)
def _bot_user(monkeypatch):
    monkeypatch.setenv("AGENT_SLACK_BOT_USER_ID", "U_ECHO_BOT")
    yield


# ======================================================================================
# flags off equals today
# ======================================================================================

def test_flags_off_touches_nothing():
    bus = FakeBus()
    d = A.handle_event(_ev("my posts are broken"), "G0MPIM:1.001", _deps(bus, enabled=False))
    assert d.ignored and d.reason == "flag_off"
    assert bus.calls == [], "with the flag off the adapter must not even READ the bus"
    assert bus.tickets == {} and bus.msgs == []


def test_attach_registers_nothing_when_master_off(monkeypatch):
    from agent.slack_convo import listener_wiring as W
    monkeypatch.delenv("SLACK_CONVO_ENABLED", raising=False)

    class _App:
        def event(self, *a, **k):
            raise AssertionError("must not register a listener while the master flag is off")

        def action(self, *a, **k):
            raise AssertionError("must not register an action while the master flag is off")

    assert W.attach(_App(), "echo", log=lambda *a: None) is None


# ======================================================================================
# the loop guards: never self, never bots, never edits
# ======================================================================================

@pytest.mark.parametrize("ev", [
    _ev("hi", bot_id="B123"),
    _ev("hi", subtype="message_changed"),
    _ev("hi", user="U_ECHO_BOT"),
    _ev(""),
])
def test_self_bot_edit_and_empty_are_ignored_without_touching_the_bus(ev):
    bus = FakeBus()
    d = A.handle_event(ev, "k", _deps(bus))
    assert d.ignored
    assert bus.calls == []


def test_a_channel_message_with_no_ticket_thread_is_silent():
    """Not every channel message. Only a reply in a thread where we already have a ticket."""
    bus = FakeBus()
    d = A.handle_event(_ev("anyone around?", channel="C_GENERAL", channel_type="channel"),
                       "k", _deps(bus))
    assert d.ignored and d.reason == "not_our_surface"
    assert bus.tickets == {}


# ======================================================================================
# thread equals ticket
# ======================================================================================

def test_thread_to_ticket_mapping_survives_listener_restart():
    """Two independent Deps objects (a restart: no shared memory) resolve the same thread to
    the same ticket, because the mapping lives in the bus's unique index, not in memory."""
    bus = FakeBus()
    d1 = A.handle_event(_ev("my facebook posts are not going out", ts="1.001"),
                        "G0MPIM:1.001", _deps(bus))
    assert not d1.ignored and d1.created
    # "restart": brand-new deps, same bus, a reply in the same conversation
    d2 = A.handle_event(_ev("still not working", ts="1.002"), "G0MPIM:1.002", _deps(bus))
    assert d2.ticket_id == d1.ticket_id
    assert d2.created is False
    assert len(bus.tickets) == 1


def test_explicit_thread_ts_maps_to_that_ticket_not_a_new_one():
    bus = FakeBus()
    d1 = A.handle_event(_ev("posts broken", channel="C_ROOM", channel_type="channel",
                            etype="app_mention", ts="5.000"), "C_ROOM:5.000", _deps(bus))
    d2 = A.handle_event(_ev("more detail here", channel="C_ROOM", channel_type="channel",
                            ts="5.100", thread_ts="5.000"), "C_ROOM:5.100", _deps(bus))
    assert d2.surface == A.SURFACE_THREAD
    assert d2.ticket_id == d1.ticket_id


# ======================================================================================
# duplicate event id creates no second row
# ======================================================================================

def test_duplicate_event_creates_no_second_row_and_no_second_reply():
    bus = FakeBus()
    ev = _ev("my posts are broken")
    A.handle_event(ev, "G0MPIM:1.001", _deps(bus))
    inbound_before = sum(1 for m in bus.msgs if m["direction"] == "inbound")
    outbound_before = sum(1 for m in bus.msgs if m["direction"] == "outbound")
    d = A.handle_event(ev, "G0MPIM:1.001", _deps(bus))       # Slack redelivers
    assert d.ignored and d.duplicate
    assert sum(1 for m in bus.msgs if m["direction"] == "inbound") == inbound_before
    assert sum(1 for m in bus.msgs if m["direction"] == "outbound") == outbound_before
    assert len(bus.tickets) == 1


def test_dedupe_key_is_channel_ts_so_message_and_app_mention_collapse():
    """Slack emits distinct event_ids for one message delivered as both `message` and
    `app_mention`; channel:ts is the message's identity (D13)."""
    from agent.slack_convo.listener_wiring import dedupe_key
    m = _ev("@echo help", channel="C_ROOM", channel_type="channel", ts="7.7")
    a = _ev("@echo help", channel="C_ROOM", channel_type="channel", ts="7.7", etype="app_mention")
    assert dedupe_key(m) == dedupe_key(a) == "C_ROOM:7.7"


# ======================================================================================
# unknown user: templated reply, route to Blake, no worker
# ======================================================================================

def test_unknown_user_gets_template_and_escalation_and_no_worker():
    bus = FakeBus()
    d = A.handle_event(_ev("my posts are broken, fix it"), "k", _deps(bus, who=IG.UNKNOWN))
    assert d.reason == "unknown_identity"
    t = bus.tickets[d.ticket_id]
    assert t["status"] == "hold" and t["escalated"] is True
    assert t["identity_kind"] == "unknown"
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_ESCALATION in kinds and A.KIND_TEMPLATE in kinds
    assert A.KIND_FIXER_REQUEST not in kinds, "no worker for an unresolved identity"
    assert A.KIND_ANSWER not in kinds, "no answer for an unresolved identity"
    assert d.classification == ""


def test_unknown_user_template_is_held_behind_the_client_flag():
    """Nothing reaches a stranger autonomously until client replies are armed (D12)."""
    bus = FakeBus()
    d = A.handle_event(_ev("hello?"), "k", _deps(bus, who=IG.UNKNOWN, client_armed=False))
    tmpl = [m for m in bus.messages_for(d.ticket_id)
            if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_TEMPLATE][0]
    assert tmpl["delivery_status"] == "held"


# ======================================================================================
# follow-up attaches to the open ticket and re-triggers
# ======================================================================================

def test_follow_up_attaches_and_retriggers_the_fixer():
    bus = FakeBus()
    d1 = A.handle_event(_ev("my facebook posts are broken", ts="1.001"), "G:1.001", _deps(bus))
    assert d1.classification == C.CODE_FIX
    bus.set_ticket(d1.ticket_id, status="hold")           # worker parked it
    d2 = A.handle_event(_ev("fix it differently: use the CrossFit Local page", ts="1.002"),
                        "G:1.002", _deps(bus))
    assert d2.ticket_id == d1.ticket_id
    assert d2.classification == C.FOLLOW_UP
    assert bus.tickets[d1.ticket_id]["status"] == "triage", "re-triggered"
    fixer_rows = [m for m in bus.messages_for(d1.ticket_id)
                  if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_FIXER_REQUEST]
    assert len(fixer_rows) == 2, "original request + the follow-up instruction"
    assert "OPS-FIX FOLLOW-UP" in fixer_rows[-1]["body"]
    assert "fix it differently" in fixer_rows[-1]["body"]


# ======================================================================================
# classification routing
# ======================================================================================

def test_code_fix_writes_fixer_request_and_ack_never_an_answer():
    bus = FakeBus()
    d = A.handle_event(_ev("my instagram post never published"), "k", _deps(bus))
    assert d.classification == C.CODE_FIX
    t = bus.tickets[d.ticket_id]
    assert t["status"] == "triage" and t["lane"] == "hold" and t["hold_tier"] == "routine"
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_FIXER_REQUEST in kinds and A.KIND_ACK in kinds
    assert A.KIND_ANSWER not in kinds


def test_fixer_request_uses_the_prefix_the_existing_worker_watches():
    bus = FakeBus()
    d = A.handle_event(_ev("posts are failing"), "k", _deps(bus))
    row = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_FIXER_REQUEST][0]
    assert row["body"].startswith("OPS-FIX REQUEST: ECHO ALERT:")
    assert row["delivery_status"] == "ready", "internal kinds are never held"


def test_question_answer_sets_verification_and_writes_answer():
    bus = FakeBus()
    ans = lambda ticket, who, msgs: {"body": "Instagram and Facebook are connected.",
                                     "grounding": {"facts": {"ig": "connected"}}}
    d = A.handle_event(_ev("are my accounts connected?"), "k", _deps(bus, answer=ans))
    assert d.classification == C.QUESTION
    t = bus.tickets[d.ticket_id]
    assert t["verification_before"] and t["verification_after"]
    assert t["status"] == "resolved"
    assert A.KIND_ANSWER in bus.outbound_kinds(d.ticket_id)


def test_question_with_no_answer_escalates_instead_of_inventing():
    bus = FakeBus()
    d = A.handle_event(_ev("what does the moon weigh?"), "k", _deps(bus, answer=lambda *a: None))
    t = bus.tickets[d.ticket_id]
    assert t["status"] == "hold" and t["escalated"]
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_ESCALATION in kinds and A.KIND_ANSWER not in kinds


def test_action_request_on_ranger_identity_goes_to_the_ranger_lane():
    bus = FakeBus()
    d = A.handle_event(_ev("please pause the ads for this week"), "k",
                       _deps(bus, identity="ranger"))
    assert d.classification == C.ACTION_REQUEST
    t = bus.tickets[d.ticket_id]
    assert t["product"] == "ranger" and t["status"] == "new"
    assert t["request_type"] == "pause_resume"


def test_undecidable_text_escalates_never_dispatches():
    bus = FakeBus()
    d = A.handle_event(_ev("ok"), "k", _deps(bus))
    assert d.reason == "escalated"
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_FIXER_REQUEST not in kinds and A.KIND_ANSWER not in kinds
    assert A.KIND_ESCALATION in kinds


# ======================================================================================
# rate limit gates dispatch, not recording
# ======================================================================================

def test_rate_limit_queues_after_cap_with_no_worker():
    bus = FakeBus()
    deps = _deps(bus, cap=2)
    for i in range(2):
        A.handle_event(_ev("broken again", channel=f"G{i}", ts=f"{i}.0"), f"G{i}:{i}.0", deps)
    d = A.handle_event(_ev("broken a third time", channel="G9", ts="9.0"), "G9:9.0", deps)
    assert d.rate_limited
    t = bus.tickets[d.ticket_id]
    assert t["status"] == "hold" and t["escalated"]
    kinds = bus.outbound_kinds(d.ticket_id)
    assert A.KIND_FIXER_REQUEST not in kinds
    assert A.KIND_TEMPLATE in kinds and A.KIND_ESCALATION in kinds


def test_staff_are_exempt_from_the_cap():
    bus = FakeBus()
    deps = _deps(bus, who=IG.STAFF, cap=1)
    A.handle_event(_ev("posts broken", channel="G0", ts="0.0", user="U_B"), "G0:0.0", deps)
    d = A.handle_event(_ev("posts broken", channel="G1", ts="1.0", user="U_B"), "G1:1.0", deps)
    assert not d.rate_limited


# ======================================================================================
# the outbox gates
# ======================================================================================

def _posted():
    calls = []

    def post(channel, text, thread_ts=None):
        calls.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return "9.999"
    return post, calls


def test_reply_never_posts_without_verification_after(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    bus = FakeBus()
    ans = lambda t, w, m: {"body": "answer", "grounding": {"x": 1}}
    d = A.handle_event(_ev("are my accounts connected?"), "k", _deps(bus, answer=ans, client_armed=True))
    bus.set_ticket(d.ticket_id, verification_after=None)   # someone cleared it
    post, calls = _posted()
    s = OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert not any("answer" in c["text"] for c in calls)
    answer_row = [m for m in bus.messages_for(d.ticket_id)
                  if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_ANSWER][0]
    assert answer_row["delivery_status"] == "suppressed"
    assert s["suppressed"] >= 1


def test_client_reply_held_when_client_flag_off(monkeypatch):
    monkeypatch.delenv("SLACK_CONVO_ECHO_CLIENT_REPLY", raising=False)
    bus = FakeBus()
    d = A.handle_event(_ev("posts broken"), "k", _deps(bus, client_armed=False))
    ack = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_ACK][0]
    assert ack["delivery_status"] == "held"
    assert A.KIND_HOLD_NOTICE in bus.outbound_kinds(d.ticket_id), "one tap notice per held row"
    post, calls = _posted()
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    monkeypatch.setenv("AGENT_OPS_FIX_CHANNEL_ID", "C_OPSFIX")
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    # nothing went to the client's conversation; internal kinds went to their channels
    assert all(c["channel"] in ("C_FIXER", "C_OPSFIX") for c in calls)
    assert any(c["channel"] == "C_FIXER" and "HELD REPLY" in c["text"] for c in calls)


def test_outbox_recheck_holds_a_ready_row_if_flag_flipped_off(monkeypatch):
    """Written ready while armed, then the flag is flipped off before dispatch: held."""
    bus = FakeBus()
    d = A.handle_event(_ev("posts broken"), "k", _deps(bus, client_armed=True))
    ack = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_ACK][0]
    assert ack["delivery_status"] == "ready"
    monkeypatch.delenv("SLACK_CONVO_ECHO_CLIENT_REPLY", raising=False)
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    monkeypatch.setenv("AGENT_OPS_FIX_CHANNEL_ID", "C_OPSFIX")
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.ticket(d.ticket_id)  # sanity
    assert [m for m in bus.msgs if m["id"] == ack["id"]][0]["delivery_status"] == "held"
    assert not any(c["channel"] == "G0MPIM" for c in calls)


def test_bot_never_posts_in_a_thread_with_no_prior_human_message(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    bus = FakeBus()
    # a ticket with an outbound row but NO inbound row (only reachable by bypassing the
    # adapter -- which is exactly what the gate is for)
    t, _ = bus.get_or_create_ticket(channel_id="G0", thread_ts="1.0", product="echo",
                                    bot_identity="echo", slack_user_id="U", identity_kind="client",
                                    client_id="g", reporter="x", raw_text="x")
    bus.record_outbound(ticket_id=t["id"], author_type="echo", body="hello there",
                        delivery_status="ready", kind=A.KIND_ACK,
                        meta={"surface": "mpim", "recipient_kind": "client"})
    post, calls = _posted()
    s = OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert calls == []
    assert s["suppressed"] == 1


def test_posted_row_gets_slack_ts_and_dm_posts_top_level(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    bus = FakeBus()
    d = A.handle_event(_ev("posts broken", channel="G0MPIM", channel_type="mpim"), "k",
                       _deps(bus, client_armed=True))
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    monkeypatch.setenv("AGENT_OPS_FIX_CHANNEL_ID", "C_OPSFIX")
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    ack_call = [c for c in calls if c["channel"] == "G0MPIM"][0]
    assert ack_call["thread_ts"] is None, "a DM/group DM continues top level, not threaded"
    ack = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_ACK][0]
    assert ack["delivery_status"] == "posted" and ack["slack_ts"] == "9.999"


def test_channel_mention_replies_in_thread(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    bus = FakeBus()
    A.handle_event(_ev("@echo posts broken", channel="C_ROOM", channel_type="channel",
                       etype="app_mention", ts="5.0"), "C_ROOM:5.0", _deps(bus, client_armed=True))
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    monkeypatch.setenv("AGENT_OPS_FIX_CHANNEL_ID", "C_OPSFIX")
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    ack_call = [c for c in calls if c["channel"] == "C_ROOM"][0]
    assert ack_call["thread_ts"] == "5.0"


def test_missing_fixer_channel_fails_loudly_not_silently(monkeypatch):
    monkeypatch.delenv("AGENT_FIXER_CHANNEL_ID", raising=False)
    bus = FakeBus()
    d = A.handle_event(_ev("hello?"), "k", _deps(bus, who=IG.UNKNOWN))
    post, calls = _posted()
    logs = []
    s = OB.run_once(bus, post, identity=IDS.get("echo"), log=logs.append)
    assert s["failed"] >= 1
    assert any("no channel configured" in l for l in logs)


def test_release_tap_flips_held_to_ready_and_refuses_non_held():
    bus = FakeBus()
    d = A.handle_event(_ev("posts broken"), "k", _deps(bus, client_armed=False))
    ack = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound" and m["attachments"]["kind"] == A.KIND_ACK][0]
    assert OB.release_held(bus, ack["id"], approved_by="U_BLAKE", log=lambda *a: None) is True
    assert [m for m in bus.msgs if m["id"] == ack["id"]][0]["delivery_status"] == "ready"
    assert bus.tickets[d.ticket_id]["approved_via"] == "slack_button"
    # a second tap on the (now ready) row is a no-op
    assert OB.release_held(bus, ack["id"], approved_by="U_BLAKE", log=lambda *a: None) is False


# ======================================================================================
# config-only onboarding of a second identity
# ======================================================================================

def test_all_five_identities_exist_in_config_and_only_echo_is_startable_by_default(monkeypatch):
    for n in ("echo", "ranger", "scout", "wrangler", "lainey"):
        assert IDS.get(n).product == n
    for n in ("RANGER", "SCOUT", "WRANGLER", "LAINEY"):
        monkeypatch.delenv(f"{n}_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv(f"{n}_SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("AGENT_SLACK_APP_TOKEN", "xapp-test")
    assert [i.name for i in IDS.startable()] == ["echo"]


def test_second_identity_onboards_by_env_alone(monkeypatch):
    """No code change: set Ranger's two token env vars and it becomes startable, in arming
    order after Echo."""
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("AGENT_SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("RANGER_SLACK_BOT_TOKEN", "xoxb-r")
    monkeypatch.setenv("RANGER_SLACK_APP_TOKEN", "xapp-r")
    assert [i.name for i in IDS.startable()] == ["echo", "ranger"]


def test_identity_flags_are_per_bot(monkeypatch):
    from agent import config
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.delenv("SLACK_CONVO_RANGER_ENABLED", raising=False)
    assert config.slack_convo_identity_enabled("echo") is True
    assert config.slack_convo_identity_enabled("ranger") is False
    monkeypatch.delenv("SLACK_CONVO_ENABLED", raising=False)
    assert config.slack_convo_identity_enabled("echo") is False, "master off wins"


def test_every_flag_defaults_off(monkeypatch):
    from agent import config
    for k in ("SLACK_CONVO_ENABLED", "SLACK_CONVO_ECHO_ENABLED",
              "SLACK_CONVO_ECHO_CLIENT_REPLY", "SLACK_CONVO_ECHO_STAFF_REPLY"):
        monkeypatch.delenv(k, raising=False)
    assert config.slack_convo_enabled() is False
    assert config.slack_convo_identity_enabled("echo") is False
    assert config.slack_convo_client_reply_armed("echo") is False
    assert config.slack_convo_staff_reply_armed("echo") is False
    assert config.slack_convo_daily_ticket_cap() == 10


# ======================================================================================
# identity gate
# ======================================================================================

def _gate(info=None, portal=None, ops=()):
    return IG.resolve("U1", slack_user_info=lambda u: info,
                      portal_lookup=lambda e: portal, operator_ids=ops)


def test_operator_is_staff_without_any_lookup():
    who = IG.resolve("U_B", slack_user_info=lambda u: (_ for _ in ()).throw(AssertionError("no lookup")),
                     portal_lookup=lambda e: None, operator_ids=("U_B",))
    assert who.kind == IG.STAFF


def test_client_owner_resolves_to_client_with_account():
    who = _gate(info={"email": "chad@x.com", "real_name": "Chad"},
                portal={"role": "client", "gyms": [{"gym_id": "g1", "relationship": "client_owner",
                                                     "account_key": "crossfitlocal"}]})
    assert who.kind == IG.CLIENT and who.account_key == "crossfitlocal" and who.gym_id == "g1"


def test_multi_gym_owner_is_unknown_never_a_guess():
    who = _gate(info={"email": "x@x.com"},
                portal={"role": "client", "gyms": [
                    {"gym_id": "g1", "relationship": "client_owner", "account_key": "a"},
                    {"gym_id": "g2", "relationship": "client_owner", "account_key": "b"}]})
    assert who.kind == IG.UNKNOWN and "ambiguous" in who.reason


@pytest.mark.parametrize("info,portal", [
    (None, None),                                          # slack knows nothing
    ({"email": ""}, None),                                 # no email on profile
    ({"email": "s@x.com"}, None),                          # no portal user
    ({"email": "s@x.com"}, {"role": "client", "gyms": []}),  # client with no gym
])
def test_strangers_and_gaps_are_unknown(info, portal):
    assert _gate(info=info, portal=portal).kind == IG.UNKNOWN


def test_lookup_failures_fall_to_unknown_never_promote():
    who = IG.resolve("U1", slack_user_info=lambda u: (_ for _ in ()).throw(RuntimeError("slack down")),
                     portal_lookup=lambda e: None)
    assert who.kind == IG.UNKNOWN
    who = IG.resolve("U1", slack_user_info=lambda u: {"email": "a@b.com"},
                     portal_lookup=lambda e: (_ for _ in ()).throw(RuntimeError("db down")))
    assert who.kind == IG.UNKNOWN


def test_bots_are_bots():
    assert _gate(info={"is_bot": True}).kind == IG.BOT


# ======================================================================================
# classifier + answer lane rails
# ======================================================================================

def test_classifier_order_and_default():
    assert C.classify("anything", has_open_ticket=True, identity_product="echo") == C.FOLLOW_UP
    assert C.classify("pause the ads", has_open_ticket=False, identity_product="ranger") == C.ACTION_REQUEST
    assert C.classify("pause the ads", has_open_ticket=False, identity_product="echo") != C.ACTION_REQUEST
    assert C.classify("my post never published", has_open_ticket=False, identity_product="echo") == C.CODE_FIX
    assert C.classify("is instagram connected?", has_open_ticket=False, identity_product="echo") == C.QUESTION
    assert C.classify("ok", has_open_ticket=False, identity_product="echo") is C.ESCALATE
    assert C.classify("", has_open_ticket=False, identity_product="echo") is C.ESCALATE


def test_llm_fallback_cannot_widen_the_label_set():
    assert C.classify("hmm", has_open_ticket=False, identity_product="echo",
                      llm=lambda t: "delete_everything") is C.ESCALATE
    assert C.classify("hmm", has_open_ticket=False, identity_product="echo",
                      llm=lambda t: C.FOLLOW_UP) is C.ESCALATE, "follow_up is decided by state, not a model"
    assert C.classify("hmm", has_open_ticket=False, identity_product="echo",
                      llm=lambda t: (_ for _ in ()).throw(RuntimeError())) is C.ESCALATE


def test_answer_lane_refuses_billing_before_any_model_call():
    from agent.slack_convo import answer_lane as AL
    called = []
    who = _who(IG.CLIENT)
    out = AL.answer({"id": "t", "raw_text": "why was I charged $149?"}, who,
                    [{"direction": "inbound", "body": "why was I charged $149?", "author_type": "client"}],
                    identity=IDS.get("echo"), fetch_state=lambda t, w: {"x": 1},
                    llm=lambda s, u: called.append(1) or "here is your bill")
    assert out is None and called == []


def test_answer_lane_strips_dashes_and_grounds():
    from agent.slack_convo import answer_lane as AL
    who = _who(IG.CLIENT)
    out = AL.answer({"id": "t", "raw_text": "connected?"}, who,
                    [{"direction": "inbound", "body": "are we connected?", "author_type": "client"}],
                    identity=IDS.get("echo"), fetch_state=lambda t, w: {"ig": "connected"},
                    llm=lambda s, u: "Yes — Instagram is connected – and posting.")
    assert "—" not in out["body"] and "–" not in out["body"]
    assert out["grounding"]["facts"] == {"ig": "connected"}


def test_answer_lane_refuses_a_model_answer_that_drifts_into_billing():
    from agent.slack_convo import answer_lane as AL
    who = _who(IG.CLIENT)
    out = AL.answer({"id": "t", "raw_text": "connected?"}, who,
                    [{"direction": "inbound", "body": "are we connected?", "author_type": "client"}],
                    identity=IDS.get("echo"), fetch_state=lambda t, w: {},
                    llm=lambda s, u: "Yes, and your subscription renews at $149.")
    assert out is None
