"""
feed_image.py — auto-fit a client's feed PHOTO into an in-spec square card.

A gym owner sometimes uploads an oddly-cropped or panoramic photo (Dale, 2026-08-18: "the
photo is not to size"). Instagram/Facebook only accept a feed aspect ratio between ~0.8 (4:5
portrait) and ~1.91 (landscape); anything outside that range gets hard-cropped by the platform
so the subject is chopped. This module detects an out-of-spec photo and re-frames it into a
clean 1080x1080 card: the WHOLE photo is contained (never cropped further) on a blurred cover
fill of itself, exactly like the story formatter (no black bars, no distortion).

In-spec photos are left ALONE (returns None -> the raw photo posts unchanged), so a good
square/portrait/landscape upload is untouched. Behind AGENT_FEED_AUTOFIT (default OFF); never
raises (any failure -> None -> the raw photo posts).
"""

import os

from . import config

# Instagram/Facebook accepted feed aspect ratios (width / height). Outside this the platform
# hard-crops, so we re-frame. 4:5 = 0.8 (tallest), 1.91:1 = 1.91 (widest).
MIN_RATIO, MAX_RATIO = 0.8, 1.91
FEED_W = FEED_H = 1080                    # square: the universally safe feed frame (IG + FB)
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def needs_autofit(width, height):
    """True when a (width, height) photo is OUTSIDE the accepted feed ratio range and would
    be hard-cropped by the platform. A zero/invalid dimension never triggers a reframe."""
    if not width or not height or width <= 0 or height <= 0:
        return False
    ratio = float(width) / float(height)
    return ratio < MIN_RATIO or ratio > MAX_RATIO


def build_feed_image(photo_path, out_path):
    """Render a 1080x1080 feed card: the whole photo CONTAINED (never cropped) on a blurred,
    darkened cover fill of itself. Returns out_path, or raises on an unreadable image (the
    caller treats any exception as fall-back-to-raw)."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    base = Image.open(photo_path).convert("RGB")
    # BACKGROUND: cover the whole square, blur + darken (no bars, ever).
    bg = ImageOps.fit(base, (FEED_W, FEED_H), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(38))
    canvas = ImageEnhance.Brightness(bg).enhance(0.5).convert("RGB")
    # FOREGROUND: the whole photo, contained + centered (never cropped).
    margin = 46
    fg = base.copy()
    fg.thumbnail((FEED_W - 2 * margin, FEED_H - 2 * margin), Image.LANCZOS)
    fx = (FEED_W - fg.width) // 2
    fy = (FEED_H - fg.height) // 2
    canvas.paste(fg, (fx, fy))
    canvas.save(out_path, "JPEG", quality=90)
    return out_path


def get_or_make_feed_image(photo_path, library_path, *, logger=None):
    """A hosted-ready 1080x1080 feed card for an OUT-OF-SPEC photo (cached in
    <library>/feedfit/), or None when: the flag is off, the file is not a usable photo, the
    photo is already in-spec (posts unchanged), or the render fails. NEVER raises."""
    log = logger or (lambda m: print(f"[feed-image] {m}"))
    if not config.feed_autofit_enabled():
        return None
    try:
        if os.path.splitext(str(photo_path))[1].lower() not in _IMG_EXTS:
            return None                                  # video / non-photo: not our job
        from PIL import Image
        with Image.open(photo_path) as im:
            w, h = im.size
        if not needs_autofit(w, h):
            return None                                  # already in-spec: post the raw photo
        import hashlib
        cache_dir = os.path.join(str(library_path), "feedfit")
        os.makedirs(cache_dir, exist_ok=True)
        with open(photo_path, "rb") as fh:
            key = hashlib.sha256(fh.read()).hexdigest()[:12]
        out = os.path.join(cache_dir, f"{key}__feed.jpg")
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        return build_feed_image(photo_path, out)
    except Exception as exc:  # noqa: BLE001 - a feed reframe may never block a post
        log(f"feed autofit failed for {os.path.basename(str(photo_path))}: "
            f"{type(exc).__name__}; posting the raw photo")
        return None
