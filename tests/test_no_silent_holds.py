"""D72 (Blake, 2026-09-11): no silent holds, and the false hard line that held Dean.

Ground truth: two real held answer rows in the last 30 days of support_messages (SELECT
only, 2026-09-11), reproduced here verbatim as regression fixtures:

  27728832  CrossFit Reverb (Dean Holcomb), FIXER-authored answer. Held with the label
            "hard line (billing, hours or schedule, injury or liability)" -- but the rules
            that actually fired were the 25-word cap (35 words) and _ANSWER_COMMITS on
            "let us know which one and we will take a look". No floor was involved.
  eb3be7d8  CrossFit Zanshin (Pete Mongeau), Echo-authored answer. Same label; the rules
            were the word cap (44 words) and "I'll flag this for someone on the team".

Both must now pass. Genuine floors (refund, moving a class, medical advice, ad budget) must
still hold -- and every hold must tell the client, put a TEAM card in #fixer, and leave the
ticket open, escalated, with the tier visible. Three paths share one disposition: the Slack
adapter (draft time), the portal bridge (draft time) and the outbox (post time)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.slack_convo import adapter as A  # noqa: E402
from agent.slack_convo import identities as IDS  # noqa: E402
from agent.slack_convo import identity_gate as IG  # noqa: E402
from agent.slack_convo import outbox as OB  # noqa: E402
from tests.test_slack_convo import FakeBus, _deps, _ev, _posted  # noqa: E402

DEAN_Q = ("When reviewing the posts, is there a way to keep the caption but switch out the "
          "picture to a different one?  There are posts where the caption is fine but it "
          "doesn't match the picture.")
DEAN_A = ("Yes, when you are reviewing a scheduled post in Echo, you can replace the image "
          "without changing the caption. Open the post in the content calendar, click the "
          "image thumbnail, and you will see an option to swap it for a different photo from "
          "your gym's connected Drive folder. The caption stays exactly as it is. If you do "
          "not see that option on a specific post, let us know which one and we will take a "
          "look.")
PETE_Q = ("I am seeing sometimes 3 or 4 posts in the same day, sometimes the same caption, "
          "different photo and sometimes same photo same caption.  Yesterday I had 4 posts "
          "on my IG account.  Now my accounts are linked so could that be presenting an "
          "issue?")
PETE_A = ("Got it, you're seeing multiple posts a day on Instagram, sometimes with repeated "
          "captions or photos, and you're wondering if the account linking is causing it. I "
          "can confirm your Instagram connection itself is fine, it's active under "
          "crossfit_zanshin and not expired. I don't have anything in front of me that "
          "explains why duplicate posts went out, so I don't want to guess at the cause. "
          "I'll flag this for someone on the team to dig into the posting activity directly.")

# (ticket, question, answer, authored_by_fixer, expected ok, expected tier)
HELD_ROW_FIXTURES = [
    ("27728832", DEAN_Q, DEAN_A, True, True, ""),
    ("27728832 (as an Echo draft)", DEAN_Q, DEAN_A, False, True, ""),
    ("eb3be7d8", PETE_Q, PETE_A, False, True, ""),
]

GENUINE_FLOORS = [
    ("refund", "is my instagram connected?", "Yes. We will refund last month's charge.",
     A.TOPIC_BILLING),
    ("price", "how much is this going to run us each month", "It is $99 a month.",
     A.TOPIC_BILLING),
    ("move a class", "is my instagram connected?",
     "Yes. I will move your saturday class to 8am.", A.TOPIC_GYM_SCHEDULE),
    ("gym hours", "what time do you open on saturday", "We open at 6.", A.TOPIC_GYM_SCHEDULE),
    ("medical", "is my instagram connected?", "Yes. You should see a doctor about that knee.",
     A.TOPIC_INJURY),
    ("liability", "someone got hurt in the video we posted, are we covered",
     "Your waiver covers it.", A.TOPIC_INJURY),
    ("ad budget", "can you bump our ad budget", "Sure, done.", A.TOPIC_ADS),
    ("pixel", "is my instagram connected?", "Yes, and I'll update the pixel for you.",
     A.TOPIC_ADS),
]


# ---- the rule -----------------------------------------------------------------------------

@pytest.mark.parametrize("ticket,question,answer,fixer,ok,tier", HELD_ROW_FIXTURES)
def test_the_real_held_rows_pass_under_the_new_rule(ticket, question, answer, fixer, ok, tier):
    v = A.auto_answer_verdict(question, answer, grounded_by_fixer=fixer)
    assert v.ok is ok and v.tier == tier, f"{ticket}: {v}"


@pytest.mark.parametrize("name,question,answer,topic", GENUINE_FLOORS)
def test_genuine_commitments_still_hold_as_org_floor(name, question, answer, topic):
    for fixer in (False, True):
        v = A.auto_answer_verdict(question, answer, grounded_by_fixer=fixer)
        assert v.held and v.tier == A.HOLD_TIER_ORG_FLOOR, f"{name} (fixer={fixer}): {v}"
        assert v.topic == topic, f"{name}: topic {v.topic!r}"


def test_deans_exact_rules_are_named_not_a_blanket_label():
    """The two checks that held Dean under the old rule, shown to be the only ones."""
    assert len(DEAN_Q.split()) == 35 > 25, "the old cap was 25 words"
    assert A.answer_commits_to_action("let us know which one and we will do it")
    assert not A.answer_commits_to_action("let us know which one and we will take a look"), \
        "attention is not a promise of action"
    assert not A.forbidden_topic(DEAN_Q) and not A.forbidden_topic(DEAN_A), \
        "no org-floor topic was ever in Dean's exchange"


def test_content_promises_are_needs_review_for_echo_and_pass_for_the_fixer():
    v = A.auto_answer_verdict("is my instagram connected?", "I'll queue it up for monday.")
    assert v.held and v.tier == A.HOLD_TIER_NEEDS_REVIEW
    assert v.rule == "answer_promises_content_action"
    v = A.auto_answer_verdict("is my instagram connected?", "I'll queue it up for monday.",
                              grounded_by_fixer=True)
    assert v.ok, "the FIXER is the reviewer; its product promises are its own to keep"


def test_a_real_world_commitment_holds_even_for_the_fixer():
    v = A.auto_answer_verdict("is my instagram connected?",
                              "Yes. I'll let your members know the class is cancelled.",
                              grounded_by_fixer=True)
    assert v.held and v.tier == A.HOLD_TIER_ORG_FLOOR


def test_pete_promises_human_follow_up_and_dean_does_not():
    assert A.promises_human_follow_up(PETE_A)
    assert not A.promises_human_follow_up(DEAN_A)


def test_hold_card_never_says_awaiting_your_tap():
    bus = FakeBus()
    bus.tickets["t-1"] = {"id": "t-1", "status": "hold"}
    row = A.write_hold_notice(bus, ident_name="echo", tid="t-1", recipient_kind="client",
                              user="U1", account_key="k", kind=A.KIND_ANSWER, body="x",
                              held_message_id="m-1", surface="mpim", why="w")
    body = bus.message(row["id"])["body"]
    assert "HELD REPLY: needs a teammate" in body and "awaiting your tap" not in body


def test_client_notice_names_the_topic_for_a_floor_and_is_honest_otherwise():
    text = A.client_hold_notice_text(A.AnswerVerdict(False, A.HOLD_TIER_ORG_FLOOR,
                                                     "forbidden_topic_in_answer", "refund",
                                                     A.TOPIC_BILLING))
    assert "billing or pricing" in text and "teammate will follow up" in text
    text = A.client_hold_notice_text(A.HOLD_TIER_NEEDS_REVIEW)
    assert "have not sent you an answer" in text


# ---- the Slack adapter path (draft time) -----------------------------------------------

def _armed(monkeypatch):
    monkeypatch.setenv("SLACK_CONVO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_ENABLED", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_CLIENT_REPLY", "true")
    monkeypatch.setenv("SLACK_CONVO_ECHO_AUTO_ANSWER", "true")
    monkeypatch.setenv("SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE", "true")
    monkeypatch.setenv("AGENT_FIXER_CHANNEL_ID", "C_FIXER")
    monkeypatch.setenv("AGENT_PORTAL_ECHO_TICKETS_ENABLED", "true")


# Classified answerable_question by the deterministic rules, and carrying a third party.
THIRD_PARTY_Q = "did our post go out? one of our members could not see it"


def _rows(bus, tid, kind):
    return [m for m in bus.messages_for(tid)
            if m["direction"] == "outbound" and m["attachments"]["kind"] == kind]


def _answering(bus, body, **kw):
    return _deps(bus, answer=lambda t, w, msgs, q: {"body": body, "grounding": {"ig": "ok"}},
                 client_armed=True, auto_answer=True, **kw)


def test_slack_adapter_floor_hold_tells_the_client_and_the_team(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _answering(bus, "Yes. We will refund last month's charge."))
    tid = d.ticket_id
    answers = _rows(bus, tid, A.KIND_ANSWER)
    assert answers and answers[0]["delivery_status"] == "held"
    assert answers[0]["attachments"]["hold_tier"] == A.HOLD_TIER_ORG_FLOOR
    cards = _rows(bus, tid, A.KIND_HOLD_NOTICE)
    assert len(cards) == 1
    assert "needs a teammate" in cards[0]["body"] and "hard line" in cards[0]["body"]
    assert "awaiting your tap" not in cards[0]["body"]
    assert "billing or pricing" in cards[0]["body"]
    notices = [m for m in _rows(bus, tid, A.KIND_TEMPLATE)
               if m["attachments"].get("hold_client_notice")]
    assert len(notices) == 1 and notices[0]["delivery_status"] == "ready", \
        "the client is told, with no tap"
    assert "billing or pricing" in notices[0]["body"]
    t = bus.tickets[tid]
    assert t["status"] == "hold" and t["escalated"] is True and t["hold_tier"] == "routine"
    assert t["verification_after"]["hold"]["tier"] == A.HOLD_TIER_ORG_FLOOR
    assert t["verification_after"]["hold"]["topic"] == A.TOPIC_BILLING
    assert t["classification"] == "answerable_question", \
        "a floor is a person's, not the FIXER's: classification stays so its poll skips it"
    assert t["verification_after"]["ig"] == "ok", "the grounding is kept beside the hold"


def test_slack_adapter_needs_review_hands_the_ticket_to_the_fixer(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    d = A.handle_event(_ev(THIRD_PARTY_Q), "k", _answering(bus, "Yes, it went out tuesday."))
    tid = d.ticket_id
    t = bus.tickets[tid]
    assert t["status"] == "hold" and t["escalated"] is True
    assert t["classification"] is None, \
        "hold + escalated + unclassified is what the FIXER's poll picks up"
    assert t["verification_after"]["hold"]["tier"] == A.HOLD_TIER_NEEDS_REVIEW
    assert t["verification_after"]["hold"]["rule"] == "question_names_a_third_party"
    notices = [m for m in _rows(bus, tid, A.KIND_TEMPLATE)
               if m["attachments"].get("hold_client_notice")]
    assert notices and notices[0]["body"] == A.TEMPLATE_HELD_FOR_REVIEW
    card = _rows(bus, tid, A.KIND_HOLD_NOTICE)[0]["body"]
    assert "Handed to the FIXER" in card and "question_names_a_third_party" in card


def test_slack_adapter_sends_dean_and_pete_and_escalates_petes_promise(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    d = A.handle_event(_ev(DEAN_Q), "k", _answering(bus, DEAN_A))
    assert _rows(bus, d.ticket_id, A.KIND_ANSWER)[0]["delivery_status"] == "ready"
    assert not _rows(bus, d.ticket_id, A.KIND_HOLD_NOTICE)
    assert bus.tickets[d.ticket_id]["status"] == "verification"

    bus = FakeBus()
    d = A.handle_event(_ev(PETE_Q, user="U_PETE"), "k2", _answering(bus, PETE_A))
    assert _rows(bus, d.ticket_id, A.KIND_ANSWER)[0]["delivery_status"] == "ready"
    t = bus.tickets[d.ticket_id]
    assert t["status"] == "hold" and t["escalated"] is True and t["classification"] is None, \
        "'I'll flag this for someone on the team' is made true: the FIXER gets the ticket"


def test_slack_adapter_unarmed_auto_answer_still_tells_the_client(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    d = A.handle_event(_ev(DEAN_Q), "k",
                       _deps(bus, answer=lambda t, w, m, q: {"body": DEAN_A, "grounding": {"x": 1}},
                             client_armed=True, auto_answer=False))
    tid = d.ticket_id
    assert _rows(bus, tid, A.KIND_ANSWER)[0]["attachments"]["hold_tier"] == A.HOLD_TIER_UNARMED
    notices = [m for m in _rows(bus, tid, A.KIND_TEMPLATE)
               if m["attachments"].get("hold_client_notice") == A.HOLD_TIER_UNARMED]
    assert len(notices) == 1
    assert "AUTO_ANSWER is off" in _rows(bus, tid, A.KIND_HOLD_NOTICE)[0]["body"]
    assert bus.tickets[tid]["classification"] == "answerable_question", \
        "a flag hold is not the FIXER's to re-answer"


def test_client_reply_off_means_the_legacy_card_only(monkeypatch):
    """With CLIENT_REPLY off nothing can reach the client, a notice included; the card is the
    only honest surface and no template row is written to sit held beside it."""
    _armed(monkeypatch)
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?"), "k",
                       _deps(bus, answer=lambda t, w, m, q: {"body": "We will refund it.",
                                                             "grounding": {"x": 1}},
                             client_armed=False, auto_answer=False))
    assert _rows(bus, d.ticket_id, A.KIND_HOLD_NOTICE)
    assert not [m for m in _rows(bus, d.ticket_id, A.KIND_TEMPLATE)
                if m["attachments"].get("hold_client_notice")]


def test_staff_answers_are_never_held_by_the_floor(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    d = A.handle_event(_ev("is my instagram connected?", user="U_STAFF", channel="D_STAFF",
                           channel_type="im"), "k",
                       _answering(bus, "We will refund it.", who=IG.STAFF))
    assert _rows(bus, d.ticket_id, A.KIND_ANSWER)[0]["delivery_status"] == "ready"
    assert not _rows(bus, d.ticket_id, A.KIND_HOLD_NOTICE)


# ---- the outbox path (post time) ---------------------------------------------------------

def _fixer_answer_ticket(bus, question, answer, *, fixer=True):
    """A ticket and a READY answer row written the way the FIXER writes them (fixer: true,
    no released_by), so the outbox's post-time re-check is the only gate left."""
    t, _ = bus.get_or_create_ticket(channel_id="G0MPIM", thread_ts="1.0", product="echo",
                                    bot_identity="echo", slack_user_id="U_CLIENT",
                                    identity_kind="client", client_id="g-1",
                                    reporter="dean@x.com", raw_text=question,
                                    classification="answerable_question", request_type=None)
    bus.record_inbound(ticket_id=t["id"], slack_event_id="e1", slack_ts="1.0",
                       author_type="client", author_id="U_CLIENT", body=question)
    bus.set_ticket(t["id"], status="verification", verification_after={"fixer": {"ok": 1}})
    meta = {"identity": "echo", "recipient_kind": "client", "surface": "mpim"}
    if fixer:
        meta["fixer"] = True
    row = bus.record_outbound(ticket_id=t["id"], author_type="echo", body=answer,
                              delivery_status="ready", kind=A.KIND_ANSWER, meta=meta)
    return t["id"], row["id"]


def test_outbox_posts_deans_fixer_answer_with_no_tap(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    tid, mid = _fixer_answer_ticket(bus, DEAN_Q, DEAN_A)
    post, calls = _posted()
    s = OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.message(mid)["delivery_status"] == "posted", bus.message(mid)["attachments"]
    assert s["held"] == 0 and any(c["channel"] == "G0MPIM" and "swap it" in c["text"]
                                  for c in calls)
    assert bus.tickets[tid]["status"] == "resolved"


def test_outbox_floor_hold_on_a_fixer_answer_is_never_silent(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    tid, mid = _fixer_answer_ticket(bus, "is my instagram connected?",
                                    "Yes. We will refund last month's charge too.")
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    held = bus.message(mid)
    assert held["delivery_status"] == "held"
    assert held["attachments"]["held_why"] == "org_floor: forbidden_topic_in_answer"
    assert not any(c["channel"] == "G0MPIM" for c in calls), "the floor never posts"
    card = _rows(bus, tid, A.KIND_HOLD_NOTICE)[0]["body"]
    assert "needs a teammate" in card and "awaiting your tap" not in card
    notices = [m for m in _rows(bus, tid, A.KIND_TEMPLATE)
               if m["attachments"].get("hold_client_notice") == A.HOLD_TIER_ORG_FLOOR]
    assert len(notices) == 1 and notices[0]["delivery_status"] == "ready"
    t = bus.tickets[tid]
    assert t["status"] == "hold" and t["escalated"] is True
    assert t["verification_after"]["hold"]["fixer_authored"] is True
    assert t["classification"] == "answerable_question", "no FIXER re-poll loop on a floor"
    # the notice itself goes out on the next tick, with no tap
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.message(notices[0]["id"])["delivery_status"] == "posted"
    assert any(c["channel"] == "G0MPIM" and "billing or pricing" in c["text"] for c in calls)


def test_outbox_applies_only_the_floor_to_a_fixer_answer(monkeypatch):
    """A FIXER answer to a 44-word question with a content promise: needs_review would
    bounce it back to the FIXER forever; the floor alone applies, and it posts."""
    _armed(monkeypatch)
    bus = FakeBus()
    tid, mid = _fixer_answer_ticket(bus, PETE_Q, "I'll queue a replacement for monday.")
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.message(mid)["delivery_status"] == "posted"


def test_outbox_unarmed_flag_hold_tells_the_client(monkeypatch):
    _armed(monkeypatch)
    monkeypatch.delenv("SLACK_CONVO_AUTO_ANSWER_OVERRIDE_UNSAFE_GATE", raising=False)
    bus = FakeBus()
    tid, mid = _fixer_answer_ticket(bus, DEAN_Q, DEAN_A)
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.message(mid)["delivery_status"] == "held"
    assert bus.message(mid)["attachments"]["hold_tier"] == A.HOLD_TIER_UNARMED
    notices = [m for m in _rows(bus, tid, A.KIND_TEMPLATE)
               if m["attachments"].get("hold_client_notice") == A.HOLD_TIER_UNARMED]
    assert len(notices) == 1
    assert "AUTO_ANSWER is off" in _rows(bus, tid, A.KIND_HOLD_NOTICE)[0]["body"]


def test_a_second_hold_on_the_same_ticket_does_not_re_notify_the_client(monkeypatch):
    _armed(monkeypatch)
    bus = FakeBus()
    tid, mid = _fixer_answer_ticket(bus, "is my instagram connected?",
                                    "Yes. We will refund last month's charge too.")
    row2 = bus.record_outbound(ticket_id=tid, author_type="echo",
                               body="And we will refund the setup fee.", delivery_status="ready",
                               kind=A.KIND_ANSWER,
                               meta={"identity": "echo", "recipient_kind": "client",
                                     "surface": "mpim", "fixer": True})
    post, calls = _posted()
    OB.run_once(bus, post, identity=IDS.get("echo"), log=lambda *a: None)
    assert bus.message(mid)["delivery_status"] == "held"
    assert bus.message(row2["id"])["delivery_status"] == "held"
    notices = [m for m in _rows(bus, tid, A.KIND_TEMPLATE)
               if m["attachments"].get("hold_client_notice") == A.HOLD_TIER_ORG_FLOOR]
    assert len(notices) == 1, "one honest line per ticket per tier"
    assert len(_rows(bus, tid, A.KIND_HOLD_NOTICE)) == 2, "but every held row gets its card"


# ---- the portal bridge path (draft time) -------------------------------------------------

def test_portal_bridge_floor_hold_tells_the_client_and_the_team(monkeypatch):
    from tests.test_portal_escalation_loop import Bus, _ticket, _slack_calls, _answering_worker
    _armed(monkeypatch)
    bus = Bus([_ticket(raw_text="Can we add our group sessions schedule to the website?")])
    seen, open_dm, post = _slack_calls()
    cards = _answering_worker(bus, (seen, open_dm, post),
                              answer_body="Yes, we can add your group sessions schedule.")
    assert seen["posted"] == []
    t = bus.tickets["t-1"]
    assert t["status"] == "hold" and t["escalated"] is True and t["hold_tier"] == "routine"
    assert t["verification_after"]["hold"]["tier"] == A.HOLD_TIER_ORG_FLOOR
    assert t["verification_after"]["hold"]["topic"] == A.TOPIC_GYM_SCHEDULE
    assert cards and "needs a teammate" in cards[0]["why"] and "hard line" in cards[0]["why"]
    notices = [m for m in bus.of_kind(A.KIND_TEMPLATE)
               if (m.get("attachments") or {}).get("hold_client_notice")]
    assert len(notices) == 1 and "hours or class schedule" in notices[0]["body"]
    assert notices[0]["delivery_status"] == "ready"


def test_portal_bridge_needs_review_clears_classification_for_the_fixer(monkeypatch):
    from tests.test_portal_escalation_loop import Bus, _ticket, _slack_calls, _answering_worker
    _armed(monkeypatch)
    bus = Bus([_ticket(raw_text=THIRD_PARTY_Q)])
    seen, open_dm, post = _slack_calls()
    _answering_worker(bus, (seen, open_dm, post), answer_body="Yes, it went out tuesday.")
    t = bus.tickets["t-1"]
    assert t["status"] == "hold" and t["escalated"] is True and t["classification"] is None
