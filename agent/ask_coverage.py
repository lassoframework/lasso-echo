"""
ask_coverage.py — the LASSO-lane ask rules (report-card build, 2026-08-28).

LASSO's audit in one sentence: "the ask is absent from every post that actually
found an audience" (74% of posts carried no next step; 0 of the top 5 reels by
plays carried an ask). This module enforces, at BUILD time on the planned LASSO
(B2B) month:

  1. REEL RULE: every VIDEO/REEL feed draft carries EXACTLY ONE clear ask —
     one ask family per publish_guard.ask_families, one destination per POST.
     Zero asks -> the approved default ask is appended. More than one family ->
     the redundant ask sentences are DELETED down to one family (and when a
     single sentence carries two families, every ask sentence is dropped and
     the default ask stands alone — always exactly one). Nothing here touches
     or assumes anything about the bio (Blake's ruling 2026-08-28: the bio's
     links stay).
  2. COVERAGE FLOOR: at least config.ask_coverage_floor() percent (default 70)
     of the month's FEED drafts carry an ask, leaving genuine no-ask room:
     testimonial / proof / welcome drafts stay askless while the floor is met
     without them.

HARD RULES: only DELETES redundant ask sentences or APPENDS the fixed approved
CTA phrase below — no facts, offers, numbers, or names are ever invented. The
default ask carries no dash and matches exactly ONE copy_gate.ASK_RE family, so
everything emitted passes copy_gate and publish_guard's multi_ask rail.

Behind AGENT_ASK_COVERAGE (default OFF); real_month_planner.apply_month_plan is
the single call site and guards the flag + the B2B profile, so gym-facing
months are untouched. Pure and injectable: no I/O, no network.
"""
from __future__ import annotations

import re

from . import config, copy_gate
from .publish_guard import ask_families

# The one approved mechanical CTA. Single ask family ('book' per
# publish_guard._ASK_FAMILY_HEAD), no dash, no invented fact/offer/number.
DEFAULT_ASK = "Book a call today."

# Pillars that are the plan's GENUINE no-ask room (pure proof / welcome posts):
# never given a mechanical ask while the coverage floor is met without them.
NO_ASK_ROOM = ("testimonial", "proof", "welcome")

_VIDEO_EXT_RE = re.compile(r"\.(mp4|mov|m4v|webm)(\?|$)", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def is_video_draft(draft) -> bool:
    """True when the draft's creative is a video (a reel). Honest signal only:
    the hosted/creative file extension. A text card or photo is never a reel."""
    for attr in ("creative_public_url", "creative_path"):
        val = str(getattr(draft, attr, "") or "")
        if _VIDEO_EXT_RE.search(val):
            return True
    return False


def _is_story(draft) -> bool:
    return bool(getattr(draft, "is_story", False)) or (
        str(getattr(draft, "draft_type", "") or "").strip().lower() == "story")


def ensure_single_ask(caption, default_ask=DEFAULT_ASK):
    """Return (new_caption, changed) where new_caption carries EXACTLY ONE ask
    family. Deterministic and safe:
      * no ask         -> append default_ask.
      * one family     -> unchanged (repeat matches of the SAME family are one
                          ask per publish_guard's family rule).
      * >1 families    -> drop the sentences that only add later families; if
                          the survivors still carry more than one family (a
                          single sentence with two asks), drop EVERY ask
                          sentence and append default_ask.
    Only deletes or appends; never rewrites a sentence, never invents copy."""
    cap = str(caption or "").rstrip()
    fams = ask_families(cap)
    if not fams:
        out = (cap + " " + default_ask).strip() if cap else default_ask
        return copy_gate.scrub(out), True
    if len(fams) == 1:
        return cap, False
    first_family = fams[0]
    kept = []
    for sentence in _SENTENCE_SPLIT_RE.split(cap):
        s_fams = ask_families(sentence)
        if s_fams and not (len(s_fams) == 1 and s_fams[0] == first_family):
            continue  # an extra-destination ask sentence: deleted
        kept.append(sentence.strip())
    out = " ".join(p for p in kept if p).strip()
    remaining = ask_families(out)
    if len(remaining) != 1:
        # A single sentence carried two families (or the first family fell with
        # a mixed sentence): drop every ask sentence, stand the default alone.
        kept = [p.strip() for p in _SENTENCE_SPLIT_RE.split(cap)
                if p.strip() and not ask_families(p)]
        out = (" ".join(kept) + " " + default_ask).strip()
    return copy_gate.scrub(out), True


def enforce_drafts(drafts, *, floor=None, default_ask=DEFAULT_ASK):
    """Enforce the reel rule + the coverage floor over a planned month's
    drafts, IN PLACE (draft.caption is rewritten per ensure_single_ask).
    Stories are untouched (their caption mirrors the paired feed and ships
    burned on media). Returns a summary dict for logs/tests."""
    if floor is None:
        floor = config.ask_coverage_floor() / 100.0
    feeds = [d for d in (drafts or []) if not _is_story(d)]
    reels_fixed = 0
    # 1. REEL RULE: every video feed carries exactly one ask.
    for d in feeds:
        if not is_video_draft(d):
            continue
        cap = getattr(d, "caption", "") or ""
        new_cap, changed = ensure_single_ask(cap, default_ask)
        if changed:
            d.caption = new_cap
            reels_fixed += 1
    # 2. COVERAGE FLOOR: raise no-ask feeds to the floor, no-ask room last.
    def _has_ask(d):
        return bool(ask_families(getattr(d, "caption", "") or ""))

    def _pillar(d):
        return str(getattr(d, "category", "") or "").strip().lower()

    total = len(feeds)
    floor_added = 0
    if total:
        import math
        covered = sum(1 for d in feeds if _has_ask(d))
        need = math.ceil(floor * total)
        candidates = sorted(
            (d for d in feeds if not _has_ask(d)),
            key=lambda d: (1 if _pillar(d) in NO_ASK_ROOM else 0,
                           str(getattr(d, "day_key", "") or "")))
        for d in candidates:
            if covered >= need:
                break
            cap = getattr(d, "caption", "") or ""
            if not str(cap).strip():
                continue  # the empty-caption guard owns blanks; never ask-pad one
            new_cap, changed = ensure_single_ask(cap, default_ask)
            if changed:
                d.caption = new_cap
                covered += 1
                floor_added += 1
        coverage = covered / total
    else:
        coverage = 0.0
    return {"feeds": total, "coverage": coverage, "reels_fixed": reels_fixed,
            "floor_added": floor_added, "floor": floor}
