"""
Story-image formatter: turn a gym's feed PHOTO into a proper 1080x1920 Instagram/
Facebook Story card (flag: AGENT_STORY_FORMAT, default OFF).

A raw landscape/portrait photo posted to a Story shows centered on black bars and
carries no text (stories publish with an empty body). This makes it STORY SIZE and
readable:

  * BACKGROUND fills the whole 9:16 frame — the photo scaled to cover, blurred and
    darkened, so there are never black bars.
  * FOREGROUND is the FULL photo (contained, never cropped) centered on top, with
    soft rounded corners, so nobody's head gets cut off.
  * CAPTION is burned into a bottom card (stories don't render body text): the day's
    OWN approved caption, scrubbed by the on-screen copy law (NO dashes), word-wrapped.
  * A small gym name sits above the caption for brand.

Pure Pillow, all local fonts, deterministic. Never fabricates copy (caption in ->
caption on the card). Formatting only ENHANCES: any failure returns None and the raw
photo posts as the story, so this can never block a post.
"""

import os
import textwrap

from . import config
from .clipper_render import scrub_onscreen

W, H = 1080, 1920
_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_MARGIN = 60
_CARD_ALPHA = 165          # bottom caption card opacity
_MAX_CAPTION_CHARS = 240   # a story overlay stays short; longer captions are trimmed


def _font(name, size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(os.path.join(_FONT_DIR, name), size)
    except Exception:
        return ImageFont.load_default()


def _rounded_mask(size, radius):
    from PIL import Image, ImageDraw
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0], size[1]], radius=radius,
                                        fill=255)
    return m


def _wrap(draw, text, font, max_w):
    """Greedy word-wrap to max_w pixels, returning lines."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def story_caption(caption):
    """The on-story text: the caption's first sentence or two, scrubbed (no dashes),
    trimmed to a story-friendly length. Empty in -> ''."""
    text = scrub_onscreen((caption or "").strip())
    # keep up to the first two sentences
    out, count = "", 0
    for chunk in text.replace("\n", " ").split(". "):
        chunk = chunk.strip()
        if not chunk:
            continue
        out = (out + " " + chunk + ".").strip()
        count += 1
        if count >= 2 or len(out) >= _MAX_CAPTION_CHARS:
            break
    if len(out) > _MAX_CAPTION_CHARS:
        cut = out[:_MAX_CAPTION_CHARS]
        out = (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip(",;") + "..."
    return out.strip()


def build_story_image(photo_path, out_path, caption="", gym_name=""):
    """Render a 1080x1920 story card from a photo. Returns out_path, or raises on a
    genuinely unreadable image (the caller treats any exception as fall-back-to-raw)."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

    base = Image.open(photo_path).convert("RGB")

    # BACKGROUND: cover the whole frame, blur + darken (no black bars, ever).
    bg = ImageOps.fit(base, (W, H), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(42))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    canvas = bg.convert("RGB")
    draw = ImageDraw.Draw(canvas, "RGBA")

    # CAPTION CARD FIRST (its height decides how much room the photo gets), so the
    # photo can center in the space ABOVE it with no dead gap.
    text = story_caption(caption)
    card_top = H - _MARGIN
    if text:
        cap_font = _font("Montserrat-SemiBold.ttf", 52)
        name_font = _font("Oswald-Bold.ttf", 42)
        inner = W - 2 * _MARGIN - 56
        lines = _wrap(draw, text, cap_font, inner)
        line_h = int(cap_font.size * 1.28)
        name = scrub_onscreen((gym_name or "").strip()).upper()
        name_h = int(name_font.size * 1.5) if name else 0
        card_h = 44 + name_h + len(lines) * line_h + 48
        card_top = H - card_h - _MARGIN
        card = Image.new("RGBA", (W - 2 * _MARGIN, card_h), (0, 0, 0, 0))
        ImageDraw.Draw(card).rounded_rectangle(
            [0, 0, W - 2 * _MARGIN, card_h], radius=34, fill=(15, 15, 18, _CARD_ALPHA))
        canvas.paste(card, (_MARGIN, card_top), card)

    # FOREGROUND PHOTO: contained (never cropped), rounded, CENTERED in the region
    # between the top margin and the caption card.
    region_top = int(H * 0.055)
    region_bot = card_top - 44
    fg = base.copy()
    fg.thumbnail((W - 2 * _MARGIN, max(200, region_bot - region_top)), Image.LANCZOS)
    radius = 36
    fg.putalpha(_rounded_mask(fg.size, radius))
    fx = (W - fg.width) // 2
    fy = region_top + max(0, (region_bot - region_top - fg.height) // 2)
    shadow = Image.new("RGBA", (fg.width + 40, fg.height + 40), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [20, 20, fg.width + 20, fg.height + 20], radius=radius, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas.paste(shadow, (fx - 20, fy - 12), shadow)
    canvas.paste(fg, (fx, fy), fg)

    # CAPTION TEXT (drawn last, over the card).
    if text:
        y = card_top + 34
        if name:
            draw.text((_MARGIN + 34, y), name, font=name_font,
                      fill=(120, 220, 130, 255))     # brand accent
            y += name_h
        for ln in lines:
            draw.text((_MARGIN + 34, y), ln, font=cap_font, fill=(255, 255, 255, 255))
            y += line_h

    canvas.convert("RGB").save(out_path, "JPEG", quality=90)
    return out_path


def get_or_make_story_image(photo_path, caption, gym_name, library_path, *,
                            logger=None):
    """Hosted-ready 9:16 story card for a photo (cached in <library>/reels/), or None
    when the flag is off / not a usable photo / render fails (raw photo posts instead).
    NEVER raises."""
    log = logger or (lambda m: print(f"[story-image] {m}"))
    if not config.story_format_enabled():
        return None
    try:
        import hashlib
        cache_dir = os.path.join(str(library_path), "reels")
        os.makedirs(cache_dir, exist_ok=True)
        with open(photo_path, "rb") as fh:
            vid_key = hashlib.sha256(fh.read()).hexdigest()[:12]
        cap_key = hashlib.sha256((caption or "").encode("utf-8")).hexdigest()[:6]
        out = os.path.join(cache_dir, f"{vid_key}_{cap_key}__story.jpg")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        return build_story_image(photo_path, out, caption=caption, gym_name=gym_name)
    except Exception as exc:  # noqa: BLE001 - a story format may never block a post
        log(f"story format failed for {os.path.basename(str(photo_path))}: "
            f"{type(exc).__name__}; posting the raw photo")
        return None
