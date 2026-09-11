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

These tests pin the wrong-output family as code_fix, and pin the two guards that keep
it honest: an owner ASKING about repeats still reaches the answer lane, and a sentence
with no Echo-domain noun still escalates (RT-M2).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.slack_convo import classifier as c  # noqa: E402


def _classify(text, **kw):
    kw.setdefault("has_open_ticket", False)
    kw.setdefault("identity_product", "echo")
    return c.classify(text, **kw)


# ---- the ticket that started this ---------------------------------------------------
def test_johns_exact_message_is_a_code_fix():
    assert _classify("9/14-9/16 are still repeat images") == c.CODE_FIX


def test_johns_first_report_shape_is_a_code_fix_too():
    assert _classify("its posting the same photos over and over while my videos "
                     "just sit there") == c.CODE_FIX


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
    "the same shot showed up several weeks in a row",
])
def test_a_wrong_output_report_reaches_the_fixer(text):
    assert _classify(text) == c.CODE_FIX, f"escalated instead of dispatching: {text!r}"


# ---- guard 1: a QUESTION about repeats is not a bug report ---------------------------
@pytest.mark.parametrize("text", [
    "how often do posts repeat?",
    "why do my images repeat?",
    "what happens if a photo repeats?",
    "can you repeat that?",
    "do you ever reuse photos?",
])
def test_asking_about_repeats_still_reaches_the_answer_lane(text):
    assert _classify(text) == c.QUESTION, f"a question was dispatched as a fix: {text!r}"


# ---- guard 2: RT-M2 still holds -- breakage alone is not enough ----------------------
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


# ---- the domain nouns a gym owner actually types -------------------------------------
@pytest.mark.parametrize("noun", ["image", "images", "picture", "pictures", "pic",
                                  "pics", "clip", "clips", "footage", "shot", "shots"])
def test_the_owner_words_for_media_are_domain_nouns(noun):
    assert c._DOMAIN_RE.search(f"the {noun} is wrong"), \
        f"{noun!r} is a word real clients type and must count as an Echo noun"


# ---- ordering: an OPEN ticket and the existing rules keep precedence -----------------
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
