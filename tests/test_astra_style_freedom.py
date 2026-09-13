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


# ---- account scope --------------------------------------------------------
# Blake, 2026-09-12: "only for LASSO right now until a proven [out]." Blake,
# 2026-09-13 (same day the style system shipped, one day later): "This applies
# to the real production system -- LASSO's own account plus any client gym
# using the auto-infographic path." The default widened from {"lasso"} to
# {"*"}; AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS still narrows it back by hand
# (e.g. "lasso" restores the 2026-09-12 scope without a code change).

def test_scope_defaults_to_every_account(monkeypatch):
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", raising=False)
    assert config.astra_style_freedom_accounts() == {"*"}


def test_master_off_means_off_for_every_account_regardless_of_scope(monkeypatch):
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM", raising=False)
    assert config.astra_style_freedom_enabled_for("lasso") is False
    assert config.astra_style_freedom_enabled_for("eng") is False
    assert config.astra_style_freedom_enabled_for(None) is False


def test_master_on_defaults_to_every_account(monkeypatch):
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", raising=False)
    assert config.astra_style_freedom_enabled_for("lasso") is True
    assert config.astra_style_freedom_enabled_for("eng") is True
    assert config.astra_style_freedom_enabled_for("gritx") is True


def test_a_missing_account_key_is_treated_as_lasso(monkeypatch):
    """Every unscoped caller in this repo (book_campaign, podcast, summit,
    stories, render-card with no --account) IS LASSO's own content pipeline."""
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", raising=False)
    assert config.astra_style_freedom_enabled_for(None) is True
    assert config.astra_style_freedom_enabled_for("") is True


def test_ig_and_fb_suffixes_are_stripped_before_the_scope_check(monkeypatch):
    """Suffix stripping still matters once the scope is narrowed by hand back
    to LASSO-only (the 2026-09-12 rollback path)."""
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", "lasso")
    assert config.astra_style_freedom_enabled_for("lasso_ig") is True
    assert config.astra_style_freedom_enabled_for("lasso_fb") is True
    assert config.astra_style_freedom_enabled_for("eng_ig") is False


def test_the_scope_env_var_narrows_the_rollout_back(monkeypatch):
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", "lasso,eng,gritx")
    assert config.astra_style_freedom_enabled_for("eng") is True
    assert config.astra_style_freedom_enabled_for("gritx_ig") is True
    assert config.astra_style_freedom_enabled_for("pierce") is False


def test_star_opens_the_scope_to_every_account(monkeypatch):
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", "*")
    assert config.astra_style_freedom_enabled_for("pierce") is True
    assert config.astra_style_freedom_enabled_for("some_new_client") is True


def test_the_brief_resolves_freedom_per_account_when_freedom_is_not_pinned(monkeypatch):
    """The actual integration point: build_infographic_brief must consult the
    per-account scope, not the bare master flag, when the caller (creative_studio
    .generate -> _astra_brief_for) hands it a real account_key. A CLIENT GYM in
    scope still gets freedom mode (composition/accent variety, ART DIRECTION
    LATITUDE) but NEVER the LASSO CANVAS MODE / locked hex text -- it gets its
    own palette latitude section instead (gym_brand_latitude)."""
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", raising=False)

    lasso_brief = astra_prompt.build_infographic_brief(
        HOOK, FACTS, cta="Book a call", account_key="lasso")
    client_brief = astra_prompt.build_infographic_brief(
        HOOK, FACTS, cta="Book a call", account_key="eng_ig")

    assert "CANVAS MODE" in lasso_brief
    assert "CANVAS MODE" not in client_brief
    assert "THE canvas" not in client_brief
    assert "ART DIRECTION LATITUDE" in client_brief
    assert "PALETTE, YOUR CALL" in client_brief
    assert "#FAF6F0" not in client_brief and "#121E3C" not in client_brief

    # narrowing the scope back to LASSO-only restores the pre-2026-09-13 brief
    # for a client gym, unchanged.
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", "lasso")
    locked_client_brief = astra_prompt.build_infographic_brief(
        HOOK, FACTS, cta="Book a call", account_key="eng_ig")
    assert "CANVAS MODE" not in locked_client_brief
    assert "THE canvas" in locked_client_brief


def test_an_explicit_freedom_argument_still_overrides_the_scope(monkeypatch):
    """`freedom=` stays the hard override the tests and a one off render use —
    account scoping only fills the gap when freedom is left unpinned. A client
    gym forced into freedom mode gets the gym palette, not LASSO's canvas."""
    monkeypatch.setenv("AGENT_ASTRA_STYLE_FREEDOM", "true")
    monkeypatch.delenv("AGENT_ASTRA_STYLE_FREEDOM_ACCOUNTS", raising=False)
    eng_brief = astra_prompt.build_infographic_brief(
        HOOK, FACTS, account_key="eng", freedom=True)
    assert "CANVAS MODE" not in eng_brief
    assert "PALETTE, YOUR CALL" in eng_brief
    assert "ART DIRECTION LATITUDE" in eng_brief
    assert "CANVAS MODE" not in astra_prompt.build_infographic_brief(
        HOOK, FACTS, account_key="lasso", freedom=False)


# ---- gym brand latitude (Blake, 2026-09-13: "create whatever it wants with
# the brain", never LASSO's own hex list, never LASSO's own voice doc) ------

def test_gym_brief_never_carries_a_lasso_hex_value(monkeypatch):
    for canvas in astra_prompt.CANVAS_ORDER:
        brief = astra_prompt.build_infographic_brief(
            HOOK, FACTS, cta="Book a call", account_key="somegym",
            freedom=True, canvas=canvas)
        for hexval in ("#FAF6F0", "#121E3C", "#5EB9E6", "#FF0000"):
            assert hexval not in brief, f"{canvas}: leaked LASSO hex {hexval}"
        assert "PALETTE, YOUR CALL" in brief
        assert "no fixed color list applies" in brief.lower()


def test_gym_brief_keeps_the_real_guardrails(monkeypatch):
    """Freedom on color; not on the genuine safety/quality rules."""
    brief = astra_prompt.build_infographic_brief(
        HOOK, FACTS, cta="Book a call", account_key="somegym", freedom=True)
    assert "NO FABRICATION" in brief
    assert "READABILITY" in brief
    assert "BANNED" in brief
    assert "—" not in brief and "–" not in brief


def test_gym_brief_says_this_gym_not_lasso(monkeypatch):
    brief = astra_prompt.build_infographic_brief(
        HOOK, FACTS, cta="Book a call", account_key="somegym", freedom=True)
    assert "this gym's own brand, not LASSO's" in brief
    lasso_brief = astra_prompt.build_infographic_brief(
        HOOK, FACTS, cta="Book a call", account_key="lasso", freedom=True)
    assert "for the LASSO brand" in lasso_brief


def test_gym_accent_law_names_no_color_but_keeps_the_one_accent_discipline():
    law = astra_prompt.accent_law_free("one_word")
    assert "exactly one" in law.lower()
    assert "#ff0000" not in law.lower()
    assert " red " not in f" {law.lower()} "


def test_is_lasso_account_matches_the_style_freedom_convention():
    assert astra_prompt.is_lasso_account(None) is True
    assert astra_prompt.is_lasso_account("") is True
    assert astra_prompt.is_lasso_account("lasso") is True
    assert astra_prompt.is_lasso_account("lasso_ig") is True
    assert astra_prompt.is_lasso_account("eng") is False
    assert astra_prompt.is_lasso_account("eng_fb") is False


def test_voice_path_for_lasso_is_unchanged(monkeypatch):
    assert astra_prompt._voice_path_for("lasso") == config.VOICE_DOC_PATH
    assert astra_prompt._voice_path_for(None) == config.VOICE_DOC_PATH
    assert astra_prompt._voice_path_for("lasso", "explicit.md") == "explicit.md"


def test_voice_path_for_a_gym_reads_its_own_durable_doc(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "client_voice_dir", lambda: str(tmp_path))
    gym_dir = tmp_path / "somegym"
    gym_dir.mkdir()
    voice_file = gym_dir / "lasso_voice.md"
    voice_file.write_text("gym voice")
    path = astra_prompt._voice_path_for("somegym_ig")
    assert path == str(voice_file)
