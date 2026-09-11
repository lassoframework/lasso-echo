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
    # round 4: HEDGED and POLITE reports. A polite opener is not a verdict on the
    # sentence, and a message can be an apology AND a report -- "thanks for the quick
    # turnaround, but the repeat images are still there" is exactly how John's second
    # report would read. Vetoing on the first two words lost all of these.
    "heads up, the same picture is on three posts next week",
    "fyi the duplicate images are back on the calendar",
    "thanks for the quick turnaround, but the repeat images are still there",
    "my manager noticed the duplicate images on the calendar",
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
    # round 4's five held-out false positives
    "we run the same promo every september so feel free to repeat those captions",
    "our saturday classes duplicate the thursday programming if that changes the posts",
    "would it be weird to repeat last month's transformation photo on the anniversary post",
    "we are going to repeat the 6 week challenge, so keep the same photos for continuity",
    "the duplicate charge on my card is sorted, unrelated to the calendar",
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


# ---- round 4: the flag OFF is provably origin/main ----------------------------------
def test_flag_off_matches_origin_main_across_the_whole_rule_surface():
    """The round-3 test only exercised repeat-family sentences, which is why eight
    breakage/question changes on the DEFAULT path went unnoticed. This compares the
    branch against origin/main's actual module over every rule family, all three
    identities, and both ticket states."""
    import itertools
    import subprocess
    import types
    src = subprocess.run(["git", "show", "origin/main:agent/slack_convo/classifier.py"],
                         capture_output=True, text=True).stdout
    if not src.strip():
        pytest.skip("origin/main not available in this checkout")
    main = types.ModuleType("main_classifier")
    exec(compile(src, "main_classifier", "exec"), main.__dict__)  # noqa: S102
    sents = [
        "the image is broken", "the images are not loading", "the clip is broken",
        "the footage didn't go out", "is the image broken?", "my pic is broken",
        "my posts are not going out", "the calendar is broken", "nothing posted today",
        "9/14-9/16 are still repeat images", "duplicate images on my calendar",
        "how often do posts repeat?", "thanks that looks great", "hey", "worth a shot",
        "the website is showing the wrong hours", "pause the ad", "cancel my post today",
        "can you scale the budget", "my instagram won't connect", "nice shot",
        "heads up, the same picture is on three posts next week",
        "the duplicate charge on my card is sorted", "why do my images repeat?",
    ]
    diffs = []
    for text, prod, tick in itertools.product(sents, ("echo", "ranger", "wrangler"),
                                              (False, True)):
        a = main.classify(text, has_open_ticket=tick, identity_product=prod)
        b = c.classify(text, has_open_ticket=tick, identity_product=prod)
        if a != b:
            diffs.append((prod, tick, text, a, b))
    assert not diffs, f"flag OFF diverged from origin/main: {diffs}"


def test_a_polite_opener_is_stripped_not_treated_as_a_verdict():
    assert c.is_repeat_report("heads up, the duplicate images are back") is True
    assert c.is_repeat_report("fyi the calendar is reusing photos") is True


def test_a_complaint_after_a_thank_you_still_reports():
    """One benign clause must not bury a real complaint -- and this is exactly how a
    second report from a patient client reads."""
    assert c.is_repeat_report(
        "thanks for the quick turnaround, but the repeat images are still there") is True
    assert c.is_repeat_report("thanks for fixing the duplicate images last week") is False


# ---- round 5: requests, preferences, and severed justifications ---------------------
@pytest.mark.parametrize("text", [
    # a justification must not be severed from its statement by " so " / ". "
    "The programming repeats every 8 weeks so the same footage works.",
    "The class schedule repeats so the same photo is fine.",
    "Great work this month. The same photo on both posts was my call.",
    "our schedule repeats so the captions repeat too",
    "the promo repeats. the same image is intentional",
    # a REQUEST or a PREFERENCE is not a bug report
    "we need you to reuse the same photo for the promo",
    "we would like the same picture on both posts",
    "I want the duplicate pictures kept as is",
    "our marketing guy requested duplicate images",
    "we asked for the same image on both platforms",
    "I'd like you to repeat that photo next week",
    "we prefer the same picture across both posts",
    "we expect the same clip on the story and the feed",
    "hoping you can reuse the duplicate photos",
    "we need the duplicate images left alone",
])
def test_a_request_or_a_justification_is_not_a_bug_report(text):
    got = _classify(text)
    assert got != c.CODE_FIX, (
        f"dispatched a fixer on {text!r} -> {got}. The owner would be told 'I read that "
        "as something not working on our side' about media they asked for on purpose.")


@pytest.mark.parametrize("text", [
    "WHY IS THE SAME PHOTO ON MY CALENDAR THREE TIMES",
    "the same photo is on 9/14 and 9/15 and 9/16",
])
def test_real_reports_in_awkward_registers_are_not_dropped_silently(text):
    """CAPS, emoji and bare-date shapes must not ESCAPE the system. A question-shaped
    complaint is allowed to reach the ANSWER lane (round 2's deliberate ordering), but
    nothing here may simply vanish."""
    assert _classify(text) in (c.CODE_FIX, c.QUESTION), f"{text!r} fell through"


def test_the_clause_splitter_only_splits_on_contrast():
    """Splitting on ' so ' and '. ' severed justifications from their statements and
    manufactured false reports."""
    assert c.is_repeat_report("the class schedule repeats so the same photo is fine") is False
    assert c.is_repeat_report(
        "thanks for the turnaround, but the duplicate images are back") is True


# ---- round 6: an INSTRUCTION is not a report ----------------------------------------
# The veto list could not keep up with bare imperatives (16 of 20 dispatched a fixer).
# Enumerating benign shapes is a losing game -- a leading-verb veto also eats "Repeat
# photos again this week on the posts", which IS a complaint. So the test is inverted:
# a clause must carry a POSITIVE report signal (someone else doing it, the client's own
# book as subject, a persistence marker, a numeric date, a run of days).
@pytest.mark.parametrize("text", [
    "Put the duplicate graphic on Thursday's post too.",
    "Stick with the same caption on the story and the feed post.",
    "Keep the duplicate images, we like them.",
    "Go with the same photo on both platforms this week, it performed well.",
    "Use that same picture of the squat rack again for the Friday reel.",
    "Reuse whatever photo you like from the open gym folder for the calendar.",
    "Run the same image on Monday and Tuesday posts.",
    "Leave the duplicate photos where they are.",
    "Swap in a duplicate of the hero picture for the story.",
    "Post the same clip again on Friday.",
    "Just repeat last week's photos on the calendar.",
    "Send me the duplicate images from the folder.",
    "Load the same footage onto both posts.",
    "Bring back the same photo for the anniversary post.",
    "My coach emailed duplicate clips for the reel.",
    "I scheduled the same picture on two days by mistake.",
    "I dropped duplicate images into the Drive folder last night by mistake.",
    # round 7: an instruction that names the owner's OWN book. The round-6 corpus said
    # "Thursday's post" / "the story" / "both platforms" and never "my calendar", so it
    # never exercised the one signal family an instruction CAN carry. 15 of 18 of these
    # dispatched a fixer request.
    "Use the same image on my posts for the rest of the month",
    "Leave the duplicate photos on my calendar",
    "Run the same clip on our reels again",
    "Our photographer sent duplicate images, they are on my calendar now",
    "Put the same photo on my story and my feed",
    "Keep the duplicate images on our calendar please",
    "Just repeat last month's photos on my calendar",
    "Swap in a duplicate on my posts for Friday",
    "Load the same footage onto our reels",
    "Stick with the same picture on my posts",
    "Send the duplicate images to my calendar",
    "Add the same clip to our stories",
    "Post the same photo on my feed again",
    "Go ahead and reuse those photos on my calendar",
    "Drop the duplicate pictures onto our posts",
    "Set the same image on my posts for both days",
    "Bring back the duplicate photo on our feed",
    "Schedule the same picture on my posts twice",
])
def test_a_bare_imperative_is_never_a_fixer_request(text):
    got = _classify(text)
    assert got != c.CODE_FIX, (
        f"dispatched a fixer on an instruction: {text!r} -> {got}. The owner would be "
        "told 'I read that as something not working on our side' about media they "
        "asked for, and their next message would be swallowed as a FOLLOW_UP.")


@pytest.mark.parametrize("text", [
    "you scheduled the same image twice",
    "it keeps putting the same picture on my feed",
    "the calendar is reusing photos",
    "the images are repeating again",
    "heads up, the duplicate images are back",
    "thanks for the turnaround, but the duplicate images are back",
    "the same photo is on 9/14 and 9/15 and 9/16",
    "same photo three days in a row",
    # round 7 recall: the possessive is WEAK evidence, not absent evidence -- a report
    # that carries only it must still get through when it does not read as a command.
    "duplicate images on my calendar",
    "my calendar has the same photo twice",
    "repeat images on my calendar again",
    "there's duplicate images",
    "duplicate pictures went out",
    "these are duplicate images again",
])
def test_a_report_signal_carries_a_real_complaint_through(text):
    assert _classify(text) == c.CODE_FIX, f"a genuine report escalated: {text!r}"


def test_a_weekday_alone_is_not_a_report_signal():
    """An instruction schedules with weekday names constantly; a complaint does not
    rely on them. Treating them as evidence let six imperatives through."""
    assert not c._REPORT_SIGNAL_RE.search("on Friday")
    assert not c._REPORT_SIGNAL_RE.search("this week")
    assert c._REPORT_SIGNAL_RE.search("on 9/14")


def test_the_possessive_alone_is_weak_evidence_not_proof():
    """Round 7's MAJOR. "my calendar" reads like a report AND an instruction names it
    just as readily, so it is necessary-but-not-sufficient: only a clause that does NOT
    open with a bare imperative may rely on it alone."""
    assert c._reads_as_a_report("duplicate images on my calendar") is True
    assert c._reads_as_a_report("leave the duplicate photos on my calendar") is False
    # a STRONG signal is unaffected by the imperative shape
    assert c._reads_as_a_report("keep showing up on my calendar") is True


def test_a_repeat_word_used_as_an_adjective_is_not_an_imperative():
    """"duplicate images" opens a report; "duplicate the images" opens an instruction.
    Treating every leading `duplicate` as a verb lost John's own phrasing."""
    assert not c._LEADING_IMPERATIVE_RE.match("duplicate images on my calendar")
    assert not c._LEADING_IMPERATIVE_RE.match("repeat photos on my posts")
    assert c._LEADING_IMPERATIVE_RE.match("just repeat last month's photos")
    assert c._LEADING_IMPERATIVE_RE.match("duplicate the hero shot")


def test_a_polite_opener_is_still_stripped():
    """Pinned because a mutation that deleted the strip survived the round-6 suite."""
    assert c._POLITE_OPENER_RE.sub("", "heads up, the duplicate images are back",
                                   count=1).strip() == "the duplicate images are back"
    assert c.is_repeat_report("heads up, the duplicate images are back") is True
