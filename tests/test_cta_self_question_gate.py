"""
CTA SELF-QUESTION GATE: no caption may carry the banned Reverb FAQ line, and no
caption's closing line may be an FAQ-mined question naming the gym itself.

Dean Holcomb, CrossFit Reverb, ticket 4941e162-2923-495f-8efb-d2554dea5aec,
2026-09-05: "Also, they all end with 'How do I get started with training at
CrossFit Reverb?' which doesn't make sense." Measured: 90 of 93 rows carried it.

This gate is CTA FORM only. It must never touch audience/topic words -- HYROX,
competitive CrossFit, and competitive athletics are an explicit standing
audience rule (Blake, 2026-09-06, reconfirming 2026-09-01: "confirmed we can
talk about CrossFit, hyrox and competitive athletics"). A test below asserts
the gate does not fire on ordinary HYROX/competitive-athlete copy, specifically
so nobody "fixes" this module into an avatar-word ban later.

Fully offline.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config  # noqa: E402
from agent.cta_self_question_gate import (  # noqa: E402
    CtaSelfQuestionGateViolation,
    assert_no_self_question_cta,
    contains_banned_literal,
    gate_violations,
    is_self_question_cta,
)

REVERB_LINE = "How do I get started with training at CrossFit Reverb?"


# ---------------------------------------------------------------------------
# contains_banned_literal
# ---------------------------------------------------------------------------

def test_banned_literal_exact_match():
    assert contains_banned_literal(f"Some caption body.\n\n{REVERB_LINE}")


def test_banned_literal_case_and_whitespace_insensitive():
    mangled = "how do i get   started with training at crossfit reverb?"
    assert contains_banned_literal(mangled)


def test_banned_literal_ignores_zero_width_characters():
    # The exact defect shape: the line was pasted with a leading U+200B.
    zw = "​" + REVERB_LINE
    assert contains_banned_literal(zw)


def test_banned_literal_fires_for_any_gym_not_just_reverb():
    # Fleet-wide, per Blake's spec -- it never was a real CTA anywhere.
    assert contains_banned_literal(f"Body copy.\n{REVERB_LINE}")


def test_banned_literal_does_not_fire_on_unrelated_text():
    assert not contains_banned_literal("Book your free intro today. Link in bio.")


# ---------------------------------------------------------------------------
# is_self_question_cta
# ---------------------------------------------------------------------------

def test_self_question_fires_on_faq_mined_shape():
    caption = "Ready to feel strong again?\n\nHow do I get started with training at Iron Forge?"
    assert is_self_question_cta(caption, "Iron Forge")


def test_self_question_requires_gym_name_present():
    # "How do I get started?" alone is a completely normal, answerable CTA.
    caption = "Ready to feel strong again?\n\nHow do I get started?"
    assert not is_self_question_cta(caption, "Iron Forge")


def test_self_question_requires_question_mark():
    caption = "Ready to feel strong again?\n\nHere is how to get started at Iron Forge."
    assert not is_self_question_cta(caption, "Iron Forge")


def test_self_question_does_not_fire_on_ordinary_direct_cta():
    # "Ready to start?" / "Sound like you?" are legitimate direct-address CTAs
    # a coach would actually write. The gate must not ban question-form CTAs
    # generally -- only the FAQ-mined, gym-named shape.
    for caption in [
        "Book your free intro today.\n\nReady to start?",
        "You've been putting it off long enough.\n\nSound like you?",
    ]:
        assert not is_self_question_cta(caption, "Iron Forge")


def test_self_question_blank_gym_name_never_matches():
    caption = "How do I get started with training at Iron Forge?"
    assert not is_self_question_cta(caption, "")
    assert not is_self_question_cta(caption, None)


def test_self_question_matches_join_and_sign_up_variants():
    for verb in ["join", "sign up for", "begin"]:
        caption = f"Big news this week.\n\nWhen can I {verb} at Iron Forge?"
        assert is_self_question_cta(caption, "Iron Forge")


# ---------------------------------------------------------------------------
# HARD RULE: HYROX / competitive CrossFit / competitive athletics are NOT
# banned by this gate. This test exists so nobody widens the gate into an
# avatar-word ban later -- Blake's 2026-09-06 ruling is explicit and repeated.
# ---------------------------------------------------------------------------

def test_gate_never_fires_on_hyrox_or_competitive_athlete_copy():
    captions = [
        "Training for your first HYROX? We've got the engine work dialed in.\n\n"
        "Book your free intro today.",
        "Competitive CrossFit athletes: this is the accessory work you're missing.\n\n"
        "Link in bio.",
        "Our strongest athletes train here. Are you next?",
    ]
    for caption in captions:
        assert not contains_banned_literal(caption)
        assert not is_self_question_cta(caption, "Iron Forge")


# ---------------------------------------------------------------------------
# gate_violations / assert_no_self_question_cta -- the plan-time assertion
# ---------------------------------------------------------------------------

def _row(gym_id="ironforge", account="ironforge_ig", post_date="2026-10-01",
         caption="", status="pending"):
    return {"gym_id": gym_id, "account": account, "post_date": post_date,
            "caption": caption, "status": status}


def test_gate_violations_empty_on_clean_batch():
    rows = [_row(caption="Book your free intro today. Link in bio.")]
    assert gate_violations(rows, "Iron Forge") == []


def test_gate_violations_finds_banned_literal():
    rows = [_row(caption=f"Body.\n\n{REVERB_LINE}")]
    found = gate_violations(rows, "Iron Forge")
    assert len(found) == 1
    assert found[0].kind == "banned_literal"


def test_gate_violations_finds_self_question():
    rows = [_row(caption="Ready?\n\nHow do I get started with training at Iron Forge?")]
    found = gate_violations(rows, "Iron Forge")
    assert len(found) == 1
    assert found[0].kind == "self_question"


def test_gate_violations_ignores_deleted_rows():
    rows = [_row(caption=f"Body.\n\n{REVERB_LINE}", status="deleted")]
    assert gate_violations(rows, "Iron Forge") == []


def test_gate_violations_ignores_empty_captions():
    rows = [_row(caption="")]
    assert gate_violations(rows, "Iron Forge") == []


def test_assert_raises_and_carries_every_violation():
    rows = [
        _row(post_date="2026-10-01", caption=f"Body.\n\n{REVERB_LINE}"),
        _row(post_date="2026-10-02",
             caption="Ready?\n\nHow do I join at Iron Forge?"),
        _row(post_date="2026-10-03", caption="Book your free intro today."),
    ]
    with pytest.raises(CtaSelfQuestionGateViolation) as exc_info:
        assert_no_self_question_cta(rows, "Iron Forge")
    assert len(exc_info.value.violations) == 2


def test_assert_clean_batch_returns_empty_list():
    rows = [_row(caption="Book your free intro today.")]
    assert assert_no_self_question_cta(rows, "Iron Forge") == []


def test_escape_hatch_enabled_false_skips_everything():
    rows = [_row(caption=f"Body.\n\n{REVERB_LINE}")]
    assert assert_no_self_question_cta(rows, "Iron Forge", enabled=False) == []


# ---------------------------------------------------------------------------
# config flag: default ON (prevent-only), escape hatch works
# ---------------------------------------------------------------------------

def test_config_flag_default_on(monkeypatch):
    monkeypatch.delenv("ECHO_CTA_SELF_QUESTION_GATE", raising=False)
    assert config.cta_self_question_gate_enabled() is True


def test_config_flag_escape_hatch(monkeypatch):
    monkeypatch.setenv("ECHO_CTA_SELF_QUESTION_GATE", "false")
    assert config.cta_self_question_gate_enabled() is False


# ---------------------------------------------------------------------------
# VERIFY BY DELETION (this repo's own doctrine, echo-account-key-audit-loop):
# prove each check actually does something by removing it and watching the
# specific test that should fail, fail.
# ---------------------------------------------------------------------------

def test_deletion_proof_literal_check_is_load_bearing():
    # If contains_banned_literal always returned False, this row would be
    # invisible to the gate. Confirm it is NOT invisible.
    rows = [_row(caption=REVERB_LINE)]
    assert len(gate_violations(rows, "Iron Forge")) == 1


def test_deletion_proof_gym_name_requirement_is_load_bearing():
    # If the gym-name requirement were removed, this ordinary CTA would wrongly
    # fire. Confirm it does NOT fire -- this is the test that would catch a
    # future "helpful" widening of the self-question check.
    assert not is_self_question_cta("How do I get started?", "Iron Forge")
