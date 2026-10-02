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
    # status/reason (Blake, 2026-09-13, REAL PIXEL GRADING): grade_card/grade_image
    # keep their original True-passes-by-default shape for backward compatibility
    # with every existing caller and test of THOSE two functions. `evaluate()`
    # below is the new SINGLE authoritative policy creative_studio.generate() is
    # wired to: it never silently passes on a skipped/failed/unparseable check.
    # status is one of "PASS", "FAIL", "UNGRADED" ("" on a GradeResult built by
    # the legacy grade_card/grade_image path, which does not set it).
    status: str = ""
    reason: str = ""


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
        "focal graphic",        # canvas/layout-override cards (split, navy, etc.)
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


def _vision_image_questions(image_bytes: bytes, vision_client) -> dict:
    """Q1, Q2, Q5: vision checks on the ACTUAL generated image bytes.
    vision_client must implement ask_image(image_bytes, question) -> str.
    Returns True/False/None per key (None on exception)."""
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
            answer = vision_client.ask_image(image_bytes, question)
            results[key] = "yes" in str(answer or "").lower()
        except Exception:
            results[key] = None
    return results


def grade_image(image_bytes: bytes, headline: str = "",
                vision_client=None) -> GradeResult:
    """
    Check Q1, Q2, Q5 on the ACTUAL generated image bytes via a vision model.
    Q3, Q4, Q6 are not re-checked here — they are prompt-level checks in grade_card.

    When vision_client is None all three return None (treated as passing) so the
    image gate never blocks a card solely because the vision client is unavailable.
    """
    if vision_client is None:
        scores = {"Q1": None, "Q2": None, "Q3": True, "Q4": True, "Q5": None, "Q6": True}
        return GradeResult(scores=scores, passed=True, failed_questions=[])

    vision = _vision_image_questions(image_bytes, vision_client)
    scores = {
        "Q1": vision.get("Q1"),
        "Q2": vision.get("Q2"),
        "Q3": True,
        "Q4": True,
        "Q5": vision.get("Q5"),
        "Q6": True,
    }
    failed = [k for k, v in scores.items() if v is False]
    passed = len(failed) <= (6 - PASS_THRESHOLD)  # pass if ≤1 hard False
    return GradeResult(scores=scores, passed=passed, failed_questions=failed)


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


# ---------------------------------------------------------------------------
# evaluate() — the SINGLE authoritative grading policy (spec section 5)
#
# Fixes three things the old grade_card/grade_image split allowed:
#   1. Q3 (single accent) and Q6 (visual anchor / explains the idea) were
#      "evidence" from STRING-MATCHING THE PROMPT ("does it say 'exactly one'
#      and 'red'", "does it say 'visual anchor'") — proof the brief ASKED for
#      something, never proof the rendered image HAS it. evaluate() checks both
#      on the actual image bytes via vision_client.ask_image, same as Q1/Q2/Q5.
#   2. A missing/failed vision client made grade_card/grade_image auto-pass
#      (None -> treated as True). evaluate() returns status="UNGRADED" instead
#      — never a silent approval — so the caller routes it to human review.
#   3. Two callers (grade_card's prompt-level pass/fail and grade_image's
#      image-level pass/fail) could disagree with whatever creative_studio.
#      generate() then decided on its own. evaluate() is the one function whose
#      `.passed`/`.status` the retry loop acts on directly, so there is no
#      second policy to drift from it.
#
# grade_card/grade_image are UNCHANGED above this comment (existing tests and
# callers keep working) but are no longer wired into creative_studio.generate();
# see that module for the call site.
# ---------------------------------------------------------------------------


def _ask_yes_no(vision_client, image_bytes, question):
    """True/False/None (None on any exception or an unparseable answer that
    starts with neither yes nor no) — never guesses a pass on failure."""
    try:
        answer = str(vision_client.ask_image(image_bytes, question) or "").strip().lower()
    except Exception:
        return None
    if answer.startswith("yes"):
        return True
    if answer.startswith("no"):
        return False
    # Tolerate "YES." / "Yes, because..." style answers that don't start clean.
    if "yes" in answer.split(".")[0].split(",")[0]:
        return True
    if "no" in answer.split(".")[0].split(",")[0]:
        return False
    return None


def _approved_copy_block(headline, cta, facts, footer=""):
    """The approved-copy block the Q7 vision check compares the rendered image
    against. MUST include the URL footer (Blake, 2026-09-13, live-sample bug):
    every card in this pipeline also renders a URL footer line
    (astra_prompt.url_footer(), e.g. "LASSOFRAMEWORK.COM") that is NOT part of
    headline/cta/facts. Omitting it here made evaluate() hard-block every
    single card in the first live run for correctly rendering its own approved
    footer — a false positive from an incomplete approved-copy list, not an
    actual fabrication. Always pass the footer actually used on the card."""
    lines = [f"HEADLINE: {headline or '(none)'}"]
    if str(cta or "").strip():
        lines.append(f"CTA: {cta}")
    for f in (facts or []):
        if str(f).strip():
            lines.append(f"APPROVED FACT: {f}")
    if str(footer or "").strip():
        lines.append(f"URL FOOTER (always rendered, approved): {footer}")
    return "\n".join(lines)


def _check_critical_copy(vision_client, image_bytes, approved_copy):
    """Q7, the hard-block check (spec: 'critical copy errors ... must hard-block
    approval regardless of overall score'). Returns (ok, detail):
      ok=True  -> rendered text matches the approved copy, nothing invented
      ok=False -> a name/number/date/URL/claim was wrong or fabricated (HARD FAIL)
      ok=None  -> could not be verified (exception or an unparseable answer);
                  the caller treats None the same as a fail for routing purposes
                  (UNGRADED, never a silent pass) but reports it distinctly."""
    question = (
        "Read every piece of rendered text on this image exactly as it appears: "
        "the headline, any call-to-action button text, any numbers, dates, "
        "URLs, names, or claims. Compare what you read against this APPROVED "
        f"SOURCE COPY (the only facts this card is allowed to state):\n"
        f"{approved_copy}\n\n"
        "Does the image invent, alter, or misstate ANY name, number, date, URL, "
        "or claim that is not in the approved source copy above, or render the "
        "approved copy incorrectly (wrong word, wrong number, wrong date)? "
        "Start your answer with exactly one word, YES or NO, then one short "
        "sentence naming the specific error if YES."
    )
    try:
        answer = str(vision_client.ask_image(image_bytes, question) or "").strip()
    except Exception as exc:
        return None, f"vision check raised {type(exc).__name__}: {exc}"
    low = answer.lower()
    if low.startswith("yes"):
        return False, answer
    if low.startswith("no"):
        return True, answer
    return None, f"unparseable vision response: {answer[:200]}"


def evaluate(image_bytes, prompt_text="", headline="", cta="", facts=None,
             footer="", vision_client=None) -> GradeResult:
    """The one grading decision creative_studio.generate()'s retry loop acts on.

    No vision_client -> GradeResult(passed=False, status="UNGRADED"): the image
    was never actually inspected, so it can never be silently approved. The
    caller routes UNGRADED to the same human-review path as a FAIL (see
    creative_studio.generate), it just reports differently in the generation
    record and the ops alert (spec section 5: "these must become an explicit
    UNGRADED/ERROR state that routes to human review, never silent approval").

    Q1/Q2/Q5: unchanged vision-image questions (alignment, scale contrast,
    thumbnail legibility). Q3/Q6: now asked of the ACTUAL IMAGE, not inferred
    from the prompt text. Q4: the rendered-headline banned-copy text check
    (dashes/"vendor") — a real check on the approved words themselves, not a
    prompt-keyword proxy for image quality, so it stays a plain text check.
    Q7: critical copy accuracy against the approved headline/cta/facts, a HARD
    BLOCK — any False collapses the whole result to FAIL regardless of the
    other six scores, and an unparseable/exception result on Q7 is UNGRADED
    rather than assumed clean.
    """
    facts = facts or []
    if vision_client is None:
        scores = {"Q1": None, "Q2": None, "Q3": None, "Q4": _q4_no_banned_copy(headline),
                  "Q5": None, "Q6": None, "Q7": None}
        return GradeResult(scores=scores, passed=False, failed_questions=[],
                           status="UNGRADED",
                           reason="no vision client available; image was never inspected")

    vision = _vision_image_questions(image_bytes, vision_client)  # Q1, Q2, Q5
    q3 = _ask_yes_no(
        vision_client, image_bytes,
        "Look at the whole image. Is there exactly ONE clearly dominant accent "
        "color used deliberately and sparingly for emphasis (never scattered "
        "across many elements, never a full-bleed background)? Answer YES or NO only.")
    q4 = _q4_no_banned_copy(headline)
    q6 = _ask_yes_no(
        vision_client, image_bytes,
        "Does this image contain one clear visual anchor (a diagram, chart, "
        "bold color block, labeled process, or oversized headline scale) that "
        "actually explains the card's idea, rather than reading as plain "
        "undifferentiated body text? Answer YES or NO only.")
    approved_copy = _approved_copy_block(headline, cta, facts, footer=footer)
    q7, q7_detail = _check_critical_copy(vision_client, image_bytes, approved_copy)

    scores = {"Q1": vision.get("Q1"), "Q2": vision.get("Q2"), "Q3": q3, "Q4": q4,
              "Q5": vision.get("Q5"), "Q6": q6, "Q7": q7}
    failed = [k for k, v in scores.items() if v is False]

    if q7 is False:
        return GradeResult(scores=scores, passed=False, failed_questions=failed,
                           status="FAIL",
                           reason=f"critical copy error (hard block): {q7_detail}")
    if q7 is None:
        return GradeResult(scores=scores, passed=False, failed_questions=failed,
                           status="UNGRADED",
                           reason=f"could not verify rendered copy: {q7_detail}")

    # Q7 passed; the remaining six use the same ≤1-hard-fail threshold as
    # grade_card/grade_image (PASS_THRESHOLD = 5 of 6).
    passed = len([k for k in failed if k != "Q7"]) <= (6 - PASS_THRESHOLD)
    return GradeResult(scores=scores, passed=passed, failed_questions=failed,
                       status=("PASS" if passed else "FAIL"))


_CORRECTIVE_HINTS = {
    "Q1": "Left-align every text element on the card; nothing may sit centered "
          "or symmetric.",
    "Q2": "Push the type scale contrast further: the headline must read "
          "dramatically larger than the eyebrow and deck lines so the "
          "hierarchy is obvious at a glance.",
    "Q3": "The accent color is either missing, duplicated, or scattered across "
          "more than one element. Pick exactly ONE accent color and use it "
          "exactly once; every other element stays in the base palette.",
    "Q4": "The rendered headline used a banned character or word (a dash, or "
          "the word 'vendor'). Re-render the headline exactly as approved, "
          "with no dash and no banned word.",
    "Q5": "The headline is too small or too low-contrast to read at thumbnail "
          "size. Enlarge the headline and raise contrast between the type and "
          "the field behind it.",
    "Q6": "The card reads as plain, undifferentiated text with no visual "
          "anchor. Add one clear diagram, chart, or bold color block that "
          "actually explains the idea, sized as the dominant element.",
    "Q7": "The previous render stated a name, number, date, URL, or claim that "
          "was not in the approved copy, or misrendered the approved copy. "
          "Render ONLY the exact approved headline, CTA, and facts given below "
          "— invent nothing, alter nothing.",
}


def corrective_instruction(grade_result: GradeResult) -> str:
    """A specific, actionable correction for the NEXT Astra request (spec
    section 6: pass the SPECIFIC visual failure, not the identical brief).
    "" when there is nothing to correct (a pass, or no result yet)."""
    if grade_result is None:
        return ""
    notes = [_CORRECTIVE_HINTS[q] for q in (grade_result.failed_questions or [])
             if q in _CORRECTIVE_HINTS]
    if grade_result.status == "UNGRADED" and grade_result.reason:
        notes.append(f"(Previous attempt could not be graded: {grade_result.reason})")
    if not notes:
        return ""
    return ("CORRECTIVE FEEDBACK FROM THE PREVIOUS ATTEMPT (fix this specifically "
            "on this render; do not just resend an identical card): "
            + " ".join(notes))
