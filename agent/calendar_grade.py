"""calendar_grade.py — scores a planned (or published) month on the LASSO
Social Report Card rubric. Deterministic, offline, no API calls.

A calendar that cannot score >= 90 (A) DOES NOT STAGE. The planner remediates
and rescores in a loop; only an A reaches the human approval queue.
Distinct from grade_gate.py, which grades individual card IMAGES.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import List

from agent import copy_gate
from agent.caption_ledger import caption_hash

WEIGHTS = {
    "consistency": 20,
    "content_mix": 20,
    "caption_craft": 20,
    "visual_match": 15,
    "right_audience": 15,
    "path_to_join": 10,
}
A_THRESHOLD = 90
BANDS = ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F"))

# Athlete-avatar leak words (first-line scan)
_ATHLETE_WORDS = re.compile(
    r"\b(compete|competition|athlete|HYROX|pr your|one.rep max)\b", re.I
)
# Hook-intent mismatch: elite language
_ELITE_WORDS = re.compile(r"\b(elite|advanced athlete)\b", re.I)

# GYM booking-specific ask terms
_BOOKING_RE = re.compile(
    r"(book|link in bio|sign up|get started|reserve|try a|schedule)", re.I
)

# B2B call ask
_B2B_CALL_RE = re.compile(r"(book a call|dm us|schedule)", re.I)

# Bare URL at end of caption (no other ask text nearby)
_BARE_URL_RE = re.compile(r"https?://\S+\s*$")

# Number in caption
_NUMBER_RE = re.compile(r"[0-9]+")

# @mention in caption
_MENTION_RE = re.compile(r"@\w+")


@dataclass
class CalendarGrade:
    total: int
    letter: str
    scores: dict           # leg -> points
    defects: list = field(default_factory=list)   # (leg, row_ref, reason)


def grade_month(rows, profile="GYM", quotas=None) -> CalendarGrade:
    scores, defects = {}, []
    scores["consistency"]    = _consistency(rows, defects)
    scores["content_mix"]    = _content_mix(rows, profile, quotas, defects)
    scores["caption_craft"]  = _caption_craft(rows, defects)
    scores["visual_match"]   = (
        _proof_numbers(rows, defects) if profile == "B2B"
        else _visual_match(rows, defects)
    )
    scores["right_audience"] = _right_audience(rows, profile, defects)
    scores["path_to_join"]   = _path(rows, profile, defects)
    total = sum(scores.values())
    letter = next(l for floor, l in BANDS if total >= floor)
    return CalendarGrade(total, letter, scores, defects)


# ---------------------------------------------------------------------------
# Leg: consistency (max 20)
# ---------------------------------------------------------------------------

def _consistency(rows, defects) -> int:
    score = 20

    # -- Day-coverage gap detection --
    dates = sorted({(r.get("post_date") or "")[:10] for r in rows if r.get("post_date")})
    if len(dates) >= 2:
        from datetime import date as _date
        for i in range(1, len(dates)):
            try:
                prev = _date.fromisoformat(dates[i - 1])
                curr = _date.fromisoformat(dates[i])
                gap = (curr - prev).days - 1
                if gap > 3:
                    defects.append(("consistency", dates[i], f"gap of {gap} days before {dates[i]}"))
                    score -= 8
                elif gap == 1:
                    defects.append(("consistency", dates[i], f"gap of 1 day before {dates[i]}"))
                    score -= 4
            except ValueError:
                pass

    # -- Duplicate caption_hash within the plan --
    hashes: list = []
    for row in rows:
        cap = row.get("caption") or ""
        hashes.append(caption_hash(cap))

    from collections import Counter
    hash_counts = Counter(hashes)
    for h, count in hash_counts.items():
        if count > 1:
            # First occurrence is "used", every additional is a dup
            dups = count - 1
            defects.append(("consistency", h[:8], f"caption hash {h[:8]} repeated {count} times"))
            score -= 8 * dups

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: content_mix (max 20)
# ---------------------------------------------------------------------------

def _content_mix(rows, profile, quotas, defects) -> int:
    score = 20
    n = len(rows)
    if n == 0:
        return 0

    # Try category_plan.validate_quotas if available
    if quotas is not None:
        try:
            from agent import category_plan
            vq = getattr(category_plan, "validate_quotas", None)
            if vq is not None:
                misses = vq(rows, quotas) or []
                for miss in misses:
                    defects.append(("content_mix", "", f"quota miss: {miss}"))
                    score -= 3
        except Exception:
            pass

    # Inline quota check: count categories
    from collections import Counter
    cats = [r.get("pillar") or r.get("category") or "" for r in rows]
    cat_counts = Counter(cats)

    # Each category over 25% of rows: -3
    for cat, count in cat_counts.items():
        pct = count / n
        if pct > 0.25:
            defects.append(("content_mix", cat, f"{cat} is {pct:.0%} of posts (over 25%)"))
            score -= 3

    # Check unbacked proof/results slots
    for row in rows:
        cat = (row.get("pillar") or row.get("category") or "").lower()
        if cat in ("proof", "results", "social_proof"):
            vision_derived = row.get("vision_derived", False)
            media_kind = row.get("media_kind", "")
            if not vision_derived and media_kind not in ("photo", "video"):
                defects.append(("content_mix", row.get("post_date", ""),
                                "proof/results slot unbacked (no vision-derived media)"))
                score -= 4

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: caption_craft (max 20)
# ---------------------------------------------------------------------------

def _caption_craft(rows, defects) -> int:
    # Hard block: ANY row with violations -> 0 for the whole leg
    for row in rows:
        cap = row.get("caption") or ""
        viols = copy_gate.violations(cap)
        if viols:
            defects.append(("caption_craft", row.get("post_date", ""),
                            f"copy violations: {viols}"))
            return 0

    score = 20

    # Soft flags: -1 each, floor at 8
    total_soft = 0
    for row in rows:
        cap = row.get("caption") or ""
        flags = copy_gate.soft_flags(cap)
        for f in flags:
            defects.append(("caption_craft", row.get("post_date", ""),
                            f"soft flag: {f}"))
            total_soft += 1

    score = max(8, score - total_soft)

    # Median caption length < 150: -4
    lengths = [len(r.get("caption") or "") for r in rows]
    if lengths:
        try:
            med = statistics.median(lengths)
        except statistics.StatisticsError:
            med = lengths[0] if lengths else 0
        if med < 150:
            defects.append(("caption_craft", "", f"median caption length {med:.0f} < 150"))
            score -= 4

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: visual_match (max 15, GYM profile)
# ---------------------------------------------------------------------------

def _visual_match(rows, defects) -> int:
    score = 15

    template_ids = set()
    for row in rows:
        vision_derived = row.get("vision_derived", False)
        media_url = row.get("media_url") or row.get("image_url") or ""
        template_id = row.get("template_id") or ""

        if not vision_derived:
            if not media_url or "stock" in media_url.lower():
                defects.append(("visual_match", row.get("post_date", ""),
                                "stock asset detected (no vision-derived media)"))
                score -= 5
            else:
                defects.append(("visual_match", row.get("post_date", ""),
                                "row not drafted from actual media"))
                score -= 3

        if template_id:
            template_ids.add(template_id)

    # Multiple different template_ids: -3 if > 1 distinct
    if len(template_ids) > 1:
        defects.append(("visual_match", "", f"mixed templates: {len(template_ids)} distinct template_ids"))
        score -= 3

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: proof_numbers (max 15, B2B profile)
# ---------------------------------------------------------------------------

def _proof_numbers(rows, defects) -> int:
    score = 15

    n = len(rows)
    want = min(8, n)

    # Count rows with a real number in caption
    rows_with_number = sum(
        1 for r in rows if _NUMBER_RE.search(r.get("caption") or "")
    )
    missing_numbers = max(0, want - rows_with_number)
    for _ in range(missing_numbers):
        score -= 1
    if missing_numbers:
        defects.append(("visual_match", "", f"only {rows_with_number}/{want} captions contain a number"))

    # Count rows with @mention of a client gym
    rows_with_mention = sum(
        1 for r in rows if _MENTION_RE.search(r.get("caption") or "")
    )
    missing_mentions = max(0, want - rows_with_mention)
    for _ in range(missing_mentions):
        score -= 1
    if missing_mentions:
        defects.append(("visual_match", "", f"only {rows_with_mention}/{want} captions contain a @mention"))

    # Mixed gym_count claims: detect different number claims like "500+" vs "1000+"
    _CLAIM_RE = re.compile(r"(\d[\d,]+\+?)\s*(?:gyms?|clients?|members?)", re.I)
    claim_vals = set()
    for r in rows:
        cap = r.get("caption") or ""
        for m in _CLAIM_RE.finditer(cap):
            claim_vals.add(m.group(1))
    if len(claim_vals) > 1:
        defects.append(("visual_match", "", f"mixed gym_count claims: {claim_vals}"))
        score -= 3

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: right_audience (max 15)
# ---------------------------------------------------------------------------

def _right_audience(rows, profile, defects) -> int:
    score = 15

    for row in rows:
        cap = row.get("caption") or ""
        first_line = cap.strip().splitlines()[0] if cap.strip() else ""

        if profile == "GYM":
            # Athlete-avatar leak: -5 each
            if _ATHLETE_WORDS.search(first_line):
                defects.append(("right_audience", row.get("post_date", ""),
                                f"athlete-avatar leak in hook: {first_line[:60]}"))
                score -= 5

        # Hook intent mismatch: elite language: -2 each
        if _ELITE_WORDS.search(first_line):
            defects.append(("right_audience", row.get("post_date", ""),
                            f"elite/advanced hook: {first_line[:60]}"))
            score -= 2

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: path_to_join (max 10)
# ---------------------------------------------------------------------------

def _path(rows, profile, defects) -> int:
    score = 10
    n = len(rows)

    # 100% of rows carry exactly one ask (ASK_RE match)
    for row in rows:
        cap = row.get("caption") or ""
        if not copy_gate.ASK_RE.search(cap):
            defects.append(("path_to_join", row.get("post_date", ""),
                            "no ask in caption"))
            score -= 1

    # GYM: >= 5 rows pointing at booking-specific terms
    if profile != "B2B":
        want_booking = min(5, n)
        booking_rows = sum(
            1 for r in rows if _BOOKING_RE.search(r.get("caption") or "")
        )
        missing_booking = max(0, want_booking - booking_rows)
        for _ in range(missing_booking):
            defects.append(("path_to_join", "",
                            "not enough booking-specific asks"))
            score -= 1

    # B2B: >= 12 rows with call ask
    if profile == "B2B":
        want_call = min(12, n)
        call_rows = sum(
            1 for r in rows if _B2B_CALL_RE.search(r.get("caption") or "")
        )
        missing_call = max(0, want_call - call_rows)
        for _ in range(missing_call):
            defects.append(("path_to_join", "",
                            "not enough B2B call asks"))
            score -= 1

    # Bare typed URL as only ask
    for row in rows:
        cap = (row.get("caption") or "").strip()
        if _BARE_URL_RE.search(cap) and not copy_gate.ASK_RE.search(cap):
            defects.append(("path_to_join", row.get("post_date", ""),
                            "bare URL at end, no ask text"))
            score -= 1

    return max(0, score)
