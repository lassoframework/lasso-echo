"""Why the FIXER never ran on John's ticket (Tough Temple, 2026-09-11).

John Weeks reported the SAME defect twice in Slack -- "9/14-9/16 are still repeat
images" -- and both times a human carried the whole ticket. The fixer lane never saw
it, and the reason is one line in the classifier: `code_fix` requires a _BREAKAGE_RE
match, and every pattern in _BREAKAGE_RE describes something NOT HAPPENING (not
posting, not going out, broken, error, failed, 404).

Echo's most common real client complaint is the opposite shape: the machine IS running
and it is producing the WRONG THING. There was no vocabulary for that at all, so every
duplicate-media report classified ESCALATE. (John's sentence also failed _DOMAIN_RE:
"photo" and "video" were domain nouns, "image" was not.)

FLAGGED OFF BY DEFAULT (AGENT_SLACK_REPEAT_CODE_FIX). An independent review measured
the first cut of this rule firing `code_fix` on 14 of 23 realistic BENIGN client
sentences, and a false code_fix is not free: the adapter sets the ticket to 'triage' so
every later message from that owner classifies FOLLOW_UP until a person closes it, and
Echo auto-replies "I read that as something not working on our side" -- to, among
others, a thank-you note. So this file pins three things, not one:
  1. real reports reach the fixer when the flag is ON,
  2. benign chatter CONTAINING repeat words and an Echo noun does NOT,
  3. the flag OFF is byte-for-byte the old behavior.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.slack_convo import classifier as c  # noqa: E402


def _classify(text, **kw):
    kw.setdefault("has_open_ticket", False)
    kw.setdefault("identity_product", "echo")
    kw.setdefault("repeat_report_enabled", True)
    return c.classify(text, **kw)


# ---- the ticket that started this ---------------------------------------------------
def test_johns_exact_message_is_a_code_fix():
    assert _classify("9/14-9/16 are still repeat images") == c.CODE_FIX


def test_johns_first_report_shape_is_a_code_fix_too():
    assert _classify("its posting the same photos over and over while my videos "
                     "just sit there") == c.CODE_FIX


# ---- the flag is the one switch -----------------------------------------------------
def test_the_flag_off_is_the_old_behavior():
    """OFF must be byte-for-byte what the classifier did before this rule existed."""
    assert _classify("9/14-9/16 are still repeat images",
                     repeat_report_enabled=False) is c.ESCALATE
    assert c.classify("duplicate images on my calendar", has_open_ticket=False,
                      identity_product="echo") is c.ESCALATE, \
        "the default must be OFF"


def test_the_flag_cannot_be_turned_on_by_a_brain_hint_or_the_llm():
    """CANCEL_POST's own invariant, applied here: the flag is the ONE switch, so a
    learned phrase or a model guess can never mint this label while it is off."""
    assert _classify("duplicate images on my calendar", repeat_report_enabled=False,
                     llm=lambda t: c.CODE_FIX) == c.CODE_FIX, \
        "the LLM may still return code_fix on its own merits"
    # ...but the repeat RULE itself did not fire:
    assert c.is_repeat_report("duplicate images on my calendar") is True
    assert _classify("duplicate images on my calendar",
                     repeat_report_enabled=False) is c.ESCALATE


# ---- the wrong-output family --------------------------------------------------------
@pytest.mark.parametrize("text", [
    "the same photo keeps showing up on different days",
    "my posts are repeating the same picture",
    "duplicate images on my calendar",
    "the images are repeating again",
    "same photo three days in a row",
    "its using the same image over and over",
    "you used the same clip twice in a row",
    "my reels are recycled from last month",
    "the calendar is reusing photos",
    "there are duplicate posts this week",
    "echo reused the same video twice",
    "the same footage showed up several weeks in a row",
])
def test_a_wrong_output_report_reaches_the_fixer(text):
    assert _classify(text) == c.CODE_FIX, f"escalated instead of dispatching: {text!r}"


# ---- THE FALSE-POSITIVE SURFACE -----------------------------------------------------
# Every sentence below contains a repeat word AND an Echo-domain noun -- the exact
# surface the first cut of this rule got wrong, and which had no test at all.
@pytest.mark.parametrize("text", [
    # 1. an instruction or a request, not a complaint
    "Can we repeat that promo post next month? it crushed",
    "Please reuse the photo from last Tuesday, our members loved it",
    "Do NOT reuse the picture of the guy in the red shirt, he left the gym",
    "Please duplicate my Instagram approach on Facebook too",
    "Let's repeat the same caption on the reel",
    # 2. the CLIENT is the actor
    "I duplicated the calendar in the portal by accident, no big deal",
    "I recycled the caption from the Memorial Day post, hope that's ok",
    "We reused the logo on our new shirts",
    "We repeat the same class schedule every week so the posts can too",
    "I keep repeating myself to my coaches about the front desk photos lol",
    "we reuse our best photos every quarter",
    # 3. approval, thanks, or a business metric
    "Thanks for fixing the duplicate images last week",
    "Love the consistency, posting the same message over and over is working",
    "Same image different caption is fine by me",
    "Repeat customers are up 12% since we started posting",
    "Heads up, we're closed Monday so the same post twice in a row would be weird",
    "we repeat that workout on purpose so the photo can too",
    # round 3's eleven survivors
    "Our anniversary promo repeats every year so just recycle last September's caption.",
    "If a clip gets reused on the story that is totally fine with us.",
    "It is fine to reuse photos from the summer challenge folder.",
    "I think I uploaded the same photos twice to the Drive folder, sorry about that.",
    "My front desk girl duplicated a bunch of images in the portal yesterday.",
    "I accidentally approved the same post twice in the dashboard.",
    "Our coach keeps sending me duplicate videos for the reels.",
    "Appreciate you sorting out the duplicate photos so fast.",
    "The repeat images issue looks resolved on my calendar now, nice work.",
    "Honestly the same picture twice did not bother me at all.",
    "Our schedule repeats weekly for the 5am class, not the posts.",
    "my trainer uploaded duplicate pics again",
    "our program repeats every 8 weeks so the captions can repeat too",
])
def test_benign_chatter_with_an_echo_noun_is_never_a_fixer_request(text):
    got = _classify(text)
    assert got != c.CODE_FIX, (
        f"dispatched a fixer request on benign chatter: {text!r} -> {got}. The owner "
        "would get 'I read that as something not working on our side' and a triaged "
        "ticket that swallows their next message as a FOLLOW_UP.")


# ---- guard: a QUESTION about repeats is not a bug report ----------------------------
@pytest.mark.parametrize("text", [
    "how often do posts repeat?",
    "why do my images repeat?",
    "what happens if a photo repeats?",
    "can you repeat that?",
    "do you ever reuse photos?",
])
def test_asking_about_repeats_still_reaches_the_answer_lane(text):
    assert _classify(text) == c.QUESTION, f"a question was dispatched as a fix: {text!r}"


# ---- guard: RT-M2 still holds -- a repeat word alone is not enough ------------------
@pytest.mark.parametrize("text", [
    "same old same old",
    "I have a duplicate set of gym keys",
    "we run the same workout three days in a row on purpose",
    "my duplicate membership charge",
])
def test_no_echo_domain_noun_still_escalates(text):
    assert _classify(text) != c.CODE_FIX, f"fired with no Echo noun: {text!r}"


def test_chatter_is_never_a_ticket():
    assert _classify("thanks, that all looks great") != c.CODE_FIX


# ---- the media nouns are SEPARATE from _DOMAIN_RE ------------------------------------
@pytest.mark.parametrize("noun", ["image", "images", "picture", "pictures", "pic",
                                  "pics", "clip", "clips", "footage"])
def test_the_owner_words_for_media_widen_the_repeat_rule(noun):
    assert c._MEDIA_NOUN_RE.search(f"the {noun} is wrong"), \
        f"{noun!r} is a word real clients type and must reach the repeat rule"


@pytest.mark.parametrize("noun", ["image", "images", "picture", "pic", "clip",
                                  "footage"])
def test_the_media_nouns_are_NOT_in_domain_re(noun):
    """_DOMAIN_RE also gates _BREAKAGE_RE, which is NOT behind the repeat flag. Putting
    these there changed the DEFAULT path: "the image is broken" and "the clip is broken"
    flipped ESCALATE -> code_fix, and "is the image broken?" flipped QUESTION ->
    code_fix, while every doc claimed the flag OFF was byte-for-byte the old classifier
    (independent audit round 3, CRITICAL)."""
    assert not c._DOMAIN_RE.search(f"the {noun} is broken"), \
        f"{noun!r} in _DOMAIN_RE silently widens the unflagged breakage rule"


@pytest.mark.parametrize("text", ["nice shot on that reel", "worth a shot",
                                  "give it a shot"])
def test_bare_shot_is_in_neither_noun_set(text):
    """Same reason bare "site" is excluded: the figurative use is the common one."""
    stripped = text.replace("reel", "thing")
    assert not c._DOMAIN_RE.search(stripped) and not c._MEDIA_NOUN_RE.search(stripped)


@pytest.mark.parametrize("text", [
    "the image is broken", "the images are not loading", "the clip is broken",
    "the footage didn't go out", "the clips are not showing up",
    "images stopped working", "the pic never went out this morning",
    "is the image broken?",
])
def test_the_flag_off_does_not_change_a_breakage_sentence(text):
    """The differential the round-2 test could not catch: it only exercised repeat-family
    sentences, so eight breakage/question changes on the DEFAULT path went unnoticed."""
    assert c.classify(text, has_open_ticket=False,
                      identity_product="echo") != c.CODE_FIX, \
        f"flag OFF dispatched a fixer on {text!r}; origin/main escalated it"


# ---- ordering: the existing rules keep precedence -----------------------------------
def test_an_open_ticket_still_wins_over_the_new_rule():
    assert _classify("9/14-9/16 are still repeat images",
                     has_open_ticket=True) == c.FOLLOW_UP


def test_breakage_ordering_is_untouched():
    """_BREAKAGE_RE is still checked BEFORE _QUESTION_RE (unchanged behavior); only the
    new wrong-output rule sits after it."""
    assert _classify("is my calendar broken?") == c.CODE_FIX


def test_a_dead_machine_report_is_unchanged():
    assert _classify("my posts are not going out") == c.CODE_FIX
    assert _classify("the calendar is broken") == c.CODE_FIX


def test_is_repeat_report_is_pure_and_handles_empty():
    assert c.is_repeat_report("") is False
    assert c.is_repeat_report(None) is False
