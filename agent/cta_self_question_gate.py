"""
cta_self_question_gate.py: the assertion that a plan pass may never write a
caption whose closing ask is an FAQ question mined about the gym itself, and
may never write the exact line that started this.

WHY THIS EXISTS
---------------
Dean Holcomb, CrossFit Reverb, support ticket 4941e162-2923-495f-8efb-d2554dea5aec,
2026-09-05: "Also, they all end with 'How do I get started with training at
CrossFit Reverb?' which doesn't make sense." He was right about why it doesn't
make sense: that line is an FAQ HEADING from his own approved source doc, written
in the READER's voice, asking about the gym BY NAME. A gym's own Instagram caption
ending with a question addressed to itself, using its own name in the third
person, reads as broken because it is broken -- it is not a call to action, it is
a question a prospect would type into Google, stapled onto the post it was mined
alongside.

caption_variety.py (this same track, PR #54) already fixed the MEASURED symptom:
that exact line was 96.8% of Reverb's book and it stopped being the shortest
"qualifying sentence" `grade_fix.py` could find. This module is the CONTENT rule
sitting next to it, enforced the same way day_shape.py is: at PLAN TIME, before a
single row reaches content_calendar, so a self-question CTA can never be written
again even if some future drafter path reintroduces the shape by a different
route than the one already patched.

Blake, 2026-09-06, re-sending the spec after the avatar-word portion of it was
correctly held back (HYROX/CrossFit/competitive-athlete audience is an explicit
standing rule, see config.avatar_athlete_rail_enabled -- this module does NOT
touch audience words, only CTA form):
  - Ban the string "How do I get started with training at CrossFit Reverb?" fleet-wide.
  - Ban any CTA phrased as a question the gym asks itself.

THE TWO CHECKS
--------------
1. EXACT STRING BAN. The literal Reverb line, normalized (case, whitespace,
   zero-width characters -- see copy_gate.scrub) is banned in ANY caption, for
   ANY gym. It was mined as an FAQ heading; it will never be a real CTA anywhere.

2. SELF-QUESTION FORM BAN. A caption's CLOSING line (caption_variety.closing_signature's
   raw input, not its normalized signature) is a self-question CTA when it is BOTH:
     a. phrased as a question (ends with '?'), AND
     b. asks how/what/when/where/whether one gets started, joins, signs up, or
        begins, at THIS GYM, NAMED. "Named" is required and load bearing: "How do
        I get started?" alone is a completely normal, answerable CTA a coach might
        write; "How do I get started at Iron Forge?" is the FAQ-mined shape, because
        no gym owner writes their own business's name into their own caption's
        closing question -- that only happens when a heading was pasted in whole.

Scoped this narrowly on purpose. A blanket "no question-form CTA" would ban
ordinary, legitimate direct-address CTAs ("Ready to start?", "Sound like you?")
that plenty of real gym copy uses well. The defect is not the question mark, it
is a business asking about itself as if it were an outside reader.

FLAGS
-----
  ECHO_CTA_SELF_QUESTION_GATE (config.cta_self_question_gate_enabled, default ON)
      Prevent-only: it can only stop a write, never cause one, so it ships armed
      by default under the same standing rule as ECHO_DAY_SHAPE_ASSERT.

Pure: no I/O, no clock, no writes.
"""
from __future__ import annotations

import re

from agent import copy_gate

# The exact line that started this. Normalized once at import time.
_BANNED_LITERAL_RAW = "How do I get started with training at CrossFit Reverb?"


def _normalize_for_literal_match(text: str) -> str:
    """Case/whitespace/zero-width-insensitive normalization for the literal ban.
    Mirrors caption_variety.normalize's scrub-then-lowercase-then-collapse, kept
    local and small so this module has no import-order dependency on it."""
    t = copy_gate.scrub(str(text or ""))
    return re.sub(r"\s+", " ", t).strip().lower()


_BANNED_LITERAL_NORM = _normalize_for_literal_match(_BANNED_LITERAL_RAW)

# "how/what/when/where/whether (do/does/can/should) I/we/you (get started/join/
# sign up/begin)". Deliberately does not require an exact phrase match beyond
# this -- "How do I get started training at Reverb" (no "with", no trailing
# CrossFit) is the same defect shape and the literal-string check above would
# miss it.
_SELF_QUESTION_STEM_RE = re.compile(
    r"\b(how|what|when|where|whether)\b.{0,40}\b"
    r"(get started|getting started|sign up|signup|join|begin|enroll)\b",
    re.I,
)


def _closing_line(caption: str) -> str:
    lines = [ln for ln in copy_gate.scrub(str(caption or "")).splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def contains_banned_literal(caption: str) -> bool:
    """True if `caption` contains the exact Reverb FAQ line anywhere, fleet-wide,
    for any gym -- not just as a closing line. It was never a real CTA and there
    is no context in which reusing it verbatim is intended."""
    norm = _normalize_for_literal_match(caption)
    return _BANNED_LITERAL_NORM in norm


def is_self_question_cta(caption: str, gym_display_name: str) -> bool:
    """True when the caption's CLOSING line is an FAQ-mined self-question CTA:
    phrased as a question, about getting started/joining/signing up, and naming
    the gym's own display name. `gym_display_name` empty or blank never matches
    (a gate that cannot identify the gym cannot judge self-reference)."""
    name = str(gym_display_name or "").strip()
    if not name:
        return False
    closing = _closing_line(caption)
    if not closing.endswith("?"):
        return False
    if not _SELF_QUESTION_STEM_RE.search(closing):
        return False
    return name.lower() in closing.lower()


class CtaSelfQuestionViolation:
    """One row whose caption trips one of the two bans. Carries enough to name
    the row and the reason out loud, the same shape as day_shape.DayViolation."""

    __slots__ = ("gym_id", "account", "post_date", "kind", "excerpt")

    def __init__(self, gym_id, account, post_date, kind, excerpt):
        self.gym_id = gym_id
        self.account = account
        self.post_date = post_date
        self.kind = kind  # 'banned_literal' or 'self_question'
        self.excerpt = excerpt

    def __repr__(self):
        return (f"CtaSelfQuestionViolation({self.gym_id} {self.account} "
                f"{self.post_date}: {self.kind})")

    def message(self):
        excerpt = (self.excerpt or "")[:100]
        if self.kind == "banned_literal":
            reason = "carries the banned Reverb FAQ line verbatim"
        else:
            reason = "closes on a self-question CTA (an FAQ mined about the gym itself)"
        return (f"{self.gym_id} {self.post_date} {self.account}: {reason} "
                f"({excerpt!r}).")


class CtaSelfQuestionGateViolation(Exception):
    """Raised when a plan pass tried to write a self-question or banned-literal
    CTA. Carries `.violations`, the full list, so the caller can log every row."""

    def __init__(self, violations):
        self.violations = list(violations or ())
        super().__init__("; ".join(v.message() for v in self.violations)
                         or "cta self-question gate violation")


_IGNORED_STATUS = ("deleted",)


def gate_violations(rows, gym_display_name: str):
    """Every row in `rows` (content_calendar-shaped dicts) that trips either
    ban. Pure. Ordered by (gym_id, post_date, account) so the report is stable."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").strip().lower() in _IGNORED_STATUS:
            continue
        caption = row.get("caption") or ""
        if not str(caption).strip():
            continue
        gym_id = str(row.get("gym_id") or "")
        account = str(row.get("account") or "")
        post_date = str(row.get("post_date") or "")
        if contains_banned_literal(caption):
            out.append(CtaSelfQuestionViolation(
                gym_id, account, post_date, "banned_literal", caption))
        elif is_self_question_cta(caption, gym_display_name):
            out.append(CtaSelfQuestionViolation(
                gym_id, account, post_date, "self_question", _closing_line(caption)))
    out.sort(key=lambda v: (v.gym_id, v.post_date, v.account))
    return out


def assert_no_self_question_cta(rows, gym_display_name: str, *, enabled=True):
    """FAIL the plan pass when any row carries a banned-literal or self-question
    closing CTA. Raises CtaSelfQuestionGateViolation carrying every offending
    row. Returns the (empty) list when the batch is clean.

    `enabled=False` (the ECHO_CTA_SELF_QUESTION_GATE escape hatch) skips the
    check entirely and returns []."""
    if not enabled:
        return []
    found = gate_violations(rows, gym_display_name)
    if found:
        raise CtaSelfQuestionGateViolation(found)
    return found
