"""
tests/test_echo_ticket_worker.py -- D46 (Blake, 2026-09-04): the portal-Echo-ticket
bridge. "with echo if someone submits a support echo should receive that then echo
should fix it verify the fix and then send slack message with them and me in the
message."

D46/D47 audit fix tests (Frame 1 + Frame 2, 2026-09-04) added at the bottom:
bot_identity stamping (Frame 1 CRITICAL -- without it, outbox.py silently never
delivers a held code_fix card or an escalation, forever), per-ticket exception
isolation (Frame 1 MINOR), and identity_gate-backed resolution that rejects
cross-tenant impersonation (Frame 2 MAJOR -- a coach/staff account hitting a
different gym's ticket endpoint must never be treated as THAT gym's client).
"""
import os

import pytest

from agent import echo_ticket_worker as W
from agent.slack_convo import adapter as A
from agent.slack_convo import classifier as C
from agent.slack_convo import identity_gate as IG


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "true")
    # C2 (2026-09-05 audit): the bridge's QUESTION branch now obeys the same D54 gates as
    # every other client-facing path -- a grounded answer sends unattended ONLY with that
    # identity's AUTO_ANSWER armed on top of CLIENT_REPLY. These tests are about the intake
    # behaviour, not the permission, so they arm it explicitly; the tests that are about the
    # permission live in tests/test_portal_escalation_loop.py and turn it off deliberately.
    for ident in ("ECHO", "SCOUT"):
        monkeypatch.setenv(f"SLACK_CONVO_{ident}_ENABLED", "true")
        monkeypatch.setenv(f"SLACK_CONVO_{ident}_CLIENT_REPLY", "true")
        monkeypatch.setenv(f"SLACK_CONVO_{ident}_AUTO_ANSWER", "true")
    monkeypatch.setenv("SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE", "true")
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    yield


def _off(monkeypatch):
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "false")


class FakeBus:
    def __init__(self, tickets):
        self.tickets = {t["id"]: dict(t) for t in tickets}
        self.inbound = []
        self.outbound = []
        self.patches = []
        self.raise_on_inbound = set()
        self.raise_on_patch = set()

    def find_new_tickets(self, *, product, source, limit=20):
        return [dict(t) for t in self.tickets.values()
               if t.get("product") == product and t.get("source") == source
               and t.get("status") == "new" and t.get("classification") is None]

    def find_fixing_tickets(self, *, product, limit=20):
        return [t for t in self.tickets.values()
               if t.get("product") == product and t.get("status") == "fixing"]

    def record_inbound(self, **kwargs):
        tid = kwargs.get("ticket_id")
        if tid in self.raise_on_inbound:
            raise RuntimeError("bus down for this row")
        self.inbound.append(kwargs)
        if kwargs.get("author_type") == "client":
            self.tickets[tid]["request_version"] += 1
        return ({"id": f"in-{len(self.inbound)}"}, False)

    def inbound_count(self, ticket_id):
        return len([m for m in self.inbound if m.get("ticket_id") == ticket_id])

    def record_outbound(self, **kwargs):
        current_version = self.tickets[kwargs["ticket_id"]].get("request_version")
        expected_version = kwargs.get("expected_request_version")
        if expected_version is not None and expected_version != current_version:
            raise RuntimeError("outbound request version changed before insert")
        meta = kwargs.get("meta") or {}
        if meta.get("delivery_identity_fence") is True:
            ticket = self.tickets[kwargs["ticket_id"]]
            expected = (meta.get("delivery_expected_product"),
                        meta.get("delivery_expected_client_id"),
                        meta.get("delivery_expected_status"),
                        meta.get("delivery_expected_classification"),
                        meta.get("delivery_expected_bot_identity"),
                        meta.get("delivery_expected_slack_user_id"))
            actual = tuple(ticket.get(field) for field in (
                "product", "client_id", "status", "classification",
                "bot_identity", "slack_user_id"))
            if expected != actual:
                raise RuntimeError("outbound delivery identity changed before insert")
        row = {"id": f"out-{len(self.outbound)}", **kwargs,
               "delivery_request_version": current_version}
        row["attachments"] = {"kind": kwargs.get("kind"), **(kwargs.get("meta") or {})}
        self.outbound.append(row)
        return row

    def set_ticket(self, ticket_id, **fields):
        self.patches.append((ticket_id, fields))
        self.tickets[ticket_id].update(fields)

    def patch_ticket_if_current(self, expected_ticket, **fields):
        if type(expected_ticket.get("request_version")) is not int:
            return None
        if expected_ticket["id"] in self.raise_on_patch:
            raise RuntimeError("bus down for this row")
        current = self.tickets[expected_ticket["id"]]
        identity = ("request_version", "status", "classification", "product",
                    "source", "client_id", "reporter", "bot_identity",
                    "slack_user_id", "slack_channel_id", "slack_thread_ts",
                    "hold_tier", "escalated")
        if any(current.get(field) != expected_ticket.get(field) for field in identity):
            return None
        self.set_ticket(expected_ticket["id"], **fields)
        return self.ticket(expected_ticket["id"])

    def ticket(self, ticket_id):
        return dict(self.tickets[ticket_id])

    def mark_message(self, message_id, delivery_status, slack_ts=None, meta_update=None):
        row = next(m for m in self.outbound if m["id"] == message_id)
        if delivery_status == "posted" and row["attachments"].get("delivery_identity_fence"):
            ticket = self.tickets[row["ticket_id"]]
            expected = (row["delivery_request_version"], *(
                row["attachments"].get(f"delivery_expected_{field}") for field in (
                    "product", "client_id", "status", "classification",
                    "bot_identity", "slack_user_id")))
            actual = tuple(ticket.get(field) for field in (
                "request_version", "product", "client_id", "status", "classification",
                "bot_identity", "slack_user_id"))
            if expected != actual:
                raise RuntimeError("outbound delivery identity changed before posted receipt")
        row["delivery_status"] = delivery_status
        row["slack_ts"] = slack_ts
        row["attachments"].update(meta_update or {})
        return dict(row)

    def claim_message(self, message_id):
        row = next(m for m in self.outbound if m["id"] == message_id)
        if row["delivery_status"] != "ready":
            return False
        row["delivery_status"] = "posting"
        return True

    def hold_uncertain_outreach(self, message_id):
        row = next(m for m in self.outbound if m["id"] == message_id)
        if row["delivery_status"] == "posted":
            return dict(row)
        if row["delivery_status"] in ("posting", "ready"):
            row["delivery_status"] = "held"
            row["attachments"].update({"outreach_delivery_uncertain": True,
                                       "held_why": "Slack may have delivered"})
        return dict(row)

    def stamp_ticket(self, ticket_id, *, channel_id, thread_ts, slack_user_id,
                     bot_identity, identity_kind, expected_ticket=None):
        current = self.ticket(ticket_id)
        if expected_ticket is None or any(
                current.get(field) != expected_ticket.get(field) for field in (
                    "request_version", "status", "classification", "product", "client_id",
                    "bot_identity", "slack_user_id", "slack_channel_id", "slack_thread_ts")):
            return None
        self.set_ticket(ticket_id, slack_channel_id=channel_id,
                        slack_thread_ts=thread_ts, slack_user_id=slack_user_id,
                        bot_identity=bot_identity, identity_kind=identity_kind)
        return self.ticket(ticket_id)

    def resolve_current_delivery(self, tid, version, status, classification, product,
                                 client_id, bot_identity, slack_user_id, channel, thread):
        t = self.tickets[tid]
        expected = (version, status, classification, product, client_id, bot_identity,
                    slack_user_id, channel, thread)
        actual = tuple(t.get(k) for k in ("request_version", "status", "classification",
                                         "product", "client_id", "bot_identity",
                                         "slack_user_id", "slack_channel_id", "slack_thread_ts"))
        if actual != expected or t.get("escalated") is True or t.get("hold_tier") is not None:
            return None
        if not any(m.get("ticket_id") == tid and m.get("delivery_status") == "posted"
                   and m.get("delivery_request_version") == version
                   and m["attachments"].get("kind") == "status"
                   and m["attachments"].get("resolve_notice") is True
                   for m in self.outbound):
            return None
        self.set_ticket(tid, status="resolved")
        return self.ticket(tid)


def _ticket(**over):
    row = {
        "id": "t-1", "product": "echo", "source": "website_tab",
        "client_id": "g-1", "reporter": "owner@gym.com",
        "raw_text": "my Instagram posts stopped going out",
        "status": "new", "classification": None,
        "request_version": 0,
        "escalated": False, "hold_tier": None,
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


# ---- identity fakes: a tiny directory of Slack users, keyed like the real
# identity_gate.resolve() expects (slack_user_id -> profile -> portal role/gyms) -----

def _directory(users):
    """users: {slack_user_id: {"email":..., "role":..., "gyms":[{gym_id,relationship,
    account_key}]}}. Returns (slack_lookup_email, slack_user_info, portal_lookup)."""
    by_email = {u["email"]: uid for uid, u in users.items()}

    def slack_lookup_email(email):
        return by_email.get(email)

    def slack_user_info(uid):
        u = users.get(uid)
        if not u:
            return {"id": uid, "is_bot": False, "email": "", "real_name": ""}
        return {"id": uid, "is_bot": False, "email": u["email"],
                "real_name": u.get("name", "Test User"),
                "is_restricted": False, "is_ultra_restricted": False}

    def portal_lookup(email):
        uid = by_email.get(email)
        if not uid:
            return None
        u = users[uid]
        return {"role": u["role"], "gyms": u.get("gyms", [])}

    return slack_lookup_email, slack_user_info, portal_lookup


def _client_deps(email="owner@gym.com", uid="U_CLIENT", gym_id="g-1",
                 account_key="crossfitlocal"):
    """The default, correctly-scoped case every pre-existing test uses: an
    authenticated client whose OWN gym_assignments row matches the ticket's
    client_id."""
    lookup, info, portal = _directory({
        uid: {"email": email, "role": "client",
              "gyms": [{"gym_id": gym_id, "relationship": "client_owner",
                       "account_key": account_key}]},
    })
    return dict(slack_lookup_email=lookup, slack_user_info=info, portal_lookup=portal,
               operator_ids=())


def _no_account_deps():
    return dict(slack_lookup_email=lambda e: None,
               slack_user_info=lambda u: {"is_bot": False, "email": ""},
               portal_lookup=lambda e: None, operator_ids=())


def _raising_lookup_deps():
    def boom(e):
        raise RuntimeError("slack down")
    return dict(slack_lookup_email=boom,
               slack_user_info=lambda u: {"is_bot": False, "email": ""},
               portal_lookup=lambda e: None, operator_ids=())


# ---- config gate -------------------------------------------------------------------

def test_intake_pass_is_a_full_noop_when_the_flag_is_off(monkeypatch):
    _off(monkeypatch)
    bus = FakeBus([_ticket()])
    log, open_dm, post = _calls()
    _notices_calls, notice = _notices()
    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, **_client_deps())
    assert result == {"processed": 0}
    assert bus.inbound == [] and bus.outbound == []


def test_fixed_pass_is_a_full_noop_when_the_flag_is_off(monkeypatch):
    _off(monkeypatch)
    bus = FakeBus([_ticket(status="fixing", verification_after={"verified": True, "fix_pr_url": "x"})])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result == {"notified": 0}
    assert log["opened"] == []


# ---- identity resolution -------------------------------------------------------------

def test_resolve_client_identity_succeeds():
    who = W.resolve_client_identity(_ticket(), **_client_deps())
    assert who.kind == "client"
    assert who.slack_user_id == "U_CLIENT"
    assert who.account_key == "crossfitlocal"
    assert who.gym_id == "g-1"


def test_resolve_client_identity_unknown_when_no_slack_account():
    who = W.resolve_client_identity(_ticket(), **_no_account_deps())
    assert who.kind == "unknown"


def test_resolve_client_identity_unknown_when_lookup_raises():
    who = W.resolve_client_identity(_ticket(), **_raising_lookup_deps())
    assert who.kind == "unknown"


def test_resolve_client_identity_unknown_when_ticket_missing_reporter():
    who = W.resolve_client_identity(_ticket(reporter=""), **_client_deps())
    assert who.kind == "unknown"


# ---- Frame 2 audit fix (MAJOR): cross-tenant impersonation is rejected ---------------
# The portal's own access check (canReadGym) lets coach/executive/owner roles read
# ANY gym, not just their own -- so a staff account authenticating against a
# DIFFERENT gym's support endpoint must never be resolved as that gym's CLIENT.

def test_resolve_client_identity_never_promotes_staff_to_client():
    lookup, info, portal = _directory({
        "U_STAFF": {"email": "coach@lassoframework.com", "role": "owner", "gyms": []},
    })
    who = W.resolve_client_identity(
        _ticket(client_id="some-other-gym", reporter="coach@lassoframework.com"),
        slack_lookup_email=lookup, slack_user_info=info, portal_lookup=portal,
        operator_ids=())
    assert who.kind == "staff"
    assert who.kind != "client"


def test_resolve_client_identity_never_promotes_coach_to_client():
    lookup, info, portal = _directory({
        "U_COACH": {"email": "coach2@lassoframework.com", "role": "coach", "gyms": []},
    })
    who = W.resolve_client_identity(
        _ticket(client_id="some-other-gym", reporter="coach2@lassoframework.com"),
        slack_lookup_email=lookup, slack_user_info=info, portal_lookup=portal,
        operator_ids=())
    assert who.kind == "coach"
    assert who.kind != "client"


def test_resolve_client_identity_rejects_a_real_client_owner_of_a_different_gym():
    """A genuine client_owner of gym g-2 hitting a ticket whose client_id claims
    g-1 (the OTHER gym) must resolve UNKNOWN, not silently get treated as g-1's
    client with g-2's own account_key/gym_id."""
    lookup, info, portal = _directory({
        "U_OTHER_OWNER": {"email": "owner@othergym.com", "role": "client",
                          "gyms": [{"gym_id": "g-2", "relationship": "client_owner",
                                   "account_key": "othergym"}]},
    })
    who = W.resolve_client_identity(
        _ticket(client_id="g-1", reporter="owner@othergym.com"),
        slack_lookup_email=lookup, slack_user_info=info, portal_lookup=portal,
        operator_ids=())
    assert who.kind == "unknown"


def test_intake_pass_escalates_rather_than_impersonates_a_cross_tenant_staff_ticket():
    """End-to-end: a staff account's ticket against a gym they don't own must
    escalate to Blake, never trigger the autonomous client-outreach fast path."""
    lookup, info, portal = _directory({
        "U_STAFF": {"email": "coach@lassoframework.com", "role": "owner", "gyms": []},
    })
    bus = FakeBus([_ticket(client_id="not-their-gym", reporter="coach@lassoframework.com")])
    log, open_dm, post = _calls()
    _, notice = _notices()
    W.intake_pass(bus, slack_lookup_email=lookup, slack_user_info=info,
                 portal_lookup=portal, operator_ids=(), open_group_dm=open_dm,
                 post_first_message=post, write_hold_notice=notice)
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    assert log["opened"] == []  # never an autonomous DM to/about the wrong gym


# ---- intake_pass: unresolved identity ------------------------------------------------

def test_intake_pass_escalates_an_unresolved_identity_never_dispatches():
    bus = FakeBus([_ticket()])
    log, open_dm, post = _calls()
    _, notice = _notices()
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                 write_hold_notice=notice, **_no_account_deps())
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    assert log["opened"] == []
    assert any(o["kind"] == A.KIND_ESCALATION for o in bus.outbound)


# ---- Frame 1 audit fix (CRITICAL): bot_identity must be stamped ----------------------
# outbox.py's dispatch gate refuses to post ANY row whose parent ticket's
# bot_identity does not match the identity currently running. A portal-inserted
# ticket never passes through get_or_create_ticket (the only OTHER place that
# stamps it), so without this stamp a held fixer_request card or an escalation row
# would sit posted-nowhere forever, with no error.

def test_intake_pass_stamps_bot_identity_even_on_an_unresolved_identity():
    bus = FakeBus([_ticket()])
    log, open_dm, post = _calls()
    _, notice = _notices()
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                 write_hold_notice=notice, identity_name="echo", **_no_account_deps())
    assert bus.tickets["t-1"]["bot_identity"] == "echo"


def test_intake_pass_stamps_bot_identity_on_a_code_fix_ticket():
    bus = FakeBus([_ticket(raw_text="my instagram posting is broken and errors out")])
    log, open_dm, post = _calls()
    _, notice = _notices()
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                 write_hold_notice=notice, identity_name="echo", **_client_deps())
    assert bus.tickets["t-1"]["bot_identity"] == "echo"


def test_intake_pass_stamps_the_correct_identity_for_a_non_echo_pass():
    bus = FakeBus([_ticket(product="portal", client_id="g-1",
                          raw_text="how do I add my group class schedule?")])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        return {"portal_status": "ok"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                 write_hold_notice=notice, product="portal", identity_name="scout",
                 fetch_state=fetch_state, llm=lambda s, u: "answer",
                 **_client_deps())
    assert bus.tickets["t-1"]["bot_identity"] == "scout"


# ---- Frame 1 audit fix (MINOR): one bad ticket must not starve the rest of the batch --

def test_intake_pass_isolates_a_bus_failure_to_the_one_ticket_that_hit_it():
    bus = FakeBus([
        _ticket(id="t-bad", reporter="owner@gym.com",
               raw_text="is my instagram connected?"),
        _ticket(id="t-good", reporter="owner@gym.com",
               raw_text="is my instagram connected?"),
    ])
    bus.raise_on_patch = {"t-bad"}
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        return {"social_status": "connected"}

    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, fetch_state=fetch_state,
                           llm=lambda s, u: "Yes, connected.",
                           mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                           **_client_deps())
    assert result == {"processed": 1}  # only t-good counted
    assert bus.tickets["t-bad"]["status"] == "new"  # never touched past the crash
    assert bus.tickets["t-good"]["status"] == "resolved"


# ---- intake_pass: question ------------------------------------------------------------

def test_intake_pass_answers_a_grounded_question_and_sends_outreach():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?")])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        return {"social_status": "connected"}

    def llm(system, user):
        return "Yes, your Instagram is connected right now."

    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, fetch_state=fetch_state, llm=llm,
                           mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                           **_client_deps())
    assert result == {"processed": 1}
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert bus.inbound == [], "portal raw_text is already the original requester record"
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

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                 write_hold_notice=notice, fetch_state=fetch_state,
                 llm=lambda s, u: "irrelevant", **_client_deps())
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    assert log["opened"] == []


def test_held_portal_answer_creates_team_card_without_customer_notice(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "false")
    bus = FakeBus([_ticket(raw_text="is my instagram connected?")])
    log, open_dm, post = _calls()
    notices, notice = _notices()

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda system, user: "Your Instagram is connected.",
                  **_client_deps())

    assert bus.tickets["t-1"]["status"] == "hold"
    assert len(notices) == 1
    assert [row["kind"] for row in bus.outbound] == [A.KIND_ANSWER]
    assert bus.outbound[0]["delivery_status"] == "held"
    assert log["opened"] == [] and log["posted"] == []


# ---- intake_pass: code_fix -- D14's hold gate is untouched ---------------------------

def test_intake_pass_holds_a_code_fix_behind_the_fixer_tap_same_as_any_other():
    bus = FakeBus([_ticket(raw_text="my instagram posting is broken and errors out")])
    log, open_dm, post = _calls()
    notices, notice = _notices()
    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, **_client_deps())
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
    assert not [o for o in bus.outbound if o["kind"] in (A.KIND_ACK, A.KIND_STATUS, A.KIND_TEMPLATE)]


def test_portal_code_fix_keeps_portal_product_with_scout_identity():
    """The bridge bot is Scout, but FIXER must receive the original portal product
    and ticket identity so the request routes to the portal support lane."""
    ticket_id = "portal-ticket-original-42"
    bus = FakeBus([_ticket(id=ticket_id, product="portal",
                           raw_text="my instagram posting is broken and errors out")])
    _, _, post = _calls()
    notices, notice = _notices()

    result = W.intake_pass(bus, open_group_dm=lambda *_: pytest.fail("must remain held"),
                           post_first_message=post, write_hold_notice=notice,
                           product="portal", identity_name="scout", **_client_deps())

    assert result == {"processed": 1}
    assert bus.tickets[ticket_id]["id"] == ticket_id
    assert bus.tickets[ticket_id]["bot_identity"] == "scout"
    assert bus.tickets[ticket_id]["product"] == "portal"
    fixer_rows = [row for row in bus.outbound if row["kind"] == A.KIND_FIXER_REQUEST]
    assert len(fixer_rows) == 1
    assert fixer_rows[0]["ticket_id"] == ticket_id
    assert fixer_rows[0]["delivery_status"] == "held"
    assert f"ticket {ticket_id} for product portal" in fixer_rows[0]["body"]
    assert "for product scout" not in fixer_rows[0]["body"]
    assert fixer_rows[0]["meta"]["identity"] == "scout"
    assert len(notices) == 1
    assert notices[0]["tid"] == ticket_id
    assert notices[0]["ident_name"] == "scout"
    assert notices[0]["body"] == fixer_rows[0]["body"]


# ---- fixed_pass -------------------------------------------------------------------

# ---- D68 (2026-09-06): the fix lane refuses instead of being inert ------------------
#
# Blake: "Either wire the producer or make the field refuse to be read as a pass. An inert
# verification field is worse than no field."
#
# FIX_VERIFICATION_PRODUCERS is empty in production, so every test below that wants the
# DELIVERY path must say so out loud by registering a producer. That is the point: the only
# thing between a ticket in 'fixing' and a client being told "fixed it" is a registry entry
# that no process currently earns, and these tests fail if that stops being true.

@pytest.fixture
def _wired(monkeypatch):
    """Pretend a fix producer exists, so the delivery path stays covered and cannot rot."""
    monkeypatch.setattr(W, "FIX_VERIFICATION_PRODUCERS", frozenset({"ops_fix"}))
    yield


def _verdict(**kw):
    """A snapshot shaped like something a REGISTERED producer wrote."""
    base = {"producer": "ops_fix", "verified": True}
    base.update(kw)
    return base


def test_the_fix_verification_lane_is_unwired_in_production():
    """The two-way guard (D55's lesson). If someone fills this registry, they have to come
    here and say why -- and every 'refuses' test below stops being vacuous at that moment."""
    assert W.FIX_VERIFICATION_PRODUCERS == frozenset(), (
        "A fix-verification producer was registered. That is a real cross-repo wiring "
        "change: confirm the producer writes verification_after onto the ORIGINATING "
        "ticket (not a row it mints), then update D68 and this test.")
    assert W.fix_verification_lane_is_wired() is False


def test_fixed_pass_refuses_rather_than_silently_polling_a_gate_that_cannot_open():
    """THE MUTATION CHECK for D68. This ticket is verified as hard as a ticket can be --
    an affirmative verdict AND a PR url. The OLD code notified this client. The new code
    must refuse, because nothing can actually put that value there, and 'notified: 0' with
    no reason is exactly the inert state the ruling forbids.

    Revert the refusal in fixed_pass and this test fails: it would return {'notified': 1}."""
    bus = FakeBus([_ticket(status="fixing", slack_user_id="U_CLIENT",
                          verification_after={"verified": True,
                                              "fix_pr_url": "https://github.com/x/y/pull/1"})])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result["notified"] == 0
    # Not merely zero -- zero WITH A NAMED REASON. A caller, a log line or a metric can now
    # tell "impossible" from "not yet", which is the whole ruling.
    assert result["refused"] == "fix_verification_lane_unwired"
    assert result["fixing"] == 1
    assert bus.tickets["t-1"]["status"] == "fixing"   # never resolved on a refusal
    assert log["opened"] == []                        # and the client is never told anything


def test_read_fix_verification_raises_loudly_while_the_lane_is_unwired():
    """'Loud, not a comment.' The accessor raises; it does not return a falsy 'not yet'."""
    with pytest.raises(W.InertVerificationLane) as e:
        W.read_fix_verification({"verification_after": _verdict()})
    assert "no registered fix producer" in str(e.value)
    assert "D68" in str(e.value)


def test_the_answer_lanes_grounding_snapshot_is_never_read_as_a_fix_verdict(_wired):
    """The column is overloaded: answer_pass writes its grounding snapshot to this same
    verification_after. That lane is wired and correct and is NOT what this pass gates on.
    Even with a producer registered, an unattributed snapshot is not a fix verdict."""
    grounding = {"social_status": {"connected": True}, "calendar_this_month": 4}
    assert W.read_fix_verification({"verification_after": grounding}) is None
    bus = FakeBus([_ticket(status="fixing", slack_user_id="U_CLIENT",
                          verification_after=grounding)])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result == {"notified": 0}
    assert bus.tickets["t-1"]["status"] == "fixing"
    assert log["opened"] == []


def test_fixed_pass_notifies_once_a_registered_producer_has_verified(_wired):
    """The legacy fixing state cannot satisfy migration 0381's resolution CAS."""
    bus = FakeBus([_ticket(status="fixing", slack_user_id="U_CLIENT",
                          verification_after=_verdict(fix_pr_url="https://github.com/x/y/pull/1"))])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result == {"notified": 0}
    assert bus.tickets["t-1"]["status"] == "fixing"
    assert log["posted"] == []


def test_fixed_pass_leaves_an_unverified_ticket_alone(_wired):
    bus = FakeBus([_ticket(status="fixing", slack_user_id="U_CLIENT",
                          verification_after=None)])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert result == {"notified": 0}
    assert bus.tickets["t-1"]["status"] == "fixing"
    assert log["opened"] == []


def test_fixed_pass_escalates_if_slack_user_id_was_never_persisted(_wired):
    bus = FakeBus([_ticket(status="fixing",
                          verification_after=_verdict(fix_pr_url="x"))])
    log, open_dm, post = _calls()
    W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post)
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    assert log["opened"] == []


# ---- D47: generalized to a second (product, identity) pair -- portal -> Scout ---------
# fixer-lane.ts (the portal's ranger-only cron) never had a reason to see a non-ranger
# ticket; a product='portal' ticket needs its own real consumer, routed to Scout per
# the identity map, not bolted onto ranger's ad-engine-specific worker.

def test_intake_pass_routes_product_portal_to_scout_identity():
    # The question text is deliberately NOT a gym-schedule one: "group class schedule" is on
    # the D54 hard-line list (a real-world commitment about a client's classes), so it would
    # hold for a tap and this test is about ROUTING, not about the permission.
    bus = FakeBus([_ticket(product="portal", raw_text="is my instagram connected?")])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        return {"portal_status": "ok"}

    def llm(system, user):
        return "You can add it from the Website tab, under Content."

    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, product="portal",
                           identity_name="scout", fetch_state=fetch_state, llm=llm,
                           mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                           **_client_deps())
    assert result == {"processed": 1}
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert len(log["opened"]) == 1
    assert "Website tab" in log["posted"][0][1]


def test_intake_pass_does_not_touch_product_echo_when_scoped_to_portal():
    """The two pipelines are independent -- scoping a pass to product='portal' must
    never also pick up an unrelated product='echo' ticket sitting in the same table."""
    bus = FakeBus([_ticket(product="echo")])
    log, open_dm, post = _calls()
    _, notice = _notices()
    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, product="portal",
                           identity_name="scout", **_client_deps())
    assert result == {"processed": 0}
    assert bus.tickets["t-1"]["status"] == "new"  # untouched


def test_intake_pass_still_defaults_to_echo_when_called_with_no_product_override():
    """Backward compatibility: every existing Echo call site (and every test above
    this one in the file) must keep working unchanged after generalization."""
    bus = FakeBus([_ticket(product="echo", raw_text="is it broken?")])
    log, open_dm, post = _calls()
    _, notice = _notices()
    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, **_client_deps())
    assert result == {"processed": 1}


def test_fixed_pass_routes_product_portal_to_scout_identity(_wired):
    bus = FakeBus([_ticket(product="portal", status="fixing", slack_user_id="U_CLIENT",
                          verification_after=_verdict(fix_pr_url="https://github.com/x/y/pull/2"))])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post,
                         product="portal", identity_name="scout")
    assert result == {"notified": 0}
    assert bus.tickets["t-1"]["status"] == "fixing"
    assert log["posted"] == []


# ---- completion receipt stamp (portal migration 0381 compatibility) -------------------

def _marks_capture(bus):
    marks = []

    def mark_message(message_id, delivery_status, slack_ts=None, meta_update=None):
        marks.append({"id": message_id, "status": delivery_status,
                      "meta_update": meta_update})
        return bus.mark_message(message_id, delivery_status, slack_ts, meta_update)

    return marks, mark_message


def test_delivered_answer_stamps_resolve_notice_with_the_current_request_version():
    """The direct grounded-answer lane resolves the ticket on delivery, so its posted
    row must carry an explicit resolve_notice bound to the ticket's current
    request_version -- what portal migration 0381's completion guard reads."""
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=5)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    marks, mark_message = _marks_capture(bus)

    def fetch_state(ticket, who):
        return {"social_status": "connected"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice, fetch_state=fetch_state,
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert len(marks) == 1 and marks[0]["status"] == "posted"
    assert marks[0]["meta_update"] == {"resolve_notice": True, "request_version": 5}


@pytest.mark.parametrize("reason", ["current_notice_disabled",
                                  "current_notice_preflight_unavailable",
                                  "current_notice_reservation_refused"])
@pytest.mark.parametrize("notice_id", ["", "reserved-notice"])
def test_intake_completion_refusal_holds_only_without_reserved_notice(monkeypatch, reason, notice_id):
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=5)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    monkeypatch.setattr(W._out, "initiate", lambda *_a, **_kw: W._out.OutreachResult(
        opened=False, reason=reason, notice_id=notice_id))

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda *_a: {"social_status": "connected"},
                  llm=lambda *_a: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())

    current = bus.tickets["t-1"]
    assert current["request_version"] == 5
    assert current["raw_text"] == "is my instagram connected?"
    assert log["opened"] == [] and log["posted"] == []
    assert current["status"] == ("verification" if notice_id else "hold")
    assert current["classification"] == (C.QUESTION if notice_id else None)
    assert current["escalated"] is (not bool(notice_id))
    assert len(bus.outbound) == (0 if notice_id else 1)
    if not notice_id:
        assert bus.outbound[0]["kind"] == A.KIND_ESCALATION
        assert reason in bus.outbound[0]["body"]
        assert not bus.outbound[0]["attachments"].get("resolve_notice")


def test_intake_completion_refusal_does_not_hold_a_newer_request(monkeypatch):
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=5)])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def refused(*_a, **_kw):
        bus.tickets["t-1"].update(request_version=6, raw_text="A newer request",
                                  status="new", classification=None)
        return W._out.OutreachResult(opened=False, reason="current_notice_reservation_refused")

    monkeypatch.setattr(W._out, "initiate", refused)
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda *_a: {"social_status": "connected"},
                  llm=lambda *_a: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    current = bus.tickets["t-1"]
    assert current["request_version"] == 6 and current["raw_text"] == "A newer request"
    assert current["status"] == "new" and current["classification"] is None
    assert current["escalated"] is False
    assert bus.outbound == [] and log["posted"] == []


@pytest.mark.parametrize("reason", ["current_notice_disabled",
                                  "current_notice_preflight_unavailable",
                                  "current_notice_reservation_refused",
                                  "current_notice_preflight_failed",
                                  "current_notice_reservation_failed"])
def test_real_outreach_pre_send_refusal_reaches_intake_hold_queue(monkeypatch, reason):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED",
                       str(reason != "current_notice_disabled"))
    monkeypatch.setenv("AGENT_SLACK_BOT_USER_ID", "U_ECHO")
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=5)])
    reservations = []
    def begin(*_a, **_kw):
        reservations.append("refused")
        if reason == "current_notice_reservation_failed":
            raise TimeoutError("reservation response lost")

    def member_check(*_a):
        if reason == "current_notice_preflight_failed":
            raise RuntimeError("membership lookup unavailable")
        return True

    monkeypatch.setattr(bus, "begin_current_notice", begin, raising=False)
    log, open_dm, post = _calls()
    _, notice = _notices()
    result = W.intake_pass(
        bus, open_group_dm=open_dm, post_first_message=post, write_hold_notice=notice,
        fetch_state=lambda *_a: {"social_status": "connected"},
        llm=lambda *_a: "Yes, your Instagram is connected right now.",
        mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
        claim_message=bus.claim_message, member_check=member_check,
        readback=(None if reason == "current_notice_preflight_unavailable" else
                  lambda *_a: pytest.fail("no reserved message to read back")),
        **_client_deps())

    assert result == {"processed": 1}
    current = bus.tickets["t-1"]
    assert current["status"] == "hold" and current["escalated"] is True
    assert current["classification"] is None and current["request_version"] == 5
    assert current["raw_text"] == "is my instagram connected?"
    assert reservations == (["refused"] if reason in (
        "current_notice_reservation_refused", "current_notice_reservation_failed") else [])
    assert len(bus.outbound) == 1 and bus.outbound[0]["kind"] == A.KIND_ESCALATION
    assert reason in bus.outbound[0]["body"] and log["posted"] == []
    assert len(log["opened"]) == (0 if reason == "current_notice_disabled" else 1)


@pytest.mark.parametrize("failure", ["membership", "reservation"])
def test_real_preflight_exception_preserves_a_concurrent_new_request(monkeypatch, failure):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", "true")
    monkeypatch.setenv("AGENT_SLACK_BOT_USER_ID", "U_ECHO")
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=5)])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def failed_preflight(*_a, **_kw):
        bus.tickets["t-1"].update(request_version=6, raw_text="A newer request",
                                  status="new", classification=None)
        raise TimeoutError("preflight unavailable")

    monkeypatch.setattr(bus, "begin_current_notice",
                        failed_preflight if failure == "reservation" else
                        lambda *_a, **_kw: pytest.fail("membership failed before reservation"),
                        raising=False)
    W.intake_pass(
        bus, open_group_dm=open_dm, post_first_message=post, write_hold_notice=notice,
        fetch_state=lambda *_a: {"social_status": "connected"},
        llm=lambda *_a: "Yes, your Instagram is connected right now.",
        mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
        claim_message=bus.claim_message,
        member_check=failed_preflight if failure == "membership" else lambda *_a: True,
        readback=lambda *_a: pytest.fail("preflight failed before Slack"),
        **_client_deps())

    current = bus.tickets["t-1"]
    assert current["request_version"] == 6 and current["raw_text"] == "A newer request"
    assert current["status"] == "new" and current["classification"] is None
    assert current["escalated"] is False
    assert bus.outbound == [] and log["posted"] == []


def test_delivered_answer_without_a_request_version_refuses_to_send():
    """A request without a trustworthy version cannot be closed safely."""
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=None)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    marks, mark_message = _marks_capture(bus)

    def fetch_state(ticket, who):
        return {"social_status": "connected"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice, fetch_state=fetch_state,
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["status"] == "new"
    assert marks == []
    assert log["posted"] == []


def test_answer_promising_human_follow_up_is_never_stamped_as_a_completion():
    """A follow-up promise keeps the ticket OPEN (routed to the FIXER), so the posted
    answer must not read as a completion to the portal guard."""
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    marks, mark_message = _marks_capture(bus)

    def fetch_state(ticket, who):
        return {"social_status": "connected"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice, fetch_state=fetch_state,
                  llm=lambda s, u: ("It looks connected. I will flag this for a "
                                    "teammate to double-check the queue."),
                  mark_message=mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    # The DM row was posted (mark captured) but carries NO completion stamp.
    assert len(marks) == 1 and marks[0]["status"] == "posted"
    assert marks[0]["meta_update"] is None


def test_direct_first_person_follow_up_keeps_ticket_open():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "It looks connected. I will follow up tomorrow.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    assert bus.outbound[0]["attachments"].get("resolve_notice") is None


def test_requester_reply_during_post_cannot_resolve_old_cycle():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, _post = _calls()
    _, notice = _notices()

    def post(channel, body):
        bus.tickets["t-1"]["request_version"] = 3
        bus.tickets["t-1"]["raw_text"] = "Actually, a different question"
        return {"ok": True, "ts": "9999.1"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["request_version"] == 3
    assert bus.tickets["t-1"]["status"] == "verification"
    assert bus.outbound[0]["delivery_request_version"] == 2


def test_new_request_during_answer_generation_stays_in_new_queue():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        bus.tickets["t-1"]["request_version"] = 3
        bus.tickets["t-1"]["raw_text"] = "Please answer my newer request"
        return {"social_status": "connected"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice, fetch_state=fetch_state,
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["request_version"] == 3
    assert bus.tickets["t-1"]["status"] == "new"
    assert bus.tickets["t-1"]["classification"] is None
    assert bus.outbound == []
    assert log["posted"] == []


def test_new_request_before_initial_owner_stamp_is_not_claimed():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    original_patch = bus.patch_ticket_if_current

    def racing_patch(expected, **fields):
        if "bot_identity" in fields:
            bus.tickets["t-1"]["request_version"] = 3
            bus.tickets["t-1"]["raw_text"] = "Newer request"
        return original_patch(expected, **fields)

    bus.patch_ticket_if_current = racing_patch
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, connected.", **_client_deps())
    assert bus.tickets["t-1"]["request_version"] == 3
    assert bus.tickets["t-1"]["status"] == "new"
    assert bus.tickets["t-1"].get("bot_identity") is None
    assert bus.outbound == []


def test_mark_failure_after_slack_post_never_resolves():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def failing_mark(*args, **kwargs):
        raise RuntimeError("database unavailable")

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=failing_mark, claim_message=bus.claim_message,
                  stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert log["posted"]
    assert bus.tickets["t-1"]["status"] == "verification"
    assert bus.outbound[0]["delivery_status"] == "held"
    assert bus.outbound[0]["attachments"]["outreach_delivery_uncertain"] is True


def test_mark_response_lost_after_committed_post_preserves_receipt_and_resolution():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def committed_then_response_lost(*args, **kwargs):
        bus.mark_message(*args, **kwargs)
        raise RuntimeError("response lost")

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=committed_then_response_lost,
                  claim_message=bus.claim_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.outbound[0]["delivery_status"] == "posted"
    assert bus.outbound[0]["attachments"]["resolve_notice"] is True
    assert bus.tickets["t-1"]["status"] == "resolved"


@pytest.mark.parametrize("changed_field,new_value", [
    ("slack_user_id", "U_DIFFERENT"),
    ("bot_identity", "scout"),
])
def test_identity_change_before_send_refuses_client_message(changed_field, new_value):
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        bus.tickets["t-1"][changed_field] = new_value
        return {"social_status": "connected"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice, fetch_state=fetch_state,
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert log["posted"] == []
    assert bus.outbound == []
    assert bus.tickets["t-1"]["status"] == "new"


def test_new_request_before_outbound_insert_cannot_inherit_its_version():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    original_insert = bus.record_outbound

    def racing_insert(**kwargs):
        bus.tickets["t-1"]["request_version"] = 3
        bus.tickets["t-1"]["raw_text"] = "Actually, I have a new question"
        return original_insert(**kwargs)

    bus.record_outbound = racing_insert
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["request_version"] == 3
    assert bus.outbound == []
    assert log["posted"] == []
    assert bus.tickets["t-1"]["status"] == "verification"


def test_recipient_change_before_outbound_insert_is_rejected():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    original_insert = bus.record_outbound

    def racing_insert(**kwargs):
        bus.tickets["t-1"]["slack_user_id"] = "U_DIFFERENT"
        return original_insert(**kwargs)

    bus.record_outbound = racing_insert
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.outbound == []
    assert log["posted"] == []


def test_recipient_change_after_insert_is_suppressed_before_post():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    original_insert = bus.record_outbound

    def racing_insert(**kwargs):
        row = original_insert(**kwargs)
        bus.tickets["t-1"]["slack_user_id"] = "U_DIFFERENT"
        return row

    bus.record_outbound = racing_insert
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.outbound[0]["delivery_status"] == "suppressed"
    assert log["posted"] == []


def test_recipient_change_during_slack_post_is_not_restored_or_resolved():
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, open_dm, _post = _calls()
    _, notice = _notices()

    def post(channel, body):
        bus.tickets["t-1"]["slack_user_id"] = "U_DIFFERENT"
        return {"ok": True, "ts": "9999.1"}

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert bus.tickets["t-1"]["slack_user_id"] == "U_DIFFERENT"
    assert bus.tickets["t-1"].get("slack_channel_id") is None
    assert bus.tickets["t-1"]["status"] == "verification"
    assert bus.outbound[0]["delivery_status"] == "held"
    assert bus.outbound[0]["attachments"].get("resolve_notice") is None


@pytest.mark.parametrize("reason", ["claim_failed", "lost_claim", "delivery_identity_changed"])
@pytest.mark.parametrize("alert_state", ["confirmed", "retry", "missing", "error", "unsuppressed"])
def test_current_attempt_refusal_uses_one_confirmed_staff_alert(monkeypatch, reason, alert_state):
    """Skip fallback only when the exact attempt has a durable suppression alert."""
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=5)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    alerts = []
    alert_checks = []

    def ensure(mid, identity):
        alert_checks.append((mid, identity))
        if alert_state == "error":
            raise RuntimeError("alert unavailable")
        if alert_state in {"missing", "unsuppressed"}:
            return None
        if not alerts:
            alerts.append(bus.record_outbound(
                ticket_id="t-1", author_type="system", body="Canceled before Slack delivery",
                delivery_status="ready", kind=A.KIND_ESCALATION,
                meta={"identity": identity, "suppressed_message_id": mid}))
        return alerts[0]

    def refused(*_a, **kw):
        # Match outreach's reserved INSERT contract. The three legacy refusal
        # reasons deliberately carry no notice_id on OutreachResult.
        mid = "exact-attempt"
        record = bus.record_outbound
        def inserted(**args):
            row = record(**args)
            if args.get("message_id") == mid:
                row["id"] = mid
            return row
        monkeypatch.setattr(bus, "record_outbound", inserted)
        row = kw["record_outbound"](
            ticket_id="t-1", author_type="echo", body="Your current answer",
            delivery_status="ready", kind="status", message_id=mid,
            meta={"fixer_current_attempt_token": "attempt-token"})
        row["delivery_status"] = "ready" if alert_state == "unsuppressed" else "suppressed"
        if alert_state == "confirmed":
            ensure(mid, "echo")  # already emitted by outreach
        return W._out.OutreachResult(opened=True, reason=reason)

    monkeypatch.setattr(bus, "ensure_suppressed_current_notice_alert", ensure, raising=False)
    monkeypatch.setattr(W._out, "initiate", refused)
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda *_a: {"social_status": "connected"},
                  llm=lambda *_a: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    assert alert_checks and all(item == ("exact-attempt", "echo") for item in alert_checks)
    assert len([row for row in bus.outbound if row["kind"] == A.KIND_ESCALATION]) == 1
    confirmed = alert_state in {"confirmed", "retry"}
    assert bus.tickets["t-1"]["status"] == ("verification" if confirmed else "hold")
    assert bus.tickets["t-1"]["escalated"] is (not confirmed)
    assert bus.tickets["t-1"]["request_version"] == 5
    assert log["posted"] == []


@pytest.mark.parametrize("reason", ["claim_failed", "lost_claim"])
@pytest.mark.parametrize("first_alert_fails", [False, True])
def test_real_outreach_claim_refusal_does_not_double_alert(monkeypatch, reason, first_alert_fails):
    monkeypatch.setenv("SLACK_CONVO_ECHO_CURRENT_NOTICE_ENABLED", "true")
    monkeypatch.setenv("AGENT_SLACK_BOT_USER_ID", "U_ECHO")
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=5)])
    log, open_dm, post = _calls()
    _, notice = _notices()
    record = bus.record_outbound
    alerts = []
    checks = []

    def inserted(**kwargs):
        row = record(**kwargs)
        if kwargs.get("message_id"):
            row["id"] = kwargs["message_id"]
        return row

    def claim(mid):
        if reason == "claim_failed":
            raise RuntimeError("claim unavailable")
        return False

    def suppress(mid, why):
        row = next(row for row in bus.outbound if row["id"] == mid)
        assert row["delivery_status"] == "ready"
        row["delivery_status"] = "suppressed"
        row["attachments"]["suppressed_why"] = why
        return dict(row)

    def ensure(mid, identity):
        row = next(row for row in bus.outbound if row["id"] == mid)
        assert row["delivery_status"] == "suppressed"
        assert row["attachments"]["fixer_current_attempt_token"]
        checks.append(mid)
        if first_alert_fails and len(checks) == 1:
            raise RuntimeError("first alert write unavailable")
        if not alerts:
            alerts.append(bus.record_outbound(
                ticket_id="t-1", author_type="system", body="Canceled before Slack delivery",
                delivery_status="ready", kind=A.KIND_ESCALATION,
                meta={"identity": identity, "suppressed_message_id": mid}))
        return alerts[0]

    monkeypatch.setattr(bus, "record_outbound", inserted)
    monkeypatch.setattr(bus, "begin_current_notice", lambda *_a, **_kw:
                        "0b9c3b7a-4077-4a45-82c7-d351d766beef", raising=False)
    monkeypatch.setattr(bus, "suppress_unclaimed_current_notice", suppress, raising=False)
    monkeypatch.setattr(bus, "ensure_suppressed_current_notice_alert", ensure, raising=False)
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda *_a: {"social_status": "connected"},
                  llm=lambda *_a: "Yes, your Instagram is connected right now.",
                  mark_message=bus.mark_message, stamp_ticket=bus.stamp_ticket,
                  claim_message=claim, member_check=lambda *_a: True,
                  readback=lambda *_a: pytest.fail("claim refusal cannot read back Slack"),
                  **_client_deps())
    assert len(checks) == 2 and checks[0] == checks[1]
    assert len(bus.outbound) == 2 and len(alerts) == 1
    assert bus.tickets["t-1"]["status"] == "verification"
    assert bus.tickets["t-1"]["escalated"] is False
    assert log["posted"] == []


# ---- completion gate (2026-10-07 live incident): an answer that asks the client for
# missing facts or an action must NOT resolve on delivery --------------------------------

def _run_answer(bus, answer_body):
    log, open_dm, post = _calls()
    _, notice = _notices()
    marks, mark_message = _marks_capture(bus)
    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                  write_hold_notice=notice,
                  fetch_state=lambda ticket, who: {"social_status": "connected"},
                  llm=lambda s, u: answer_body,
                  mark_message=mark_message, stamp_ticket=bus.stamp_ticket,
                  **_client_deps())
    return log, marks


@pytest.mark.parametrize("answer_body", [
    # asks for screenshots
    "I can't see the error yet. Can you send us a screenshot of the Instagram login screen?",
    # asks for post links
    "Which post should I update? Please share the link to the post that looks wrong.",
    # asks for dates
    "Could you confirm the date the schedule stopped publishing? "
    "Would you also tell us the timeframe you expected?",
    # asks for connection / action details
    "To check the connection, please provide the account name and grant access, "
    "or forward the login details.",
    "Please share the error message you see when connecting.",
    "Please reply with the URL of the post that failed.",
    "Please take a screenshot of the error you see.",
    "Could you capture a screenshot of the page?",
])
def test_answer_asking_client_for_details_stays_open_after_delivery(answer_body):
    """Synthetic reproduction of the 2026-10-07 incident: an answer body that asks
    the client for screenshots, post links, dates or connection/action details must
    keep the ticket OPEN after the response is delivered -- never resolve on
    delivery -- and route to staff follow-up."""
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    log, marks = _run_answer(bus, answer_body)
    # The answer WAS delivered to the client's DM ...
    assert len(log["posted"]) == 1
    assert len(marks) == 1 and marks[0]["status"] == "posted"
    assert marks[0]["meta_update"] is None, "needs-more-info answer is not a completion"
    assert bus.outbound[0]["attachments"].get("resolve_notice") is None
    # ... but the ticket stays open and escalated for staff follow-up.
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    hold = (bus.tickets["t-1"].get("verification_after") or {}).get("hold") or {}
    assert hold.get("reason") == W.CLIENT_DETAILS_MARKER
    assert hold.get("reason") != A.FOLLOW_UP_MARKER
    cards = [r for r in bus.outbound if r["kind"] == A.KIND_ESCALATION
             and r["meta"].get("client_details_requested")]
    assert len(cards) == 1
    assert cards[0]["delivery_status"] == "ready"
    assert "investigate internally; keep ticket open" in cards[0]["body"]


def test_answer_matching_both_patterns_is_carded_exactly_once():
    """An answer that both promises a human follow-up AND asks for details routes
    through the single shared path -- one card, one stamp, no duplicate outreach."""
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=2)])
    body = ("I will follow up once we know more. Can you send us a screenshot of "
            "the error screen?")
    log, marks = _run_answer(bus, body)
    assert len(log["posted"]) == 1
    assert marks[0]["meta_update"] is None
    assert bus.tickets["t-1"]["status"] == "hold"
    follow_up_rows = [r for r in bus.outbound
                      if r["kind"] == A.KIND_ESCALATION
                      and r["body"].startswith("FOLLOW-UP PROMISED")]
    assert len(follow_up_rows) == 1
    assert bus.tickets["t-1"]["verification_after"]["hold"]["reason"] == A.FOLLOW_UP_MARKER


def test_complete_grounded_answer_still_resolves_on_delivery():
    """The gate is conservative: a genuinely complete, grounded answer still
    closes normally after verified delivery."""
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=3)])
    log, marks = _run_answer(
        bus, "Yes, your Instagram is connected right now. Your last post went out "
             "on schedule and nothing is queued.")
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert len(log["posted"]) == 1
    assert marks[0]["meta_update"] == {"resolve_notice": True, "request_version": 3}
    # No staff follow-up card (the delivery RECEIPT shares KIND_ESCALATION).
    follow_up_rows = [r for r in bus.outbound
                      if r["kind"] == A.KIND_ESCALATION
                      and r["body"].startswith("FOLLOW-UP PROMISED")]
    assert follow_up_rows == []


@pytest.mark.parametrize("answer_body", [
    "Your error message is Instagram's temporary connection warning.",
    "The account name is LASSO Fitness and the latest post went out today.",
    "We received the screenshot and link, and confirmed the schedule is active.",
])
def test_grounded_answers_mention_requested_objects_still_resolve(answer_body):
    bus = FakeBus([_ticket(raw_text="is my instagram connected?", request_version=3)])
    _, marks = _run_answer(bus, answer_body)
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert marks[0]["meta_update"] == {"resolve_notice": True, "request_version": 3}


def test_common_request_phrasings_are_recognized_without_needing_live_delivery():
    assert W._needs_more_information("Let me know which account you are using.")
