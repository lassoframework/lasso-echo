"""The closed set: keys, sentences, polarity, and the one-implementation rule."""
import ast
import os

import pytest

from agent.client_dm_support import readings as R


# ---------------------------------------------------------------------------
# THE COMPLETE LIST OF THINGS ECHO CAN SAY TO A PAYING CLIENT.
#
# A two-way guard (D68): editing a sentence fails this test, deleting one fails it,
# and adding one fails it. That is the point -- the set is small enough to review on
# one screen, and it must never grow without somebody reading it.
# ---------------------------------------------------------------------------
PINNED_SENTENCES = {
    "drive_source_revoked": (
        "Your gym's Google Drive folder is no longer shared with Echo, so the photo "
        "sync cannot read it."),
    "drive_library_usable": "Your media library has {v} file(s) Echo can post.",
    "drive_sync_ran": "I ran your photo sync just now.",
    "drive_files_added": "It added {v} new file(s).",
    "cta_pool_count": (
        "Your brand voice doc has {v} call(s) to action for Echo to rotate through, "
        "which is why your posts are going out without one."),
}

PINNED_ASKS = {
    "cta_needed": (
        "I cannot write one for you, because a call to action has to be your real "
        "booking link, phone number or offer. What would you like your posts to ask "
        "people to do?"),
    "drive_reshare": (
        "Would you be able to re-share that folder with Echo in your portal? Once "
        "it is shared again, we can check whether the sync picks it back up."),
}


def test_every_sentence_echo_can_say_is_pinned():
    speakable = {k: r.say for k, r in R.READINGS.items() if r.speakable()}
    assert speakable == PINNED_SENTENCES, (
        "the set of sentences this capability may auto-send to a client changed. That "
        "is never a refactor: read the new text as a gym owner would, then update the "
        "pin.")


def test_every_ask_is_pinned():
    assert dict(R.ASKS) == PINNED_ASKS


def test_unspeakable_readings_stay_unspeakable():
    """Three readings deliberately have no sentence. Giving one a `say` would let a
    plural/ambiguous state be described with singular copy nobody reviewed, or expose
    an internal data defect to a client."""
    for key in ("drive_active_sources", "drive_identity_split", "voice_doc_present"):
        assert not R.READINGS[key].speakable(), key
        with pytest.raises(R.Ungrounded) as e:
            R.READINGS[key].sentence(True)
        # The MESSAGE matters: a first mutation run found this test green with the
        # no-sentence branch deleted, because the say_when and type checks below it
        # refused for a different reason. Assert the branch that is supposed to fire.
        assert "may not speak it" in str(e.value), key


def test_no_sentence_promises_future_human_action():
    """D52's rule. Every client-facing constant is checked, not just reviewed."""
    banned = ("we will look", "someone will", "the team will", "i'll have someone",
              "will get back to you", "a human will")
    for text in list(PINNED_SENTENCES.values()) + list(PINNED_ASKS.values()):
        low = text.lower()
        for phrase in banned:
            assert phrase not in low, f"{text!r} promises future human action"


# ---------------------------------------------------------------------------
# Polarity: say_when, which replaced the previous build's requires/requires_true/
# requires_false triple. Round 2 and round 3 each found one polarity hole in that
# triple; one field with one meaning cannot have a missing twin.
# ---------------------------------------------------------------------------
def test_a_boolean_sentence_refuses_the_wrong_polarity():
    r = R.DRIVE_SOURCE_REVOKED
    assert r.sentence(True)
    with pytest.raises(R.Ungrounded):
        r.sentence(False)


def test_a_zero_only_sentence_refuses_a_nonzero_count():
    """cta_pool_count's sentence ends 'which is why your posts are going out without
    one'. That is only true at zero. Round 7 shipped the equivalent claim off a gym
    with three working CTAs."""
    assert R.CTA_POOL_COUNT.sentence(0)
    with pytest.raises(R.Ungrounded):
        R.CTA_POOL_COUNT.sentence(3)


def test_a_bool_can_never_satisfy_an_int_reading():
    """bool is a subclass of int; without the explicit check, True would render as a
    file count of 1."""
    with pytest.raises(R.Ungrounded):
        R.DRIVE_LIBRARY_USABLE.sentence(True)
    with pytest.raises(R.Ungrounded):
        R.Snapshot.build("drive_media", "diagnosis", "g",
                         {"drive_library_usable": True})


def test_an_unregistered_key_cannot_enter_a_snapshot():
    with pytest.raises(R.Ungrounded):
        R.Snapshot.build("drive_media", "diagnosis", "g", {"totally_made_up": 1})


def test_a_structured_value_cannot_enter_a_snapshot():
    """A dict or a list could smuggle a whole sentence through a slot."""
    for bad in ({"a": 1}, ["x"], object()):
        with pytest.raises(R.Ungrounded):
            R.Snapshot.build("drive_media", "diagnosis", "g",
                             {"drive_library_usable": bad})


def test_a_snapshot_refuses_an_unknown_stage():
    with pytest.raises(R.Ungrounded):
        R.Snapshot.build("drive_media", "whenever", "g", {})


def test_require_refuses_a_missing_reading_rather_than_defaulting():
    s = R.Snapshot.build("drive_media", "diagnosis", "g", {"drive_library_usable": 3})
    assert s.require("drive_library_usable") == 3
    with pytest.raises(R.Ungrounded):
        s.require("drive_sync_ran")


# ---------------------------------------------------------------------------
# THE PRODUCER CHECK (D68). No test double, no live service.
# ---------------------------------------------------------------------------
def test_every_reading_names_a_producer_that_really_exists():
    assert R.assert_producers_exist() is True


def test_the_producer_check_actually_fails_when_a_producer_is_gone():
    """A two-way guard on the guard: an assertion that cannot fail is not an
    assertion."""
    broken = R.Reading(key="drive_sync_ran", type=bool, measure="x",
                       producer="agent.definitely_not_a_module.nope", say="")
    original = dict(R._REGISTRY)  # noqa: SLF001
    try:
        R._REGISTRY["drive_sync_ran"] = broken  # noqa: SLF001
        with pytest.raises(R.Ungrounded):
            R.assert_producers_exist()
    finally:
        R._REGISTRY.clear()       # noqa: SLF001
        R._REGISTRY.update(original)  # noqa: SLF001


# ---------------------------------------------------------------------------
# THE ONE-IMPLEMENTATION RULE.
#
# Round 7's MAJOR-1 and the residual pass's MINOR-1 were both "this package parses a
# thing another module already parses, and the two disagree about a real document".
# The structural answer is that this package does not parse anything at all.
# ---------------------------------------------------------------------------
_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "agent", "client_dm_support")

# probes.py owns two anchored key-shape patterns (uuid vs account-key slug) which are
# not a re-parse of any other module's document. Nothing else may compile a regex.
_REGEX_ALLOWED = {"probes.py"}


def test_the_parser_allowlist_is_pinned():
    """A TWO-WAY guard on the guard below (D68): assert the allow-list still contains
    only what it must. Otherwise a future edit quietly widens it back and the test that
    depends on it stays green while meaning nothing -- which a mutation run confirmed
    was possible here."""
    assert _REGEX_ALLOWED == {"probes.py"}


def test_package_defines_no_parser_of_its_own():
    offenders = []
    for fname in sorted(os.listdir(_PKG)):
        if not fname.endswith(".py") or fname in _REGEX_ALLOWED:
            continue
        with open(os.path.join(_PKG, fname), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=fname)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                    "compile", "findall", "search", "match", "fullmatch", "sub"):
                base = node.value
                if isinstance(base, ast.Name) and base.id == "re":
                    offenders.append(f"{fname}:{node.lineno} re.{node.attr}")
    assert not offenders, (
        "this package started parsing documents itself: " + "; ".join(offenders) +
        ". A CTA count, a heading, an eligibility rule -- if another module already "
        "reads it, call that module. Two implementations of one reading is how a gym "
        "with three working CTAs was told it had none.")


def test_the_cta_count_comes_from_the_caption_pipelines_own_extractor():
    assert R.CTA_POOL_COUNT.producer == "agent.voice._extract_ctas"


def test_the_library_count_comes_from_the_selectors_own_predicate():
    assert R.DRIVE_LIBRARY_USABLE.producer == "agent.gym_media_selector.is_usable"
