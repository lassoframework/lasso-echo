"""
story_layout.py — the SINGLE layout authority for a Story frame (2026-09-01 fix,
Blake's three rulings after the overlay burn's 0/10 pixel-measured proof failure).

Owns EVERYTHING about where things sit on a 1080x1920 Story frame so no two
systems (the Roxx overlay burn in clipper_render.py, the LASSO brand bar drawn
by add_brand_frame) ever draw in the same region while guessing about each
other's geometry independently. That guessing was the root of ruling 3: the
brand bar physically sat inside a hardcoded "reserved bottom 310px" band the
overlay's safe-zone check ALSO guessed at independently — two unrelated magic
numbers that happened to avoid collision by luck, not by construction.

  * BRAND_BAR_H is the brand bar's REAL footprint (clipper_render.add_brand_frame
    imports it from here and draws exactly this many pixels). The bottom safe
    zone is DERIVED from it — "the brand bar IS the bottom reserved zone," not
    a second, independent guess that can drift out of sync.
  * MAX_CHARS_PER_LINE is measured from the ACTUAL font ffmpeg's drawtext
    resolves in production (confirmed live via `fc-match Arial` on the
    deployed `echo` Railway container: DejaVu Sans "Book" — Arial itself is not
    installed there), not an assertion. See _measure_char_budget()'s doc below
    for the method.
  * identity_anchor_line / identity_text_for_bar are the ONE place identity-
    anchor copy is formatted, so the hook-frame anchor line and the ask-frame
    brand-bar text can never drift out of sync with each other.

Deterministic, offline, no network — stdlib + Pillow (already an Echo
dependency: video_assets.py / podcast_quote_card.py / demo_calendar_render.py
all use PIL fonts).
"""
from __future__ import annotations

import os

# ---- frame geometry (canonical; clipper_render + story_overlay both defer to
# this module so there is exactly ONE definition of the Story frame) --------
FRAME_W = 1080
FRAME_H = 1920

# Top safe zone: nothing burned above this line. Unchanged by tonight's fix —
# the failure report found no top-zone violations, only bottom / overflow /
# anchor ones.
SAFE_TOP = 250

# The LASSO brand bar's real footprint. THIS, not a hardcoded band, is what the
# bottom of the Story is reserved for (ruling 3: "the brand bar IS the bottom
# reserved zone"). clipper_render.add_brand_frame draws exactly this many
# pixels; change it HERE and every consumer (the overlay safe-zone check, the
# ask-frame box) moves with it — they can no longer drift apart.
BRAND_BAR_H = 70
# Breathing room between the last line of ask-frame overlay text and the top
# edge of the brand bar, so the two never touch even at the tightest legal
# position.
BOTTOM_MARGIN = 24


def brand_bar_top(frame_h=FRAME_H):
    """The y-pixel where the brand bar's solid navy fill begins."""
    return frame_h - BRAND_BAR_H


def safe_zone_bounds(frame_h=FRAME_H):
    """(min_y, max_y) any burned text box must stay within. max_y is DERIVED
    from the brand bar's real footprint (brand_bar_top - BOTTOM_MARGIN), never
    an independent constant that can silently fall out of sync with what
    add_brand_frame actually draws."""
    return SAFE_TOP, brand_bar_top(frame_h) - BOTTOM_MARGIN


def safe_zone_ok(box, frame_h=FRAME_H):
    """True when a text box (y_top, y_bottom) sits fully inside the safe zone."""
    y_top, y_bottom = box
    lo, hi = safe_zone_bounds(frame_h)
    return y_top >= lo and y_bottom <= hi


# ---- fonts: the ACTUAL font ffmpeg's drawtext resolves ---------------------
# clipper_render's overlay burn passed font='Arial' to ffmpeg's drawtext. On
# the deployed Railway container Arial is not installed; fontconfig
# substitutes it — confirmed LIVE (2026-09-01, `fc-match Arial` on the `echo`
# service): "DejaVuSans.ttf: DejaVu Sans Book". Bundling that EXACT file here
# (agent/assets/fonts/DejaVuSans.ttf, pulled from the running container) and
# passing an explicit fontfile= to drawtext (see clipper_render) means the
# character budget measured below can never describe a font that isn't
# actually the one rendering the pixels — that ambiguity was itself a small
# version of ruling 3's "two systems guessing about each other" problem.
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
OVERLAY_FONT_PATH = os.path.join(_FONTS_DIR, "DejaVuSans.ttf")           # hook/ask body copy
ANCHOR_FONT_PATH = os.path.join(_FONTS_DIR, "DejaVuSansMono-Bold.ttf")   # small identity line

OVERLAY_FONT_SIZE = 58     # px — unchanged from the prior clipper_render value
ANCHOR_FONT_SIZE = 30      # px — a small utility line, not a headline
OVERLAY_LINE_GAP = 1.32    # line height multiplier
OVERLAY_PAD = 26           # px — vertical scrim padding (top/bottom of the box)
OVERLAY_PAD_X = 64         # px — HORIZONTAL scrim padding (left/right). This was
                           # 0 before tonight (the scrim ran x=0..width edge-to-edge
                           # with drawtext only centered, never width-constrained) —
                           # the direct cause of 10/10 renders clipping text off
                           # both edges of the frame.
OVERLAY_OUTER_MARGIN = 24  # px — breathing room off the safe-zone boundary
ANCHOR_GAP = 12            # px — gap between a hook box and its anchor line

_FONT_CACHE = {}


def _font(path, size):
    from PIL import ImageFont
    key = (path, int(size))
    f = _FONT_CACHE.get(key)
    if f is None:
        f = ImageFont.truetype(path, int(size))
        _FONT_CACHE[key] = f
    return f


def measure_text_width(text, *, font_path=OVERLAY_FONT_PATH, font_size=OVERLAY_FONT_SIZE):
    """REAL rendered pixel width of `text` in the exact font ffmpeg's drawtext
    resolves (see OVERLAY_FONT_PATH). This is the render-time ground truth the
    burn's shrink-to-fit failsafe checks against — never an in-memory guess,
    never trusted from a pre-burn estimate."""
    return _font(font_path, font_size).getlength(str(text or ""))


# ---- the character budget (ruling 1: a CAP, not shrink-to-fit) -------------
# MEASUREMENT METHOD (show-your-work, per Blake's ruling):
#   1. usable_width = FRAME_W - 2*OVERLAY_PAD_X = 1080 - 128 = 952px (the box's
#      real horizontal padding on both sides — see OVERLAY_PAD_X above).
#   2. Per-character cost: the ENGLISH-LETTER-FREQUENCY-WEIGHTED MEAN glyph
#      width of A-Z, UPPERCASE (all overlay text is upper-cased at layout —
#      see story_overlay.layout_overlay), in the ACTUAL production font
#      (OVERLAY_FONT_PATH) at OVERLAY_FONT_SIZE, measured with Pillow's
#      ImageFont.getlength — the same FreeType glyph-advance metrics ffmpeg's
#      drawtext uses to lay out the same font file. Frequency-weighted, not a
#      flat A..Z mean, because real overlay copy is English prose, not a
#      random letter draw: E/T/A/O/I/N are common, W/M/Q are rare — a flat
#      mean over-charges the budget for letters that rarely show up.
#   3. NO credit is given for spaces (a worst-realistic assumption — a hook
#      line with unusually few spaces, e.g. "IRREPLACEABLE UNSTOPPABLE", still
#      has to fit). Real overlay lines DO carry spaces (roughly 1 in 6
#      characters in natural English sentences), so a typical real line has
#      headroom below this cap — which is exactly why the render-time
#      shrink-to-fit failsafe (fit_font_size, below) should almost never fire
#      in production; if it fires often, that is itself a signal the cap or
#      the padding needs revisiting.
#   Computed at import time from the bundled font file (never hand-typed), so
#   this constant can never silently drift from the font it describes.
_LETTER_FREQ_PCT = {
    "E": 12.02, "T": 9.10, "A": 8.12, "O": 7.68, "I": 7.31, "N": 6.95,
    "S": 6.28, "R": 6.02, "H": 5.92, "D": 4.32, "L": 3.98, "U": 2.88,
    "C": 2.71, "M": 2.61, "F": 2.30, "Y": 2.11, "W": 2.09, "G": 2.03,
    "P": 1.82, "B": 1.49, "V": 1.11, "K": 0.69, "X": 0.17, "Q": 0.11,
    "J": 0.10, "Z": 0.07,
}  # standard English letter-frequency table (percent of letter occurrences)


def _measure_char_budget(pad_x=OVERLAY_PAD_X, frame_w=FRAME_W,
                         font_path=OVERLAY_FONT_PATH, font_size=OVERLAY_FONT_SIZE):
    """Returns (max_chars, usable_width_px, weighted_px_per_char) — the full
    show-your-work triple, not just the final number."""
    usable = frame_w - 2 * pad_x
    total_pct = sum(_LETTER_FREQ_PCT.values())
    weighted_px = sum(
        measure_text_width(ch, font_path=font_path, font_size=font_size) * pct
        for ch, pct in _LETTER_FREQ_PCT.items()
    ) / total_pct
    return int(usable // weighted_px), usable, weighted_px


MAX_CHARS_PER_LINE, OVERLAY_USABLE_W, OVERLAY_PX_PER_CHAR = _measure_char_budget()


# ---- shrink-to-fit render-time failsafe (ruling 1c) -------------------------
# ONLY fires when a line still doesn't fit at burn time despite BOTH the
# generation-time cap (story_overlay.wrap_line, MAX_CHARS_PER_LINE) and the
# approval-card editor cap (portal repo — see PROGRESS.md's 2026-09-01 note)
# having already been enforced. It firing at all means both of those already
# failed to catch something, so the caller must log it loudly (ops_alerts).
# Never below this floor — legibility on a phone screen.
SHRINK_FONT_FLOOR = 34


def fit_font_size(text, *, max_width, start_size=OVERLAY_FONT_SIZE,
                  font_path=OVERLAY_FONT_PATH, floor=SHRINK_FONT_FLOOR, step=2):
    """The largest font size (start_size stepping down to floor) at which
    `text` renders <= max_width in font_path. Returns (size, fits): fits=False
    means even the floor size still overflows — the caller uses the floor size
    anyway (it is the least-bad legible option) but MUST treat fits=False as a
    loud failure, not a silent one."""
    size = start_size
    while size > floor:
        if measure_text_width(text, font_path=font_path, font_size=size) <= max_width:
            return size, True
        size -= step
    fits = measure_text_width(text, font_path=font_path, font_size=floor) <= max_width
    return floor, fits


# ---- identity anchor formatting (ONE place, hook line + ask-frame bar) -----
def identity_anchor_line(tokens):
    """The small identity-anchor line burned on EVERY hook frame (ruling 3).
    `tokens` is the gym's identity_tokens (city / gym name); joined + upper-
    cased. Empty/None tokens -> ''."""
    parts = [str(t).strip() for t in (tokens or ()) if str(t or "").strip()]
    return " ".join(parts).upper()


def identity_text_for_bar(tokens, fallback="LASSO"):
    """The identity text the brand bar carries on the ASK frame IN PLACE OF
    its normal handle text (ruling 3: the identity anchor moves INTO the brand
    bar for the ask frame, so the ask frame's <=2-line overlay budget stays
    free for the ask alone — the brand bar IS the identity anchor there).
    Falls back to the normal LASSO wordmark text when no tokens are available
    (never blanks the bar)."""
    line = identity_anchor_line(tokens)
    return line or fallback


# ---- box geometry (hook / ask content areas, anchor line) ------------------
def _block_h(n_lines, *, font_size=OVERLAY_FONT_SIZE, pad=OVERLAY_PAD):
    line_h = int(font_size * OVERLAY_LINE_GAP)
    return pad * 2 + line_h * max(1, n_lines)


def hook_box(n_lines, *, frame_h=FRAME_H):
    """(y_top, y_bottom) for a HOOK frame's content box (anchor='top'),
    positioned just inside the top safe boundary."""
    safe_top, _ = safe_zone_bounds(frame_h)
    y_top = safe_top + OVERLAY_OUTER_MARGIN
    return y_top, y_top + _block_h(n_lines)


def anchor_box(hook_bounds, *, frame_h=FRAME_H):
    """(y_top, y_bottom) for the small identity-anchor line, placed just below
    a hook frame's content box (own layout element, never counted against the
    hook's <=2-line content budget)."""
    _, y_bottom = hook_bounds
    y_top = y_bottom + ANCHOR_GAP
    line_h = int(ANCHOR_FONT_SIZE * OVERLAY_LINE_GAP)
    return y_top, y_top + line_h


def ask_box(n_lines, *, frame_h=FRAME_H):
    """(y_top, y_bottom) for the ASK frame's content box (anchor='bottom'),
    anchored just above the brand bar's real top edge (via safe_zone_bounds,
    which is itself derived from BRAND_BAR_H)."""
    _, safe_bottom = safe_zone_bounds(frame_h)
    return safe_bottom - _block_h(n_lines), safe_bottom
