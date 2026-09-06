"""Dean Holcomb, CrossFit Reverb, support ticket 4941e162-2923-495f-8efb-d2554dea5aec
(2026-09-05): "All the captions are almost the same as one another. Also, they all end
with 'How do I get started with training at CrossFit Reverb?' which doesn't make sense."

These tests pin the MEASUREMENT and the ANTI-REPETITION SELECTOR. The numbers in the
docstrings are from his real production book (93 rows / 31 posts, 2026-08-31..2026-09-30)
and from the fleet-wide sweep run alongside it.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent import caption_variety as cv
from agent import copy_gate


# The EXACT string production stapled onto 90 of Reverb's 93 rows, U+200B and all.
REVERB_CTA = "​How do I get started with training at CrossFit Reverb?"


# ---------------------------------------------------------------------------
# 1. Zero-width normalization (copy_gate)
# ---------------------------------------------------------------------------

def test_zero_width_space_is_scrubbed_from_captions():
    """Python's \\s does not match U+200B (category Cf), str.strip() does not remove
    it, and scrub only handled dashes, so the character rode all the way into 90
    published-pending captions."""
    assert "​" in REVERB_CTA
    out = copy_gate.scrub(REVERB_CTA)
    assert "​" not in out
    assert out == "How do I get started with training at CrossFit Reverb?"


def test_all_invisible_junk_characters_are_scrubbed():
    for ch in ("​", "⁠", "﻿", "­"):
        assert copy_gate.scrub(f"Book{ch} your intro") == "Book your intro", repr(ch)


def test_zero_width_joiner_is_preserved_so_emoji_survive():
    """U+200D and U+200C hold emoji sequences together. Stripping them would
    corrupt emoji gyms legitimately post, so they are deliberately NOT scrubbed."""
    emoji = "Coach day \U0001f469‍\U0001f33e lets go"
    assert copy_gate.scrub(emoji) == emoji
    assert copy_gate.scrub("a‌b") == "a‌b"


def test_scrub_prompt_also_strips_invisible_characters():
    assert "​" not in copy_gate.scrub_prompt(REVERB_CTA)


# ---------------------------------------------------------------------------
# 2. CTA shape. The semantic root of "doesn't make sense".
# ---------------------------------------------------------------------------

def test_reverb_faq_heading_is_rejected_as_a_cta():
    """THE defect. ASK_RE said yes because the heading contains the phrase
    'get started'; it is a question the reader is ASKED, not an instruction."""
    assert copy_gate.ASK_RE.search(REVERB_CTA), "the old gate accepted it"
    assert copy_gate.cta_defects(REVERB_CTA) == ["cta_is_question"]
    assert copy_gate.is_cta_shaped(REVERB_CTA) is False


def test_a_question_without_a_question_mark_is_still_not_a_cta():
    assert "cta_is_question" in copy_gate.cta_defects("How do I get started")


@pytest.mark.parametrize("cta", [
    "Book your free No Sweat Intro",
    "Send us a message to get started",
    "Visit crossfitreverb.com to book your first visit",
    "Schedule a free no sweat intro",
    "Link in our bio",
    "Claim your spot",
])
def test_real_gym_ctas_are_accepted(cta):
    """The gate must reject questions WITHOUT rejecting the CTAs gyms actually
    write, including every entry in the auto-generated bible's rotation."""
    assert copy_gate.cta_defects(cta) == [], cta


def test_text_with_no_ask_phrase_at_all_is_not_a_cta():
    assert "cta_no_ask_phrase" in copy_gate.cta_defects("We had a great week.")


def test_empty_text_is_not_a_cta():
    assert copy_gate.cta_defects("") == ["cta_empty"]
    assert copy_gate.cta_defects("   ") == ["cta_empty"]


# ---------------------------------------------------------------------------
# 3. Signatures
# ---------------------------------------------------------------------------

def test_closing_signature_catches_the_repeated_tail():
    """Nothing in the codebase inspected a caption's TAIL before. Two captions
    with different bodies and the same stapled ask collide here."""
    a = "You are ready to start.\n\n" + REVERB_CTA
    b = "Rope climbs intimidate most people.\n\n" + REVERB_CTA
    assert cv.closing_signature(a) == cv.closing_signature(b)
    assert "​" not in cv.closing_signature(a)


def test_closing_signature_normalizes_case_spacing_and_invisibles():
    """Two tails that differ only in case, spacing, or an invisible character are
    the SAME closing line to a reader. Without normalization they read as two
    distinct closings and the anti-repetition rail waves the repeat through."""
    base = "Body line here.\nBook your free intro"
    variants = [
        "Body line here.\nBOOK YOUR FREE INTRO",
        "Body line here.\nBook  your   free intro  ",
        "Body line here.\n​Book your free intro",
    ]
    for v in variants:
        assert cv.closing_signature(v) == cv.closing_signature(base), v


def test_opening_signature_collapses_the_same_move():
    """'You're staring at the rig' and 'You're staring at the rig wondering...'
    are the same opening move; the first four normalized words say so."""
    assert (cv.opening_signature("You're staring at the rig wondering if you belong.")
            == cv.opening_signature("You're staring at the rig, unsure where to start."))


def test_opening_signature_separates_genuinely_different_hooks():
    assert (cv.opening_signature("Rope climbs intimidate most people.")
            != cv.opening_signature("You're staring at the rig."))


def test_structure_signature_separates_shape_not_words():
    """Same shape, completely different words: still a collision."""
    a = "You are stuck. You want a plan. We build one. Come see us."
    b = "You are tired. You want help. We give it. Stop by today."
    assert cv.structure_signature(a) == cv.structure_signature(b)


def test_structure_signature_notices_a_real_shape_change():
    short = "Rope climbs intimidate people."
    longer = ("Rope climbs intimidate people.\n\nMost never try one. A coach breaks it "
              "into three steps. You will get there. It takes weeks not years. "
              "Come find out how it works for you.")
    assert cv.structure_signature(short) != cv.structure_signature(longer)


def test_length_bands_separate_sizes():
    assert cv.length_band("Short one here.") == "short"
    assert cv.length_band(" ".join(["word"] * 50)) == "long"
    assert cv.length_band(" ".join(["word"] * 100)) == "xlong"


# ---------------------------------------------------------------------------
# 4. Book-level measurement. These are the Part A numbers, reproducible.
# ---------------------------------------------------------------------------

def _book(captions, start_day=1):
    return [{"post_date": f"2026-09-{start_day + i:02d}", "caption": c}
            for i, c in enumerate(captions)]


def test_report_reproduces_the_reverb_shape():
    """A book where every post carries the same stapled ask must report one
    dominant closing line and a pile of closing collisions."""
    caps = [f"Hook number {i} that is different every single day.\n\n{REVERB_CTA}"
            for i in range(12)]
    r = cv.report(_book(caps), window=10)
    assert r["posts"] == 12
    assert r["distinct"]["closing"] == 1
    assert r["top_share"]["closing"] == 1.0
    assert [c for c in r["collisions"] if c["kind"] == "closing"]


def test_report_counts_posts_not_rows():
    """The same-date IG feed + FB mirror + paired story are ONE post syndicated
    three ways. Counting them as three is what made a 31 post book look like 93
    rows with 31 captions, and it must not register as duplication."""
    cap = "You are ready.\n\nBook your free intro."
    rows = [{"post_date": "2026-09-01", "account": a, "format": f, "caption": cap}
            for a, f in (("instagram", "feed"), ("facebook", "feed"),
                         ("instagram", "story"))]
    r = cv.report(rows, window=10)
    assert r["posts"] == 1
    assert r["collisions"] == []


def test_a_varied_book_reports_no_closing_collisions():
    caps = [f"Hook {i} for the day.\n\nBook your intro on day {i}." for i in range(12)]
    r = cv.report(_book(caps), window=10)
    assert [c for c in r["collisions"] if c["kind"] == "closing"] == []


def test_collisions_respect_the_window():
    """A closing reused far enough back is NOT a collision. The rule is 'not
    inside the window', not 'never repeat': a gym with four approved CTAs must
    still be able to fill a 31 day book."""
    caps = ["Same tail A.\n\nBook your intro."]
    caps += [f"Filler hook {i}.\n\nSchedule your tour number {i}." for i in range(6)]
    caps += ["Same tail B.\n\nBook your intro."]
    rows = _book(caps)
    assert [c for c in cv.collisions(rows, window=3) if c["kind"] == "closing"] == []
    assert [c for c in cv.collisions(rows, window=10) if c["kind"] == "closing"]


# ---------------------------------------------------------------------------
# 5. The selector. THE mechanical guarantee.
# ---------------------------------------------------------------------------

def test_pick_non_colliding_skips_a_used_option():
    pool = ["Book your free intro", "Send us a message to get started"]
    assert cv.pick_non_colliding(pool, ["book your free intro"]) == pool[1]


def test_pick_non_colliding_normalizes_before_comparing():
    """A zero-width character or stray case must not let the same CTA through
    twice. That is precisely how the same line reached 90 rows."""
    pool = ["Book your free intro"]
    with pytest.raises(cv.NoOptionAvailable):
        cv.pick_non_colliding(pool, ["​BOOK   your free intro "])


def test_pick_non_colliding_raises_rather_than_repeating():
    """It must NOT silently fall back to a colliding value. The caller has to
    decide honestly (leave the post ask-less), because quietly returning a
    duplicate is the original bug."""
    with pytest.raises(cv.NoOptionAvailable):
        cv.pick_non_colliding(["Book your intro"], ["book your intro"])
    with pytest.raises(cv.NoOptionAvailable):
        cv.pick_non_colliding([], [])


def test_rotate_delivers_every_option_before_repeating():
    """The auto-generated bible has promised '### CTA rotation (cycle in order,
    one per post)' since day one and no code ever delivered it."""
    pool = ["a", "b", "c"]
    assert [cv.rotate(pool, i) for i in range(6)] == ["a", "b", "c", "a", "b", "c"]
    assert cv.rotate([], 3) is None


# ---------------------------------------------------------------------------
# 6. Per-post FORM PLAN (AGENT_CAPTION_FORM_PLAN)
# ---------------------------------------------------------------------------
# The SB7 prompt asked every post for the same shape ("Body max 260 characters")
# plus a soft "VARY the ENTRY POINT". A soft instruction repeated 31 times
# produced 31 similar captions: 90.3% of Reverb's opened "You + problem", 100%
# of rows sat in one length band, sentence-count sd was 1.3 on a mean of 4.9.

def test_consecutive_posts_get_different_shapes():
    """THE point of the plan. Neighbours must differ on BOTH axes, otherwise the
    book flattens exactly the way it did."""
    plans = [cv.form_plan(i) for i in range(12)]
    for a, b in zip(plans, plans[1:]):
        assert a["hook_family"] != b["hook_family"], (a, b)
        assert a["length_band"] != b["length_band"], (a, b)


def test_form_plan_covers_every_hook_family_within_a_book():
    """A month must exercise the whole repertoire, not two of six families."""
    seen = {cv.form_plan(i)["hook_family"] for i in range(31)}
    assert seen == set(cv.HOOK_FAMILIES)


def test_form_plan_spreads_length_across_every_band():
    bands = [cv.form_plan(i)["length_band"] for i in range(31)]
    assert set(bands) == {p[0] for p in cv.LENGTH_PLANS}
    # and no band dominates the book the way 'mid' held 100% of Reverb's
    assert max(bands.count(b) for b in set(bands)) / len(bands) < 0.4


def test_hook_and_length_pair_does_not_repeat_early():
    """6 families x 3 length plans must not cycle back before a month is out."""
    pairs = [(p["hook_family"], p["length_band"])
             for p in (cv.form_plan(i) for i in range(18))]
    assert len(set(pairs)) == 18


def test_form_plan_is_deterministic():
    assert cv.form_plan(7) == cv.form_plan(7)


def test_form_plan_carries_no_content():
    """STYLE ONLY. A plan must never contain a topic, a claim, or copy."""
    plan = cv.form_plan(3)
    assert set(plan) == {"hook_family", "length_band", "min_sentences",
                         "max_sentences", "max_chars"}
    assert plan["hook_family"] in cv.HOOK_FAMILIES
    assert isinstance(plan["max_chars"], int)


# --- the prompt block ------------------------------------------------------

def test_form_block_is_empty_without_a_plan():
    """Flag OFF must leave the prompt byte-for-byte as it was."""
    from agent.drafter import StoryBrandGenerator
    assert StoryBrandGenerator._form_block(None) == ""
    assert StoryBrandGenerator._form_block({}) == ""


def test_form_block_states_the_opening_move_and_the_length():
    from agent.drafter import StoryBrandGenerator
    block = StoryBrandGenerator._form_block(cv.form_plan(2))
    assert "OPENING:" in block and "LENGTH:" in block
    assert "sentences" in block


def test_every_hook_family_has_a_prompt_instruction():
    """A family with no instruction would silently degrade to no guidance."""
    from agent.drafter import StoryBrandGenerator
    for fam in cv.HOOK_FAMILIES:
        assert fam in StoryBrandGenerator._HOOK_INSTRUCTIONS, fam
        block = StoryBrandGenerator._form_block({"hook_family": fam})
        assert "OPENING:" in block


def test_different_indexes_produce_different_prompt_blocks():
    from agent.drafter import StoryBrandGenerator
    blocks = {StoryBrandGenerator._form_block(cv.form_plan(i)) for i in range(6)}
    assert len(blocks) == 6


def test_form_plan_helper_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_CAPTION_FORM_PLAN", raising=False)
    from agent import client_content
    assert client_content._form_plan_for_day("2026-09-04") is None


def test_form_plan_helper_returns_a_plan_when_armed(monkeypatch):
    monkeypatch.setenv("AGENT_CAPTION_FORM_PLAN", "true")
    from agent import client_content
    plan = client_content._form_plan_for_day("2026-09-04")
    assert plan and plan["hook_family"] in cv.HOOK_FAMILIES


def test_form_plan_helper_advances_with_the_day(monkeypatch):
    """Keyed on the day ordinal, the same index category_for_day already uses,
    so the shape moves with the book instead of being constant."""
    monkeypatch.setenv("AGENT_CAPTION_FORM_PLAN", "true")
    from agent import client_content
    a = client_content._form_plan_for_day("2026-09-04")
    b = client_content._form_plan_for_day("2026-09-05")
    assert a["hook_family"] != b["hook_family"]
