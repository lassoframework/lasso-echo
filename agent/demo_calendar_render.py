"""
demo_calendar_render.py — PIL compositor for the 30-day demo calendar cards.

Renders the 30 feed cards (1080x1080) and 30 paired story cards (1080x1920), two posts
per day, for the done-for-you demo calendar to content_library/demo_calendar/. Pure PIL:
no API key,
no network, no model-rendered (garble-prone) text. Reuses the summit_render font set
and text helpers so type is house style and dash-safe.

Design rules honored here:
  * Clean data-viz / editorial ONLY. No illustrated scenes, no figures, no photos, no
    AI-looking art (feedback_creative_quality + the creative_studio banned list). Every
    card is type + designed data element (labeled tiles, funnel bars, panels, a plan
    grid, a contrast column). One red accent per card.
  * NO em / en / hyphen dashes in any on-image text (a startup assertion enforces this).
  * NO statistics baked into the pixels. The stat slab is retired and, per the build
    brief, an on-image number must exist verbatim in 02_verified_stats.md; rather than
    risk that gate, the cards carry the pillar HEADLINE and non-numeric structural
    labels only. The receipt numbers live in the caption (assembled from approved
    sources and pixel-gated there), never on the image. A startup assertion enforces
    that no card text contains a digit.

DISTINCT ART (Blake ruling): all 30 feed cards + all 30 story cards (60 total) must be
DISTINCT images. Same pillar MAY share a visual FAMILY (headline vocabulary, palette lineage,
data vocabulary) but NEVER a byte-identical or visually-identical image. The pillar
copy bank repeats hooks across days (e.g. six All-in-one days), so the compositor can
no longer key art off the pillar alone. Instead every card derives a deterministic
VARIANT from (pillar, num): a ground shade, a layout composition, and an accent
placement. Same-pillar cards are clearly kin (same palette lineage + labels) but no
two are identical. render_all() hashes every emitted file and RAISES on any collision
so a regression can never reintroduce duplicate art. A day's feed and its paired story
render at different aspect ratios (1080x1080 vs 1080x1920), so they are never identical.

Entry point: render_all(out_dir) renders every card and returns the paths.
"""

import hashlib
import os

from PIL import Image, ImageDraw

from . import summit_render as sr
from .demo_calendar_queue import DEMO_POSTS, _story_filename

# Reuse the house palette + helpers from the summit compositor (single source of truth).
CREAM = sr.CREAM
NAVY = sr.NAVY
RED = sr.RED
SKY = sr.SKY
WHITE = sr.WHITE
MUTE_CREAM = sr.MUTE_CREAM
MUTE_NAVY = sr.MUTE_NAVY

# Ground shades in the LASSO navy lineage. All are dark premium grounds (white ink +
# sky secondary read on every one), so cards in the same pillar family stay kin while
# each variant carries its own ground. CHARCOAL and DEEP are tuned so the sky accent,
# outline tiles, and red chip all keep contrast.
CHARCOAL = (24, 27, 34)
DEEP_NAVY = (12, 20, 44)
GROUNDS = [NAVY, CHARCOAL, DEEP_NAVY]

FEED = 1080
STORY_W, STORY_H = 1080, 1920
MARGIN = sr.MARGIN

_f = sr._f
_tw = sr._tw
_th = sr._th
_tracked = sr._tracked
_wrap = sr._wrap
_fit = sr._fit
_headline = sr._headline

ANTON = sr.ANTON
OSWALD = sr.OSWALD
OSWALD_B = sr.OSWALD_B
MONT = sr.MONT
MONT_SB = sr.MONT_SB


# ---- per-pillar on-image content (labels only, NEVER a statistic) ----------------------
# Each pillar renders a short editorial eyebrow + the pillar HEADLINE, then a designed
# data element built from NON-NUMERIC labels. No digits anywhere (asserted at import).

_EYEBROW = {
    "All in one offer": "ALL IN ONE",
    "Sales are now": "SALES ARE NOW",
    "We do the heavy lifting": "WE DO THE HEAVY LIFTING",
    "The portal": "THE PORTAL",
    "Proof": "PROOF",
}

# short, dash-free, digit-free on-image headlines per pillar (editorial anchor).
_ONIMAGE_HEADLINE = {
    "All in one offer": "One platform for your whole gym",
    "Sales are now": "We chase. You close.",
    "We do the heavy lifting": "Your social, done for you",
    "The portal": "One login. Every result.",
    "Proof": "Built by gym owners, for gym owners",
}

# the ONE red word per headline (grade-gate Q3: exactly one red element via headline).
# NOTE: for pillars whose data element carries the single red (tiles/steps/bars/panels),
# the headline is rendered in plain ink and this word is IGNORED, so the card still holds
# exactly one red. Only the Proof pillar (no red data element) uses the red headline word.
_RED_WORD = {
    "All in one offer": "one",
    "Sales are now": "close.",
    "We do the heavy lifting": "you",
    "The portal": "result.",
    "Proof": "owners,",
}

# non-numeric structural labels for the data element under the headline.
_TILES = {
    "All in one offer": ["ADS", "NURTURE", "WEBSITE", "SOCIAL", "REPORTING"],
    "The portal": ["LEADS", "CONTENT", "REPORTING"],
}
_STEPS = {
    "We do the heavy lifting": ["We plan the month",
                                "We draft every post",
                                "A human approves it",
                                "We track what works"],
}
_BARS = {
    # non-numeric funnel legs (labels only; no percentages baked in)
    "Sales are now": ["LEADS", "NURTURE", "BOOKED", "CLOSED"],
}
_PROOF_LINES = [
    "Real gyms. Real systems. Real numbers.",
    "The receipts are in the caption.",
]


def _assert_no_digits(text):
    assert not any(c.isdigit() for c in text), \
        f"demo card on-image text must carry NO statistic: {text!r}"


def _assert_no_dash(text):
    for ch in text:
        assert ch not in "‐‑‒–—―−-", \
            f"demo card on-image text must be dash free: {text!r}"


def _check(text):
    _assert_no_digits(text)
    _assert_no_dash(text)
    return text


# ---- deterministic per-post variation --------------------------------------------------
# Every card derives its look from (pillar, num) so it is repeatable and same-pillar cards
# read as one family. `pos` is the 0-based occurrence of this num within its pillar (there
# are up to six same-pillar days), which selects the ground shade and the layout so the
# first, second, third ... All-in-one card each differ while staying kin.

def _pillar_position(post):
    """0-based index of this post among the posts sharing its pillar (ordered by num)."""
    same = [p["num"] for p in DEMO_POSTS if p["pillar"] == post["pillar"]]
    same.sort()
    return same.index(post["num"])


def _variant(post):
    """The deterministic (ground, layout_index, accent_index) for a post.

    ground        : which navy-lineage ground shade this card sits on.
    layout_index  : which composition the pillar's data element uses (kin, not identical).
    accent_index  : which element in the data row carries the single red accent.
    All three are keyed off the pillar position so no two same-pillar cards collide, yet
    every one of them clearly belongs to the pillar family."""
    pos = _pillar_position(post)
    ground = GROUNDS[pos % len(GROUNDS)]
    layout_index = pos  # each element renderer maps this onto its own small layout set
    return ground, layout_index, pos


def _footer(d, ink, mute, w, h):
    fy = h - MARGIN - 20
    ex = _tracked(d, (MARGIN, fy), "LASSO", _f(ANTON, 34), ink, 8)
    d.text((ex + 14, fy + 8), "GYM MARKETING MADE SIMPLE", font=_f(OSWALD, 20), fill=mute)


# ---- data elements. Each takes an accent_i so the single red can move between cards, and
# a layout_i so same-pillar cards vary their composition while staying kin. -------------

def _label_tiles(d, y, tiles, ink, w, big=False, accent_i=0, layout_i=0):
    """Label tiles carrying the single red chip at position accent_i. layout_i picks the
    arrangement: 0 = single full-width row, 1 = two stacked rows, 2 = a tidy grid. Same
    labels, same palette; a different shape per same-pillar card."""
    accent_i %= len(tiles)
    vf = _f(OSWALD_B, 30 if big else 24)
    gap = 20

    def _one(x, ty, tw, tile_h, i, label):
        if i == accent_i:
            d.rectangle([x, ty, x + tw, ty + tile_h], fill=RED)
            tile_ink = WHITE
        else:
            d.rectangle([x, ty, x + tw, ty + tile_h], outline=ink, width=2)
            tile_ink = ink
        lab = _check(label)
        d.text((x + (tw - _tw(d, lab, vf)) // 2, ty + (tile_h - vf.size) // 2 - 4),
               lab, font=vf, fill=tile_ink)

    n = len(tiles)
    if layout_i % 3 == 0:
        # one full-width row
        tile_h = 132 if big else 108
        tw = (w - 2 * MARGIN - gap * (n - 1)) // n
        for i, t in enumerate(tiles):
            _one(MARGIN + i * (tw + gap), y, tw, tile_h, i, t)
        return y + tile_h
    if layout_i % 3 == 1:
        # two stacked rows (top row gets the larger share of the labels)
        tile_h = 120 if big else 100
        top = tiles[: (n + 1) // 2]
        bot = tiles[(n + 1) // 2:]
        for r, group in enumerate((top, bot)):
            if not group:
                continue
            m = len(group)
            tw = (w - 2 * MARGIN - gap * (m - 1)) // m
            ty = y + r * (tile_h + gap)
            base = 0 if r == 0 else len(top)
            for j, t in enumerate(group):
                _one(MARGIN + j * (tw + gap), ty, tw, tile_h, base + j, t)
        return y + 2 * (tile_h + gap) - gap
    # layout_i % 3 == 2: a two-column grid of stacked tiles
    tile_h = 100 if big else 88
    cols = 2
    colw = (w - 2 * MARGIN - gap) // cols
    for i, t in enumerate(tiles):
        c, r = i % cols, i // cols
        _one(MARGIN + c * (colw + gap), y + r * (tile_h + gap), colw, tile_h, i, t)
    rows = (n + cols - 1) // cols
    return y + rows * (tile_h + gap) - gap


def _label_steps(d, y, steps, ink, w, accent_i=0, layout_i=0):
    """Numbered step rows using WORD ordinals (never digits). The accent box sits at
    accent_i. layout_i toggles the marker shape (square chip vs pill) so same-pillar
    step cards read as kin, not clones."""
    accent_i %= len(steps)
    words = ["ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX"]
    nf = _f(OSWALD_B, 26)
    tf = _f(MONT_SB, 30)
    row = 116
    pill = (layout_i % 2 == 1)
    for i, s in enumerate(steps):
        cy = y + i * row
        box = 60
        col = RED if i == accent_i else NAVY
        if pill:
            d.rounded_rectangle([MARGIN, cy, MARGIN + box, cy + box], radius=18, fill=col)
        else:
            d.rectangle([MARGIN, cy, MARGIN + box, cy + box], fill=col)
        num = _check(words[i])
        d.text((MARGIN + (box - _tw(d, num, nf)) // 2, cy + (box - nf.size) // 2 - 2),
               num, font=nf, fill=WHITE)
        for j, ln in enumerate(_wrap(d, _check(s), tf, w - 2 * MARGIN - box - 30)[:2]):
            d.text((MARGIN + box + 24, cy + 4 + j * 38), ln, font=tf, fill=ink)
    return y + len(steps) * row


def _label_bars(d, y, labels, ink, w, accent_i=0, layout_i=0):
    """Stage LABEL bars (no percentages). The accent bar sits at accent_i. layout_i
    flips the funnel direction: 0 = descending (widest at top), 1 = ascending. Same
    labels, same track color; a mirrored silhouette per same-pillar card."""
    accent_i %= len(labels)
    lf = _f(OSWALD_B, 28)
    row = 108
    full = w - 2 * MARGIN
    widths = [1.0, 0.82, 0.64, 0.46]
    if layout_i % 2 == 1:
        widths = list(reversed(widths))
    track = tuple(int(b * 0.82 + 255 * 0.18) for b in NAVY) if ink == WHITE else (222, 216, 208)
    for i, label in enumerate(labels):
        cy = y + i * row
        d.text((MARGIN, cy), _check(label), font=lf, fill=ink)
        track_y = cy + 44
        d.rounded_rectangle([MARGIN, track_y, MARGIN + full, track_y + 32], radius=16,
                            fill=track)
        fillw = int(full * widths[i % len(widths)])
        col = RED if i == accent_i else (SKY if ink == WHITE else NAVY)
        d.rounded_rectangle([MARGIN, track_y, MARGIN + fillw, track_y + 32], radius=16,
                            fill=col)
    return y + len(labels) * row


def _panels(d, y, labels, ink, w, accent_i=0, layout_i=0):
    """Labeled dashboard panels. The accent panel (solid red) sits at accent_i. layout_i
    toggles between full-width stacked panels (0) and a left rail of panels with the
    accent panel widened (1) so the portal cards read as one family, never clones."""
    accent_i %= len(labels)
    pf = _f(OSWALD_B, 32)
    n = len(labels)
    gap = 20
    ph = 110
    for i, label in enumerate(labels):
        cy = y + i * (ph + gap)
        if layout_i % 2 == 1 and i != accent_i:
            x1 = w - MARGIN - int((w - 2 * MARGIN) * 0.78)  # inset non-accent panels
        else:
            x1 = MARGIN
        if i == accent_i:
            d.rectangle([MARGIN, cy, w - MARGIN, cy + ph], fill=RED)
            pink = WHITE
            tx = MARGIN + 28
        else:
            d.rectangle([x1, cy, w - MARGIN, cy + ph], outline=ink, width=2)
            pink = ink
            tx = x1 + 28
        lab = _check(label)
        d.text((tx, cy + (ph - pf.size) // 2 - 2), lab, font=pf, fill=pink)
    return y + n * (ph + gap)


def _proof_lines(d, y, ink, w, layout_i=0):
    """The proof pillar's supporting lines. The single red already lives in the headline
    word for this pillar, so this element stays ink only. layout_i draws a short accent
    rule above the lines on alternating cards for family variation (rule is sky, not a
    second red)."""
    lf = _f(MONT_SB, 34)
    if layout_i % 2 == 1:
        d.rectangle([MARGIN, y, MARGIN + 120, y + 8], fill=SKY)
        y += 34
    for ln_txt in _PROOF_LINES:
        for ln in _wrap(d, _check(ln_txt), lf, w - 2 * MARGIN):
            d.text((MARGIN, y), ln, font=lf, fill=ink)
            y += 48
        y += 8
    return y


# pillars whose DATA element carries the single red accent -> headline stays plain ink.
_DATA_RED_PILLARS = {"All in one offer", "The portal", "We do the heavy lifting",
                     "Sales are now"}


def _render_one(post, w, h, out_path):
    """Render a single card at (w, h) to out_path. Ground shade, layout, and accent
    placement are derived deterministically from (pillar, num) so every card is a
    distinct member of its pillar family (never a byte-identical clone)."""
    pillar = post["pillar"]
    ground, layout_i, accent_i = _variant(post)
    bg, ink, mute = ground, WHITE, MUTE_NAVY
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    cw = w - 2 * MARGIN

    # story frames start lower so the top/bottom safe bands stay clear
    y = MARGIN + (220 if h > FEED else 0)

    _tracked(d, (MARGIN, y), _check(_EYEBROW[pillar]), _f(OSWALD_B, 30), SKY, 5)
    y += 66

    # Headline red only when the data element carries no red (keeps exactly one red).
    if pillar in _DATA_RED_PILLARS:
        red_tokens = set()
    else:
        red_tokens = set(x.strip(".,").upper() for x in _RED_WORD[pillar].split())
    hf, lines = _fit(d, _check(_ONIMAGE_HEADLINE[pillar]).upper(), cw, 3, 96)
    y = _headline(d, MARGIN, y + 6, lines, hf, red_tokens, ink)
    y += 40

    if pillar == "The portal":
        _panels(d, y, _TILES["The portal"], ink, w, accent_i=accent_i, layout_i=layout_i)
    elif pillar in _TILES:
        _label_tiles(d, y, _TILES[pillar], ink, w, big=True,
                     accent_i=accent_i, layout_i=layout_i)
    elif pillar in _STEPS:
        _label_steps(d, y, _STEPS[pillar], ink, w, accent_i=accent_i, layout_i=layout_i)
    elif pillar in _BARS:
        _label_bars(d, y, _BARS[pillar], ink, w, accent_i=accent_i, layout_i=layout_i)
    else:  # Proof
        _proof_lines(d, y, ink, w, layout_i=layout_i)

    _footer(d, ink, mute, w, h)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


def render_all(out_dir):
    """Render all 30 feed cards + all 30 story cards (2 posts/day) to out_dir. Returns
    the paths.

    Distinct-art gate (Blake ruling): every emitted file is hashed by its bytes and any
    collision RAISES. Same-pillar cards share a family but must never be identical, and
    a day's feed and its paired story must differ too (they render at different aspect
    ratios). All 60 emitted files must be distinct."""
    paths = []
    for post in DEMO_POSTS:
        feed_path = os.path.join(out_dir, post["filename"])
        _render_one(post, FEED, FEED, feed_path)
        paths.append(feed_path)
        if post["is_story"]:
            story_path = os.path.join(out_dir, _story_filename(post["filename"]))
            _render_one(post, STORY_W, STORY_H, story_path)
            paths.append(story_path)

    seen = {}
    for p in paths:
        with open(p, "rb") as fh:
            h = hashlib.sha256(fh.read()).hexdigest()
        if h in seen:
            raise AssertionError(
                "demo calendar produced duplicate art: "
                f"{os.path.basename(p)} is byte-identical to "
                f"{os.path.basename(seen[h])}. Every one of the 30 feed cards + 30 "
                "story cards (60 total) must be a DISTINCT image.")
        seen[h] = p
    return paths
