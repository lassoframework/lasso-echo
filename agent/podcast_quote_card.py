"""
Guest quote card compositor - pure PIL, modeled on summit_render.

Renders a 1080x1080 quote card for the Gym Marketing Made Simple podcast: a
Memorable Quote (verbatim, from podcast_docparse) set as the hero, attributed to
the guest and the episode. Text is drawn with PIL so it can never garble, and the
LASSO wordmark is the REAL asset (agent/assets/brand/lasso_wordmark.png) composited
with alpha, never a typeset fake logo.

House style, grade-gate aligned:
  - one canvas (navy or cream), house palette CREAM/NAVY/RED/SKY/WHITE
  - exactly ONE red element on the card (the opening quotation mark), grade Q3
  - a PART of the quote in CAPS for emphasis, the remainder sentence case
  - guest name, EPISODE line, and the podcast name typeset in the house fonts
  - a closing quotation mark, and the wordmark small, once, white on navy

Verbatim guard: like podcast_cards, a quote carrying any dash family character
(em, en, or hyphen) is REFUSED with a ValueError. On-image copy is dash free; use
"to" not a dash range. Offline, no API, no network.
"""

import os
import re

from PIL import Image

from .summit_render import (
    _f, _tw, _wrap,
    ANTON, OSWALD, MONT,
    CREAM, NAVY, RED, SKY, WHITE,
)

SIZE = 1080
MARGIN = 96

MUTE_CREAM = (122, 122, 128)
MUTE_NAVY = (168, 178, 198)

PODCAST_NAME = "GYM MARKETING MADE SIMPLE PODCAST"

# The real LASSO wordmark asset. NEVER generated, NEVER typeset as a fake logo.
_BRAND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "brand")
WORDMARK = os.path.join(_BRAND_DIR, "lasso_wordmark.png")

_QUOTE_ANY_HYPHEN_RE = re.compile(r"-")  # any ASCII hyphen is banned in verbatim quotes


def _guard_verbatim(quote_text):
    """Refuse a quote carrying any banned dash or any ASCII hyphen. Raised loudly
    so a dashed line is fixed at the source, never silently rendered.
    Delegates to copy_gate.violations for em/en/intraword detection; also checks
    for any ASCII hyphen (including ' - ' between spaces) since verbatim quotes
    must be completely hyphen-free."""
    from . import copy_gate
    v = copy_gate.violations(quote_text or "")
    if not v and _QUOTE_ANY_HYPHEN_RE.search(quote_text or ""):
        v = ["hyphen"]
    if v:
        raise ValueError(
            "quote card refused: quote text carries a dash character (em, en, or "
            f"hyphen) [{', '.join(v)}]. On-image copy is dash free; use 'to' not a dash range.")


def split_caps_emphasis(quote_text, caps_span=None):
    """
    Split a quote into (caps_part, rest) for the CAPS emphasis treatment.

    caps_span may be:
      - None  -> the first CLAUSE is emphasized (text up to the first comma,
                 colon, or semicolon; if none, the whole quote goes CAPS).
      - int   -> the first <caps_span> words are emphasized.
      - str   -> that exact leading substring is emphasized (must be a prefix of
                 the quote, case-insensitively).

    Returns (caps_part_upper, rest_original_case). The rest keeps its original
    casing (sentence case as written); the caps part is upper-cased for render.
    Whitespace between the two is normalized to a single leading space on rest.
    """
    q = (quote_text or "").strip()
    if not q:
        return "", ""
    if caps_span is None:
        # first clause: up to the first comma / colon / semicolon, OR a sentence
        # boundary (a period followed by whitespace, so decimals like 3.5 do not
        # split). A two-sentence quote emphasizes the first sentence, the rest
        # reads sentence case.
        m = re.search(r"[,:;]|\.(?=\s)", q)
        if m:
            sep = q[m.start()]
            # a sentence period stays on the caps part so it reads correctly and
            # verbatim ("SYSTEM. Fix..."); a comma/colon/semicolon is dropped so
            # the remainder reads clean.
            head = q[:m.start() + (1 if sep == "." else 0)].strip()
            tail = q[m.start() + 1:].strip()
        else:
            head, tail = q, ""
    elif isinstance(caps_span, int):
        words = q.split()
        n = max(0, min(caps_span, len(words)))
        head = " ".join(words[:n])
        tail = " ".join(words[n:])
    else:
        prefix = str(caps_span).strip()
        if prefix and q.lower().startswith(prefix.lower()):
            head = q[:len(prefix)].strip()
            tail = q[len(prefix):].strip()
        else:
            head, tail = q, ""
    return head.upper(), tail.strip()


def _fit_quote(d, caps_part, rest, max_w, max_lines=6, start=72, floor=34):
    """Find the largest Montserrat size at which the full quote (caps part joined
    to the rest) wraps within max_lines. Returns (font, lines, caps_word_count)
    where caps_word_count is how many leading WORDS render in the caps color, so
    the caller draws the emphasis without re-splitting."""
    full = (caps_part + " " + rest).strip() if rest else caps_part
    caps_words = len(caps_part.split())
    s = start
    while s >= floor:
        fo = _f(MONT, s)
        lines = _wrap(d, full, fo, max_w)
        if len(lines) <= max_lines:
            return fo, lines, caps_words
        s -= 3
    fo = _f(MONT, floor)
    return fo, _wrap(d, full, fo, max_w), caps_words


def _draw_quote(d, x, y, lines, font, caps_words, ink):
    """Draw the wrapped quote, colouring the first caps_words words in the ink's
    emphasis tone (caps part is BRIGHTER: WHITE on navy, NAVY on cream) and the
    remainder in a slightly muted tone. Returns the y after the last line. The one
    red element is the quotation mark drawn by the caller, not this text."""
    caps_fill = WHITE if ink == WHITE else NAVY
    rest_fill = (214, 222, 236) if ink == WHITE else (72, 84, 112)
    lh = int(font.size * 1.34)
    seen = 0
    for i, line in enumerate(lines):
        cx = x
        for word in line.split():
            fill = caps_fill if seen < caps_words else rest_fill
            d.text((cx, y + i * lh), word, font=font, fill=fill)
            cx += _tw(d, word + " ", font)
            seen += 1
    return y + len(lines) * lh


def _paste_wordmark(img, canvas):
    """Composite the REAL wordmark asset, small, once, bottom-RIGHT (clear of the
    bottom-left attribution text). On navy it is rendered white (the asset is
    tinted to WHITE through its own alpha); on cream it is rendered navy. Returns
    the (x, y, w, h) box it occupied so a test can assert non-background pixels
    landed there."""
    mark = Image.open(WORDMARK).convert("RGBA")
    target_w = 170
    scale = target_w / mark.width
    target_h = max(1, int(round(mark.height * scale)))
    mark = mark.resize((target_w, target_h), Image.LANCZOS)
    # Recolor: keep the asset's own alpha, replace RGB with the on-canvas tone so
    # the wordmark reads white on navy (or navy on cream). Never a fake logo: the
    # SHAPE is the real asset's alpha channel, only the fill tone is set.
    tone = WHITE if canvas == "navy" else NAVY
    alpha = mark.split()[-1]
    solid = Image.new("RGBA", mark.size, tone + (255,))
    solid.putalpha(alpha)
    x = SIZE - MARGIN - target_w
    y = SIZE - MARGIN - target_h
    img.paste(solid, (x, y), solid)
    return (x, y, target_w, target_h)


def render_quote_card(quote_text, guest_name, episode_num, out_path,
                      canvas="navy", caps_span=None):
    """
    Render a 1080x1080 guest quote card to out_path and return out_path.

    quote_text  a Memorable Quote, rendered VERBATIM (dash free; ValueError if not)
    guest_name  the guest, typeset upper-case in Oswald (e.g. "JANE DOE")
    episode_num the episode number, shown as "EPISODE {n}"
    canvas      "navy" (default) or "cream"
    caps_span   emphasis control passed to split_caps_emphasis (None = first clause)

    The card carries exactly ONE red element (the opening quotation mark), the
    quote with a CAPS-emphasized opening, the guest name, the EPISODE line, the
    podcast name, a closing quotation mark, and the real wordmark composited once.
    """
    from PIL import ImageDraw

    _guard_verbatim(quote_text)

    canvas = "cream" if str(canvas).lower() == "cream" else "navy"
    bg = NAVY if canvas == "navy" else CREAM
    ink = WHITE if canvas == "navy" else NAVY
    mute = MUTE_NAVY if canvas == "navy" else MUTE_CREAM

    img = Image.new("RGB", (SIZE, SIZE), bg)
    d = ImageDraw.Draw(img)
    cw = SIZE - 2 * MARGIN

    # Eyebrow: the podcast name, small tracked caps at the top.
    d.text((MARGIN, MARGIN), PODCAST_NAME, font=_f(OSWALD, 26), fill=mute)

    # Opening quotation mark: the ONE red element (grade Q3). Oversized Anton.
    qf = _f(ANTON, 200)
    open_y = MARGIN + 44
    d.text((MARGIN - 8, open_y), "“", font=qf, fill=RED)

    # The quote, CAPS-emphasized opening + sentence-case remainder.
    caps_part, rest = split_caps_emphasis(quote_text, caps_span)
    quote_top = open_y + 172
    quote_max_h = SIZE - MARGIN - 210 - quote_top  # leave room for attribution
    font, lines, caps_words = _fit_quote(d, caps_part, rest, cw)
    # If the wrap runs past the attribution zone, shrink until it fits vertically.
    while len(lines) * int(font.size * 1.34) > quote_max_h and font.size > 34:
        font = _f(MONT, font.size - 3)
        lines = _wrap(d, (caps_part + " " + rest).strip() if rest else caps_part,
                      font, cw)
    qy = _draw_quote(d, MARGIN, quote_top, lines, font, caps_words, ink)

    # Closing quotation mark, just after the quote body, in ink (not a 2nd red).
    d.text((MARGIN, qy + 4), "”", font=_f(ANTON, 96), fill=mute)

    # The real wordmark, composited once (never a typeset logo). Bottom-right;
    # the attribution text sits bottom-left, clear of it.
    _paste_wordmark(img, canvas)

    # Attribution block, bottom-left: guest name over the EPISODE line, stacked so
    # the two share a baseline band with the wordmark and never overlap it.
    guest = (guest_name or "").strip().upper()
    try:
        ep_label = f"EPISODE {int(episode_num)}"
    except (TypeError, ValueError):
        ep_label = f"EPISODE {str(episode_num).strip()}"
    ep_font = _f(OSWALD, 30)
    guest_font = _f(OSWALD, 44)
    ep_y = SIZE - MARGIN - 40
    d.text((MARGIN, ep_y), ep_label, font=ep_font,
           fill=SKY if canvas == "navy" else mute)
    if guest:
        d.text((MARGIN, ep_y - 58), guest, font=guest_font, fill=ink)

    img.save(out_path)
    return out_path
