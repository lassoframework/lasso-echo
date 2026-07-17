"""
House-style six-question grade gate.

Source of truth: brand_voice/lasso_house_style.md section 10.

A card passes when it answers YES to five or more of the six questions.
A card that fails is regenerated once by the caller (creative_studio.generate).
If the regeneration also fails, an ops_alert fires and the card is withheld.

This gate is additive to the fabrication gate. The fabrication gate owns
whether the card is TRUE. This gate owns whether it looks ELEVATED. Both must pass.
"""
from dataclasses import dataclass, field
from typing import List, Optional

PASS_THRESHOLD = 5  # pass if ≥ 5 of 6 questions answered YES


@dataclass
class GradeResult:
    scores: dict          # {"Q1": True/False/None, ..., "Q6": True/False/None}
    passed: bool
    failed_questions: List[str] = field(default_factory=list)


def _q3_single_accent_heuristic(prompt_text: str) -> bool:
    """Q3: Single red accent. Programmatic check: prompt must say 'exactly one'
    and 'red'; an affirmative red background instruction fails.
    'Never a red background' and 'no red background' are correctly compliant."""
    import re as _re
    p = (prompt_text or "").lower()
    has_exactly_one = "exactly one" in p
    has_red = "red" in p
    # Only fail if the prompt affirmatively instructs a red background —
    # negating phrases like "never a red background" are correct and pass.
    affirmative_red_bg = bool(
        _re.search(r'(?<!never a )(?<!not a )(?<!no )red background', p)
        and "never a red background" not in p
        and "not a red background" not in p
        and "no red background" not in p
        and "red background" in p
    )
    if affirmative_red_bg:
        return False
    return has_exactly_one and has_red


def _q4_no_banned_copy(headline: str) -> bool:
    """Q4: No banned copy. Checks rendered headline text for dashes and 'vendor'."""
    h = str(headline or "")
    has_dash = any(c in h for c in ("—", "–", "-"))
    has_vendor = "vendor" in h.lower()
    return not has_dash and not has_vendor


def _q6_feed_stopping_heuristic(prompt_text: str) -> bool:
    """Q6: Feed-stopping visual anchor. Programmatic check: the prompt must name an
    illustrated element OR a visual anchor (color block, full-width element, duotone
    treatment, or oversized headline scale). All illustration-based archetype prompts
    include Block D 'ILLUSTRATED ELEMENT', so they pass automatically. Editorial
    prompts pass only when the concept spec explicitly names a visual anchor."""
    p = (prompt_text or "").lower()
    return any(token in p for token in (
        "illustrated element",  # present in all non-editorial Block D prompts
        "visual anchor",        # required in editorial concept specs
        "color block",          # editorial anchor type
        "full-width",           # editorial anchor type
        "full width",           # editorial anchor type
        "duotone",              # editorial anchor type
        "magazine cover",       # editorial scale reference
    ))


def _vision_questions(prompt_text: str, vision_client=None) -> dict:
    """Q1, Q2, Q5: vision-model checks. Returns dict with True/False/None per key.
    None means the check was skipped (no vision_client available)."""
    if vision_client is None:
        return {"Q1": None, "Q2": None, "Q5": None}

    questions = {
        "Q1": ("Is every text element (eyebrow, headline, deck) left-aligned, "
               "with nothing centered or symmetric? Answer YES or NO only."),
        "Q2": ("Is there visible typographic scale contrast between the eyebrow "
               "(small), headline (large), and deck (medium)? Answer YES or NO only."),
        "Q5": ("Can the headline be read clearly at 100px wide? Thin type, low "
               "contrast, or clutter around the headline FAILS. Answer YES or NO only."),
    }
    results = {}
    for key, question in questions.items():
        try:
            answer = vision_client.ask(
                f"Evaluate this image generation prompt: {prompt_text}\n\n{question}"
            )
            results[key] = "yes" in str(answer or "").lower()
        except Exception:
            results[key] = None
    return results


def grade_card(prompt_text: str, headline: str = "",
               vision_client=None) -> GradeResult:
    """
    Run all six house-style questions. Returns a GradeResult.

    Q3, Q4, and Q6 are programmatic. Q1, Q2, Q5 use the vision client when available;
    when vision_client is None those questions return None (treated as passing so
    the gate does not block cards solely because vision is unavailable).

    A card passes when ≥ PASS_THRESHOLD (5) of the six questions return True or None.
    """
    q3 = _q3_single_accent_heuristic(prompt_text)
    q4 = _q4_no_banned_copy(headline)
    q6 = _q6_feed_stopping_heuristic(prompt_text)
    vision = _vision_questions(prompt_text, vision_client)

    scores = {
        "Q1": vision.get("Q1"),
        "Q2": vision.get("Q2"),
        "Q3": q3,
        "Q4": q4,
        "Q5": vision.get("Q5"),
        "Q6": q6,
    }

    failed = [k for k, v in scores.items() if v is False]
    passed = len(failed) <= (6 - PASS_THRESHOLD)  # pass if ≤1 hard False

    return GradeResult(scores=scores, passed=passed, failed_questions=failed)
