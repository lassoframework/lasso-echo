"""lever_stamp.py — Wave 7 feature stamping: classify the LEVERS of a post at
draft time so the monthly retro can learn from them.

The retro can only learn levers it can see. These classifiers stamp every
content_calendar row (going forward, behind AGENT_LEARNING_LOOP) with:

  hook_family      question | bold_claim | story_open | number_lead | pain_callout
  ask_type         booking_link | dm | comment_keyword | bio | none
  caption_len_band short (<150) | mid (150-500) | long (>500)
  time_slot        the publish schedule band the post lands in
  has_member_face  only ever from the existing vision sidecar; never guessed here

PURE: no I/O, no network, no invented values. A caption these heuristics cannot
classify still gets an honest label (bold_claim / none), never a fabricated one.
The ask_type regexes are drawn from the SAME families as copy_gate.ASK_RE so the
gate and the learner agree on what an ask is.
"""
from __future__ import annotations

import re

HOOK_FAMILIES = ("question", "bold_claim", "story_open", "number_lead", "pain_callout")
ASK_TYPES = ("booking_link", "dm", "comment_keyword", "bio", "none")
LEN_BANDS = ("short", "mid", "long")

# ---- hook_family (heuristic on the FIRST line — the hook) ---------------------

_NUMBER_LEAD_RE = re.compile(r"^\s*\$?\d")
_PAIN_RE = re.compile(
    r"\b(tired of|sick of|struggling|stuck|frustrat|exhausted|overwhelmed|"
    r"burn(?:ed|t) out|can'?t seem|stop \w+ing|quit(?:ting)?|plateau|"
    r"nothing (?:works|is working)|fed up)\b", re.I)
_STORY_RE = re.compile(
    r"^\s*(i |i'|we |my |our |last (week|month|year|night)|when i|when we|"
    r"a (?:member|client|coach) |meet |this is )", re.I)


def hook_family(caption: str) -> str:
    """Classify the caption's HOOK (its first line) into one of HOOK_FAMILIES.

    Priority: an explicit question wins; then a number lead (digits open the
    line); then a pain callout (pain vocabulary anywhere in the hook); then a
    story open (first person / narrative opener); everything else is a
    bold_claim (the honest default for a declarative hook)."""
    t = str(caption or "").strip()
    first = t.splitlines()[0].strip() if t else ""
    if not first:
        return "bold_claim"
    if "?" in first:
        return "question"
    if _NUMBER_LEAD_RE.match(first):
        return "number_lead"
    if _PAIN_RE.search(first):
        return "pain_callout"
    if _STORY_RE.match(first):
        return "story_open"
    return "bold_claim"


# ---- ask_type (regex on the ASK_RE families from copy_gate) --------------------

_ASK_COMMENT_RE = re.compile(r"\bcomment [\"“']?\w+[\"”']?", re.I)
_ASK_DM_RE = re.compile(r"\b(dm us|dm [\"“']?\w+[\"”']?|message us)\b", re.I)
_ASK_BOOKING_RE = re.compile(
    r"\b(book (a|your) (call|intro|class|spot)|schedule (a|your)|sign up|"
    r"get started|claim your|reserve your|try a (free )?class|"
    r"start (here|today|your))\b", re.I)
_ASK_BIO_RE = re.compile(r"\blink in (our )?bio\b", re.I)


def ask_type(caption: str) -> str:
    """Classify the caption's ASK into one of ASK_TYPES.

    Priority: a comment keyword ask is the most specific, then a DM ask, then a
    booking/action ask, then a bare bio pointer. No ask at all -> 'none'
    (which copy_gate already soft-flags as no_ask)."""
    t = str(caption or "")
    if _ASK_COMMENT_RE.search(t):
        return "comment_keyword"
    if _ASK_DM_RE.search(t):
        return "dm"
    if _ASK_BOOKING_RE.search(t):
        return "booking_link"
    if _ASK_BIO_RE.search(t):
        return "bio"
    return "none"


# ---- caption_len_band ----------------------------------------------------------

def caption_len_band(caption: str) -> str:
    """short (<150) | mid (150-500) | long (>500), on the raw caption length."""
    n = len(str(caption or ""))
    if n < 150:
        return "short"
    if n <= 500:
        return "mid"
    return "long"


# ---- time_slot ------------------------------------------------------------------

def time_slot_band(hhmm: str) -> str:
    """Map an HH:MM publish time to a named slot band. Unparseable -> 'unknown'
    (never a guessed band)."""
    try:
        hour = int(str(hhmm).strip().split(":")[0])
    except (ValueError, AttributeError, IndexError):
        return "unknown"
    if 5 <= hour < 8:
        return "early_morning"
    if 8 <= hour < 11:
        return "morning"
    if 11 <= hour < 14:
        return "midday"
    if 14 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _default_time_for(fmt: str) -> str:
    """The publish schedule's default HH:MM for a feed vs story slot. Read from
    config lazily so the module imports clean in isolation."""
    try:
        from . import config
        if (fmt or "") == "story":
            return getattr(config, "POSTING_MORNING_TIME", "") or getattr(
                config, "POSTING_PRIMARY_TIME", "")
        return getattr(config, "POSTING_PRIMARY_TIME", "")
    except Exception:
        return ""


# ---- row stamping ----------------------------------------------------------------

def stamp_row(row: dict, scheduled_time: str | None = None) -> dict:
    """Stamp the Wave 7 lever columns onto ONE content_calendar row dict, in
    place, and return it. Only fills a lever that is missing/empty — an
    already-stamped row is never overwritten. has_member_face is stamped ONLY
    when the row already carries a vision-sidecar verdict (people flag); it is
    never inferred here.
    """
    if not isinstance(row, dict):
        return row
    caption = row.get("caption") or ""
    if not row.get("hook_family"):
        row["hook_family"] = hook_family(caption)
    if not row.get("ask_type"):
        row["ask_type"] = ask_type(caption)
    if not row.get("caption_len_band"):
        row["caption_len_band"] = caption_len_band(caption)
    if not row.get("time_slot"):
        hhmm = scheduled_time or _default_time_for(row.get("format") or "")
        row["time_slot"] = time_slot_band(hhmm) if hhmm else "unknown"
    # has_member_face: pass through an existing sidecar verdict only.
    if "has_member_face" not in row and isinstance(row.get("people"), bool):
        row["has_member_face"] = row["people"]
    return row
