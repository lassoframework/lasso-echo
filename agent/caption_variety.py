"""caption_variety.py — the anti-repetition rail for a gym's forward book.

WHY THIS EXISTS (Dean Holcomb, CrossFit Reverb, support ticket
4941e162-2923-495f-8efb-d2554dea5aec, 2026-09-05):

    "I reviewed the proposed posts and noticed a couple issues. All the captions
     are almost the same as one another. Also, they all end with 'How do I get
     started with training at CrossFit Reverb?' which doesn't make sense."

He was right, and the numbers were worse than the complaint. Measured on his
real book (93 rows, 2026-08-31..2026-09-30, 31 distinct days):

  * 90 of 93 rows (96.8%) ended with the SAME line.
  * 28 of his 31 day-captions (90.3%) opened with You / You're / You've / Your.
  * caption_len_band was 'mid' on 100% of rows; word count sd was 9 on a mean
    of 55, so every caption was the same size.
  * mean sentence count 4.9, sd 1.3: every caption was the same shape.
  * mean trigram Jaccard between his day-captions was 0.0917, the HIGHEST in
    the fleet (piercefitness 0.0047, fleet median ~0.023).

And it was fleet-wide, not a Reverb bug. Top closing line as a share of the
book: hillcountry 100%, train 87.5%, theboltonclub 86.2%, gritx 69.9%,
topfuel 61.2%, eng 53.9%.

THE POINT OF THIS MODULE. Variety was a PROMPT SUGGESTION before (drafter's
_avoid_openings_block, which never blocks anything). Nothing in the codebase
inspected a caption's TAIL or its SHAPE for repetition at all. This module makes
variety MECHANICAL and MEASURABLE: signatures you can count, collisions you can
assert on, and a selector that cannot hand back the same thing twice inside a
window.

WHAT THIS MODULE IS NOT. It writes no copy and invents nothing. It only
measures the shape of copy that already exists and chooses BETWEEN options a
caller already had. Every string it returns came in from the caller.
"""
from __future__ import annotations

import re
import unicodedata

from agent import copy_gate

# How far back a post has to be before its shape may be reused. A month of
# daily posting is ~31 posts; a 10-post window means a reader scrolling a gym's
# recent grid never sees the same opening or the same closing ask twice.
DEFAULT_WINDOW = 10

# Opening signature width. Four normalized content words is enough to catch
# "You're staring at the rig" vs "You're staring at the equipment" as the same
# move, which is exactly the collision Dean saw and the drafter's existing
# 4-word openings_collide check already uses.
OPENING_WORDS = 4

_WORD_RE = re.compile(r"[a-z0-9']+")
_SENT_SPLIT_RE = re.compile(r"[.!?]+(?:\s|$)")

# Second-person problem openers. Not banned, but capped: they were 90.3% of
# Reverb's book and 96.8% of hillcountry's. A hook family that is nearly the
# whole book is a monoculture whatever the family is.
_SECOND_PERSON_OPEN_RE = re.compile(r"^(you|your|you're|youre|you've|youve|you'll|youll)\b", re.I)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lowercase, strip invisible characters and accents, collapse whitespace.

    Runs copy_gate.scrub first so a zero-width character (the U+200B that
    prefixed every one of Reverb's 90 stapled CTAs) can never make two
    otherwise identical signatures look different.
    """
    t = copy_gate.scrub(str(text or ""))
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize(text))


def sentences(caption: str) -> list[str]:
    """Non-empty sentences, counted across the whole caption."""
    flat = re.sub(r"\s+", " ", copy_gate.scrub(str(caption or ""))).strip()
    if not flat:
        return []
    return [s.strip() for s in _SENT_SPLIT_RE.split(flat) if s.strip()]


# ---------------------------------------------------------------------------
# Signatures. Two captions colliding on any one of these read as "the same
# post again" to a human scrolling a feed.
# ---------------------------------------------------------------------------

def opening_signature(caption: str, words: int = OPENING_WORDS) -> str:
    """The first `words` normalized words of the caption's first line.

    Reverb had 19 distinct opening bigrams across 31 days, but 90.3% of them
    were the same "You + problem" move, which is why counting DISTINCT openers
    understated the sameness and this signature is deliberately wider.
    """
    first_line = (copy_gate.scrub(str(caption or "")).splitlines() or [""])[0]
    w = _WORD_RE.findall(normalize(first_line))
    return " ".join(w[:words])


def closing_signature(caption: str) -> str:
    """The caption's last non-empty line, normalized.

    THE signature nothing in the codebase looked at before. 90 of Reverb's 93
    rows shared this exact value.
    """
    lines = [ln for ln in copy_gate.scrub(str(caption or "")).splitlines() if ln.strip()]
    return normalize(lines[-1]) if lines else ""


def length_band(caption: str) -> str:
    """Word-count band. Bands are chosen so a normal gym book spreads across
    them; Reverb's whole book sat in one band."""
    n = len(_words(caption))
    if n < 25:
        return "short"
    if n < 45:
        return "mid"
    if n < 70:
        return "long"
    return "xlong"


def structure_signature(caption: str) -> str:
    """The caption's SHAPE, independent of its words.

    (sentence-count band, paragraph count band, opener move, ask present).
    Reverb's 31 captions produced essentially one value of this.
    """
    sents = len(sentences(caption))
    if sents <= 2:
        sent_band = "s1"
    elif sents <= 4:
        sent_band = "s3"
    elif sents <= 6:
        sent_band = "s5"
    else:
        sent_band = "s7"

    scrubbed = copy_gate.scrub(str(caption or ""))
    paras = len([p for p in re.split(r"\n\s*\n", scrubbed) if p.strip()])
    para_band = "p1" if paras <= 1 else ("p2" if paras == 2 else "p3")

    first_line = (scrubbed.splitlines() or [""])[0]
    if _SECOND_PERSON_OPEN_RE.match(first_line.strip()):
        opener = "2p"
    elif first_line.strip().endswith("?"):
        opener = "q"
    else:
        opener = "st"

    ask = "ask" if copy_gate.ASK_RE.search(scrubbed) else "noask"
    return f"{sent_band}/{para_band}/{opener}/{ask}"


def signatures(caption: str) -> dict:
    """Every signature for one caption, in one call."""
    return {
        "opening": opening_signature(caption),
        "closing": closing_signature(caption),
        "structure": structure_signature(caption),
        "length_band": length_band(caption),
    }


# ---------------------------------------------------------------------------
# Collision detection across a book
# ---------------------------------------------------------------------------

# The signature kinds a forward book is checked against. `length_band` is
# deliberately NOT a collision kind: four bands cannot fill a 31 day book
# without repeats. It is reported as a DISTRIBUTION instead (see report()).
COLLISION_KINDS = ("opening", "closing", "structure")


def _post_key(row) -> str:
    """One POST, not one row. The same-date IG feed + FB mirror + paired story
    are one post syndicated three ways (client_month_run._story_from_feed and
    the FB dict(row) copy), and counting them as three duplicates is exactly
    the mistake that made Reverb's book look like 93 posts with 31 captions.
    """
    return str(row.get("post_date") or row.get("date") or "")


def collisions(rows, window: int = DEFAULT_WINDOW, kinds=COLLISION_KINDS) -> list[dict]:
    """Every pair of POSTS inside `window` of each other sharing a signature.

    `rows` are content_calendar-shaped dicts with `post_date` and `caption`.
    Rows are collapsed to one post per date, then ordered by date; the window
    is counted in POSTS, so a gap in the calendar does not hide a collision.
    """
    by_date = {}
    for r in rows or []:
        key = _post_key(r)
        cap = r.get("caption") or ""
        if not key or not str(cap).strip():
            continue
        by_date.setdefault(key, cap)

    ordered = sorted(by_date.items())
    sigs = [(d, signatures(c)) for d, c in ordered]

    out = []
    for i, (date_i, sig_i) in enumerate(sigs):
        for j in range(i + 1, min(i + 1 + window, len(sigs))):
            date_j, sig_j = sigs[j]
            for kind in kinds:
                v = sig_i.get(kind)
                if v and v == sig_j.get(kind):
                    out.append({"kind": kind, "value": v,
                                "dates": [date_i, date_j],
                                "distance": j - i})
    return out


def report(rows, window: int = DEFAULT_WINDOW) -> dict:
    """The measurable sameness of a book, in the same numbers Part A used.

    Returns posts, distinct counts per signature kind, the share of the book
    held by the single most common value of each kind, and the collisions.
    A caller can assert on any of these; adjectives are not a metric.
    """
    by_date = {}
    for r in rows or []:
        key = _post_key(r)
        cap = r.get("caption") or ""
        if not key or not str(cap).strip():
            continue
        by_date.setdefault(key, cap)

    caps = list(by_date.values())
    n = len(caps)
    out = {"posts": n, "window": window, "collisions": [], "distinct": {},
           "top_share": {}, "length_bands": {}}
    if not n:
        return out

    for kind in ("opening", "closing", "structure", "length_band"):
        counts = {}
        for c in caps:
            counts[signatures(c)[kind]] = counts.get(signatures(c)[kind], 0) + 1
        out["distinct"][kind] = len(counts)
        out["top_share"][kind] = round(max(counts.values()) / n, 4)
        if kind == "length_band":
            out["length_bands"] = counts

    out["collisions"] = collisions(rows, window=window)
    return out


# ---------------------------------------------------------------------------
# The selector. This is the mechanical guarantee.
# ---------------------------------------------------------------------------

class NoOptionAvailable(Exception):
    """Every option in the pool would collide inside the window.

    Deliberately an exception and not a silent fallback: the caller must
    decide honestly (use no ask at all, widen the pool, or leave the post
    alone). Quietly returning a colliding value is how 90 identical CTAs
    happened in the first place.
    """


def pick_non_colliding(pool, recent, *, key=None):
    """Return the first item in `pool` whose key is not in `recent`.

    `pool` is an ORDERED list of options the caller already had (approved
    copy only; this function never authors anything). `recent` is the
    signatures already used inside the window. `key` maps an item to its
    signature and defaults to `normalize`.

    Raises NoOptionAvailable when every option collides.
    """
    keyfn = key or normalize
    used = {normalize(r) if isinstance(r, str) else r for r in (recent or [])}
    for item in pool or []:
        if keyfn(item) not in used:
            return item
    raise NoOptionAvailable(
        f"all {len(pool or [])} option(s) already used in the window")


# ---------------------------------------------------------------------------
# Per-post FORM PLAN
# ---------------------------------------------------------------------------
#
# WHY. The SB7 prompt asks for the same shape on every post: "Body max 260
# characters", plus a soft "VARY the ENTRY POINT. Do not open every caption the
# same way." A soft instruction repeated identically 31 times produces 31
# similar captions, which is what the fleet actually shows: 90.3% of Reverb's
# captions opened "You + problem", every caption sat in one length band, and
# mean sentence count was 4.9 with an sd of 1.3.
#
# The model was never told to write a DIFFERENT SHAPE today. These plans tell
# it, concretely, one shape per post. They are STYLE ONLY: a plan never carries
# a fact, never names a topic, and never overrides the approved source. The
# fabrication gate downstream is unchanged.

HOOK_FAMILIES = (
    "second_person_problem",   # "You're staring at the rig wondering..."
    "scene",                   # what is actually happening in the photo
    "myth_bust",               # a belief the reader holds that is not true
    "question",                # a real question, asked once
    "outcome_first",           # lead with where they end up
    "flat_statement",          # a short declarative, no wind up
)

LENGTH_PLANS = (
    ("short", 1, 2, 220),
    ("mid", 3, 4, 450),
    ("long", 4, 6, 700),
)


def form_plan(index, hook_families=HOOK_FAMILIES, length_plans=LENGTH_PLANS):
    """The FORM this post should take, as a function of its position in the book.

    The hook cycles every 6 and the length every 3, but 6 and 3 share a factor,
    so indexing both on `i` would pair the same hook with the same length every
    time and the book would carry only 6 distinct shapes. The length index
    carries a `i // len(hook_families)` drift so the PAIR does not repeat until
    post 18, which comfortably covers a month, while consecutive posts still
    always differ on both axes.

    Deterministic, so the same book replans identically and a diff stays
    reviewable. STYLE ONLY: returns no topic, no claim and no copy.
    """
    i = int(index)
    nh, nl = len(hook_families), len(length_plans)
    hook = hook_families[i % nh]
    band, smin, smax, cap = length_plans[(i + i // nh) % nl]
    return {"hook_family": hook, "length_band": band,
            "min_sentences": smin, "max_sentences": smax, "max_chars": cap}


def rotate(pool, index):
    """Deterministic round robin over `pool`.

    Used where a caller has an index (a day ordinal) rather than a history.
    Guarantees no repeat until the pool is exhausted, which is precisely the
    guarantee the auto-generated bible's '### CTA rotation (cycle in order,
    one per post)' section PROMISED and no code ever delivered: the old
    _booking_cta_for returned the first match and every post got it.
    """
    items = list(pool or [])
    if not items:
        return None
    return items[int(index) % len(items)]


def honest_ask_ceiling(n_posts, pool_size, window: int = DEFAULT_WINDOW) -> int:
    """The MOST asks a book of `n_posts` can honestly carry, given the gym has
    `pool_size` approved CTAs and no two posts inside `window` may share a
    closing.

    Blake, 2026-09-06: *"No fake/generic/repeated CTA just to hit a target."*
    The anti-repetition rail already refuses to repeat a closing inside the
    window, so a gym with one approved CTA physically cannot ask more than once
    per window. Asking it for a third of its book is asking it to repeat, which
    is the thing the ruling forbids and the thing Dean complained about.

    The rule is "no two posts INSIDE `window` of each other share a closing", so
    one CTA may reappear only once every `window + 1` posts, not once every
    `window`. The ceiling is therefore `p * ceil(n / (window + 1))`, never more
    than the book itself. The off-by-one is not cosmetic and was found by
    measuring rather than reasoning: with one CTA on a 31 post book at window
    10, `_fix_craft` places 3 (posts 1, 12, 23), not 4.

    This is a property of the gym's APPROVED CONTENT, not of the code, and it
    moves the moment a human adds a CTA to that gym's bible.
    """
    n = int(n_posts or 0)
    p = int(pool_size or 0)
    if n <= 0 or p <= 0:
        return 0
    w = int(window) if int(window or 0) > 0 else DEFAULT_WINDOW
    slots = -(-n // (w + 1))             # ceil(n / (w + 1)), integer only
    return min(n, p * slots)
