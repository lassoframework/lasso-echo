"""The lane: hard limits, routing asymmetry, delivery, cards, and the pinned
contracts of the modules this one talks to.

The two real cases this capability exists for are at the bottom, end to end.
"""
import pytest

from agent.client_dm_support import arming as A
from agent.client_dm_support import conditions as C
from agent.client_dm_support import lane as L
from agent.client_dm_support import no_ad_rail as N
from agent.client_dm_support import probes as P
from tests.test_client_dm_no_fakes import FaithfulBus, _ticket


class Store:
    def __init__(self, sources=(), assets=()):
        self.sources, self.assets = list(sources), list(assets)

    def list_sources(self, gym_id=None, include_inactive=False):
        rows = [s for s in self.sources if gym_id in (None, s["gym_id"])]
        return rows if include_inactive else [s for s in rows if s["active"]]

    def list_assets(self, gym_id, source_id=None):
        return [a for a in self.assets if a["gym_id"] == gym_id]


def src(gym="crossfitlocal", sid="s1", active=True, revoked=False):
    return {"id": sid, "gym_id": gym, "kind": "gym_drive", "active": active,
            "revoked_externally": revoked, "folder_id": "F",
            "folder_name": "Ad Photos"}


def assets(n, gym="crossfitlocal", sid="s1", eligible=True):
    return [{"id": f"{gym}-{i}", "gym_id": gym, "source_id": sid,
             "eligible": eligible, "excluded_by_coach": False} for i in range(n)]


VOICE_TODO = ("### CTA rotation (cycle in order, one per post)\n"
              "> TODO: this section was missing or empty in the intake. Fill it by "
              "hand before activation.\n### Next\n")
VOICE_REAL = ("### CTA rotation (cycle in order, one per post)\n"
              "1. Book a free call. Link in bio.\n"
              "2. Send this to a friend.\n### Next\n")


# ---------------------------------------------------------------------------
# BLAKE'S HARD LIMITS.
# ---------------------------------------------------------------------------
AD_MESSAGES = [
    "can you double my ad budget?",
    "raise the daily spend on facebook",
    "pause everything we are paying for",
    "my cpl is way too high this week",
    "change the targeting to 25-45 women",
    "launch the new campaign tomorrow",
    "turn off the ads for a week",
    "boost the reel from friday",
    "what are we spending per lead",
    "add another ad set",
    # MINOR (audit of PR #68): four plain phrasings the belt used to miss entirely,
    # producing an on-topic-but-wrong reply about photos instead of escalating.
    "put more money behind the tuesday one",
    "scale us up to $50 a day",
    "promote that post to more people",
    "stop showing our ads to men over 60",
]

OTHER_HARD_LINES = [
    "can I get a refund on last month's invoice",
    "one of our members got injured, what do we do",
    "our lawyer wants to see the liability waiver",
    "what's the api token for the pixel",
    "please change our class schedule on the site",
]

# THE PHRASINGS THIS BELT PROVABLY MISSES.
#
# These three are D67's round-9 cases verbatim -- the ones that ended a nine-round
# loop, because in that design the belt WAS the last line of defence and 15 of 16
# must-hold messages posted to a client with no tap.
#
# They are recorded here rather than fixed, deliberately, and this is the single most
# important test in this file. Adding "took money out", "we open at" and "other gym"
# to ESCALATE_ALWAYS would make this list green and would be D68's exact failure mode:
# enumerating an open set, fitting to the sample, feeling like progress. The next three
# phrasings would be found by the next audit.
#
# What changed is not the belt. It is that the belt no longer decides anything. Below,
# each of these still produces NO reply -- not because a keyword matched, but because
# no probe measures a fact that answers them, so no condition matches and no sentence
# can be constructed at all. The unsafe cases stopped being cases to enumerate and
# became a structural impossibility.
BELT_MISSES = [
    "did you guys take money out twice this month?",
    "we open at 5 now, does the calendar know?",
    "can you look at the other gym's account",
]


@pytest.mark.parametrize("text", AD_MESSAGES + OTHER_HARD_LINES + BELT_MISSES)
def test_no_hard_line_message_can_ever_produce_a_reply(text):
    """THE RULE. Asserted over every hard-line message, including the ones the belt
    does not catch."""
    d = L.decide(text=text, gym_key="crossfitlocal", may_reply=True,
                 deps={"store": Store([src()], assets(0))})
    assert d.outcome == L.Outcome.ESCALATE, text
    assert d.reply_text == ""


@pytest.mark.parametrize("text", AD_MESSAGES + OTHER_HARD_LINES)
def test_the_belt_catches_these_before_anything_reads_a_fact(text):
    """THE MUTATION TARGET for the ad hard-block. Every ad-money phrasing is stopped
    unconditionally, first, before a single fact is read."""
    assert L.hard_line(text) is True
    d = L.decide(text=text, gym_key="crossfitlocal", may_reply=True,
                 deps={"store": Store([src()], assets(0))})
    assert d.reason == L.ESCALATE_ALWAYS_REASON


@pytest.mark.parametrize("text", BELT_MISSES)
def test_the_belt_misses_these_and_it_changes_nothing(text):
    """Read the BELT_MISSES comment above before touching this test."""
    assert L.hard_line(text) is False, (
        "somebody added this phrasing to ESCALATE_ALWAYS. That is the enumeration loop "
        "D68 names. The point of this test is that the belt does NOT need to catch it.")
    d = L.decide(text=text, gym_key="crossfitlocal", may_reply=True,
                 deps={"store": Store([src()], assets(0))})
    assert d.outcome == L.Outcome.ESCALATE
    assert d.reply_text == ""
    # held by the SHAPE of the capability, not by a keyword: nothing measures this.
    assert "no enumerated condition family" in d.reason


def test_a_hard_line_riding_along_with_a_routable_phrase_still_escalates():
    """Round 4's finding: the routable half must not win. This is the follow-up shape
    that matters most -- 'forget the photos, can you double my ad budget?'"""
    d = L.decide(text="forget the photos, can you double my ad budget?",
                 gym_key="crossfitlocal", may_reply=True,
                 deps={"store": Store([src()], assets(0))})
    assert d.outcome == L.Outcome.ESCALATE
    assert d.reason == L.ESCALATE_ALWAYS_REASON


def test_the_hard_line_belt_is_checked_before_the_gym_key_is_even_resolved():
    d = L.decide(text="raise the ad budget", gym_key="", may_reply=True, deps={})
    assert d.reason == L.ESCALATE_ALWAYS_REASON


def test_the_package_contains_no_ad_rail():
    assert N.assert_no_ad_rail() is True


def test_the_ad_tripwire_actually_fires(monkeypatch):
    """A two-way guard on the tripwire: a scanner that can no longer see a rail
    returns the same empty list as a repo with no rail -- D68's inert-identical-to-
    healthy shape, landing on the ad guarantee."""
    monkeypatch.setattr(N, "TRIPWIRE_LITERALS", ("gym_drive",))
    with pytest.raises(N.AdRailPresent):
        N.assert_no_ad_rail()


def test_the_tripwire_module_list_is_pinned():
    assert N.TRIPWIRE_MODULES == frozenset({"facebook_business", "pipeboard"})
    assert "/act_" in N.TRIPWIRE_LITERALS
    assert "graph.facebook.com" in N.TRIPWIRE_LITERALS


def test_the_package_imports_no_ad_module_transitively():
    """The real argument, not the tripwire: nothing in this package's own imports
    reaches an ads surface."""
    import ast
    import os
    pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "agent", "client_dm_support")
    imported = set()
    for fname in sorted(os.listdir(pkg)):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(pkg, fname), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=fname)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
                imported.update(a.name for a in node.names)
    for banned in ("meta_ads", "pipeboard", "facebook_business", "marketing_api",
                   "ad_writer", "campaigns"):
        assert banned not in imported, f"{banned} is now importable from this package"


# ---------------------------------------------------------------------------
# THE ROUTER, WHICH IS NOT A SAFETY GATE.
# ---------------------------------------------------------------------------
def test_a_routable_message_picks_exactly_one_family():
    assert L.route("the posts have no photos") == P.PROBE_DRIVE
    assert L.route("we need a real call to action") == P.PROBE_CTA


def test_a_message_matching_two_families_escalates_rather_than_picking_one():
    assert L.route("the photos have no cta on them") is None


def test_an_unroutable_message_escalates():
    d = L.decide(text="hey how's it going", gym_key="crossfitlocal", may_reply=True,
                 deps={"store": Store([src()], assets(0))})
    assert d.outcome == L.Outcome.ESCALATE
    assert "no enumerated condition family" in d.reason


def test_routing_to_the_WRONG_family_still_cannot_produce_a_reply():
    """The asymmetry the router's safety rests on: a misroute means that probe's
    readings match no condition, so it escalates. Safety is downstream of the router,
    in what the answer is derived from."""
    d = L.decide(text="anything", gym_key="crossfitlocal", may_reply=True,
                 deps={"probe_id": P.PROBE_CTA, "voice_dir": "/v",
                       "read_text": lambda p: VOICE_REAL})
    assert d.outcome == L.Outcome.ESCALATE
    assert "does not match any enumerated condition" in d.reason


# ---------------------------------------------------------------------------
# THE JOIN KEY.
# ---------------------------------------------------------------------------
def test_a_portal_uuid_is_refused_rather_than_queried_with():
    d = L.decide(text="no photos", gym_key="43f2707f-6c25-4b18-ae12-bb3abd48907c",
                 may_reply=True, deps={})
    assert d.outcome == L.Outcome.ESCALATE
    assert "portal gym uuid" in d.reason


def test_a_leaky_store_cannot_make_this_gym_sync_a_rival_gyms_folder():
    """Round 2's actual repro, and the reason the action re-asserts the tenant at the
    WRITE path and not only at the read path: a store whose gym filter is broken (a
    docstring lie, a dropped param, a cached client) returns another gym's source, and
    everything downstream trusts it. A first mutation run found this check unheld
    because every other fixture's store filters correctly."""
    class Leaky(Store):
        def list_sources(self, gym_id=None, include_inactive=False):
            return [src("SOMEBODY_ELSE", "rival")]          # ignores gym_id entirely

    ran = []
    d = L.decide(text="my posts have no photos", gym_key="crossfitlocal",
                 may_reply=True,
                 deps={"store": Leaky(),
                       "sync_source": lambda s, **kw: ran.append(s["id"]) or
                       {"ok": True, "inserted": 9}})
    assert ran == [], "this lane ran a sync on another gym's Drive folder"
    assert d.outcome == L.Outcome.ESCALATE
    assert "SOMEBODY_ELSE" in d.reason and "not 'crossfitlocal'" in d.reason


def test_an_unresolvable_gym_key_escalates_rather_than_guessing():
    d = L.decide(text="no photos", gym_key="", may_reply=True, deps={})
    assert d.outcome == L.Outcome.ESCALATE
    assert "account key" in d.reason


def test_the_gym_key_comes_from_the_repos_anti_divergence_primitive(monkeypatch):
    from agent import account_key_resolve as _akr
    seen = []
    monkeypatch.setattr(_akr, "portal_key_for_gym",
                        lambda gid, **kw: seen.append(gid) or "crossfitlocal")
    assert L._gym_key_for({"client_id": "uuid-1"}) == "crossfitlocal"  # noqa: SLF001
    assert seen == ["uuid-1"]


# ---------------------------------------------------------------------------
# ESCALATE-ONLY MODE COMPOSES NOTHING.
# ---------------------------------------------------------------------------
def test_with_may_reply_false_no_client_text_is_ever_produced():
    store = Store([src()], assets(0))

    def sync(source, **kw):
        store.assets.extend(assets(5))
        return {"ok": True, "inserted": 5}

    d = L.decide(text="my posts have no photos", gym_key="crossfitlocal",
                 may_reply=False, deps={"store": store, "sync_source": sync})
    assert d.outcome == L.Outcome.ESCALATE
    assert d.reply_text == ""
    assert "not armed to reply" in d.reason
    # the fix still ran and was still verified -- escalate-only is not "do nothing"
    assert d.audit["verification"].startswith("drive_library_usable rose")


# ---------------------------------------------------------------------------
# DELIVERY AND THE CARD.
# ---------------------------------------------------------------------------
def live_arm():
    return A.preflight("echo", env={A.ENV_MASTER: "true", A.ENV_CLIENT_REPLY: "true",
                                    A.ENV_LIVE_ACK: A.required_ack(
                                        "echo", client_reply_armed=True)},
                       client_reply_armed=True)


class Ident:
    name = "echo"
    product = "echo"


def test_every_reply_also_writes_a_human_card():
    """Round 4's conclusion, and it is the reason the keyword belt is never the thing
    standing between a client and a wrong outcome: a human sees every message this
    lane touches, either way."""
    bus = FaithfulBus([_ticket("1")])
    d = L.Decision(L.Outcome.REPLY, "ok", gym_key="crossfitlocal",
                   condition_id="drive_library_empty",
                   reply_text="I ran your photo sync just now.",
                   client_text="no photos and also please raise my budget")
    wrote = L._deliver(bus, _ticket("1"), Ident(), d, live_arm(), "mpim")  # noqa: SLF001
    assert wrote == {"reply": True, "card": True, "undelivered": 0}
    kinds = [w["kind"] for w in bus.written]
    assert kinds == ["status", "escalation"]
    card = bus.written[1]["body"]
    assert "no photos and also please raise my budget" in card
    assert "Nothing else in their message was read" in card


def test_every_written_row_carries_the_identity_stamp():
    """outbox._dispatch_one suppresses any row with no identity stamp (outbox.py:325),
    so a row without it lands in the DB and reaches nobody."""
    bus = FaithfulBus([_ticket("1")])
    d = L.Decision(L.Outcome.REPLY, "ok", reply_text="x", client_text="y")
    L._deliver(bus, _ticket("1"), Ident(), d, live_arm(), "mpim")  # noqa: SLF001
    for w in bus.written:
        assert w["meta"]["identity"] == "echo"
        assert w["meta"][L.LANE_META] == L.LANE_NAME


def test_a_reply_row_is_written_ready_and_marks_the_recipient_as_a_client():
    bus = FaithfulBus([_ticket("1")])
    d = L.Decision(L.Outcome.REPLY, "ok", reply_text="x", client_text="y")
    L._deliver(bus, _ticket("1"), Ident(), d, live_arm(), "mpim")  # noqa: SLF001
    reply = bus.written[0]
    assert reply["delivery_status"] == L.DELIVERY_READY
    assert reply["meta"]["recipient_kind"] == "client"
    assert reply["meta"]["surface"] == "mpim"


def test_the_card_says_QUEUED_not_SENT():
    """Round 5's finding: with the outbox's own flag off, the reply is HELD at post
    time and the client gets nothing, so a card saying 'I auto-replied' was false on
    100% of replies."""
    bus = FaithfulBus([_ticket("1")])
    d = L.Decision(L.Outcome.REPLY, "ok", reply_text="x", client_text="y")
    L._deliver(bus, _ticket("1"), Ident(), d, live_arm(), "mpim")  # noqa: SLF001
    card = bus.written[1]["body"]
    assert "QUEUED" in card and "the receipt is the record" in card
    assert "I auto-replied" not in card


def test_an_undelivered_reply_is_reported_loudly_not_swallowed():
    class Broken(FaithfulBus):
        def record_outbound(self, **kw):
            if kw["kind"] == "status":
                raise RuntimeError("insert failed")
            return super().record_outbound(**kw)

    bus = Broken([_ticket("1")])
    d = L.Decision(L.Outcome.REPLY, "ok", reply_text="x", client_text="y")
    wrote = L._deliver(bus, _ticket("1"), Ident(), d, live_arm(), "mpim")  # noqa: SLF001
    assert wrote["undelivered"] == 1 and wrote["reply"] is False
    assert "Nobody has told this client anything" in bus.written[0]["body"]


def test_the_clients_own_words_are_bounded_and_escaped_on_the_card():
    """A pasted message or a Drive folder name must not forge structure in a card a
    human reads."""
    bus = FaithfulBus([_ticket("1")])
    d = L.Decision(L.Outcome.ESCALATE, "why",
                   client_text="<!channel> ```x``` " + "z" * 50_000)
    L._deliver(bus, _ticket("1"), Ident(), d, live_arm(), "mpim")  # noqa: SLF001
    card = bus.written[0]["body"]
    assert "<!channel>" not in card and "&lt;!channel&gt;" in card
    assert "```" not in card
    # bounded, not merely escaped: a card body must stay readable and must fit the
    # 8000-char column bus.record_outbound truncates at.
    assert card.count("z") <= L._fenced.__defaults__[0]                # noqa: SLF001
    assert len(card) < 1500


def test_the_fence_caps_at_its_declared_length():
    assert len(L._fenced("q" * 50_000)) < 700                          # noqa: SLF001
    assert L._fenced("q" * 50_000).endswith(" ...")                    # noqa: SLF001


def test_the_arming_verdict_is_re_checked_at_delivery_not_only_at_decision():
    """Belt and braces, and the belt must actually hold: even handed a Decision that
    carries reply text, _deliver must write NO client row unless the arming verdict
    permits it. A first mutation run found this branch could be deleted with nothing
    going red, because decide() already refuses to compose when unarmed."""
    d = L.Decision(L.Outcome.REPLY, "ok", condition_id="drive_library_empty",
                   reply_text="I ran your photo sync just now.", client_text="hi")
    for env in ({A.ENV_MASTER: "true"},
                {A.ENV_MASTER: "true", A.ENV_CLIENT_REPLY: "true"}):
        bus = FaithfulBus([_ticket("1")])
        arm = A.preflight("echo", env=env, client_reply_armed=True)
        wrote = L._deliver(bus, _ticket("1"), Ident(), d, arm, "mpim")  # noqa: SLF001
        assert wrote["reply"] is False, env
        assert [w["kind"] for w in bus.written] == ["escalation"], env


def test_an_already_handled_ticket_is_never_answered_twice():
    bus = FaithfulBus([_ticket("1")])
    assert L._actionable(bus, _ticket("1")) is True         # noqa: SLF001
    d = L.Decision(L.Outcome.REPLY, "ok", reply_text="x", client_text="y")
    L._deliver(bus, _ticket("1"), Ident(), d, live_arm(), "mpim")  # noqa: SLF001
    assert L._actionable(bus, _ticket("1")) is False        # noqa: SLF001


def test_the_lane_reads_the_clients_NEWEST_message_not_their_oldest():
    """bus.recent_messages orders created_at DESC. The previous build read it forwards
    and therefore never saw a follow-up -- including the ad-budget one."""
    bus = FaithfulBus([_ticket("1")], messages={"1": [
        {"direction": "inbound", "author_type": "client", "created_at": "2026-09-01",
         "body": "my posts have no photos", "attachments": {"surface": "mpim"}},
        {"direction": "inbound", "author_type": "client", "created_at": "2026-09-05",
         "body": "forget the photos, double my ad budget",
         "attachments": {"surface": "mpim"}},
    ]})
    text, surface = L._newest_client_message(bus, _ticket("1"))   # noqa: SLF001
    assert "double my ad budget" in text
    assert surface == "mpim"


# ---------------------------------------------------------------------------
# THE PASS.
# ---------------------------------------------------------------------------
def test_run_once_with_the_master_off_reads_nothing_and_says_so(monkeypatch):
    monkeypatch.delenv(A.ENV_MASTER, raising=False)
    out = L.run_once(bus=FaithfulBus([_ticket("1")]), identity=Ident())
    assert out["mode"] == A.MODE_OFF and out["handled"] == 0
    assert A.ENV_MASTER in out["reason"]


def test_run_once_in_escalate_only_cards_every_ticket_and_replies_to_none(monkeypatch):
    monkeypatch.setenv(A.ENV_MASTER, "true")
    monkeypatch.delenv(A.ENV_CLIENT_REPLY, raising=False)
    monkeypatch.delenv(A.ENV_LIVE_ACK, raising=False)
    monkeypatch.setattr(L, "_gym_key_for", lambda t: "crossfitlocal")
    bus = FaithfulBus([_ticket("1", raw_text="my posts have no photos")])
    store = Store([src()], assets(0))
    out = L.run_once(bus=bus, identity=Ident(), deps={"store": store})
    assert out["mode"] == A.MODE_ESCALATE_ONLY
    assert out["handled"] == 1 and out["replies"] == 0 and out["cards"] == 1
    assert [w["kind"] for w in bus.written] == ["escalation"]


def test_run_once_refuses_to_run_at_all_if_a_boot_check_fails(monkeypatch):
    monkeypatch.setenv(A.ENV_MASTER, "true")
    monkeypatch.setattr(N, "TRIPWIRE_LITERALS", ("gym_drive",))
    out = L.run_once(bus=FaithfulBus([]), identity=Ident())
    assert out["ok"] is False and "boot check failed" in out["reason"]
    assert out["handled"] == 0


def test_one_exploding_ticket_never_sinks_the_pass(monkeypatch):
    monkeypatch.setenv(A.ENV_MASTER, "true")
    monkeypatch.setattr(L, "_gym_key_for", lambda t: "crossfitlocal")

    class Boom(Store):
        def list_sources(self, **kw):
            raise RuntimeError("supabase 503")

    bus = FaithfulBus([_ticket("1", raw_text="no photos"),
                       _ticket("2", raw_text="no photos")])
    out = L.run_once(bus=bus, identity=Ident(), deps={"store": Boom()})
    assert out["handled"] == 2 and out["cards"] == 2 and out["ok"] is True


# ---------------------------------------------------------------------------
# GAP 1 (audit of PR #68): the poll must not be scoped to one identity/product.
# ---------------------------------------------------------------------------
def test_run_once_defaults_to_the_identitys_OWN_product_not_a_fixed_constant(
        monkeypatch):
    """A single run_once() call for identity=scout must poll product='scout', not
    the fixed 'echo' DEFAULT_PRODUCT -- the exact mismatch that made every non-echo
    identity's tickets invisible."""
    class Scout:
        name = "scout"
        product = "scout"

    monkeypatch.setenv(A.ENV_MASTER, "true")
    monkeypatch.setattr(L, "_gym_key_for", lambda t: "")

    bus = FaithfulBus([_ticket("1", product="scout")])
    out = L.run_once(bus=bus, identity=Scout())
    assert out["handled"] == 1, "run_once(identity=Scout()) did not poll product='scout'"


def test_run_once_all_identities_finds_tickets_a_bare_run_once_would_miss(monkeypatch):
    """THE END-TO-END PROOF for GAP 1. Every production caller used to invoke
    run_once() with no arguments at all -- identity='echo' by default. A real client
    ticket filed through Scout (per the established practice that client Slack group
    DMs land with Scout, Blake and the owner) has product='scout' and was invisible.
    run_once_all_identities must see it; the old call shape must not."""
    monkeypatch.setenv(A.ENV_MASTER, "true")
    monkeypatch.setattr(L, "_gym_key_for", lambda t: "")  # escalates; we only assert visibility

    old_shape_bus = FaithfulBus([_ticket("1", product="scout",
                                         raw_text="the posts have no photos")])
    old_shape = L.run_once(bus=old_shape_bus)  # exactly how runner.py used to call it
    assert old_shape["handled"] == 0, (
        "a bare run_once() saw a scout-product ticket; GAP 1 is not actually closed")

    bus = FaithfulBus([_ticket("1", product="scout",
                               raw_text="the posts have no photos")])
    out = L.run_once_all_identities(bus=bus)
    assert out["handled"] == 1
    assert out["identities"]["scout"]["handled"] == 1
    assert out["identities"]["echo"]["handled"] == 0
    assert set(out["identities"]) == set(L.POLL_IDENTITIES)


def test_run_once_all_identities_excludes_lainey():
    assert "lainey" not in L.POLL_IDENTITIES
    assert set(L.POLL_IDENTITIES) == {"echo", "ranger", "scout", "wrangler"}


# ---------------------------------------------------------------------------
# PINNED CONTRACTS OF THE MODULES THIS LANE TALKS TO.
# ---------------------------------------------------------------------------
def test_the_polled_statuses_and_sources_are_legal_values():
    from tests.test_db_constraint_contract import SUPPORT_TICKETS_STATUS_VALUES
    for s in L.POLL_STATUSES:
        assert s in SUPPORT_TICKETS_STATUS_VALUES
    assert set(L.POLL_STATUSES) == {"new", "triage", "hold"}
    assert set(L.POLL_SOURCES) == {"slack_conversation", "website_tab"}


def test_verification_is_deliberately_excluded_from_the_poll():
    """That status means the adapter already drafted an answer on the D67-locked lane,
    held for a tap. Replying here too would give the client two messages about one
    question."""
    assert "verification" not in L.POLL_STATUSES


def test_the_delivery_status_this_lane_writes_is_a_legal_value():
    from tests.test_db_constraint_contract import (
        SUPPORT_MESSAGES_DELIVERY_STATUS_VALUES)
    assert L.DELIVERY_READY in SUPPORT_MESSAGES_DELIVERY_STATUS_VALUES
    assert L.DELIVERY_READY == "ready"


def test_the_kinds_this_lane_writes_are_real_adapter_kinds():
    from agent.slack_convo import adapter as _a
    status, escalation = L._kinds()                            # noqa: SLF001
    assert status == _a.KIND_STATUS and status in _a.CONVERSATIONAL_KINDS
    assert escalation == _a.KIND_ESCALATION and escalation in _a.INTERNAL_KINDS


def test_this_lane_never_writes_a_kind_answer_row():
    """KIND_ANSWER routes through the D67-locked auto-answer gate. This lane must not
    reach into that decision in either direction."""
    import os
    pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "agent", "client_dm_support")
    for fname in os.listdir(pkg):
        if fname.endswith(".py"):
            with open(os.path.join(pkg, fname), encoding="utf-8") as fh:
                assert "KIND_ANSWER" not in fh.read(), fname


def test_the_lane_marker_is_pinned():
    """Renaming it makes every historical record invisible, so the lane re-replies
    once to every ticket it has ever answered."""
    assert L.LANE_META == "client_dm_lane"
    assert L.LANE_NAME == "client_dm_support"


def test_the_flag_defaults_off():
    import os
    from agent import config
    os.environ.pop("AGENT_CLIENT_DM_AUTOFIX", None)
    os.environ.pop("AGENT_CLIENT_DM_CLIENT_REPLY", None)
    assert config.client_dm_autofix_enabled() is False
    assert config.client_dm_client_reply_enabled() is False


def test_the_runner_calls_this_lane_behind_its_flag():
    """A capability with no production caller is inert in exactly the way a healthy
    idle one is (D68). Assert the wiring statically."""
    import inspect
    from agent import runner
    source = inspect.getsource(runner.run_daily)
    assert "config.client_dm_autofix_enabled()" in source
    assert "from .client_dm_support.lane import run_once_all_identities" in source


def test_the_runner_calls_all_client_carrying_identities_not_just_echo():
    """GAP 1 (audit of PR #68): the previous caller invoked run_once() with no
    arguments, which defaults to identity='echo' alone. Scout, Ranger and Wrangler
    are all armed in production and all resolve identity_kind='client', so a real
    client ticket filed through any of them was invisible to this lane. The runner
    must call the ALL-IDENTITIES entry point, never the single-identity one."""
    import inspect
    from agent import runner
    source = inspect.getsource(runner.run_daily)
    assert "from .client_dm_support.lane import run_once_all_identities" in source
    assert "from .client_dm_support.lane import run_once\n" not in source


# ---------------------------------------------------------------------------
# THE TWO REAL CASES, END TO END.
# ---------------------------------------------------------------------------
def test_chad_edwards_crossfitlocal_the_case_with_a_fix():
    """'the approval-queue posts have no photos'. Real state on 2026-09-06: one active
    gym_drive source ('Ad Photos', connected 21:18 UTC, active, not revoked) and ZERO
    media_asset rows, because the nightly sync at AGENT_DAILY_HOUR_UTC=12 had already
    run before he connected.

    The success condition is mechanical, not a judgement: usable assets 0 -> >0."""
    store = Store([src("crossfitlocal")], [])
    ran = []

    def sync(source, **kw):
        ran.append(source["id"])
        store.assets.extend(assets(54, "crossfitlocal"))
        return {"ok": True, "inserted": 54}

    d = L.decide(text="the posts waiting for my approval have no photos on them",
                 gym_key="crossfitlocal", may_reply=True,
                 deps={"store": store, "sync_source": sync})

    assert d.outcome == L.Outcome.REPLY
    assert d.condition_id == "drive_library_empty"
    assert ran == ["s1"], "the scoped fix did not run"
    assert d.reply_text == (
        "I ran your photo sync just now. It added 54 new file(s). "
        "Your media library has 54 file(s) Echo can post.")
    # every sentence traces to a reading measured AFTER the fix
    assert d.audit["stage"] == "verification"
    assert d.audit["verification"] == "drive_library_usable rose from 0 to 54"
    assert d.audit["readings"] == {"drive_sync_ran": True, "drive_files_added": 54,
                                   "drive_library_usable": 54}


def test_chad_edwards_gets_no_reply_if_the_sync_inserts_nothing():
    """The verification is what stops 'I ran it' becoming a lie: an empty folder, a
    MIME filter that rejects everything, a share that reads but yields nothing."""
    store = Store([src("crossfitlocal")], [])
    d = L.decide(text="my posts have no photos", gym_key="crossfitlocal",
                 may_reply=True,
                 deps={"store": store,
                       "sync_source": lambda s, **kw: {"ok": True, "inserted": 0}})
    assert d.outcome == L.Outcome.ESCALATE
    assert "did not increase" in d.reason


def test_chad_edwards_gets_no_reply_if_the_share_was_actually_revoked():
    store = Store([src("crossfitlocal")], [])
    d = L.decide(text="my posts have no photos", gym_key="crossfitlocal",
                 may_reply=True,
                 deps={"store": store,
                       "sync_source": lambda s, **kw: {"ok": False, "revoked": True}})
    assert d.outcome == L.Outcome.ESCALATE
    assert "share revoked" in d.reason


def test_crossfitlocal_as_it_stands_today_gets_no_reply():
    """Measured live 2026-09-07: crossfitlocal now holds 54 usable assets. The
    scheduled run happened, so no condition matches and a human looks at it. A lane
    that replied here would be answering a question the data says is already closed."""
    store = Store([src("crossfitlocal")], assets(54, "crossfitlocal"))
    d = L.decide(text="my posts have no photos", gym_key="crossfitlocal",
                 may_reply=True, deps={"store": store})
    assert d.outcome == L.Outcome.ESCALATE
    assert d.audit["readings"]["drive_library_usable"] == 54


def test_john_weeks_toughtemple_the_case_with_NO_fix_only_a_question():
    """'wants a real CTA'. Real state: the gym's lasso_voice.md '### CTA rotation'
    section is literally the unfilled intake placeholder.

    There is NO fix Echo may perform. A CTA is client-specific content, and inventing
    one violates CLAUDE.md's hardest rule -- client content only, no invented facts,
    offers, prices or stats. The correct behaviour is to ask him."""
    d = L.decide(text="the posts need a real call to action",
                 gym_key="toughtemple52040e", may_reply=True,
                 deps={"voice_dir": "/vd", "read_text": lambda p: VOICE_TODO})

    assert d.outcome == L.Outcome.REPLY
    assert d.condition_id == "cta_pool_empty"
    assert C.CONDITIONS["cta_pool_empty"].action == "", (
        "the no-fix path grew an action; a CTA is the client's own content")
    assert d.reply_text == (
        "Your brand voice doc has 0 call(s) to action for Echo to rotate through, "
        "which is why your posts are going out without one. I cannot write one for "
        "you, because a call to action has to be your real booking link, phone number "
        "or offer. What would you like your posts to ask people to do?")
    assert d.audit["stage"] == "diagnosis"
    assert d.audit["verification"] == "no write was performed; nothing to verify"


def test_john_weeks_gets_no_reply_once_his_doc_has_real_ctas():
    """Round 7's defect: a gym with three working CTAs and one leftover '> TODO' note
    was auto-told its section was the blank onboarding placeholder, that it had zero
    CTAs, and asked to redo work already done -- while the caption pipeline was
    appending one of those three to every post."""
    doc = VOICE_REAL.replace("### Next",
                             "> TODO: add two more before spring\n### Next")
    d = L.decide(text="we need a real call to action", gym_key="toughtemple52040e",
                 may_reply=True,
                 deps={"voice_dir": "/vd", "read_text": lambda p: doc})
    assert d.outcome == L.Outcome.ESCALATE
    assert d.audit["readings"]["cta_pool_count"] == 2


def test_a_gym_with_no_voice_doc_at_all_gets_no_reply():
    d = L.decide(text="we need a call to action", gym_key="somegym", may_reply=True,
                 deps={"voice_dir": "/vd", "read_text": lambda p: None})
    assert d.outcome == L.Outcome.ESCALATE


def test_a_gym_whose_media_rows_disagree_about_their_owner_gets_no_reply():
    """Live 2026-09-07: toughtemple52040e carries 70 media_asset rows whose source
    belongs to toughtemple086f51, and has no media_source of its own. Told 'my posts
    have no photos', a gym-id-keyed probe sees zero sources and 70 assets."""
    store = Store([], assets(70, "toughtemple52040e", sid="FOREIGN"))
    snap = P.probe_drive("toughtemple52040e", store=store)
    assert snap.get("drive_identity_split") is True
    assert snap.get("drive_active_sources") == 0
    d = L.decide(text="my posts have no photos", gym_key="toughtemple52040e",
                 may_reply=True, deps={"store": store})
    assert d.outcome == L.Outcome.ESCALATE


def test_a_gym_with_six_active_folders_gets_no_reply():
    """Live 2026-09-07: train7164ae502 really has six. Every Drive sentence here is
    singular, so pluralising copy nobody has reviewed is not an option."""
    store = Store([src("train7164ae502", f"s{i}") for i in range(6)], [])
    d = L.decide(text="my posts have no photos", gym_key="train7164ae502",
                 may_reply=True, deps={"store": store})
    assert d.outcome == L.Outcome.ESCALATE
    assert d.audit["readings"]["drive_active_sources"] == 6


# ==========================================================================
# AN ASK IS SPENT ONCE THE CLIENT HAS ANSWERED IT.
#
# MEASURED, NOT REASONED. lane.decide() was run against John Weeks' REAL brand bible,
# read from the production volume on 2026-09-07, and his REAL latest message. It
# returned Outcome.REPLY and composed "What would you like your posts to ask people to
# do?" -- the question Echo had already asked him and that he had just answered.
#
# Every control passed, correctly. The reading is right, the condition genuinely
# applies, the reply is faithfully grounded. The gap is that _already_handled asks "has
# THIS LANE written here", and Echo's ORDINARY reply path is what asked him -- so a lane
# that had never spoken on that thread saw a fresh ticket.
# ==========================================================================
JOHN_ANSWER = ("Yes let's include a call to action to book a free intro class, similar "
               "to what our paid ads are doing")


def _john_decide(*, echo_already_asked):
    return L.decide(text=JOHN_ANSWER, gym_key="toughtemple52040e", may_reply=True,
                    deps={"read_text": lambda p: VOICE_TODO},
                    echo_already_asked=echo_already_asked)


def test_johns_real_case_is_not_asked_the_same_question_twice():
    d = _john_decide(echo_already_asked=True)
    assert d.outcome is L.Outcome.ESCALATE, d.reason
    assert "already spoken" in d.reason
    assert not getattr(d, "reply_text", "")


def test_the_identical_message_on_a_fresh_thread_still_gets_the_ask():
    """The refusal must be about the CONVERSATION, not about the words -- otherwise it
    is a phrase denylist wearing a different hat, which is D67's failure. Same text,
    no prior Echo message, and the ask fires."""
    d = _john_decide(echo_already_asked=False)
    assert d.outcome is L.Outcome.REPLY, d.reason
    assert "What would you like your posts to ask people to do?" in d.reply_text


def test_a_condition_WITH_an_action_still_runs_on_a_thread_echo_has_spoken_on():
    """Scoped to action-less outcomes on purpose. A condition with an action CHANGES
    something; running a gym's Drive sync is still right after Echo has spoken to them,
    and over-refusing would quietly turn the capability off."""
    store = Store(sources=[src()], assets=[])
    calls = []

    def _sync(source, **kw):
        calls.append(source)
        store.assets.extend(assets(4))
        return {"ok": True, "inserted": 4}

    d = L.decide(text="the posts waiting for me have no photos",
                 gym_key="crossfitlocal", may_reply=True,
                 deps={"store": store, "sync_source": _sync, "log": lambda m: None},
                 echo_already_asked=True)
    assert d.outcome is L.Outcome.REPLY, d.reason
    assert calls, "the scoped fix did not run"


def _thread(*rows):
    return FaithfulBus([_ticket("1")], {"1": list(rows)})


def _in(author, at, body="x"):
    return {"direction": "inbound", "author_type": author, "created_at": at,
            "body": body, "attachments": {"surface": "mpim"}}


def _out(author, at, kind, status, body="?"):
    return {"direction": "outbound", "author_type": author, "created_at": at,
            "body": body, "delivery_status": status,
            "attachments": {"kind": kind, "recipient_kind": "client"}}


def test_prior_echo_speech_is_counted_off_the_thread_not_guessed():
    """The predicate against the REAL row shapes production writes."""
    # Echo answered, and the gym wrote afterwards -> the ask is spent.
    assert L._echo_already_asked(_thread(
        _in("client", "T03", JOHN_ANSWER),
        _out("echo", "T02", "answer", "posted"),
        _in("client", "T01")), "1") is True

    # An `ack` counts too: the client saw it, it is part of the exchange.
    assert L._echo_already_asked(_thread(
        _in("client", "T03"),
        _out("echo", "T02", "ack", "posted"),
        _in("client", "T01")), "1") is True

    # The client has only just written; Echo has said nothing.
    assert L._echo_already_asked(_thread(_in("client", "T01")), "1") is False

    # Echo spoke LAST -- no answer has come back, so nothing is spent.
    assert L._echo_already_asked(_thread(
        _out("echo", "T02", "answer", "posted"),
        _in("client", "T01")), "1") is False


def test_a_COACH_answering_is_the_gym_answering():
    """FOUND BY AN ADVERSARIAL AUDIT of the first version of this predicate.

    adapter.author_type_for's whole range is {staff, coach, client} (adapter.py:550),
    over identity_gate's kinds: a gym OWNER resolves to CLIENT, a gym's COACH to COACH,
    and STAFF is Blake and the LASSO team. The first version listed
    ("client","user","human","") -- a guessed set that contained two values production
    never writes and omitted the one it does. A coach answering Echo's question left
    `seen_client` False, so the re-ask bug survived untouched on every coach-authored
    thread.
    """
    from agent.slack_convo import identity_gate as _ig
    # The whole range of adapter.author_type_for, pinned: anything outside it is not a
    # value production writes, and anything inside it must be handled deliberately.
    assert {_ig.STAFF, _ig.COACH, _ig.CLIENT} == {"staff", "coach", "client"}

    assert L._echo_already_asked(_thread(
        _in("coach", "T03", JOHN_ANSWER),
        _out("echo", "T02", "answer", "posted"),
        _in("coach", "T01")), "1") is True, (
        "a coach answered Echo's question and it was not counted as the gym answering"
    )


def test_a_LASSO_staff_message_is_not_the_gym_answering():
    """The other side of the same rule: `staff` is Blake and the team, not the gym."""
    assert L._echo_already_asked(_thread(
        _in("staff", "T03"),
        _out("echo", "T02", "answer", "posted"),
        _in("client", "T01")), "1") is False


@pytest.mark.parametrize("kind", ["escalation", "fixer_request", "hold_notice"])
def test_an_internal_card_is_not_evidence_that_echo_spoke_to_the_client(kind):
    """THE OVER-REFUSAL LIVE DATA CAUGHT.

    support_messages on prod (2026-09-07) holds author_type='system' rows for these
    three kinds -- INTERNAL cards delivered to the fixer channel, which the gym owner
    never sees -- and several carry attachments.recipient_kind='client', so
    recipient_kind is no discriminator either. Counting one as "Echo spoke" would
    suppress a legitimate FIRST ask on any ticket that had merely been escalated
    internally, which on live data is most of them.
    """
    from agent.slack_convo import adapter as _a
    assert kind in _a.INTERNAL_KINDS and kind not in _a.CONVERSATIONAL_KINDS
    assert L._echo_already_asked(_thread(
        _in("client", "T03", JOHN_ANSWER),
        _out("system", "T02", kind, "posted", body="internal card"),
        _in("client", "T01")), "1") is False, (
        f"an internal {kind!r} card the client never saw was counted as Echo having "
        f"asked them something"
    )


@pytest.mark.parametrize("kind", ["receipt", "digest", "a_kind_invented_in_2027",
                                  "", None])
def test_a_kind_this_package_does_not_recognise_refuses_by_default(kind):
    """Membership is an ALLOWLIST, never "not internal". The difference only shows on a
    kind nobody has thought of: an allowlist refuses it, a denylist would count it as
    client-visible and suppress a legitimate ask. adapter.py records that the PORTAL
    decides client visibility by a denylist and that a new internal kind is therefore
    visible to the client by default there -- which is exactly why this side must not
    copy that shape."""
    assert L._echo_already_asked(_thread(
        _in("client", "T03", JOHN_ANSWER),
        _out("echo", "T02", kind, "posted"),
        _in("client", "T01")), "1") is False


@pytest.mark.parametrize("status", ["ready", "held", "failed", "suppressed", None])
def test_an_undelivered_reply_never_consumed_an_ask(status):
    """A row still queued, held behind an arming flag, or failed reached nobody."""
    assert L._echo_already_asked(_thread(
        _in("client", "T03", JOHN_ANSWER),
        _out("echo", "T02", "answer", status),
        _in("client", "T01")), "1") is False


def test_the_two_kind_sets_are_the_ones_this_predicate_was_written_against():
    """A two-way pin. If adapter's sets move, this predicate's meaning moves with them
    and somebody should look."""
    from agent.slack_convo import adapter as _a
    assert _a.CONVERSATIONAL_KINDS == {"ack", "answer", "template", "status"}
    assert _a.INTERNAL_KINDS == {"escalation", "fixer_request", "hold_notice"}
    assert not (_a.CONVERSATIONAL_KINDS & _a.INTERNAL_KINDS)


def test_a_bus_read_failure_does_not_invent_a_prior_ask():
    """Failing closed HERE would mean muting the lane on every Supabase blip, which is
    a different harm. The absence of evidence is not evidence of a prior ask."""
    class _Blind:
        def recent_messages(self, *a, **k):
            raise RuntimeError("supabase 503")

    assert L._echo_already_asked(_Blind(), "1") is False


def test_the_pass_actually_wires_the_thread_state_into_the_decision(monkeypatch):
    """END TO END through run_once. Two correct halves and a dead wire between them is
    the 'built but not wired' shape this package's own history is full of, so the join
    is asserted rather than assumed."""
    seen = []
    real_decide = L.decide

    def _spy(**kw):
        seen.append(kw.get("echo_already_asked"))
        return real_decide(**kw)

    monkeypatch.setattr(L, "decide", _spy)
    msgs = [{"direction": "inbound", "author_type": "client",
             "created_at": "2026-09-06T01:00", "body": "no cta on my posts",
             "attachments": {"surface": "mpim"}},
            {"direction": "outbound", "author_type": "echo",
             "created_at": "2026-09-06T02:00", "body": "what are your CTAs?",
             "delivery_status": "posted", "attachments": {"kind": "answer"}},
            {"direction": "inbound", "author_type": "client",
             "created_at": "2026-09-06T03:00", "body": JOHN_ANSWER,
             "attachments": {"surface": "mpim"}}]
    monkeypatch.setenv(A.ENV_MASTER, "true")
    monkeypatch.delenv(A.ENV_CLIENT_REPLY, raising=False)
    monkeypatch.delenv(A.ENV_LIVE_ACK, raising=False)
    bus = FaithfulBus([_ticket("1")], {"1": msgs})
    monkeypatch.setattr(L, "_gym_key_for", lambda t: "toughtemple52040e")
    out = L.run_once(bus=bus, identity=Ident(), deps={"read_text": lambda p: VOICE_TODO})
    assert out["handled"] == 1, out
    assert seen == [True], seen


def test_the_newest_message_from_a_COACH_is_the_one_decided_on():
    """The SAME guessed author list lived twice in this file, and fixing only one copy
    is the two-implementations failure this whole package exists to stop.

    `_newest_client_message` also read ("client","user","human","") -- so a thread whose
    newest message came from a gym COACH skipped it and fell through to
    `ticket.raw_text`, the ORIGINAL message. That is the "decided on the client's oldest
    words" defect the function's own docstring says was fixed, arriving through the
    author filter instead of through the ordering, and it takes the hard-line belt with
    it: a coach writing "actually, just double our ad budget" would never be read.
    """
    bus = _thread(
        _in("coach", "T03", "actually, just double our ad budget"),
        _in("client", "T01", "my posts have no photos"))
    text, surface = L._newest_client_message(
        bus, _ticket("1", raw_text="my posts have no photos"))
    assert "double our ad budget" in text, (
        "a coach's newest message was skipped and the ORIGINAL text was used instead"
    )
    assert surface == "mpim"
    # ...and because it IS read, the hard-line belt sees it.
    assert L.hard_line(text) is True


def test_a_LASSO_staff_message_is_not_mistaken_for_the_clients_words():
    """The other direction: a human from LASSO talking in the thread is not a support
    request, so the client's own newest words are still what gets decided on."""
    bus = _thread(
        _in("staff", "T03", "on it, looking now"),
        _in("client", "T01", "my posts have no photos"))
    text, _ = L._newest_client_message(bus, _ticket("1"))
    assert text == "my posts have no photos"
