"""
tests/test_slack_convo_autonomy.py -- the 2026-09-05 autonomy wave.

Blake, looking at #fixer: "#fixer is not autonomous. Every ticket says 'the classifier did
not decide' and every held draft is the escalation placeholder, not a real answer."

Four independent causes were found live, and this file is the regression suite for all four
plus the two new capabilities built on top of them:

  D51  classify_llm=None was hardcoded in listener_wiring.live_deps(), so in PRODUCTION the
       classifier could never reach its LLM branch at all. Everything the deterministic
       regexes did not recognise fell to ESCALATE by construction.
  RTF-1 _BREAKAGE_RE missed whole families of ordinary client phrasing -- "my posts are not
       going out", "it stopped posting", "the site is showing the wrong hours" -- so even
       plain breakage reports escalated instead of opening a fix request.
  D52  The client-facing text on both escalation paths promised "I have flagged it for the
       LASSO team and they will follow up here" as a fact, and was written as a REPLY row so
       the #fixer card said "HELD REPLY awaiting your tap" over something that answers
       nothing.
  D53  Cards named a raw Slack id and no gym, so a human could not tell who was waiting.

  D50  Cross-product routing: a confident website question is drafted with the website
       identity's knowledge even when it arrived somewhere else.
  D54  Auto-answer: a NEW, narrower permission than CLIENT_REPLY, gating only the grounded
       answer path, with hard lines that no flag can widen.

Everything runs against the same FakeBus the main suite uses. No network anywhere.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.slack_convo import adapter as A  # noqa: E402
from agent.slack_convo import classifier as C  # noqa: E402
from agent.slack_convo import identities as IDS  # noqa: E402
from agent.slack_convo import identity_gate as IG  # noqa: E402
from agent.slack_convo import listener_wiring as LW  # noqa: E402
from agent.slack_convo import outbox as OB  # noqa: E402

from tests.test_slack_convo import FakeBus, _deps, _ev, _posted, _who  # noqa: E402


# =========================================================================================
# RTF-1: the deterministic gap that made ordinary breakage reports escalate
# =========================================================================================
#
# Each string below is a shape a real gym owner actually writes. The comment records what
# classify() returned BEFORE this wave (all of them: None, "the classifier did not decide")
# and what it returns now. The two marked REAL are the only two genuine, non-test tickets in
# the bus on 2026-09-05; the rest are realistic fixtures, written because the eight tickets
# sitting in #fixer that day were all [phase4-audit] test probes from blake+zztest, not
# client messages, and tuning a classifier against your own test harness's strings proves
# nothing.

BREAKAGE_FIXTURES = [
    # text                                                    before -> after
    "my facebook posts are not going out",                  # None -> code_fix
    "our posts are not going out anymore",                  # None -> code_fix
    "instagram stopped posting last week",                  # None -> code_fix
    "the calendar is not updating",                         # None -> code_fix
    "my site is showing the wrong hours",                   # None -> code_fix
    "the website has the wrong address on it",              # None -> code_fix
    "nothing posted yesterday",                             # None -> code_fix
    "our reels havent gone out since friday",               # None -> code_fix
    "the portal keeps logging me out",                      # None -> code_fix
    "it wont connect to instagram",                         # None -> code_fix
]

# REAL ticket, brokerdale (dale@brokerdale.realestate), 2026-09-05 01:02 UTC. Before this
# wave this classified as None and escalated with the placeholder.
REAL_DALE = ("Friday Sept 5th I had clicked deny and waited on recreation of one of the "
             "posts. You can still see the post I denied with the status re-creating on it "
             "over 24 hours later yet nothing was recreated.")

# REAL ticket a9efa713, portal Website tab, 2026-09-04 02:22 UTC.
REAL_A9EFA713 = "Can we add our group sessions schedule to the website?"

# The false-positive guard. RT-M2's rule (breakage AND an Echo-domain noun) is what makes
# widening _BREAKAGE_RE safe, so these must all STILL escalate or stay chatter: every one
# contains a breakage-shaped phrase with nothing of ours anywhere near it.
NOT_BREAKAGE_FIXTURES = [
    "i cant make it thursday",
    "sorry that was my error",
    "our coach is not coming in today",
    "the parking lot is broken up pretty bad",
    "my knee is still not working right after the squat session",
]


@pytest.mark.parametrize("text", BREAKAGE_FIXTURES)
def test_real_client_breakage_phrasings_now_classify_as_code_fix(text):
    assert C.classify(text, has_open_ticket=False, identity_product="echo") == C.CODE_FIX


def test_real_ticket_dale_recreating_post_now_classifies_as_code_fix():
    """The one genuine client ticket of 2026-09-05. 'nothing was recreated' is the phrase
    that was missing; 'post' / 'posts' is the domain noun RT-M2 requires alongside it."""
    assert C.classify(REAL_DALE, has_open_ticket=False,
                      identity_product="echo") == C.CODE_FIX


def test_real_ticket_a9efa713_is_a_question_not_breakage():
    """a9efa713 is a website CONTENT request, not a breakage report. It classified as a
    question before this wave too (it ends in '?'), and must keep doing so -- the thing that
    changes for it is WHERE the answer comes from (D50 below), never what it is."""
    assert C.classify(REAL_A9EFA713, has_open_ticket=False,
                      identity_product="scout") == C.QUESTION


@pytest.mark.parametrize("text", NOT_BREAKAGE_FIXTURES)
def test_widening_breakage_did_not_create_false_code_fixes(text):
    """A breakage word with no Echo-domain noun is still never a code fix (RT-M2)."""
    assert C.classify(text, has_open_ticket=False,
                      identity_product="echo") != C.CODE_FIX


# =========================================================================================
# D51: the LLM fallback exists in production, and REFUSES TO BOOT when it is claimed and absent
# =========================================================================================

def test_classifier_consults_the_llm_only_after_every_rule_declines():
    seen = []

    def llm(text):
        seen.append(text)
        return C.QUESTION

    # a rule DOES decide this one: the llm is never called
    assert C.classify("my instagram posts are not going out", has_open_ticket=False,
                      identity_product="echo", llm=llm) == C.CODE_FIX
    assert seen == []
    # nothing decides this one: the llm fills the middle
    assert C.classify("something feels off with the account", has_open_ticket=False,
                      identity_product="echo", llm=llm) == C.QUESTION
    assert len(seen) == 1


def test_llm_verdict_outside_the_fixed_set_is_discarded():
    assert C.classify("something feels off", has_open_ticket=False, identity_product="echo",
                      llm=lambda t: "sure, let me help") is None


def test_llm_exception_escalates_and_never_dispatches():
    def boom(text):
        raise RuntimeError("model down")
    assert C.classify("something feels off", has_open_ticket=False, identity_product="echo",
                      llm=boom) is None


def test_build_classify_llm_returns_none_when_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", raising=False)
    lines = []
    assert LW.build_classify_llm(IDS.get("echo"), log=lines.append) is None


def test_build_classify_llm_refuses_to_boot_when_claimed_but_absent(monkeypatch):
    """THE ASSERTION. The whole bug class this wave exists for is a capability that is
    configured ON with nothing behind it -- it looks exactly like working software from the
    outside. A deployment in that state must crash at boot, not quietly degrade."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "true")
    with pytest.raises(LW.NotWiredError):
        LW.build_classify_llm(IDS.get("echo"), log=lambda *a: None,
                              factory=lambda: None)
    with pytest.raises(LW.NotWiredError):
        LW.build_classify_llm(IDS.get("echo"), log=lambda *a: None,
                              factory=lambda: (_ for _ in ()).throw(RuntimeError("no key")))


def test_no_api_key_with_the_flag_on_refuses_to_boot(monkeypatch):
    """THE REQUIREMENT, not the seam. The first version of this suite only ever proved the
    assertion by injecting a factory that returned None -- while the REAL factory could
    neither return None nor raise, so a keyless production deployment booted, logged
    "classifier LLM wired", and escalated every message. This is the test that would have
    caught that, and it uses the production factory."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert C.default_classify_llm() is None, "the production factory must fail at BUILD time"
    with pytest.raises(LW.NotWiredError):
        LW.build_classify_llm(IDS.get("echo"), log=lambda *a: None)


def test_with_a_key_the_production_factory_builds_a_real_callable(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    llm = LW.build_classify_llm(IDS.get("echo"), log=lambda *a: None)
    assert callable(llm)


def test_build_classify_llm_wires_a_real_callable_when_the_flag_is_on(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "true")
    sentinel = lambda text: C.QUESTION  # noqa: E731
    assert LW.build_classify_llm(IDS.get("echo"), log=lambda *a: None,
                                 factory=lambda: sentinel) is sentinel


def test_default_classify_llm_never_returns_a_label_outside_the_set(monkeypatch):
    """The model's raw output is filtered, so a chatty or hallucinated response escalates
    rather than mislabelling. No network: default_llm is patched."""
    from agent.slack_convo import answer_lane as AL
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setattr(AL, "default_llm", lambda s, u, model=None: "I think it is a question")
    assert C.default_classify_llm()("anything") is None
    monkeypatch.setattr(AL, "default_llm", lambda s, u, model=None: "code_fix\n")
    assert C.default_classify_llm()("anything") == C.CODE_FIX


def test_live_deps_no_longer_hardcodes_none(monkeypatch):
    """The literal regression: `classify_llm=None` in live_deps(). With the flag on, the
    deps object must carry a real callable."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLASSIFIER_LLM", "true")
    monkeypatch.setenv("AGENT_SLACK_BOT_TOKEN", "xoxb-not-a-real-token")
    monkeypatch.setattr(C, "default_classify_llm", lambda model=None: (lambda t: C.QUESTION))
    deps = LW.live_deps(IDS.get("echo"), bus=FakeBus(), log=lambda *a: None)
    assert callable(deps.classify_llm)
    assert deps.classify_llm("x") == C.QUESTION


# =========================================================================================
# D52: the false promise, and the card that called a placeholder a reply
# =========================================================================================

def test_the_false_promise_template_is_gone_and_cannot_come_back():
    """The removed constant, asserted structurally so a copy-paste restores nothing.

    Scope note: outreach.py's own approved-outreach copy says "I am on it and will follow up
    here", which is a DIFFERENT and true statement -- that message only exists after Blake
    has tapped approve on a specific outreach, so a person really is on it. What is banned is
    this one: an escalation placeholder that claims a human is engaged at the moment it is
    auto-written, on a path where nothing has been decided at all."""
    assert not hasattr(A, "TEMPLATE_ESCALATED"), "the old constant must not exist"
    banned = "This one needs a person, so I have flagged it for the LASSO team"
    import pathlib
    for path in pathlib.Path(A.__file__).parent.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if banned in line and not line.strip().startswith("#"):
                pytest.fail(f"{path.name}: the false-promise template is back: {line}")
    # audit 2: a grep for one sentence would pass on any reworded equivalent lie, so the
    # REQUIREMENT is asserted too -- no client-facing constant on an undecided path may
    # promise future human action, in whatever words.
    promises = ("follow up", "get back to you", "will be in touch", "someone will",
                "we will reach out", "will look into", "will fix")
    for name in ("TEMPLATE_NO_ANSWER_YET", "TEMPLATE_UNKNOWN", "TEMPLATE_QUEUED"):
        body = getattr(A, name).lower()
        for promise in promises:
            assert promise not in body, f"{name} promises future action: {promise!r}"


def test_undecided_message_gets_an_honest_template_and_a_no_draft_card():
    bus = FakeBus()
    d = A.handle_event(_ev("hmm, thinking about the whole thing"), "k",
                       _deps(bus, client_armed=False))
    rows = [m for m in bus.messages_for(d.ticket_id) if m["direction"] == "outbound"]
    templates = [m for m in rows if m["attachments"]["kind"] == A.KIND_TEMPLATE]
    assert templates, "the person still gets one honest acknowledgement"
    body = templates[0]["body"]
    assert "will follow up here" not in body
    assert "could not answer this one myself" in body
    assert templates[0]["attachments"]["no_draft"] is True
    cards = [m for m in rows if m["attachments"]["kind"] == A.KIND_HOLD_NOTICE]
    assert cards and cards[0]["body"].startswith(f"HELD {A.NO_DRAFT_LABEL}")
    assert "PLACEHOLDER TEXT (not an answer)" in cards[0]["body"]
    assert "HELD REPLY" not in cards[0]["body"]


def test_escalation_card_says_plainly_that_nothing_was_drafted():
    bus = FakeBus()
    d = A.handle_event(_ev("hmm, thinking about the whole thing"), "k", _deps(bus))
    esc = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound"
           and m["attachments"]["kind"] == A.KIND_ESCALATION][0]
    assert A.NO_DRAFT_LABEL in esc["body"]
    assert "Nothing has been drafted for the client" in esc["body"]
    assert "the classifier did not decide what this is" in esc["body"]


def test_unanswerable_question_card_says_no_grounded_facts():
    bus = FakeBus()
    d = A.handle_event(_ev("what does the moon weigh?"), "k",
                       _deps(bus, answer=lambda *a, **k: None))
    esc = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound"
           and m["attachments"]["kind"] == A.KIND_ESCALATION][0]
    assert "no answer drafted" in esc["body"]
    assert "nothing was written for the client to read" in esc["body"]


# =========================================================================================
# D53: cards a human can read
# =========================================================================================

def _client_who(uid="U_CLIENT"):
    return IG.Identity(IG.CLIENT, uid, email="owner@birddog.com", display="Sam Rivera",
                       account_key="birddog", gym_id="gym-uuid-1",
                       reason="portal client_owner")


def test_card_names_the_person_and_the_gym_not_a_raw_slack_id():
    bus = FakeBus()
    deps = _deps(bus, describe_gym=lambda gid: "Bird Dog CrossFit")
    deps.resolve_identity = lambda uid: _client_who(uid)
    d = A.handle_event(_ev("hmm, thinking about the whole thing"), "k", deps)
    esc = [m for m in bus.messages_for(d.ticket_id)
           if m["direction"] == "outbound"
           and m["attachments"]["kind"] == A.KIND_ESCALATION][0]
    assert "Sam Rivera" in esc["body"]
    assert "Bird Dog CrossFit" in esc["body"]
    assert "owner@birddog.com" in esc["body"]
    assert "ASKED: hmm, thinking about the whole thing" in esc["body"]
    assert "PROPOSED:" in esc["body"] and "STATUS:" in esc["body"]
    assert "U_CLIENT" in esc["body"], "the id is still there for lookups, just not alone"


def test_card_falls_back_through_account_key_then_id_and_never_blank():
    bus = FakeBus()
    deps = _deps(bus, describe_gym=lambda gid: "")   # name lookup finds nothing
    deps.resolve_identity = lambda uid: _client_who(uid)
    d = A.handle_event(_ev("hmm, thinking"), "k", deps)
    esc = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ESCALATION][0]
    assert "gym birddog" in esc["body"]


def test_gym_lookup_failure_never_blocks_the_card():
    bus = FakeBus()

    def boom(gid):
        raise RuntimeError("supabase down")

    deps = _deps(bus, describe_gym=boom)
    deps.resolve_identity = lambda uid: _client_who(uid)
    d = A.handle_event(_ev("hmm, thinking"), "k", deps)
    assert any(m["attachments"].get("kind") == A.KIND_ESCALATION
               for m in bus.messages_for(d.ticket_id))


def test_unresolved_identity_is_said_in_plain_words_with_what_was_tried():
    who = IG.Identity(IG.UNKNOWN, "U_NOBODY", reason="no email on slack profile")
    line = A.unresolved_identity_line(who)
    assert line == "unresolved identity: no email on slack profile"


def test_a9efa713_shaped_unresolved_reason_reaches_the_card():
    """The real one: reporter NULL on a portal ticket. The specific reason must survive to
    the card instead of being flattened to 'identity_unknown'."""
    who = IG.Identity(IG.UNKNOWN, "", reason="ticket missing reporter or client_id")
    assert "ticket missing reporter or client_id" in A.unresolved_identity_line(who)


def test_fixer_request_card_still_carries_no_display_name():
    """RT-m3 is NOT relaxed for the card that reaches the Bash-armed worker."""
    txt = A.fixer_request_text(IDS.get("echo"), "T1", "posts are not going out",
                               _client_who(), "U_CLIENT")
    assert "Sam Rivera" not in txt
    assert "U_CLIENT" in txt


# =========================================================================================
# D50: cross-product routing -- which BOT answers, never which GYM
# =========================================================================================

def test_product_hint_is_confident_only_on_an_unmistakable_website_question():
    assert C.product_hint(REAL_A9EFA713) == ("websites", C.CONFIDENT)
    assert C.product_hint("can you change the hours on our website")[1] == C.CONFIDENT
    # a competing product noun in the same message drops it to unsure
    assert C.product_hint("should i post that to instagram or put it on the website")[1] \
        == C.UNSURE
    assert C.product_hint("my posts are not going out")[1] == C.UNSURE
    assert C.product_hint("")[1] == C.UNSURE


def test_confident_website_question_is_answered_with_wrangler_knowledge():
    bus = FakeBus()
    seen = {}

    def ans(ticket, who, messages, question=None, answer_identity=None):
        seen["identity"] = answer_identity.name
        seen["product"] = answer_identity.product
        return {"body": "Yes, we can add that.", "grounding": {"site": "live"}}

    deps = _deps(bus, identity="scout", answer=ans, cross_product=True)
    d = A.handle_event(_ev(REAL_A9EFA713), "k", deps)
    assert seen == {"identity": "wrangler", "product": "websites"}
    t = bus.ticket(d.ticket_id)
    # FRAME 2: everything that decides WHERE this lands is untouched.
    assert t["bot_identity"] == "scout"
    assert t["product"] == "scout"
    assert t["slack_channel_id"] == "G0MPIM"
    assert t["verification_after"]["routed_from_product"] == "scout"
    # m2 (audit 2): the record says the KNOWLEDGE moved, not that the facts came from a
    # websites seam -- there is no websites seam, and claiming one would be a false entry in
    # the verification record this system treats as evidence.
    assert t["verification_after"]["knowledge_and_voice_of"] == "wrangler"
    assert "unchanged by routing" in t["verification_after"]["facts_source"]


def test_cross_product_routing_does_not_fire_when_the_flag_is_off():
    bus = FakeBus()
    seen = {}

    def ans(ticket, who, messages, question=None, answer_identity=None):
        seen["identity"] = answer_identity.name
        return {"body": "x", "grounding": {"a": 1}}

    A.handle_event(_ev(REAL_A9EFA713), "k",
                   _deps(bus, identity="scout", answer=ans, cross_product=False))
    assert seen["identity"] == "scout"


def test_low_confidence_stays_with_the_entry_point_identity():
    bus = FakeBus()
    seen = {}

    def ans(ticket, who, messages, question=None, answer_identity=None):
        seen["identity"] = answer_identity.name
        return {"body": "x", "grounding": {"a": 1}}

    A.handle_event(_ev("should i put that on instagram or the website?"), "k",
                   _deps(bus, identity="scout", answer=ans, cross_product=True))
    assert seen["identity"] == "scout"


def test_cross_product_never_targets_lainey():
    """Lainey stays off, always. It can never be selected as an answering identity."""
    ident = IDS.get("scout")

    class D:
        cross_product_armed = staticmethod(lambda: True)
        log = staticmethod(lambda *a, **k: None)

    for text in ("lainey", "sms", "call the lead", "the website"):
        target, _ = A._answer_identity(D(), ident, text)
        assert target.name != "lainey"


def test_cross_product_answer_still_uses_only_the_asking_gyms_account():
    """The Frame 2 guarantee stated as a test: `who` reaches the answer lane unchanged, so
    every live fact is still keyed off the asking gym's own account key."""
    bus = FakeBus()
    captured = {}

    def ans(ticket, who, messages, question=None, answer_identity=None):
        captured["account_key"] = who.account_key
        captured["gym_id"] = who.gym_id
        return {"body": "x", "grounding": {"a": 1}}

    deps = _deps(bus, identity="scout", answer=ans, cross_product=True)
    deps.resolve_identity = lambda uid: _client_who(uid)
    A.handle_event(_ev(REAL_A9EFA713), "k", deps)
    assert captured == {"account_key": "birddog", "gym_id": "gym-uuid-1"}


# =========================================================================================
# D54: auto-answer -- a narrower permission, with hard lines no flag can widen
# =========================================================================================

def _answering_deps(bus, **kw):
    return _deps(bus, answer=lambda *a, **k: {"body": "Yes, instagram is connected.",
                                              "grounding": {"ig": "connected"}}, **kw)


def test_grounded_answer_holds_when_auto_answer_is_off_even_with_client_reply_armed():
    """The permission Blake actually reviewed when he armed CLIENT_REPLY did not include
    sending model-written answers unattended. It does not now."""
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=False))
    ans = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ANSWER][0]
    assert ans["delivery_status"] == "held"
    cards = [m for m in bus.messages_for(d.ticket_id)
             if m["attachments"].get("kind") == A.KIND_HOLD_NOTICE]
    assert cards, "a held answer always gets a card"


def test_grounded_answer_is_ready_when_auto_answer_is_armed():
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    ans = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ANSWER][0]
    assert ans["delivery_status"] == "ready"


def test_acks_and_templates_are_unaffected_by_the_auto_answer_flag():
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=False))
    ack = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ACK][0]
    assert ack["delivery_status"] == "ready"


HARD_LINE_QUESTIONS = [
    "how much am i being charged this month?",
    "can i get a refund for august?",
    "what are your hours on labor day?",
    "can you change our class schedule to 6am?",
    "one of our members hurt her knee, what should we tell her?",
    "is that a liability issue for us?",
]


@pytest.mark.parametrize("text", HARD_LINE_QUESTIONS)
def test_hard_lines_never_auto_answer_whatever_the_flags_say(text):
    bus = FakeBus()
    d = A.handle_event(_ev(text), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    rows = [m for m in bus.messages_for(d.ticket_id)
            if m["attachments"].get("kind") == A.KIND_ANSWER]
    for r in rows:
        assert r["delivery_status"] == "held", f"{text!r} must never send unattended"


def test_hard_line_is_re_checked_at_post_time_not_only_at_draft_time(monkeypatch):
    """A row written by an older process, or before a flag flip, still cannot slip out."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    row = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ANSWER][0]
    for m in bus.msgs:                    # as an older writer would have left the row
        if m["id"] == row["id"]:
            m["attachments"]["auto_answer_forbidden"] = True
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert not any("instagram is connected" in c["text"] for c in calls)
    assert bus.message(row["id"])["delivery_status"] == "held"


def test_post_time_gate_holds_an_answer_when_auto_answer_is_off(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.delenv("SLACK_CONVO_ECHO_AUTO_ANSWER", raising=False)
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    row = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ANSWER][0]
    assert row["delivery_status"] == "ready"          # written when the flag looked armed
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.message(row["id"])["delivery_status"] == "held"   # caught at post time
    assert not any("instagram is connected" in c["text"] for c in calls)


def test_a_released_answer_still_posts_after_a_human_tap(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=False))
    row = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ANSWER][0]
    assert OB.release_held(bus, row["id"], approved_by="U06EPUUCL13",
                           identity=IDS.get("echo"), log=lambda *a: None)
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert any("instagram is connected" in c["text"] for c in calls)


# =========================================================================================
# Receipts: what the client was actually told, and when
# =========================================================================================

def _receipts(bus, tid):
    """C3: a receipt is an ESCALATION row carrying attachments.receipt -- a kind the portal
    already hides from clients. Anything that reads receipts must key on the marker, never
    on a kind of its own."""
    return [m for m in bus.messages_for(tid)
            if m["attachments"].get("kind") == A.KIND_ESCALATION
            and m["attachments"].get("receipt")]


def test_an_auto_answer_posts_a_receipt_naming_what_was_sent(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    rec = _receipts(bus, d.ticket_id)
    assert len(rec) == 1
    body = rec[0]["body"]
    assert "RECEIPT:" in body
    assert "SENT AUTOMATICALLY (no tap)" in body
    assert "Yes, instagram is connected." in body, "the receipt quotes the real sent text"
    assert rec[0]["attachments"]["auto_answer"] is True
    assert rec[0]["attachments"]["kind"] in A.CLIENT_INVISIBLE_KINDS
    assert rec[0]["attachments"]["sent_at"]
    # and it is an internal kind: it goes to the fixer channel, never the client's thread
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert any(c["channel"] == "C_FIXER" and "RECEIPT:" in c["text"] for c in calls)


def test_a_receipt_is_never_written_before_the_post_succeeds(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))

    def failing_post(channel, text, thread_ts=None, blocks=None):
        if "instagram is connected" in text:
            raise RuntimeError("slack down")
        return "1.0"

    OB.run_once(bus, failing_post, identity=IDS.get("echo"), log=lambda *a: None)
    assert _receipts(bus, d.ticket_id) == [], "no receipt for a delivery that did not happen"


def test_no_internal_kind_is_readable_by_a_client():
    """C3, as a cross-repo contract test.

    Client visibility is decided in lasso-ops-portal (src/lib/support/client-visible.ts and
    migration 0310) by a DENYLIST of three kinds. Any internal kind this repo invents that is
    not in that list is visible to the client by default, in a repo this one cannot import.
    That is how kind='receipt' leaked the fixer channel id and "SENT AUTOMATICALLY (no tap)"
    into a client's portal thread. If you add an internal kind and this test fails, the
    portal's list must change FIRST, in its own PR, before the kind ships here."""
    assert A.INTERNAL_KINDS <= A.CLIENT_INVISIBLE_KINDS, (
        f"internal kinds the portal would show a client: "
        f"{sorted(A.INTERNAL_KINDS - A.CLIENT_INVISIBLE_KINDS)}")


def test_a_receipt_never_uses_a_kind_the_portal_would_show(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    post, _calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    for r in _receipts(bus, d.ticket_id):
        assert r["attachments"]["kind"] in A.CLIENT_INVISIBLE_KINDS


def test_acks_get_no_receipt(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("my posts are not going out"), "k",
                       _deps(bus, client_armed=True))
    post, _calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    kinds = [r["attachments"]["receipt_kind"] for r in _receipts(bus, d.ticket_id)]
    assert A.KIND_ACK not in kinds


def test_the_honest_template_gets_a_receipt_so_blake_sees_what_landed(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("hmm, thinking about the whole thing"), "k",
                       _deps(bus, client_armed=True))
    post, _calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    bodies = [r["body"] for r in _receipts(bus, d.ticket_id)]
    assert any("could not answer this one myself" in b for b in bodies)


def test_a_human_resolution_is_receipted_too(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("hmm, thinking about the whole thing"), "k",
                       _deps(bus, client_armed=True))
    assert OB.resolve_and_notify(bus, d.ticket_id, approved_by="U06EPUUCL13",
                                 identity=IDS.get("echo"), log=lambda *a: None)
    post, _calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    bodies = [r["body"] for r in _receipts(bus, d.ticket_id)]
    assert any(OB.RESOLVED_NOTICE[:30] in b for b in bodies), \
        "the receipt shows the client the exact words they were sent"


# =========================================================================================
# RTF-2: the portal bridge passed the ANSWER LANE's callable to the CLASSIFIER
# =========================================================================================

def test_a_mis_shaped_classifier_callable_refuses_to_boot():
    """answer_lane.default_llm(system, user, model=None) is not a classifier. Wiring it as
    one raised TypeError on every message and escalated silently -- the live cause of the
    one real client ticket of 2026-09-05 escalating. It now fails loudly at boot instead."""
    from agent.slack_convo import answer_lane as AL
    with pytest.raises(LW.NotWiredError):
        LW.assert_classifier_shape(AL.default_llm, IDS.get("echo"))
    LW.assert_classifier_shape(lambda text: C.QUESTION, IDS.get("echo"))  # the right shape


def test_the_answer_lane_llm_is_never_the_one_handed_to_classify():
    """RTF-2 as behaviour, not as a source substring (audit 2: the old version of this test
    asserted a literal line of code, so it passed on a dropped value and failed on a
    reformat). Two DIFFERENT callables go in; the test asserts each is called with the
    signature it actually has, which is the thing that was wrong."""
    from agent import echo_ticket_worker as ETW
    seen = {"classify": [], "answer": []}

    class _Bus:
        def find_new_tickets(self, **kw):
            return [{"id": "t-1", "product": "echo", "source": "website_tab",
                     "client_id": "g-1", "reporter": "owner@gym.com",
                     "raw_text": "wholly ambiguous sentence", "status": "new"}]

        def inbound_count(self, tid):
            return 1

        def ticket(self, tid):
            return {"id": tid, "status": "new"}

        def set_ticket(self, tid, **f):
            return {}

        def record_outbound(self, **kw):
            return {"id": "m-1"}

        def count_outbound_kind_since(self, *a, **k):
            return 0

        def messages(self, tid, limit=200):
            return []

    def classify_llm(text):                       # one positional arg
        seen["classify"].append(text)
        return None

    def answer_llm(system, user, model=None):     # three, the answer lane's shape
        seen["answer"].append(system)
        return "NO_ANSWER"

    os.environ["AGENT_PORTAL_ECHO_TICKETS_ENABLED"] = "true"
    ETW.intake_pass(
        _Bus(), slack_lookup_email=lambda e: "U1",
        slack_user_info=lambda u: {"id": u, "is_bot": False, "email": "owner@gym.com"},
        portal_lookup=lambda e: {"role": "client",
                                 "gyms": [{"gym_id": "g-1", "relationship": "client_owner",
                                           "account_key": "k"}]},
        open_group_dm=lambda ids: {"ok": False}, post_first_message=lambda c, t: {"ok": False},
        write_hold_notice=lambda **kw: None, classify_llm=classify_llm,
        fetch_state=lambda t, w: {}, llm=answer_llm, log=lambda *a, **k: None)
    assert seen["classify"] == ["wholly ambiguous sentence"], \
        "classify() must be handed the one-argument callable, and actually called with it"


# =========================================================================================
# Test-data marking: our own probes never resurface in a count, a poll or a card
# =========================================================================================

from agent.slack_convo import testdata as TD  # noqa: E402


def test_the_eight_live_probes_are_all_recognised_as_test_data():
    """The exact shapes of the eight rows that were sitting in #fixer on 2026-09-05."""
    probes = [
        {"raw_text": "[phase4-audit b86c6769] happy path: my facebook posts are not going out",
         "reporter": "blake+zztest@lassoframework.com", "slack_user_id": "U0BV9D5A17W"},
        {"raw_text": "[phase4-audit 4a6ee62e] escalation path: unresolvable sender",
         "reporter": "U0000000000", "slack_user_id": "U0000000000"},
        {"raw_text": "[phase4-audit-scout 77a1d598] happy path",
         "reporter": "blake+zztest@lassoframework.com", "slack_user_id": "U0BV9D5A17W"},
        {"raw_text": "[phase4-audit-wrangler aa77f8e1] happy path: wrong hours",
         "reporter": "blake+zztest@lassoframework.com", "slack_user_id": "U0BV9D5A17W"},
        {"raw_text": "test escalation path, please ignore",
         "reporter": "nonexistent-unresolvable-test@lassoframework.com"},
    ]
    for p in probes:
        assert TD.is_test_ticket(p), p


def test_the_two_real_client_tickets_are_never_marked_test():
    """a9efa713 and Dale's. Wrongly hiding a client's ticket is the failure that matters."""
    real = [
        {"raw_text": REAL_A9EFA713, "reporter": None, "slack_user_id": None},
        {"raw_text": REAL_DALE, "reporter": "dale@brokerdale.realestate"},
        {"raw_text": "my posts are not going out", "reporter": "owner@birddog.com"},
        # a client who happens to use the word audit, or brackets, is still a client
        {"raw_text": "[urgent] can you audit our page?", "reporter": "owner@gym.com"},
    ]
    for t in real:
        assert not TD.is_test_ticket(t), t


def test_an_unreadable_row_is_treated_as_real_not_hidden():
    assert TD.is_test_ticket(None) is False


def test_the_durable_column_is_honoured_when_it_exists():
    assert TD.is_test_ticket({"is_test": True, "raw_text": "anything"}) is True


def test_exclude_test_filters_a_mixed_list():
    rows = [{"raw_text": "[phase4-audit x] probe"}, {"raw_text": REAL_DALE}]
    assert TD.exclude_test(rows) == [{"raw_text": REAL_DALE}]


# =========================================================================================
# AUDIT 2 (2026-09-05): the findings a green suite was blind to, each with the fake that
# hid it replaced by one shaped like production.
# =========================================================================================

def _real_signature_hold_notice(bus):
    """The production hold-notice callable, not a **kwargs sponge.

    C1 of audit 2 was invisible because EVERY test passed `write_hold_notice=lambda **kw:
    ...`, which accepts any signature -- including the one that raised TypeError in
    production on every held portal answer. A fake that cannot fail the way production fails
    is not a test double, it is a blindfold."""
    from agent import echo_ticket_wiring as ETW
    return ETW._write_hold_notice_factory(bus)


def test_the_portal_hold_path_actually_writes_a_card_through_the_real_callable():
    """C1 (audit 2), re-tested through PRODUCTION (audit 3: the first version of this test
    asserted a substring of inspect.getsource, the same anti-pattern this file criticises
    two hundred lines up -- it broke on a reformat and passed on a semantic change).

    This runs the real `_write_hold_notice_factory` against a real bus fake, so a missing or
    renamed required argument raises exactly as it did in production."""
    from agent import echo_ticket_worker as ETW
    from agent import echo_ticket_wiring as ETWiring
    import os as _os

    class _Bus:
        def __init__(self):
            self.rows = []

        def find_new_tickets(self, **kw):
            return [{"id": "t-1", "product": "echo", "source": "website_tab",
                     "client_id": "g-1", "reporter": "owner@gym.com",
                     "raw_text": "is my instagram connected?", "status": "new"}]

        def inbound_count(self, tid):
            return 1

        def ticket(self, tid):
            return {"id": tid, "status": "new"}

        def set_ticket(self, tid, **f):
            return {}

        def record_outbound(self, **kw):
            self.rows.append(kw)
            return {"id": f"m-{len(self.rows)}"}

        def count_outbound_kind_since(self, *a, **k):
            return 0

        def messages(self, tid, limit=200):
            return []

    bus = _Bus()
    _os.environ["AGENT_PORTAL_ECHO_TICKETS_ENABLED"] = "true"
    for var in ("SLACK_CONVO_ECHO_AUTO_ANSWER",):
        _os.environ.pop(var, None)
    ETW.intake_pass(
        bus, slack_lookup_email=lambda e: "U1",
        slack_user_info=lambda u: {"id": u, "is_bot": False, "email": "owner@gym.com"},
        portal_lookup=lambda e: {"role": "client",
                                 "gyms": [{"gym_id": "g-1", "relationship": "client_owner",
                                           "account_key": "k"}]},
        open_group_dm=lambda ids: {"ok": True, "channel_id": "G"},
        post_first_message=lambda c, t: {"ok": True, "ts": "1.1"},
        # THE REAL production callable, not a **kwargs sponge
        write_hold_notice=ETWiring._write_hold_notice_factory(bus),
        fetch_state=lambda t, w: {"social_status": {"instagram": "connected"}},
        llm=lambda system, user, model=None: "Yes, it is connected.",
        classify_llm=None, log=lambda *a, **k: None)
    cards = [r for r in bus.rows if r.get("kind") == A.KIND_HOLD_NOTICE]
    assert cards, "a held portal answer must produce a real hold card through the real factory"


def test_delivered_is_not_the_same_as_opened():
    """C2 (audit 2): OutreachResult.opened is True when conversations.open succeeded, which
    includes the case where the message post FAILED. Callers must read `delivered`."""
    from agent.slack_convo import outreach as OU
    posted_calls = []

    def open_group_dm(ids):
        return {"ok": True, "channel_id": "G_DM"}

    def post_first_message(chan, text):
        posted_calls.append(text)
        return {"ok": False}          # slack refused

    rows = []
    res = OU.initiate(
        {"id": "t-1", "source": "website_tab", "reporter": "owner@gym.com",
         "slack_user_id": "U_C", "client_id": "g-1", "reporter_verified": True},
        IG.Identity(IG.CLIENT, "U_C", email="owner@gym.com", account_key="k", gym_id="g-1",
                    reason="t"),
        IDS.get("echo"), open_group_dm=open_group_dm,
        post_first_message=post_first_message,
        record_outbound=lambda **kw: rows.append(kw) or {"id": "r1"},
        stamp_ticket=lambda *a, **k: None, message_text="hello",
        mark_message=lambda *a, **k: None, log=lambda *a, **k: None)
    assert res.opened is True, "the DM channel really did open"
    assert res.delivered is False, "but nothing reached the client"


PORTAL_MUST_HOLD = [
    "a member tweaked her back, what do we tell her?",
    "do we need them to sign anything before they train?",
    "what do we owe you at the end of the term?",
    "what time does the gym open on saturday?",
    "can you post that our saturday group is moving to 8am?",
]


@pytest.mark.parametrize("text", PORTAL_MUST_HOLD)
def test_the_portal_bridge_enforces_the_allowlist_not_just_the_denylist(text, monkeypatch):
    """Finding 2 (audit 3, CRITICAL): the portal bridge called auto_answer_forbidden twice
    and auto_answer_allowed NEVER, so every sentence that dodges the denylist posted to a
    client's group DM with no tap -- on the exact path the previous audit's C2 was filed
    against. The old tests asserted the PREDICATE only, so they passed while a call site
    ignored it. These drive the call site."""
    from agent import echo_ticket_worker as ETW
    import os as _os
    for var in ("SLACK_CONVO_ENABLED", "SLACK_CONVO_ECHO_ENABLED",
                "SLACK_CONVO_ECHO_CLIENT_REPLY", "SLACK_CONVO_ECHO_AUTO_ANSWER"):
        monkeypatch.setenv(var, "true")
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "true")
    posted = []

    class _Bus:
        def __init__(self):
            self.rows = []
            self.status = "new"

        def find_new_tickets(self, **kw):
            return [{"id": "t-1", "product": "echo", "source": "website_tab",
                     "client_id": "g-1", "reporter": "owner@gym.com",
                     "raw_text": text, "status": "new"}]

        def inbound_count(self, tid):
            return 1

        def ticket(self, tid):
            return {"id": tid, "status": self.status}

        def set_ticket(self, tid, **f):
            self.status = f.get("status", self.status)
            return {}

        def record_outbound(self, **kw):
            self.rows.append(kw)
            return {"id": f"m-{len(self.rows)}"}

        def count_outbound_kind_since(self, *a, **k):
            return 0

        def messages(self, tid, limit=200):
            return []

    bus = _Bus()
    ETW.intake_pass(
        bus, slack_lookup_email=lambda e: "U1",
        slack_user_info=lambda u: {"id": u, "is_bot": False, "email": "owner@gym.com"},
        portal_lookup=lambda e: {"role": "client",
                                 "gyms": [{"gym_id": "g-1", "relationship": "client_owner",
                                           "account_key": "k"}]},
        open_group_dm=lambda ids: {"ok": True, "channel_id": "G_DM"},
        post_first_message=lambda c, t: posted.append(t) or {"ok": True, "ts": "1.1"},
        write_hold_notice=lambda **kw: None,
        fetch_state=lambda t, w: {"social_status": {"instagram": "connected"}},
        llm=lambda system, user, model=None: "Here is an answer about that.",
        classify_llm=None, log=lambda *a, **k: None)
    assert posted == [], f"{text!r} reached a client with no tap"
    assert bus.status == "hold"


@pytest.mark.parametrize("text", [
    "what time does the gym open on saturday",
    "what time is the 6am class",
    "our saturday classes are moving to 8am",
    "we moved the 6am class to 5:30, can you update everything",
    "tell members the 9am is cancelled tomorrow",
    "a member tweaked her back, what do we tell her",
    "someone got dizzy during a workout, are we covered",
    "do we need them to sign anything before they train",
    "how much is this going to run us each month",
    "can we downgrade to the cheaper plan",
    "we want to cancel and get our money back",
])
def test_ordinary_phrasing_cannot_walk_past_the_hard_lines(text):
    """M3 (audit 2): eleven real sentences an auditor walked straight past a denylist of
    topics. A denylist has to enumerate every phrasing of every dangerous subject and is
    wrong the moment someone says it differently, so unattended sending is now gated by an
    ALLOWLIST of what this system can actually observe, with the denylist as a second layer."""
    assert not A.auto_answer_allowed(text)


@pytest.mark.parametrize("text", [
    "is my instagram connected?",
    "did my posts go out this week?",
    "what is on the calendar for october?",
    "is the october schedule loaded?",
    "why is my facebook account disconnected?",
])
def test_the_questions_auto_answer_exists_for_still_pass(text):
    """The allowlist must not make the capability inert -- that is its own failure mode."""
    assert A.auto_answer_allowed(text)


def test_post_time_hard_line_re_reads_the_body_not_just_the_marker(monkeypatch):
    """M2 (audit 2): gate 5a trusted the stored attachments marker alone, so a row written by
    any writer that omits it posted a hard-line answer with no tap. The previous test for
    this injected the marker and asserted the lookup -- it proved the boolean, not the rule,
    which is exactly why M2 shipped underneath it."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    row = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ANSWER][0]
    for m in bus.msgs:                       # a row from a writer that knows nothing of D54
        if m["id"] == row["id"]:
            m["body"] = "Your gym hours are 5am to 8pm and the monthly charge is $149."
            m["attachments"].pop("auto_answer_forbidden", None)
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert not any("monthly charge" in c["text"] for c in calls), \
        "a hard line in the BODY must be caught at post time even with no marker"
    assert bus.message(row["id"])["delivery_status"] == "held"


def test_a_verification_that_did_not_succeed_is_never_announced_as_a_fix():
    """M5 (audit 2): _fix_summary_text claimed "Fixed it and confirmed the change is live"
    on the mere PRESENCE of verification_after, without reading it."""
    from agent import echo_ticket_worker as ETW
    # Audit 3 finding 1: the first fix asked "is this verdict one of five known FALSE
    # values?", so every one of these read as SUCCESS and the client was told the fix was
    # confirmed live. The rule is an allowlist of affirmatives now: anything we do not
    # recognise makes no claim at all.
    for snapshot in ({"verified": False, "reason": "could not reproduce"},
                     {"verified": "not verified"}, {"verified": "pending"},
                     {"verified": "unverified"}, {"verified": "could not verify"},
                     {"passed": "0 of 3"}, {"ok": {"http": 500}}, {"ok": "timeout"},
                     {"success": "partial"}, {"status": "failed"},
                     # a PR link is not a verification: a PR can be open, closed or reverted
                     {"fix_pr_url": "https://x/1", "merged": False, "deployed": False},
                     {"fix_pr_url": "https://x/1"}, {}, None):
        assert ETW._fix_summary_text(snapshot) is None, snapshot
    for snapshot in ({"verified": True}, {"verified": "yes"}, {"status": "passed"},
                     {"ok": True, "fix_pr_url": "https://x/1"}):
        assert "Fixed it" in (ETW._fix_summary_text(snapshot) or ""), snapshot


def test_a_client_cannot_hide_their_own_ticket_by_pasting_a_probe_tag():
    """m5 (audit 2): the INTAKE drop needs a stronger predicate than the report exclusion --
    find_new_tickets is the only intake poll there is, so a drop is permanent."""
    from agent.slack_convo import testdata as TD2
    client_row = {"raw_text": "[smoke-test 12] is what my log says, my posts are not going out",
                  "reporter": "owner@realgym.com"}
    assert TD2.is_test_ticket_strict(client_row) is False
    probe = {"raw_text": "[phase4-audit 1] probe", "reporter": "blake+zztest@lassoframework.com"}
    assert TD2.is_test_ticket_strict(probe) is True


def test_a_failed_release_does_not_mark_the_row_delivered():
    """Finding 3 (audit 3): release_approved_outreach marked the held row 'posted' on
    `opened`, which is True even when the post failed -- and outreach_request is NOT on the
    portal's hidden list, so a client could read a message that was never sent to them."""
    from agent.slack_convo import outreach as OU
    marks = []
    held = {"id": "r1", "ticket_id": "t-1", "delivery_status": "held",
            "body": "hello there", "attachments": {"kind": A.KIND_OUTREACH_REQUEST,
                                                   "identity": "echo"}}
    res = OU.release_approved_outreach(
        "r1",
        {"id": "t-1", "source": "website_tab", "reporter": "owner@gym.com",
         "slack_user_id": "U_C", "client_id": "g-1", "reporter_verified": True},
        IG.Identity(IG.CLIENT, "U_C", email="owner@gym.com", account_key="k", gym_id="g-1",
                    reason="t"),
        IDS.get("echo"), get_held_message=lambda mid: held,
        open_group_dm=lambda ids: {"ok": True, "channel_id": "G_DM"},
        post_first_message=lambda c, t: {"ok": False},
        record_outbound=lambda **kw: {"id": "r2"},
        stamp_ticket=lambda *a, **k: None,
        mark_message=lambda mid, status, **kw: marks.append((mid, status)),
        log=lambda *a, **k: None)
    assert res.delivered is False
    assert ("r1", "posted") not in marks, "a failed send must never mark the row delivered"
    assert any(status == "failed" for _mid, status in marks)


def test_a_dead_classifier_key_is_loud_not_silent(monkeypatch, capsys):
    """Finding 4 (audit 3): a present-but-INVALID key cannot be caught at boot without a
    network call, so the requirement is that it can never be SILENT. classify() swallows
    every exception by design, which is indistinguishable from a healthy classifier with
    nothing to say -- the D51 flood again."""
    from agent.slack_convo import answer_lane as AL
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-invalid")

    def boom(system, user, model=None):
        raise RuntimeError("401 invalid x-api-key")

    monkeypatch.setattr(AL, "default_llm", boom)
    llm = C.default_classify_llm()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            llm("anything at all")
    out = capsys.readouterr().out
    assert "model call failed" in out
    assert "CRITICAL" in out, "a repeatedly dead key must escalate its own log level"
    assert "ANTHROPIC_API_KEY" in out, "the log must name what to check"
    # and the classifier still fails CLOSED around it
    assert C.classify("anything at all", has_open_ticket=False, identity_product="echo",
                      llm=llm) is None


def test_no_client_facing_constant_promises_future_human_action():
    """Finding 5 (audit 3): ACK_CODE_FIX said "You will hear back here from the team once it
    is verified" -- the exact sentence V-M6 records as removed for having no mechanism,
    restored in a different constant, and the D52 test only scanned three others."""
    promises = ("follow up", "hear back", "get back to you", "will be in touch",
                "someone will", "we will reach out", "will look into", "will fix")
    client_facing = [n for n in dir(A)
                     if n.startswith(("TEMPLATE_", "ACK_")) and isinstance(getattr(A, n), str)]
    assert len(client_facing) >= 6, "the scan must cover every client-facing constant"
    for name in client_facing:
        body = getattr(A, name).lower()
        for promise in promises:
            assert promise not in body, f"{name} promises future action: {promise!r}"


def test_the_cross_product_answer_introduces_the_bot_that_is_actually_speaking():
    """Finding 13 (audit 3): the routed answer was drafted with a system prompt saying "You
    are Wrangler" while Scout's bot posted it in Scout's DM."""
    from agent.slack_convo import answer_lane as AL
    seen = {}

    def llm(system, user):
        seen["system"] = system
        return "Yes, we can add that."

    AL.answer({"id": "t-1", "raw_text": "q"},
              IG.Identity(IG.CLIENT, "U", account_key="k", gym_id="g", reason="t"),
              [], "can we add our hours to the website?",
              identity=IDS.get("wrangler"), speaks_as="scout",
              fetch_state=lambda t, w: {"site": "live"}, llm=llm)
    assert seen["system"].startswith("You are Scout"), seen["system"][:40]


# =========================================================================================
# AUDIT 4 (2026-09-05): the fixes for audit 3, audited
# =========================================================================================

REAL_VERIFY_SNAPSHOTS = [
    # THE ACTUAL SHAPE, read from ~/scout-listener src/index.js runVerify (audit 4 went and
    # looked; audit 3's fix had guessed a vocabulary of verdict words that this producer
    # never writes, which made fixed_pass permanently inert).
    ({"phase": "after", "exit_code": 0, "tail": "5455 passed", "at": "2026-09-05T00:00:00Z"},
     True),
    ({"phase": "after", "exit_code": 1, "tail": "1 failed", "at": "2026-09-05T00:00:00Z"},
     False),
    ({"phase": "before", "exit_code": 2, "tail": "collection error", "at": "x"}, False),
    # audit 3's cases must stay closed
    ({"verified": "not verified"}, False), ({"verified": "pending"}, False),
    ({"passed": "0 of 3"}, False), ({"ok": "timeout"}, False),
    ({"success": "partial"}, False), ({"fix_pr_url": "https://x/1"}, False),
    # audit 4 finding 10: first-present-key-wins let a contradiction through
    ({"status": "completed", "result": "failed"}, False),
    ({"verified": True}, True), ({"status": "passed"}, True),
]


@pytest.mark.parametrize("snapshot,expected", REAL_VERIFY_SNAPSHOTS)
def test_verification_verdict_matches_the_real_producer(snapshot, expected):
    from agent import echo_ticket_worker as ETW
    assert ETW.verification_succeeded(snapshot) is expected, snapshot


def test_an_unreadable_verification_leaves_the_ticket_in_the_poll():
    """Audit 4 finding 1's second half: an unreadable snapshot used to be escalated as
    'verification_not_a_success' AND flipped to status='hold', which removes the ticket from
    find_fixing_tickets forever -- so a fix that later verified could never notify anyone."""
    from agent import echo_ticket_worker as ETW
    assert ETW.verification_is_unreadable({"weird": "shape"}) is True
    assert ETW.verification_is_unreadable({"exit_code": 1}) is False
    assert ETW.verification_is_unreadable({"verified": False}) is False


@pytest.mark.parametrize("text", [
    "make a post that says we are moving to 8am",
    "put a post up saying we are closed for the holiday",
    "could you throw up a post saying we changed our hours",
    "schedule a post telling everyone we are closed friday",
    "draft a post saying our rates go up in january",
    "let everyone know on instagram that we are closed",
    "we need a post announcing our new saturday time",
    "our saturday classes are moving to 8am, can you update the post",
])
def test_asking_us_to_author_a_statement_never_auto_answers(text):
    """Audit 4 finding 3: eight ordinary phrasings walked past the publish guard. Enumerating
    polite request forms was the wrong axis -- what they share is a content verb plus a claim
    marker, i.e. asking us to AUTHOR a statement rather than report what is true."""
    assert not A.may_auto_answer(text)


@pytest.mark.parametrize("text", [
    "is my instagram connected?",
    "did my posts go out this week?",
    "what is on the calendar for october?",
    "is the october schedule loaded?",
    "did anything publish yesterday?",
])
def test_the_publish_guard_did_not_make_the_capability_inert(text):
    assert A.may_auto_answer(text)


def test_the_post_time_gate_runs_the_whole_rule_not_half(monkeypatch):
    """Audit 4 finding 4: the post-time gate re-read only the DENYLIST, so the claim that
    both layers are checked at draft AND post time was false, and a row from a writer that
    predates the marker could post something may_auto_answer rejects."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering_deps(bus, client_armed=True, auto_answer=True))
    # rewrite the ticket's question to one the whole rule rejects, and strip the marker, as a
    # process that predates D54 would have left it
    bus.tickets[d.ticket_id]["raw_text"] = "can you post that we are moving to 8am?"
    row = [m for m in bus.messages_for(d.ticket_id)
           if m["attachments"].get("kind") == A.KIND_ANSWER][0]
    for m in bus.msgs:
        if m["id"] == row["id"]:
            m["attachments"].pop("auto_answer_forbidden", None)
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.message(row["id"])["delivery_status"] == "held"
    assert not any("connected" in c["text"] and c["channel"] == "G0MPIM" for c in calls)


def test_a_routed_answer_never_carries_the_other_bots_voice_doc():
    """Audit 4 finding 5: swapping one token of the system prompt left the appended VOICE
    DOC -- the longer, far more specific identity instruction -- naming the other bot five
    times, including "Wrangler is the LASSO team member who...". The voice a client hears
    must belong to the bot that is actually speaking."""
    from agent.slack_convo import answer_lane as AL
    seen = {}

    def llm(system, user):
        seen["system"] = system
        return "Yes, we can add that."

    AL.answer({"id": "t-1", "raw_text": "q"},
              IG.Identity(IG.CLIENT, "U", account_key="k", gym_id="g", reason="t"),
              [], "can we add our hours to the website?",
              identity=IDS.get("wrangler"), speaks_as="scout",
              fetch_state=lambda t, w: {"site": "live"}, llm=llm)
    system = seen["system"]
    assert system.startswith("You are Scout")
    assert "Wrangler" not in system, \
        "the speaking bot must never be handed another bot's voice doc"
    assert "websites" in system, "the SUBJECT still moves, which is the point of routing"


def test_a_transient_release_failure_leaves_the_row_retappable():
    """Audit 4 finding 6: marking the held row failed on ANY non-delivery burned Blake's
    Release card on a transient fault, re-creating the silent no-op tap."""
    from agent.slack_convo import outreach as OU
    marks = []
    held = {"id": "r1", "ticket_id": "t-1", "delivery_status": "held", "body": "hello",
            "attachments": {"kind": A.KIND_OUTREACH_REQUEST, "identity": "echo"}}
    res = OU.release_approved_outreach(
        "r1",
        {"id": "t-1", "source": "website_tab", "reporter": "o@g.com", "slack_user_id": "U_C",
         "client_id": "g-1", "reporter_verified": True},
        IG.Identity(IG.CLIENT, "U_C", email="o@g.com", account_key="k", gym_id="g-1",
                    reason="t"),
        IDS.get("echo"), get_held_message=lambda mid: held,
        open_group_dm=lambda ids: {"ok": False},          # transient Slack fault
        post_first_message=lambda c, t: {"ok": True, "ts": "1"},
        record_outbound=lambda **kw: {"id": "r2"}, stamp_ticket=lambda *a, **k: None,
        mark_message=lambda mid, status, **kw: marks.append((mid, status)),
        log=lambda *a, **k: None)
    assert res.delivered is False
    assert marks == [], "a transient fault must leave the row held so a retap still works"


def test_the_dead_key_counter_is_actually_read_somewhere(monkeypatch):
    """Audit 4 finding 7: the counter was assigned and read nowhere -- D56's own pattern
    inside the fix for it."""
    from agent.slack_convo import listener_wiring as LW2

    class _App:
        def event(self, *a, **k):
            return lambda f: f

        def action(self, *a, **k):
            return lambda f: f

    llm = lambda text: None                                            # noqa: E731
    llm.failure_state = {"consecutive_failures": 4}
    bus = FakeBus()
    deps = _deps(bus, classify_llm=llm)
    w = LW2.ConvoWiring(_App(), IDS.get("echo"), deps, post=lambda *a, **k: "1",
                        log=lambda *a, **k: None)
    assert w.classifier_health()["classifier_llm"] == "DEAD"
    assert "classifier_llm" in w.health_line()


def test_resolve_refuses_when_the_client_notice_would_be_held(monkeypatch):
    """Audit 4 finding 9: the tap marked the ticket resolved and reported ok even when the
    trust ladder would hold the notice -- Blake taps "Resolved, tell them", the ticket says
    resolved, and the client is never told."""
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.delenv("SLACK_CONVO_ECHO_CLIENT_REPLY", raising=False)
    bus = FakeBus()
    d = A.handle_event(_ev("my posts are not going out"), "k", _deps(bus))
    bus.tickets[d.ticket_id]["slack_channel_id"] = "G0MPIM"
    assert OB.resolve_and_notify(bus, d.ticket_id, approved_by="U06EPUUCL13",
                                 identity=IDS.get("echo"), log=lambda *a: None) is False
    assert bus.tickets[d.ticket_id]["status"] != "resolved"
