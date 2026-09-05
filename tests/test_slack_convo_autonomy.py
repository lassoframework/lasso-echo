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
    assert t["verification_after"]["answered_with_product"] == "websites"


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
    return [m for m in bus.messages_for(tid)
            if m["attachments"].get("kind") == A.KIND_RECEIPT]


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


def test_portal_bridge_classifies_with_the_classifier_callable_not_the_answer_lane():
    """The regression, at the call site: _intake_one must hand classify() the callable that
    takes one argument, and must keep handing the answer lane its own."""
    import inspect
    from agent import echo_ticket_worker as ETW
    src = inspect.getsource(ETW._intake_one)
    assert "identity_product=ident.product, llm=classify_llm)" in src
    assert "classify_llm" in inspect.signature(ETW.intake_pass).parameters


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
