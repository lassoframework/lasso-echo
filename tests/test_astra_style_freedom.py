"""
The Astra style freedom system (brand_voice/lasso_house_style.md section 12).

Blake, 2026-09-13: "all the infographics look the same in context, feel and look
... take off canvas has to be cream and let it have more freedom in look and feel."

Every test here is OFFLINE: nothing renders, nothing reaches a network. These
assert on the BRIEF text, which is what actually produced the sameness.

The contract under test:
  - flag OFF (the default) -> the brief is byte for byte what it always was
  - flag ON  -> canvas, composition, and accent vary per card, deterministically
  - the locked items stay locked ON and OFF: colors, two type families, no
    dashes, no fabrication, the readability bar, the six-question grade gate
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import astra_prompt, config, grade_gate  # noqa: E402

HOOK = "Speed to lead wins"
FACTS = ["Answer in five minutes"]


def _brief(**kw):
    kw.setdefault("cta", "Book a call")
    return astra_prompt.build_infographic_brief(HOOK, FACTS, **kw)


# ---- the flag itself -------------------------------------------------------

def test_freedom_defaults_off(monkeypatch):
    """House rule: every new capability ships behind a flag that defaults OFF."""
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM", raising=False)
    assert config.astra_style_freedom_enabled() is False


def test_flag_off_keeps_the_original_locked_brief(monkeypatch):
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM", raising=False)
    brief = _brief()
    # the cream-locked palette and the four Full Gym blocks, exactly as before
    assert "THE canvas" in brief
    assert "THREE ELEMENT VISUAL METAPHOR" in brief
    assert "CTA BUTTON BLOCK" in brief
    assert "ART DIRECTION LATITUDE" not in brief
    assert "CANVAS MODE" not in brief


def test_flag_on_frees_the_card(monkeypatch):
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    brief = _brief()
    assert "CANVAS MODE" in brief
    assert "ART DIRECTION LATITUDE" in brief
    # cream is no longer declared THE canvas
    assert "THE canvas" not in brief


def test_freedom_argument_overrides_the_flag(monkeypatch):
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM", raising=False)
    assert "CANVAS MODE" in _brief(freedom=True)
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    assert "CANVAS MODE" not in _brief(freedom=False)


# ---- the selection ---------------------------------------------------------

def test_selection_is_deterministic_so_a_rerender_is_stable():
    assert astra_prompt.style_for(HOOK) == astra_prompt.style_for(HOOK)


def test_selection_actually_varies_across_cards():
    """The whole point. One card per headline, and they must not converge."""
    heads = [
        "One platform. Every lead.", "Speed to lead wins",
        "Stop duct taping systems", "Your show rate is the leak",
        "The follow up is the offer", "Leads are not the problem",
        "Book more. Chase less.", "Honest numbers or no numbers",
        "Zero blind spots", "Ad spend is not a strategy",
        "The four leg stool", "What your front desk misses",
    ]
    styles = [astra_prompt.style_for(h) for h in heads]
    assert len({s["canvas"] for s in styles}) >= 4
    assert len({s["composition"] for s in styles}) >= 4
    assert len({s["accent"] for s in styles}) >= 3
    # The property that matters is that no ONE look dominates the run. Some
    # collision is expected and fine (12 draws over 49 canvas/composition pairs);
    # the same card shape three times in twelve is not.
    import collections
    pairs = collections.Counter((s["canvas"], s["composition"]) for s in styles)
    assert len(pairs) >= 7
    assert pairs.most_common(1)[0][1] <= 3


def test_the_loud_canvases_stay_rare_across_a_long_run():
    """Variety, not a shouting grid: RED is punctuation, never the house field."""
    import collections
    spread = collections.Counter(
        astra_prompt.style_for(f"LASSO card {i}")["canvas"] for i in range(120))
    assert set(spread) == set(astra_prompt.CANVAS_ORDER)   # all seven get used
    assert spread["red"] < spread["cream"]
    assert spread["red"] < spread["navy"]
    assert spread["red"] <= 120 * 0.12


def test_cream_is_one_of_seven_not_the_default():
    assert "cream" in astra_prompt.CANVAS_ORDER
    assert len(astra_prompt.CANVAS_ORDER) == 7
    assert set(astra_prompt.CANVAS_MODES) == set(astra_prompt.CANVAS_ORDER)


def test_a_concept_can_still_pin_its_own_look():
    style = astra_prompt.style_for(HOOK, canvas="ink", composition="type_poster",
                                   accent="one_word")
    assert style == {"canvas": "ink", "composition": "type_poster",
                     "accent": "one_word"}
    brief = _brief(freedom=True, canvas="ink", composition="type_poster")
    assert "CANVAS MODE INK" in brief
    assert "COMPOSITION TYPE POSTER" in brief


def test_an_unknown_token_raises_rather_than_rendering_off_system():
    for kw in ({"canvas": "chartreuse"}, {"composition": "collage"},
               {"accent": "glitter"}):
        with pytest.raises(ValueError):
            astra_prompt.style_for(HOOK, **kw)


def test_button_free_compositions_never_draw_the_button_accent():
    """Six of seven modes carry no CTA button, so the accent cannot live there."""
    for comp in astra_prompt._NO_BUTTON_MODES:
        for head in ("a", "b", "c", "d", "e", "f", "g", "h"):
            style = astra_prompt.style_for(head, composition=comp)
            assert style["accent"] != "cta_button"


# ---- what must stay locked -------------------------------------------------

def test_red_field_flips_the_accent_to_white_so_the_card_never_fights_itself():
    law = astra_prompt.accent_law("red", "one_word")
    assert "white" in law.lower()
    assert "exactly one" in law.lower()
    normal = astra_prompt.accent_law("navy", "one_word")
    assert "#FF0000" in normal and "exactly one" in normal.lower()


def test_duotone_is_the_only_canvas_that_lifts_the_photography_ban():
    duo = astra_prompt.banned_for("duotone")
    assert "PHOTOGRAPHY on this card" in duo
    assert "never a competitive athlete" in duo       # the LASSO avatar rule holds
    for canvas in ("cream", "navy", "red", "split", "sky", "ink"):
        assert astra_prompt.banned_for(canvas) == astra_prompt.BANNED
        assert "photorealistic photography" in astra_prompt.banned_for(canvas)


def test_every_combination_passes_the_grade_gate_and_the_copy_rules():
    """294 combinations. A freed card is graded by the same gate as a locked one."""
    checked = 0
    for canvas in astra_prompt.CANVAS_ORDER:
        for comp in astra_prompt.COMPOSITION_ORDER:
            for accent in astra_prompt.ACCENT_ORDER:
                brief = _brief(freedom=True, canvas=canvas,
                               composition=comp, accent=accent)
                assert grade_gate.grade_card(brief, headline=HOOK).passed, \
                    f"{canvas}/{comp}/{accent} failed the house-style gate"
                assert "—" not in brief and "–" not in brief
                assert "NO FABRICATION" in brief
                assert "READABILITY" in brief
                checked += 1
    assert checked == 294


def test_freedom_keeps_the_locked_colors_and_type_families(monkeypatch):
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    brief = _brief()
    for hexval in ("#FAF6F0", "#121E3C", "#5EB9E6", "#FF0000"):
        assert hexval in brief
    assert "Anton" in brief and "Oswald" in brief
    assert "never more than two" in brief


def test_freedom_still_blocks_a_banned_headline(monkeypatch):
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    with pytest.raises(ValueError):
        astra_prompt.build_infographic_brief("Pick the right vendor", FACTS)


def test_freedom_still_invents_nothing(monkeypatch):
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    brief = astra_prompt.build_infographic_brief("Hook", [])
    assert "NO FABRICATION" in brief
    assert "APPROVED CONTEXT" not in brief


def test_freedom_never_instructs_a_centered_or_symmetric_composition():
    """The prompt-level ban is NOT weakened by the freedom system."""
    from agent import creative_studio
    for canvas in astra_prompt.CANVAS_ORDER:
        for comp in astra_prompt.COMPOSITION_ORDER:
            brief = _brief(freedom=True, canvas=canvas, composition=comp)
            creative_studio._check_prompt_hard_rules(brief)  # raises on failure


def test_no_two_rules_claim_the_same_accent():
    """A brief that says the button IS the red element AND that the accent is a
    node contradicts itself. The freedom cut of the flat editorial spec defers
    to the color law instead; the locked-mode constant is untouched."""
    assert ("This is the ONE red element on the card."
            in astra_prompt.FLAT_EDITORIAL_SPEC)
    free = astra_prompt.COMPOSITION_MODES["flat_editorial"]
    assert "This is the ONE red element on the card." not in free
    assert "COLOR LAW" in free
    brief = _brief(freedom=True, composition="flat_editorial", accent="one_node")
    assert brief.count("ONE red element") == 0
    assert "red #FF0000 is used exactly one time" in brief
