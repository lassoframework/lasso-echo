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


def _story_video_drawtext(caption, gym_name):
    """The ffmpeg -vf filter that burns the day's caption into a bottom card on a 9:16
    story VIDEO, so a video story is never captionless (stories publish empty-body, so
    the text must live on the media). Reuses story_caption (scrubbed, no dashes, short)
    and wraps it to a fixed column. Text stays up for the WHOLE clip (a story is short).
    Returns '' when there is no usable caption text (caller then treats the video story
    as un-captionable)."""
    text = story_caption(caption)
    if not text:
        return ""
    lines = textwrap.wrap(text, width=34) or [text]
    name = scrub_onscreen((gym_name or "").strip()).upper()

    def esc(s):
        # ffmpeg drawtext escaping: backslash, quote, colon, percent.
        return (s.replace("\\", "").replace("'", "’")
                 .replace(":", "\\:").replace("%", "\\%"))

    # A translucent bottom band behind the text for legibility, then each line.
    # drawbox resolves its geometry against the INPUT frame via ih/iw (NOT bare h/w:
    # inside drawbox, h/w are the box's own dimensions, so 'y=h*0.62' is self-referential
    # and this ffmpeg build fails the whole filtergraph -> the video story burn returned
    # None and the story published captionless, Dale 2026-08-20). drawtext below correctly
    # uses h/w for the main frame, so those stay.
    filters = ["drawbox=x=0:y=ih*0.62:w=iw:h=ih*0.38:color=black@0.55:t=fill"]
    y = "h*0.66"
    if name:
        filters.append(
            f"drawtext=text='{esc(name)}':fontsize=46:fontcolor=0x78DC82:"
            f"x=(w-text_w)/2:y={y}")
        y = "h*0.72"
    for i, ln in enumerate(lines[:5]):
        yy = f"({y})+{i}*70"
        filters.append(
            f"drawtext=text='{esc(ln)}':fontsize=52:fontcolor=white:"
            f"box=0:x=(w-text_w)/2:y={yy}")
    return ",".join(filters)


def get_or_make_story_video(video_path, caption, gym_name, library_path, *,
                            runner=None, logger=None):
    """A 9:16 story VIDEO with the day's caption BURNED IN (cover-cropped to 1080x1920),
    cached in <library>/reels/, or None when the flag is off / there is no caption text /
    the render fails. This is the video counterpart of get_or_make_story_image: because
    IG/FB stories publish with an EMPTY body, a raw video story carries NO caption at all
    (Dale's captionless story, 2026-08-15). Burning the caption onto the story video is
    the only way a video story shows its words. NEVER raises: any failure returns None so
    the caller can decide (it drops the story rather than ship it captionless)."""
    log = logger or (lambda m: print(f"[story-video] {m}"))
    if not config.story_format_enabled():
        return None
    try:
        import hashlib
        from . import action_reel
        run = runner or action_reel._run
        vf = _story_video_drawtext(caption, gym_name)
        if not vf:
            log(f"no caption text for {os.path.basename(str(video_path))}; "
                "cannot caption the video story")
            return None
        cache_dir = os.path.join(str(library_path), "reels")
        os.makedirs(cache_dir, exist_ok=True)
        with open(video_path, "rb") as fh:
            vid_key = hashlib.sha256(fh.read()).hexdigest()[:12]
        cap_key = _caption_key(caption)
        out = os.path.join(cache_dir, f"{vid_key}_{cap_key}__storyvid.mp4")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        vf_full = ("scale=1080:1920:force_original_aspect_ratio=increase,"
                   "crop=1080:1920,fps=30,format=yuv420p," + vf)
        cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(video_path),
               "-vf", vf_full, "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", str(out)]
        run(cmd, "story-video")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        return None
    except Exception as exc:  # noqa: BLE001 - a story format may never crash the build
        log(f"story video failed for {os.path.basename(str(video_path))}: "
            f"{type(exc).__name__}")
        return None


def _caption_key(caption):
    """The 6-char content key this module burns into a story media FILENAME
    (get_or_make_story_image / _story_video use it as `cap_key`). Two callers deriving
    it from the SAME caption text get the SAME key, so a story's rendered media can be
    checked against a later (possibly edited) caption WITHOUT re-rendering or storing
    the raw source. Empty caption -> the key of ''."""
    import hashlib
    return hashlib.sha256((caption or "").encode("utf-8")).hexdigest()[:6]


# A rendered story asset filename carries `..._<cap_key>__story.jpg` (photo) or
# `..._<cap_key>__storyvid.mp4` (video). These markers let the publisher tell (a) that
# a story's media was caption-burned by this module, and (b) which caption it burned.
_STORY_MEDIA_MARKERS = ("__story.jpg", "__story.jpeg", "__storyvid.mp4")


def story_media_carries_caption(image_url, caption):
    """Does this story's rendered media (image_url) carry THIS caption?

    Returns:
      * True  when the media filename is a caption-burned story asset AND its embedded
        cap_key matches _caption_key(caption) — the media shows the current caption;
      * False when it is a caption-burned story asset but the cap_key does NOT match —
        the caption was edited AFTER the media was rendered, so the media is STALE
        (would publish the OLD/blank caption). The caller HOLDS the story;
      * True  when the media is NOT a caption-burned story asset at all (e.g. a raw URL
        from a flag-off baseline build, or a non-story) — this guard makes no claim
        about media it did not produce, so it never blocks the existing baseline.

    Pure + deterministic + schema-free: it reads only the filename this module wrote,
    so it works across services (the publisher runs on a different box than the portal
    edit) with no new column and no raw source retention."""
    url = (image_url or "").split("?", 1)[0]
    name = url.rsplit("/", 1)[-1]
    marker = next((m for m in _STORY_MEDIA_MARKERS if name.endswith(m)), None)
    if marker is None:
        return True  # not our burned asset: do not judge it
    stem = name[: -len(marker)]
    # filename shape: <vid_key>_<cap_key>  -> the last underscore-separated chunk.
    burned_cap_key = stem.rsplit("_", 1)[-1] if "_" in stem else stem
    return burned_cap_key == _caption_key(caption)


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
        cap_key = _caption_key(caption)
        out = os.path.join(cache_dir, f"{vid_key}_{cap_key}__story.jpg")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        return build_story_image(photo_path, out, caption=caption, gym_name=gym_name)
    except Exception as exc:  # noqa: BLE001 - a story format may never block a post
        log(f"story format failed for {os.path.basename(str(photo_path))}: "
            f"{type(exc).__name__}; posting the raw photo")
        return None
