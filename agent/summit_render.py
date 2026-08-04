"""
Shared PIL rendering primitives for the Summit-style card compositors
(summit cards, guest quote cards, welcome posts): house palette, house fonts,
and small text-measurement/tracking helpers. Pure PIL, no network, no API.

Colors are RGB tuples (not hex strings) because callers concatenate them with
an alpha value (e.g. `NAVY + (255,)`) to build RGBA fills for composited
assets like the LASSO wordmark. Values match brand_voice house style / the
hex constants in agent/pdf_report.py:
  NAVY  #121E3C   RED  #FF0000   CREAM #FAF6F0   SKY #5EB9E6
"""

import os

from PIL import ImageFont

SIZE = 1080
MARGIN = 96

NAVY = (18, 30, 60)
RED = (255, 0, 0)
CREAM = (250, 246, 240)
SKY = (94, 185, 230)
WHITE = (255, 255, 255)

MUTE_CREAM = (122, 122, 128)
MUTE_NAVY = (168, 178, 198)

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
ANTON = os.path.join(_FONTS_DIR, "Anton-Regular.ttf")
OSWALD = os.path.join(_FONTS_DIR, "Oswald-Medium.ttf")
MONT = os.path.join(_FONTS_DIR, "Montserrat-Medium.ttf")

_FONT_CACHE = {}


def _f(font_path, size):
    """Load (and cache) a truetype font at a given size. Falls back to PIL's
    bitmap default font if the asset is unavailable, so rendering never raises
    in an environment missing the bundled font files."""
    key = (font_path, size)
    font = _FONT_CACHE.get(key)
    if font is None:
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception:
            font = ImageFont.load_default()
        _FONT_CACHE[key] = font
    return font


def _tw(d, text, font):
    """Pixel width of `text` set in `font`, per the given ImageDraw."""
    if not text:
        return 0
    bbox = d.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _th(d, text, font):
    """Pixel height of `text` set in `font`, per the given ImageDraw."""
    if not text:
        return 0
    bbox = d.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _wrap(d, text, font, max_w):
    """Greedy word-wrap of `text` into lines no wider than max_w. Always
    returns at least one (possibly empty) line."""
    words = (text or "").split()
    if not words:
        return [""]
    lines = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if not cur or _tw(d, trial, font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _tracked_w(d, text, font, tracking=0):
    """Total pixel width of `text` rendered character-by-character with extra
    `tracking` pixels between glyphs (matches what `_tracked` actually draws,
    unlike measuring the string in one shot which ignores the added spacing)."""
    if not text:
        return 0
    width = sum(_tw(d, ch, font) for ch in text)
    return width + tracking * (len(text) - 1)


def _tracked(d, xy, text, font, fill, tracking=0):
    """Draw `text` letter-spaced by `tracking` extra pixels between glyphs
    (not after the last one, so the drawn extent matches `_tracked_w` exactly
    and centering math using `_tracked_w` lines up with what is actually
    drawn). Returns the x position just past the last glyph drawn."""
    x, y = xy
    for i, ch in enumerate(text):
        d.text((x, y), ch, font=font, fill=fill)
        x += _tw(d, ch, font)
        if i < len(text) - 1:
            x += tracking
    return x
