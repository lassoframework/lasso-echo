"""
tests/test_brain_feeds_captions.py — AGENT_BRAIN_FEEDS_CAPTIONS.

Blake, 2026-09-06: "i want echo to build a brain for all gyms it digest weekly
the best post and then is able to digest all the post for the week see trends on
what is working and use that + the gyms brain to create the best post."

This file covers the CONSUMER half. agent/cross_gym_guidance.py shipped built,
tested and CALLED BY NOTHING — the "built but not wired" pattern D68 catalogues
four instances of, and the whole point of this change is that it is now wired
into agent/drafter.py's SB7 prompt assembly.

The rulings pinned here, one test per rail:

  1. FLAG OFF IS BYTE FOR BYTE TODAY'S PROMPT. Not "similar", not "equivalent":
     the exact same string, asserted by building the prompt twice and comparing.
  2. THE CONSUMER EXISTS, BY NAME. A static assertion that drafter really calls
     cross_gym_guidance, with no test double, so the module cannot quietly become
     unconsumed again.
  3. FORM ONLY, AND BELOW EVERYTHING. The hints sit below the brand voice doc and
     below the approved source, and are labeled as never a fact and never an
     override.
  4. NO CONTENT CAN CROSS. A rollup carrying caption text, a stat, an offer or a
     handle in its guidance produces NOTHING in the prompt: the item is dropped
     by the whitelist on the read, not sanitised into a hint.
  5. TWO FLAGS. AGENT_BRAIN_FEEDS_CAPTIONS on its own does nothing while
     AGENT_CROSS_GYM_BRAIN is off.

Offline: no LLM call is ever made (the caption call is stubbed and the prompt it
was handed is captured), no network, no database.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, drafter  # noqa: E402
from agent.accounts import Account, Platform  # noqa: E402
from agent.jobs import cross_gym_brain as brain  # noqa: E402
from agent.voice import VoiceDoc  # noqa: E402


def _voice():
    return VoiceDoc(raw="BRAND BIBLE BODY. We help busy parents train.",
                    hashtags=["#GetFit"], ctas=["Book a free intro."])


def _acct():
    return Account(key="gym_beta_ig", display_name="Gym Beta",
                   platform=Platform.INSTAGRAM, token_env="T", target_id_env="TID")


class _Creative:
    client_note = "APPROVED SOURCE TEXT: we run a 6 week beginner program."
    stem = "photo_01"
    path = "photo_01.jpg"


class _Rollup:
    """A permissive fake store: it hands back whatever rollup it was given and
    refuses nothing, so a whitelist test can only pass because the code under
    test dropped the item — never because the fake did."""

    def __init__(self, guidance_items):
        self.row = {"run_at": "2026-09-06T02:00:00+00:00",
                    "window_start": "2026-06-08", "window_end": "2026-09-06",
                    "window_days": 90, "guidance": list(guidance_items)}

    def latest_rollup(self):
        return dict(self.row)


_CLEAN_ITEM = {"lever": "hook_family", "value": "question",
               "format_stratum": "feed", "direction": "favor",
               "effect_size": 0.62, "n": 24, "gyms": 3, "q_value": 0.004,
               "source": brain.SOURCE_LEVERS}


@pytest.fixture
def prompts(monkeypatch):
    """Capture the exact user prompt the LLM would have been handed."""
    seen = []

    def _fake_llm(system, user):
        seen.append(user)
        return "A caption that mentions nothing numeric at all."

    monkeypatch.setattr(drafter, "_call_llm_caption", _fake_llm)
    return seen


def _build(monkeypatch, prompts, *, feeds=False, cross_gym=False, items=None,
           store=None):
    """Build one caption and return the prompt the generator composed."""
    monkeypatch.setenv("AGENT_BRAIN_FEEDS_CAPTIONS", "true" if feeds else "false")
    monkeypatch.setenv("AGENT_CROSS_GYM_BRAIN", "true" if cross_gym else "false")
    if items is not None or store is not None:
        from agent import cross_gym_guidance
        fake = store if store is not None else _Rollup(items or [])
        monkeypatch.setattr(brain, "SupabaseBrainStore", lambda *a, **k: fake)
        assert cross_gym_guidance.brain is brain
    before = len(prompts)
    drafter.StoryBrandGenerator().build(_voice(), _Creative(), account=_acct())
    assert len(prompts) > before, "no prompt was composed"
    return prompts[-1]


# ---------------------------------------------------------------------------
# 1. the flag
# ---------------------------------------------------------------------------

def test_flag_defaults_off():
    assert config.brain_feeds_captions_enabled() is False


def test_flag_off_is_byte_for_byte_todays_prompt(monkeypatch, prompts):
    """THE HEADLINE RULING. With the flag off the prompt is the EXACT string it
    was before this change — compared byte for byte against a build made with the
    consumer removed entirely, not merely eyeballed for similarity."""
    with_flag_off = _build(monkeypatch, prompts, feeds=False, cross_gym=True,
                           items=[_CLEAN_ITEM])

    # the same build with the consumer physically absent from the class
    monkeypatch.setattr(drafter.StoryBrandGenerator, "_cross_gym_form_block",
                        staticmethod(lambda account: ""))
    without_consumer = _build(monkeypatch, prompts, feeds=False, cross_gym=True,
                              items=[_CLEAN_ITEM])
    assert with_flag_off == without_consumer


def test_the_brain_flag_alone_is_not_enough(monkeypatch, prompts):
    """AGENT_BRAIN_FEEDS_CAPTIONS on, AGENT_CROSS_GYM_BRAIN off: the read side
    self-gates, so still nothing reaches the prompt. BOTH taps are required."""
    armed_both = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
                        items=[_CLEAN_ITEM])
    only_feeds = _build(monkeypatch, prompts, feeds=True, cross_gym=False,
                        items=[_CLEAN_ITEM])
    assert "FLEET FORM SIGNALS" in armed_both
    assert "FLEET FORM SIGNALS" not in only_feeds


def test_armed_but_nothing_learned_is_also_todays_prompt(monkeypatch, prompts):
    """Both flags armed and an EMPTY rollup guidance list (the fleet's honest
    output whenever nothing clears the bar): still byte for byte today's prompt.
    An empty result must add no block, not an empty block."""
    off = _build(monkeypatch, prompts, feeds=False, cross_gym=True, items=[])
    armed_but_empty = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
                             items=[])
    assert armed_but_empty == off


# ---------------------------------------------------------------------------
# 2. the consumer exists, by name
# ---------------------------------------------------------------------------

def test_the_drafter_really_calls_cross_gym_guidance():
    """WIRING ASSERTION, no test double. cross_gym_guidance shipped "built and
    tested but unconsumed"; if this assertion ever fails the module has gone back
    to being dead code while every other test in the repo still passes."""
    import inspect

    src = inspect.getsource(drafter.StoryBrandGenerator._cross_gym_form_block)
    assert "from . import cross_gym_guidance" in src
    assert "cross_gym_guidance.prompt_lines(key)" in src

    build_src = inspect.getsource(drafter.StoryBrandGenerator.build)
    assert "self._cross_gym_form_block(account)" in build_src
    assert "{cross_gym_block}" in build_src


def test_the_stale_not_wired_docstring_is_gone():
    """agent/cross_gym_guidance.py's module docstring used to say the module was
    "deliberately NOT wired". A comment that describes the opposite of reality is
    how the next session concludes the lane is dead and rebuilds it."""
    from agent import cross_gym_guidance
    doc = cross_gym_guidance.__doc__ or ""
    assert "deliberately NOT wired" not in doc
    assert "_cross_gym_form_block" in doc


# ---------------------------------------------------------------------------
# 3. form only, and below everything
# ---------------------------------------------------------------------------

def test_hints_sit_below_the_brand_bible_and_the_approved_source(monkeypatch,
                                                                 prompts):
    """ORDER IS THE GUARANTEE. The brand voice doc and the approved source come
    FIRST; fleet form hints come after everything, so they read as shape guidance
    and can never be mistaken for the thing to say."""
    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
               items=[_CLEAN_ITEM])
    bible = p.index("BRAND VOICE DOC:")
    source = p.index("CLIENT NOTE ON THIS POST:")
    hints = p.index("FLEET FORM SIGNALS")
    assert bible < source < hints
    # and below this gym's OWN learned preferences too, when there are any
    assert p.index("BRAND BIBLE BODY.") < hints
    assert p.index("APPROVED SOURCE TEXT:") < hints


def test_the_block_labels_itself_as_never_a_fact(monkeypatch, prompts):
    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
               items=[_CLEAN_ITEM])
    block = p.split("FLEET FORM SIGNALS", 1)[1]
    for phrase in ("shape guidance ONLY", "NO facts", "NO offers",
                   "never override"):
        assert phrase in block
    assert "open with one real question the reader is asking themselves" in block


# ---------------------------------------------------------------------------
# 4. no content can cross
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("poison", [
    # a caption fragment as a lever value
    {"lever": "hook_family", "value": "Sarah lost 42 lbs in 12 weeks",
     "format_stratum": "feed", "direction": "favor", "effect_size": 2.0,
     "n": 9, "gyms": 3, "q_value": 0.001},
    # an offer / price
    {"lever": "pillar", "value": "$99 for 6 weeks unlimited",
     "format_stratum": "feed", "direction": "favor", "effect_size": 2.0,
     "n": 9, "gyms": 3, "q_value": 0.001},
    # a handle
    {"lever": "pillar", "value": "@gymfamous", "format_stratum": "feed",
     "direction": "favor", "effect_size": 2.0, "n": 9, "gyms": 3,
     "q_value": 0.001},
    # a real lever and value, but smuggling text in an extra field
    {"lever": "hook_family", "value": "question", "format_stratum": "feed",
     "direction": "favor", "effect_size": 2.0, "n": 9, "gyms": 3,
     "q_value": 0.001, "note": "copy this line: down 42 lbs, ask Sarah how"},
])
def test_no_content_from_another_gym_can_reach_a_prompt(monkeypatch, prompts,
                                                        poison):
    """BLAKE'S ABSOLUTE BOUNDARY, at the consumer. A stored guidance row carrying
    a caption fragment, an offer, a price or a handle is DROPPED whole on the
    read — never stripped of the bad part and rendered anyway. The store here is
    permissive, so only the whitelist in the code under test can stop it."""
    armed = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
                   items=[poison])
    off = _build(monkeypatch, prompts, feeds=False, cross_gym=True,
                 items=[poison])
    assert armed == off, "a poisoned item changed the prompt"
    for leaked in ("Sarah", "42 lbs", "$99", "gymfamous", "copy this line"):
        assert leaked not in armed


def test_a_clean_item_survives_alongside_a_poisoned_one(monkeypatch, prompts):
    """The drop is per item, not per rollup: one bad row must not silently
    suppress the fleet's real guidance, and one good row must not carry a bad one
    in with it."""
    poison = {"lever": "pillar", "value": "$99 six week challenge",
              "format_stratum": "feed", "direction": "favor",
              "effect_size": 2.0, "n": 9, "gyms": 3, "q_value": 0.001}
    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
               items=[_CLEAN_ITEM, poison])
    block = p.split("FLEET FORM SIGNALS", 1)[1]
    assert "open with one real question" in block
    assert "$99" not in p and "challenge" not in p
    assert block.count("\n- ") == 1


def test_every_hint_line_is_built_only_from_whitelisted_tokens(monkeypatch,
                                                               prompts):
    """STRUCTURAL, and stronger than a scan for bad words: every hint line must be
    EXACTLY one of this module's own constant phrases plus two integers.

    That is the isolation guarantee made mechanical. The lever VALUE read from
    the rollup selects a phrase and is never itself printed, so no string that
    came out of a database can appear in a prompt at all — not a caption
    fragment, not an offer, not a handle, not even a legitimate lever token."""
    items = [dict(_CLEAN_ITEM),
             {"lever": "ask_present", "value": "no", "format_stratum": "feed",
              "direction": "favor", "effect_size": 0.6, "n": 45, "gyms": 4,
              "q_value": 0.008, "source": brain.SOURCE_TOP_POSTS}]
    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True, items=items)
    block = p.split("FLEET FORM SIGNALS", 1)[1].split("\n\n", 1)[0]
    lines = [ln[2:] for ln in block.splitlines() if ln.startswith("- ")]
    assert len(lines) == 2

    from agent import cross_gym_guidance as cgg
    constants = {ph for table in cgg._PHRASES.values() for ph in table.values()}
    for line in lines:
        phrase, _, tail = line.partition(" (fleet form signal across ")
        assert phrase in constants, f"{phrase!r} is not a module constant"
        gyms, _, n = tail.rstrip(")").partition(" gyms, n=")
        assert gyms.isdigit() and n.isdigit()


def test_a_lever_the_body_writer_cannot_act_on_is_not_rendered(monkeypatch,
                                                               prompts):
    """A finding about pillar, slot, format or media type is real and stays in the
    rollup, but the caption BODY writer cannot act on it: the pillar and the slot
    were chosen upstream, before this prompt existed. Shipping it here would be
    noise competing with the brand bible for the model's attention."""
    for lever, value in (("pillar", "testimonial"), ("time_slot", "morning"),
                         ("format", "reel"), ("media_product_type", "reels")):
        item = {"lever": lever, "value": value, "format_stratum": "*",
                "direction": "favor", "effect_size": 1.2, "n": 10, "gyms": 2,
                "q_value": 0.004, "source": brain.SOURCE_LEVERS}
        armed = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
                       items=[item])
        assert "FLEET FORM SIGNALS" not in armed, lever


def test_an_avoid_is_rendered_as_what_to_do_not_what_to_shun(monkeypatch,
                                                            prompts):
    """MEASURED DEFECT, now fenced. The first renderer emitted the raw tokens
    ("avoid ask_present yes"), and against the real model that took the caption
    body's ask rate from 0/30 to 5/15 (Fisher p = 0.0025) — the OPPOSITE of the
    instruction, because a negation that names the thing makes it salient.

    An avoid is now rendered as the positive instruction for the opposite value,
    and the word "avoid" never reaches the prompt."""
    avoid_yes = {"lever": "ask_present", "value": "yes", "format_stratum": "feed",
                 "direction": "avoid", "effect_size": -0.66, "n": 23, "gyms": 2,
                 "q_value": 0.008, "source": brain.SOURCE_LEVERS}
    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True,
               items=[avoid_yes])
    block = p.split("FLEET FORM SIGNALS", 1)[1].split("\n\n", 1)[0]
    assert "end the body without asking the reader to do anything" in block
    assert "avoid" not in block.lower()
    assert "ask_present" not in block


def test_favor_and_avoid_of_one_binary_lever_render_once(monkeypatch, prompts):
    """The real fleet produced BOTH "favor ask_present no" and "avoid
    ask_present yes" — the same instruction twice. Saying it twice re-introduces
    exactly the salience problem the phrasing fix exists to remove."""
    items = [
        {"lever": "ask_present", "value": "no", "format_stratum": "feed",
         "direction": "favor", "effect_size": 0.66, "n": 45, "gyms": 4,
         "q_value": 0.008, "source": brain.SOURCE_LEVERS},
        {"lever": "ask_present", "value": "yes", "format_stratum": "feed",
         "direction": "avoid", "effect_size": -0.66, "n": 23, "gyms": 2,
         "q_value": 0.008, "source": brain.SOURCE_LEVERS},
    ]
    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True, items=items)
    block = p.split("FLEET FORM SIGNALS", 1)[1].split("\n\n", 1)[0]
    assert block.count("\n- ") == 1


# ---------------------------------------------------------------------------
# 5. it never breaks a caption
# ---------------------------------------------------------------------------

def test_a_broken_rollup_read_never_blocks_a_caption(monkeypatch, prompts):
    """A hint is a nicety; a caption is the product. Any failure reading the
    rollup degrades to no hints and the caption is still written."""

    class _Boom:
        def latest_rollup(self):
            raise RuntimeError("postgrest down")

    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True, store=_Boom())
    assert "FLEET FORM SIGNALS" not in p
    assert "BRAND VOICE DOC:" in p


def test_no_account_means_no_hints(monkeypatch, prompts):
    """The guidance API requires a tenant scope. No account -> no read at all."""
    monkeypatch.setenv("AGENT_BRAIN_FEEDS_CAPTIONS", "true")
    monkeypatch.setenv("AGENT_CROSS_GYM_BRAIN", "true")

    def _explode(*a, **k):
        raise AssertionError("the rollup must not be read without an account")

    monkeypatch.setattr(brain, "SupabaseBrainStore", _explode)
    assert drafter.StoryBrandGenerator._cross_gym_form_block(None) == ""


def test_a_stale_rollup_is_not_served(monkeypatch, prompts):
    """Stale fleet guidance is worse than none: a rollup older than the read
    API's max age adds nothing to the prompt."""
    old = _Rollup([_CLEAN_ITEM])
    old.row["run_at"] = "2025-01-01T00:00:00+00:00"
    p = _build(monkeypatch, prompts, feeds=True, cross_gym=True, store=old)
    assert "FLEET FORM SIGNALS" not in p


# ---------------------------------------------------------------------------
# 6. status surface
# ---------------------------------------------------------------------------

def test_status_block_reports_the_flag():
    import inspect

    from agent import __main__ as m
    src = inspect.getsource(m)
    assert "AGENT_BRAIN_FEEDS_CAPTIONS" in src
    assert "config.brain_feeds_captions_enabled()" in src


def test_the_phrase_table_and_the_writable_lever_list_agree():
    """TWO-WAY GUARD (D68: "assert the allow-list still CONTAINS what it must, not
    only that writers stay inside it").

    A mutation check caught this: removing the `lever not in WRITABLE_LEVERS`
    check changed NOTHING, because _PHRASES happens to have no entry for pillar,
    slot or format. The rule was really being enforced by an absence, and an
    absence is not a rule — the day someone adds a phrase for `pillar`, the
    WRITABLE_LEVERS check becomes the only thing standing between a planning
    lever and the caption prompt, and nothing would have failed.

    So pin BOTH directions: the phrase table may not grow a lever the body writer
    cannot act on, AND every writable lever must have a phrase for every value the
    brain can emit (a missing phrase silently drops real guidance)."""
    from agent import cross_gym_guidance as cgg

    assert set(cgg._PHRASES) == set(cgg.WRITABLE_LEVERS), (
        "the phrase table and the writable lever list disagree")
    for lever in cgg.WRITABLE_LEVERS:
        assert set(cgg._PHRASES[lever]) == set(brain.ALLOWED_LEVER_VALUES[lever]), (
            f"{lever}: every value the brain can emit needs a phrase, and no "
            f"phrase may exist for a value it cannot")
    # and every phrase is a plain instruction, never a lever token
    for lever, table in cgg._PHRASES.items():
        for value, phrase in table.items():
            assert lever not in phrase and "_" not in phrase.split(" (")[0]


def test_an_avoid_with_no_opposite_is_dropped_not_guessed():
    """There is no single "not a question" hook to recommend, so an avoid on a
    multi valued lever has no positive instruction and is DROPPED. Guessing one
    would invent guidance the statistics never supported."""
    from agent import cross_gym_guidance as cgg

    assert cgg._instruction({"lever": "hook_family", "value": "question",
                             "direction": "avoid"}) is None
    assert cgg._instruction({"lever": "ask_present", "value": "yes",
                             "direction": "avoid"}) == cgg._PHRASES["ask_present"]["no"]
