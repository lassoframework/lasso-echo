"""
demo_calendar_render.py — PIL compositor for the 30-day demo calendar cards.

Renders the 30 feed cards (1080x1080) and the 6 story cards (1080x1920) for the
done-for-you demo calendar to content_library/demo_calendar/. Pure PIL: no API key,
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

Entry point: render_all(out_dir) renders every card and returns the paths.
"""

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

FEED = 1080
STORY_W, STORY_H = 1080, 1920
MARGIN = sr.MARGIN

_f = sr._f
_tw = sr._tw
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

# the ONE red word per headline (grade-gate Q3: exactly one red element via headline)
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


def _footer(d, ink, mute, w, h):
    fy = h - MARGIN - 20
    ex = _tracked(d, (MARGIN, fy), "LASSO", _f(ANTON, 34), ink, 8)
    d.text((ex + 14, fy + 8), "GYM MARKETING MADE SIMPLE", font=_f(OSWALD, 20), fill=mute)


def _label_tiles(d, y, tiles, ink, w, big=False):
    """A row of outlined label tiles; the first tile is a solid red chip (the one red)."""
    n = len(tiles)
    gap = 20
    tw = (w - 2 * MARGIN - gap * (n - 1)) // n
    tile_h = 132 if big else 108
    vf = _f(OSWALD_B, 30 if big else 24)
    for i, t in enumerate(tiles):
        x = MARGIN + i * (tw + gap)
        if i == 0:
            d.rectangle([x, y, x + tw, y + tile_h], fill=RED)
            tile_ink = WHITE
        else:
            d.rectangle([x, y, x + tw, y + tile_h], outline=ink, width=2)
            tile_ink = ink
        label = _check(t)
        d.text((x + (tw - _tw(d, label, vf)) // 2, y + (tile_h - vf.size) // 2 - 4),
               label, font=vf, fill=tile_ink)
    return y + tile_h


def _label_steps(d, y, steps, ink, w):
    """Numbered step rows using WORD ordinals (never digits) so the card stays digit
    free. The first box is red (the one red)."""
    words = ["ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX"]
    nf = _f(OSWALD_B, 26)
    tf = _f(MONT_SB, 30)
    row = 116
    for i, s in enumerate(steps):
        cy = y + i * row
        box = 60
        col = RED if i == 0 else NAVY if ink == WHITE else NAVY
        d.rectangle([MARGIN, cy, MARGIN + box, cy + box], fill=col)
        num = _check(words[i])
        d.text((MARGIN + (box - _tw(d, num, nf)) // 2, cy + (box - nf.size) // 2 - 2),
               num, font=nf, fill=WHITE)
        for j, ln in enumerate(_wrap(d, _check(s), tf, w - 2 * MARGIN - box - 30)[:2]):
            d.text((MARGIN + box + 24, cy + 4 + j * 38), ln, font=tf, fill=ink)
    return y + len(steps) * row


def _label_bars(d, y, labels, ink, w):
    """A descending set of bars carrying stage LABELS only (no percentages). Each bar is
    progressively shorter to read as a funnel; the first is the one red."""
    lf = _f(OSWALD_B, 28)
    row = 108
    full = w - 2 * MARGIN
    widths = [1.0, 0.82, 0.64, 0.46]
    track = tuple(int(b * 0.82 + 255 * 0.18) for b in NAVY) if ink == WHITE else (222, 216, 208)
    for i, label in enumerate(labels):
        cy = y + i * row
        d.text((MARGIN, cy), _check(label), font=lf, fill=ink)
        track_y = cy + 44
        d.rounded_rectangle([MARGIN, track_y, MARGIN + full, track_y + 32], radius=16,
                            fill=track)
        fillw = int(full * widths[i % len(widths)])
        col = RED if i == 0 else (SKY if ink == WHITE else NAVY)
        d.rounded_rectangle([MARGIN, track_y, MARGIN + fillw, track_y + 32], radius=16,
                            fill=col)
    return y + len(labels) * row


def _panels(d, y, labels, ink, w):
    """Stacked labeled dashboard panels for the portal pillar (one screen). First is red."""
    pf = _f(OSWALD_B, 32)
    n = len(labels)
    gap = 20
    ph = 110
    for i, label in enumerate(labels):
        cy = y + i * (ph + gap)
        if i == 0:
            d.rectangle([MARGIN, cy, w - MARGIN, cy + ph], fill=RED)
            pink = WHITE
        else:
            d.rectangle([MARGIN, cy, w - MARGIN, cy + ph], outline=ink, width=2)
            pink = ink
        lab = _check(label)
        d.text((MARGIN + 28, cy + (ph - pf.size) // 2 - 2), lab, font=pf, fill=pink)
    return y + n * (ph + gap)


def _proof_lines(d, y, ink, w):
    lf = _f(MONT_SB, 34)
    for ln_txt in _PROOF_LINES:
        for ln in _wrap(d, _check(ln_txt), lf, w - 2 * MARGIN):
            d.text((MARGIN, y), ln, font=lf, fill=ink)
            y += 48
        y += 8
    return y


def _render_one(post, w, h, out_path):
    """Render a single card at (w, h) to out_path. Navy canvas for feed, cream for
    story variety would be fine, but we keep a consistent premium navy for the demo so
    the calendar reads as one branded set."""
    pillar = post["pillar"]
    bg, ink, mute = NAVY, WHITE, MUTE_NAVY
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    cw = w - 2 * MARGIN

    # story frames start lower so the top/bottom safe bands stay clear
    y = MARGIN + (220 if h > FEED else 0)

    _tracked(d, (MARGIN, y), _check(_EYEBROW[pillar]), _f(OSWALD_B, 30), SKY, 5)
    y += 66

    red_tokens = set(x.strip(".,").upper() for x in _RED_WORD[pillar].split())
    hf, lines = _fit(d, _check(_ONIMAGE_HEADLINE[pillar]).upper(), cw, 3, 96)
    y = _headline(d, MARGIN, y + 6, lines, hf, red_tokens, ink)
    y += 40

    if pillar == "The portal":
        _panels(d, y, _TILES["The portal"], ink, w)
    elif pillar in _TILES:
        _label_tiles(d, y, _TILES[pillar], ink, w, big=True)
    elif pillar in _STEPS:
        _label_steps(d, y, _STEPS[pillar], ink, w)
    elif pillar in _BARS:
        _label_bars(d, y, _BARS[pillar], ink, w)
    else:  # Proof
        _proof_lines(d, y, ink, w)

    _footer(d, ink, mute, w, h)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


def render_all(out_dir):
    """Render all 30 feed cards + the 6 story cards to out_dir. Returns the paths."""
    paths = []
    for post in DEMO_POSTS:
        feed_path = os.path.join(out_dir, post["filename"])
        _render_one(post, FEED, FEED, feed_path)
        paths.append(feed_path)
        if post["is_story"]:
            story_path = os.path.join(out_dir, _story_filename(post["filename"]))
            _render_one(post, STORY_W, STORY_H, story_path)
            paths.append(story_path)
    return paths
