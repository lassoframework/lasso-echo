"""calendar_grade.py — scores a planned (or published) month on the LASSO
Social Report Card rubric. Deterministic, offline, no API calls.

A calendar that cannot score >= 90 (A) DOES NOT STAGE. The planner remediates
and rescores in a loop; only an A reaches the human approval queue.
Distinct from grade_gate.py, which grades individual card IMAGES.

GYM PROFILE RUBRIC (Blake's ruling, 2026-08-27): the GYM profile does NOT
grade image quality. Clients upload their own media; Echo controls captions
and content mix only, so a gym can never be held below A for the photos it
chose to send. The visual_match leg is SKIPPED for GYM and the remaining five
legs (raw max 85) are RENORMALIZED to 0-100 by scaling the raw sum by 100/85
(chosen over redistributing visual's 15 points into other legs so every leg
keeps its documented point values and no leg's internal math changes). The
B2B profile still grades proof_numbers under the visual_match leg out of a
raw 100, unchanged. Letter bands and A_THRESHOLD=90 apply to the normalized
total for both profiles.

DUPLICATE COUNTING: one calendar post deliberately spans several rows — a
feed is cross-posted to Instagram AND Facebook, and its paired story carries
the same caption on the same date. Same-date rows sharing a caption hash are
therefore ONE post, never a repeat; a hash that appears on MORE THAN ONE
post_date is a true duplicate and is penalized per extra date.

POSTS, NOT ROWS (2026-08-31). The duplicate leg has always known that one post
spans several rows, but every OTHER caption-judged leg scored per ROW — so a
single caption missing its ask was counted two or three times (ENG's one HYROX
hook produced 3 defects; gritx's 30 flagged posts produced 88). Every
caption-judged leg (caption_craft, path_to_join, right_audience, content_mix)
now scores POSTS via posts_of(). The histogram stops lying and the score stops
depending on how many platforms a gym cross-posts to.

CAPTION-LESS POSTS ARE EXEMPT, EXPLICITLY (2026-08-31). A story carries its
caption BURNED ON THE MEDIA and a GBP photo post has no caption at all. Judging
those by feed-caption rules invents defects that no repair can ever clear —
51 of LASSO's 80 forward posts were held that way. Such posts are exempt from
the caption legs, COUNTED in CalendarGrade.exempt, and named in the digest.
They are never silently dropped.

RATE, NOT RAW COUNT (2026-08-31). caption_craft and path_to_join used to
subtract a fixed amount PER defective row, so a 168-row book and a 14-row book
were judged against the same absolute budget and any large book floored out at
the first handful of defects. A book that repaired 27 of its 30 bad posts
scored exactly the same as one that repaired none, which is precisely why the
nightly repair loop looked stuck. Those legs now deduct in proportion to the
SHARE of eligible posts that are defective, with the same worst-case penalty as
before, so partial progress actually moves the number.
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

# GYM profile: visual_match is skipped (clients own their media), so the raw
# max is 85 and the total is renormalized to 0-100 (see module docstring).
_GYM_RAW_MAX = sum(WEIGHTS.values()) - WEIGHTS["visual_match"]  # 85

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

# Worst-case deductions for the rate-scored legs. These reproduce the OLD
# floors exactly at a 100% defect rate (caption_craft floored at 8 = 20 - 12;
# path_to_join's ask check could take the leg to the floor), so a fully broken
# book scores what it always scored — only PARTIAL progress now registers.
_SOFT_FLAG_MAX_PENALTY = 12
_ASK_MAX_PENALTY = 7

# Fewest posts for the 25% mix cap to be a measurable question. At 12 posts a
# pillar may hold 3, so a four-pillar month satisfies it; at 7 posts it may hold
# 1, which demands seven distinct pillars and marks a gym down for arithmetic.
_MIX_CAP_MIN_POSTS = 12


@dataclass
class CalendarGrade:
    total: int
    letter: str
    scores: dict           # leg -> points
    defects: list = field(default_factory=list)   # (leg, row_ref, reason)
    exempt: dict = field(default_factory=dict)    # rule -> posts explicitly exempted


def posts_of(rows):
    """Group calendar ROWS into POSTS.

    One post deliberately spans several rows: an Instagram feed row, its
    Facebook mirror, and the paired story all carry the SAME caption on the
    SAME date. Scoring a caption per row therefore counts one defect two or
    three times. Every caption-judged leg groups first through here.

    Returns [((post_date, caption_hash), [row, ...]), ...] in stable row order.
    """
    groups: dict = {}
    for r in rows or []:
        key = (str(r.get("post_date") or "")[:10],
               caption_hash(r.get("caption") or ""))
        groups.setdefault(key, []).append(r)
    return list(groups.items())


def _has_caption(group) -> bool:
    """A post is judged by caption rules only when it HAS a caption.

    An empty caption is not a defect: a story's caption is burned onto its
    media and a GBP photo post carries none at all. See the module docstring —
    these posts are exempt explicitly and counted, never silently dropped."""
    return bool((group[0].get("caption") or "").strip())


def _eligible_posts(rows, exempt, rule):
    """The posts a caption rule may judge, recording how many it exempted."""
    posts = posts_of(rows)
    eligible = [(k, g) for k, g in posts if _has_caption(g)]
    skipped = len(posts) - len(eligible)
    if exempt is not None and skipped:
        exempt[rule] = skipped
    return eligible


def grade_month(rows, profile="GYM", quotas=None) -> CalendarGrade:
    scores, defects, exempt = {}, [], {}
    scores["consistency"]    = _consistency(rows, defects)
    scores["content_mix"]    = _content_mix(rows, profile, quotas, defects,
                                            exempt=exempt)
    scores["caption_craft"]  = _caption_craft(rows, defects, exempt=exempt)
    if profile == "B2B":
        # B2B grades proof numbers under the visual_match leg (unchanged).
        scores["visual_match"] = _proof_numbers(rows, defects)
    # GYM: NO visual_match leg. Clients upload their own media; Echo controls
    # captions and mix only, so image quality is never graded (Blake, 2026-08-27).
    scores["right_audience"] = _right_audience(rows, profile, defects,
                                               exempt=exempt)
    scores["path_to_join"]   = _path(rows, profile, defects, exempt=exempt)
    raw = sum(scores.values())
    if profile == "B2B":
        total = raw
    else:
        # Renormalize the five remaining legs (raw max 85) to 0-100.
        total = int(raw * 100 / _GYM_RAW_MAX + 0.5)

    # Spec: "the 20x repeat month grades F on consistency."
    # When consistency is zeroed by duplicate captions, the calendar cannot
    # score above F overall — cap total at 59 to ensure letter == "F".
    consistency_dup = any(
        d[0] == "consistency" and "repeated" in d[2]
        for d in defects
    )
    if scores["consistency"] == 0 and consistency_dup:
        total = min(total, 59)

    letter = next(l for floor, l in BANDS if total >= floor)
    return CalendarGrade(total, letter, scores, defects, exempt)


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

    # -- Duplicate caption_hash across DISTINCT post dates --
    # One post spans several rows BY DESIGN: a feed is cross-posted to
    # Instagram AND Facebook, and its paired story shares the caption on the
    # same date. Same-date rows sharing a hash are ONE post, not a repeat; a
    # hash seen on MORE THAN ONE date is a true duplicate (-8 per extra date).
    dates_by_hash: dict = {}
    for row in rows:
        cap = (row.get("caption") or "").strip()
        # EMPTY captions are BY DESIGN, never duplicates (2026-08-31: hash("") counted
        # LASSO's captionless story book + GBP photo posts as 'repeated 30 times',
        # zeroed consistency, and the dedupe purge then DENIED 100+ healthy rows on the
        # same primitive). A story's caption lives burned on its media and a GBP photo
        # post has no caption; matching nothing-to-nothing says nothing about repeats.
        if not cap:
            continue
        if str(row.get("format") or "").strip().lower() == "story":
            continue          # a story shares its paired feed's caption by design
        h = caption_hash(cap)
        d = str(row.get("post_date") or "")[:10]
        dates_by_hash.setdefault(h, set()).add(d)

    for h, ds in dates_by_hash.items():
        count = len(ds)
        if count > 1:
            # First date is "used", every additional date is a dup
            dups = count - 1
            defects.append(("consistency", h[:8], f"caption hash {h[:8]} repeated {count} times"))
            score -= 8 * dups

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: content_mix (max 20)
# ---------------------------------------------------------------------------

def _content_mix(rows, profile, quotas, defects, exempt=None) -> int:
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

    # Inline quota check: count categories.
    # SPRINT AWARENESS (Blake's 10-day sprint ruling): a summit SPRINT window is
    # intentionally summit-heavy (the backward-anchored cadence that tightens into
    # a continuous run before the event). Counting that intended concentration as a
    # content_mix defect would fail every summit-season month the A-gate must stage.
    # So summit rows dated inside a sprint window do NOT count toward the 25% cap
    # (they are Blake's design, not a mix flaw). Every OTHER category, and summit
    # OUTSIDE a sprint window, is capped exactly as before.
    from collections import Counter
    try:
        from . import summit_queue as _sq
        _sprint = set(_sq.sprint_days())
    except Exception:  # noqa: BLE001 - a summit-queue import issue never breaks grading
        _sprint = set()

    def _counts_toward_cap(r):
        cat = (r.get("pillar") or r.get("category") or "")
        if cat == "summit" and str(r.get("post_date") or "")[:10] in _sprint:
            return False  # intended sprint concentration, not a defect

        return True

    # Mix is a property of POSTS, not rows: a gym that cross-posts one photo to
    # IG + FB + a story has not published three servings of that pillar.
    post_groups = posts_of(rows)
    capped_posts = [grp for _k, grp in post_groups if _counts_toward_cap(grp[0])]
    cat_counts = Counter(
        (grp[0].get("pillar") or grp[0].get("category") or "")
        for grp in capped_posts
    )
    denom = len(capped_posts) or len(post_groups) or n

    # Each category over 25% of the CAP-ELIGIBLE posts: -3.
    #
    # SMALL BOOKS ARE EXEMPT, EXPLICITLY (2026-08-31). The cap asks "is this
    # MONTH dominated by one pillar", and that question is only well posed for
    # a month-sized book. On a 7-post book the cap allows floor(0.25 * 7) = 1
    # post per pillar, so satisfying it needs SEVEN distinct pillars; sunnyside
    # and topfuel were being marked down for arithmetic no repair could ever
    # clear, which is exactly the kind of unfixable flag that makes the nightly
    # loop look broken. Below _MIX_CAP_MIN_POSTS the cap is not measured, the
    # exemption is COUNTED, and the digest says so.
    if denom < _MIX_CAP_MIN_POSTS:
        if exempt is not None:
            exempt["content_mix: 25% cap not measurable below "
                   f"{_MIX_CAP_MIN_POSTS} posts"] = denom
    else:
        for cat, count in cat_counts.items():
            pct = count / denom
            if pct > 0.25:
                defects.append(("content_mix", cat,
                                f"{cat} is {pct:.0%} of posts (over 25%)"))
                score -= 3

    # Check unbacked proof/results slots. LIVE-SHAPE FIX (2026-08-31): the old check
    # read `vision_derived` / `media_kind`, which are NOT columns on content_calendar
    # (verified live: only `pillar` exists) — so EVERY proof/results row always flagged
    # "unbacked", a phantom defect nothing could ever fix (ENG's book was held at F on
    # three of these). A proof slot is honestly judged from what a row actually
    # carries: it is unbacked only when it has NO real media attached (empty image_url)
    # and no Drive-sourced asset. Rows that DO carry vision_derived/media_kind (test
    # fakes, a future migration) still honor them.
    for _k, grp in post_groups:
        row = grp[0]
        cat = (row.get("pillar") or row.get("category") or "").lower()
        if cat in ("proof", "results", "social_proof"):
            if any(r.get("vision_derived", False) for r in grp):
                continue
            if any(r.get("media_kind", "") in ("photo", "video") for r in grp):
                continue
            # A post is backed when ANY of its rows carries real media.
            has_media = any(bool((r.get("image_url") or "").strip()
                                 or (r.get("source_media_asset_id") or "").strip())
                            for r in grp)
            if not has_media:
                defects.append(("content_mix", row.get("post_date", ""),
                                "proof/results slot unbacked (no media on the row)"))
                score -= 4

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: caption_craft (max 20)
# ---------------------------------------------------------------------------

def _caption_craft(rows, defects, exempt=None) -> int:
    # Hard block: ANY row with violations -> 0 for the whole leg
    for row in rows:
        cap = row.get("caption") or ""
        viols = copy_gate.violations(cap)
        if viols:
            defects.append(("caption_craft", row.get("post_date", ""),
                            f"copy violations: {viols}"))
            return 0

    score = 20
    eligible = _eligible_posts(rows, exempt, "caption_craft: caption-less posts")
    if not eligible:
        return score

    # Soft flags, scored PER POST at a RATE (see module docstring). A book where
    # every post is flagged lands on the same floor of 8 it always did; a book
    # that repaired most of its posts now scores like it.
    flagged = 0
    for (day, _h), grp in eligible:
        flags = copy_gate.soft_flags(grp[0].get("caption") or "")
        if flags:
            flagged += 1
            for f in flags:
                defects.append(("caption_craft", day, f"soft flag: {f}"))
    score -= int(round(_SOFT_FLAG_MAX_PENALTY * flagged / len(eligible)))

    # Median caption length < 150: -4 (over posts, not rows)
    lengths = [len(grp[0].get("caption") or "") for _k, grp in eligible]
    try:
        med = statistics.median(lengths)
    except statistics.StatisticsError:
        med = lengths[0]
    if med < 150:
        defects.append(("caption_craft", "", f"median caption length {med:.0f} < 150"))
        score -= 4

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: visual_match (max 15) — NO LONGER GRADED for the GYM profile
# (Blake, 2026-08-27: clients upload their own media; Echo owns captions and
# mix only). Kept for reference/tooling; grade_month never calls it.
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

def _right_audience(rows, profile, defects, exempt=None) -> int:
    """Avatar leaks are scored PER POST, not per row. One off-avatar hook is one
    brand mistake, not three, however many platforms it is mirrored to. The
    per-post penalty is unchanged (-5 athlete, -2 elite), so a book with several
    genuinely off-avatar posts still loses the leg."""
    score = 15

    for (day, _h), grp in _eligible_posts(rows, exempt,
                                          "right_audience: caption-less posts"):
        cap = grp[0].get("caption") or ""
        first_line = cap.strip().splitlines()[0] if cap.strip() else ""

        if profile == "GYM":
            # Athlete-avatar leak: -5 each
            if _ATHLETE_WORDS.search(first_line):
                defects.append(("right_audience", day,
                                f"athlete-avatar leak in hook: {first_line[:60]}"))
                score -= 5

        # Hook intent mismatch: elite language: -2 each
        if _ELITE_WORDS.search(first_line):
            defects.append(("right_audience", day,
                            f"elite/advanced hook: {first_line[:60]}"))
            score -= 2

    return max(0, score)


# ---------------------------------------------------------------------------
# Leg: path_to_join (max 10)
# ---------------------------------------------------------------------------

def _path(rows, profile, defects, exempt=None) -> int:
    score = 10
    eligible = _eligible_posts(rows, exempt, "path_to_join: caption-less posts")
    n = len(eligible)
    if not n:
        return score

    # Every post carries an ask. Scored at a RATE so repairing most of a book
    # actually moves the leg (see module docstring); a book where NO post asks
    # still loses the same worst case it always did.
    missing = 0
    for (day, _h), grp in eligible:
        cap = grp[0].get("caption") or ""
        if not copy_gate.ASK_RE.search(cap):
            defects.append(("path_to_join", day, "no ask in caption"))
            missing += 1
    score -= int(round(_ASK_MAX_PENALTY * missing / n))

    # GYM: >= 5 posts pointing at booking-specific terms
    if profile != "B2B":
        want_booking = min(5, n)
        booking_posts = sum(
            1 for _k, grp in eligible
            if _BOOKING_RE.search(grp[0].get("caption") or "")
        )
        missing_booking = max(0, want_booking - booking_posts)
        for _ in range(missing_booking):
            defects.append(("path_to_join", "",
                            "not enough booking-specific asks"))
            score -= 1

    # B2B: >= 12 posts with call ask
    if profile == "B2B":
        want_call = min(12, n)
        call_posts = sum(
            1 for _k, grp in eligible
            if _B2B_CALL_RE.search(grp[0].get("caption") or "")
        )
        missing_call = max(0, want_call - call_posts)
        for _ in range(missing_call):
            defects.append(("path_to_join", "",
                            "not enough B2B call asks"))
            score -= 1

    # Bare typed URL as only ask
    for (day, _h), grp in eligible:
        cap = (grp[0].get("caption") or "").strip()
        if _BARE_URL_RE.search(cap) and not copy_gate.ASK_RE.search(cap):
            defects.append(("path_to_join", day,
                            "bare URL at end, no ask text"))
            score -= 1

    return max(0, score)
