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

    def find_new_tickets(self, *, product, source, limit=20):
        return [t for t in self.tickets.values()
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
        return ({"id": f"in-{len(self.inbound)}"}, False)

    def inbound_count(self, ticket_id):
        return len([m for m in self.inbound if m.get("ticket_id") == ticket_id])

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
    bus = FakeBus([_ticket(status="fixing", verification_after={"fix_pr_url": "x"})])
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
    bus.raise_on_inbound = {"t-bad"}
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        return {"social_status": "connected"}

    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, fetch_state=fetch_state,
                           llm=lambda s, u: "Yes, connected.", **_client_deps())
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
                           **_client_deps())
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

    W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                 write_hold_notice=notice, fetch_state=fetch_state,
                 llm=lambda s, u: "irrelevant", **_client_deps())
    assert bus.tickets["t-1"]["status"] == "hold"
    assert bus.tickets["t-1"]["escalated"] is True
    assert log["opened"] == []


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
    bus = FakeBus([_ticket(product="portal", raw_text="where do I add a new coach?")])
    log, open_dm, post = _calls()
    _, notice = _notices()

    def fetch_state(ticket, who):
        return {"portal_status": "ok"}

    def llm(system, user):
        return "You can add it from the Website tab, under Content."

    result = W.intake_pass(bus, open_group_dm=open_dm, post_first_message=post,
                           write_hold_notice=notice, product="portal",
                           identity_name="scout", fetch_state=fetch_state, llm=llm,
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


def test_fixed_pass_routes_product_portal_to_scout_identity():
    bus = FakeBus([_ticket(product="portal", status="fixing", slack_user_id="U_CLIENT",
                          verification_after={"fix_pr_url": "https://github.com/x/y/pull/2"})])
    log, open_dm, post = _calls()
    result = W.fixed_pass(bus, open_group_dm=open_dm, post_first_message=post,
                         product="portal", identity_name="scout")
    assert result == {"notified": 1}
    assert bus.tickets["t-1"]["status"] == "resolved"
    assert "Fixed it" in log["posted"][0][1]
