"""
Welcome-post templates for new gym announcements.

Ten templates that announce a new gym partnership. The approach is split cleanly,
same as the Summit cards, to kill the "model garbled the text / invented a logo"
class of failure at the source:

  BACKGROUND ART  ->  Nano Banana Pro (gemini-3-pro-image), the EXISTING creative
                      studio image path. The prompt asks for depth, texture, and
                      atmosphere ONLY: no text, no letters, no logos, no words.
                      When no Nano key is present (local dev), a procedural PIL
                      depth field stands in so every proof still renders and can be
                      audited; the real Pro art swaps in on Railway, cached to the
                      persistent volume so a re-fill never re-pays generation.

  TEXT + LOGOS    ->  composed in code (this module, PIL). Eyebrow, the WELCOME TO
                      LASSO headline, gym name, owner name, footer. The real LASSO
                      wordmark asset is composited (tinted through its own alpha for
                      contrast), never a generated logo. The new gym's own logo drops
                      into a LOGO SAFE ZONE (>= 13% of canvas) that the background
                      keeps visually calm so any gym logo reads on it.

Two fill fields only: GYM NAME and OWNER NAME, composed at fixed positions.

No dashes, no hyphens, no en/em dashes in any on-image copy (house style). Only
verified stats (1,000+ gym owners). Palette + type: house style Section 2 and 8.

Public API:
  make_welcome(template_id, gym_name, owner_name, logo_path, ...) -> final PNG path
  render_blank(template_id, ...)   -> template with placeholder fills, no gym logo
  slots_dict() / write_slots_json  -> slots.json v2 (logo safe zones + text slots)
  TEMPLATES                        -> the 10 template specs
"""

import json
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import config
from .summit_render import (
    SIZE, MARGIN, CREAM, NAVY, RED, SKY, WHITE, MUTE_CREAM, MUTE_NAVY,
    ANTON, OSWALD, MONT, _f, _tw, _th, _wrap, _tracked, _tracked_w,
)

# House-verified proof stat (brand_voice/knowledge/02_verified_stats.md).
PROOF_STAT = "1,000+ GYM OWNERS TRUST LASSO"

LOGO_WORDMARK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "brand", "lasso_wordmark.png")
LOGO_MARK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "brand", "lasso_mark.png")

HEADLINE = "WELCOME TO LASSO"

# Logo safe zone must be >= 13% of the 1080x1080 canvas (151,632 px). Every zone
# below clears that floor and sits in a region the background keeps calm.
_MIN_ZONE_FRAC = 0.13

# --------------------------------------------------------------------------
# FORMATS. Feed is the square 1080x1080 (SIZE, from summit_render). Story is the
# 9:16 vertical 1080x1920: SAME design system, background REGENERATED native to
# the taller frame (never a crop of the square art), text stacked vertically.
#   - Story safe band: all text + the logo live between 15% and 85% of frame
#     height (288..1632), and nothing sits in the top 250px (platform UI) or the
#     bottom 250px (reply bar / stickers).
#   - The logo slot is centered and sits in the visual middle third, >= 13% of
#     the canvas (1080x1920 = 2,073,600 px; the 540x540 zone is 14.06%).
# --------------------------------------------------------------------------
STORY_W, STORY_H = 1080, 1920
STORY_MARGIN = 96

# The single source of truth for a genuine story asset size. Every guard layer
# (render assert, host guard, publish backstop) checks against this exact tuple so
# a square feed image (1080x1080) can NEVER masquerade as a 9:16 story.
STORY_SIZE = (STORY_W, STORY_H)


def is_story_size(size):
    """True only for an exact genuine 9:16 story render (1080x1920). A square feed
    image, a None, or any off-size asset is rejected. Shared by the render assert,
    the host guard, and the publish backstop so they can never disagree."""
    try:
        return tuple(size) == STORY_SIZE
    except TypeError:
        return False
STORY_SAFE_TOP = 288        # 15% of 1920 (also clears the 250px UI band)
STORY_SAFE_BOTTOM = 1632    # 85% of 1920 (also clears the 250px reply band)
# centered logo plate in the visual middle third (vertical 640..1280)
STORY_LOGO_ZONE = (270, 730, 540, 540)

FORMATS = {
    "feed": {"w": SIZE, "h": SIZE},
    "story": {"w": STORY_W, "h": STORY_H},
}


def _fmt_dims(fmt):
    """(w, h) for a format id. Unknown formats fail loud."""
    if fmt not in FORMATS:
        raise KeyError(f"unknown welcome format {fmt!r} (have {', '.join(FORMATS)})")
    return FORMATS[fmt]["w"], FORMATS[fmt]["h"]


def story_zone(template):
    """The story logo safe zone for a template. Shared centered middle-third zone
    for every template (the tall frame stacks vertically, so the feed's left/right
    variation does not apply); kept as a function so a per-template override can be
    introduced later without touching callers."""
    return template.get("story_zone", STORY_LOGO_ZONE)


# --------------------------------------------------------------------------
# The 10 templates. 1 to 5 evolve the reference set; 6 to 10 are new.
#   base:      canvas tone the text is composed against (navy or cream)
#   bg_style:  drives the Pro prompt AND the procedural placeholder
#   accent:    where the ONE red element lives
#               "word"      -> one red word in the headline (composed)
#               "block"     -> a red tab/block (composed)
#               "rule"      -> a red hairline rule (composed)
#               "in_bg"     -> the red lives in the background art (streak /
#                              diagonal / particles); text adds NO red (Q3)
#   logo_zone: (x, y, w, h) the gym-logo safe zone
#   eyebrow:   the small caps label
# --------------------------------------------------------------------------
TEMPLATES = [
    {"id": "T1", "name": "Navy editorial", "base": "navy",
     "bg_style": "editorial_navy", "accent": "word",
     "eyebrow": "NOW PARTNERED WITH LASSO",
     "direction": "navy editorial, oversized headline, one red word",
     "logo_zone": (560, 300, 424, 388)},
    {"id": "T2", "name": "Cream editorial", "base": "cream",
     "bg_style": "editorial_cream", "accent": "word",
     "eyebrow": "WELCOME TO THE FAMILY",
     "direction": "cream editorial, calm depth, one red word",
     "logo_zone": (560, 300, 424, 388)},
    {"id": "T3", "name": "Red block", "base": "cream",
     "bg_style": "block_cream", "accent": "block",
     "eyebrow": "NEW PARTNER",
     "direction": "cream with a bold red block eyebrow tab",
     "logo_zone": (560, 320, 424, 388)},
    {"id": "T4", "name": "Split panel", "base": "navy",
     "bg_style": "split_panel", "accent": "in_bg",
     "eyebrow": "NOW WORKING WITH LASSO",
     "direction": "navy left panel, cream right, red seam accent",
     "logo_zone": (566, 300, 420, 420)},
    {"id": "T5", "name": "Badge and proof", "base": "cream",
     "bg_style": "badge_cream", "accent": "rule",
     "eyebrow": "OFFICIAL LASSO PARTNER",
     "direction": "badge treatment with the 1,000+ owners proof line",
     "logo_zone": (560, 292, 424, 400)},
    {"id": "T6", "name": "Duotone interior", "base": "navy",
     "bg_style": "duotone_interior", "accent": "word",
     "eyebrow": "NOW PARTNERED WITH LASSO",
     "direction": "duotone gym interior, atmospheric, no faces",
     "logo_zone": (566, 300, 420, 400)},
    {"id": "T7", "name": "Red light streak", "base": "navy",
     "bg_style": "red_streak", "accent": "in_bg",
     "eyebrow": "WELCOME TO LASSO",
     "direction": "dark navy with a dramatic red light streak",
     "logo_zone": (96, 300, 420, 400)},
    {"id": "T8", "name": "Topographic cream", "base": "cream",
     "bg_style": "topo_cream", "accent": "word",
     "eyebrow": "NOW PARTNERED WITH LASSO",
     "direction": "cream with a subtle topographic texture",
     "logo_zone": (560, 300, 424, 400)},
    {"id": "T9", "name": "Diagonal split", "base": "navy",
     "bg_style": "diagonal_split", "accent": "in_bg",
     "eyebrow": "NOW WORKING WITH LASSO",
     "direction": "bold diagonal navy and red split",
     "logo_zone": (96, 300, 420, 400)},
    {"id": "T10", "name": "Celebration", "base": "navy",
     "bg_style": "celebration", "accent": "in_bg",
     "eyebrow": "WELCOME TO THE FAMILY",
     "direction": "premium confetti energy, red and sky particles on navy",
     "logo_zone": (566, 300, 420, 400)},
]

_BY_ID = {t["id"]: t for t in TEMPLATES}

# Blake's kept set (2026-08-03): T1, T2, T7, T8, T9, T10. Retired templates stay
# defined for history but are never offered to a client during onboarding.
RETIRED = {"T3", "T4", "T5", "T6"}


def active_templates():
    """The kept templates, in order. Onboarding draws welcome posts from these."""
    return [t for t in TEMPLATES if t["id"] not in RETIRED]


def get_template(template_id):
    t = _BY_ID.get(template_id)
    if t is None:
        raise KeyError(f"unknown welcome template {template_id!r} "
                       f"(have {', '.join(_BY_ID)})")
    return t


def text_column(template):
    """The (x, width) of the text column: opposite the logo zone, stopping a gap
    short of it so composed text can never enter the zone. Single source of truth
    shared by the compositor and the layout-overlap grade check."""
    zx, _zy, zw, _zh = template["logo_zone"]
    gap = 40
    if zx > SIZE * 0.45:
        col_x = MARGIN
        col_w = zx - gap - col_x
    else:
        col_x = zx + zw + gap
        col_w = SIZE - MARGIN - col_x
    return col_x, col_w


def _ink_for(base):
    return WHITE if base == "navy" else NAVY


def _mute_for(base):
    return MUTE_NAVY if base == "navy" else MUTE_CREAM


def _base_rgb(base):
    return NAVY if base == "navy" else CREAM


# ==========================================================================
# BACKGROUND ART
# ==========================================================================

_NO_TEXT_RULE = (
    "ABSOLUTELY NO text, NO letters, NO numbers, NO words, NO typography, NO logos, "
    "NO watermarks, NO signage, NO UI, NO captions of any kind anywhere in the image. "
    "Background art only."
)


def background_prompt(template, fmt="feed"):
    """The Nano Banana Pro prompt for one template's BACKGROUND ART ONLY.

    Every prompt: (1) forbids all text/letters/logos, (2) names the atmosphere,
    (3) requires the logo safe-zone region to stay visually CALM (low detail, low
    contrast) so a gym logo composited there stays legible, and (4) satisfies the
    house grade heuristic (exactly one red element, a focal graphic, never a red
    background).

    fmt selects the framing: 'feed' asks for a square 1:1 composition; 'story' asks
    for a native vertical 9:16 composition with a calm CENTER band (the logo lands
    in the middle third) and the accent pushed to the upper or lower third."""
    t = template
    if fmt == "story":
        calm = ("Keep the CENTER of the frame, the whole middle third, visually CALM: "
                "low detail, low contrast, smooth and uncluttered, so a logo can sit "
                "centered there and read. Push all energy and the accent to the top "
                "third or the bottom third. ")
        shape = "Vertical 9:16 portrait composition, full bleed, native to a tall frame. "
    else:
        zx, zy, zw, zh = t["logo_zone"]
        # describe the calm zone in ninths so the model can place calm space
        side = "right" if zx > SIZE * 0.45 else "left"
        vert = "upper" if zy < SIZE * 0.4 else "middle"
        calm = (f"Keep the {vert} {side} region of the frame visually CALM: low detail, "
                f"low contrast, smooth and uncluttered, so a logo can sit there and read. ")
        shape = "Square 1:1 composition, full bleed. "
    # RED is accent-aware. Composed-accent templates (word / block / rule) carry
    # their single red in the PIL text layer, so the background must have NO red at
    # all (else the card shows two reds). in_bg templates carry the one red in the
    # art itself.
    if t["accent"] == "in_bg":
        red_feature = {
            "red_streak": ("One dramatic diagonal red #FF0000 light streak sweeps across "
                           "the frame as the single bold accent; no other red anywhere. "),
            "diagonal_split": ("One side of the bold diagonal split is vivid red #FF0000 as "
                               "the single color accent; no other red anywhere. "),
            "split_panel": ("One crisp vertical red #FF0000 seam divides the two panels as "
                            "the single accent; no other red anywhere. "),
            "celebration": ("A cluster of red #FF0000 confetti particles in one area is the "
                            "single accent; all other particles are sky blue #5EB9E6; no "
                            "other red anywhere. "),
        }[t["bg_style"]]
    else:
        red_feature = ("CRITICAL COLOR RULE: use ONLY navy #121E3C, cream #FAF6F0, and "
                       "sky blue #5EB9E6. There must be ZERO red, crimson, scarlet, "
                       "maroon, or orange anywhere in the image. The red accent is added "
                       "separately in the layout, so the background carries none. ")
    common = (f"{_NO_TEXT_RULE} {calm}"
              f"{shape}"
              "Premium B2B agency quality, editorial, clean, not busy. "
              f"{red_feature}"
              "This is a focal graphic background, not a scene with people's faces.")
    styles = {
        "editorial_navy": (
            "A deep navy #121E3C background with a soft radial glow and a subtle single "
            "sky-blue #5EB9E6 light gradient sweeping in from one side. Minimal and "
            "spacious with quiet atmospheric depth. "),
        "editorial_cream": (
            "A warm cream #FAF6F0 background with a very soft navy #121E3C depth wash in "
            "one corner and gentle paper-grain texture. Spacious and calm. "),
        "block_cream": (
            "A cream #FAF6F0 background, mostly flat and clean, with one bold navy "
            "#121E3C geometric color block anchored in a corner as a focal graphic and a "
            "faint depth wash opposite it. "),
        "split_panel": (
            "A background split into a deep navy #121E3C panel on the left and a warm "
            "cream #FAF6F0 field on the right, a clean architectural divide with flat "
            "color fields and subtle depth. "),
        "badge_cream": (
            "A cream #FAF6F0 background with a soft centered navy #121E3C medallion glow "
            "and a faint concentric ring motif. Refined, award-like, uncluttered. "),
        "duotone_interior": (
            "A duotone photographic background of a modern empty boutique gym interior at "
            "golden hour, treated entirely in navy #121E3C and cream #FAF6F0 duotone, "
            "atmospheric depth, absolutely no people and no faces. Heavy soft focus and "
            "shallow depth of field so most of the frame is a smooth blurred wall with no "
            "sharp objects; cinematic and very quiet. "),
        "red_streak": (
            "A dark navy #121E3C background, cinematic and energetic with motion blur, "
            "lens glow, and deep shadow. "),
        "topo_cream": (
            "A cream #FAF6F0 background with an elegant topographic contour-line relief "
            "embossed across it in navy #121E3C at low opacity, denser toward one corner "
            "and thinning to open space. Tactile, premium, quiet. "),
        "diagonal_split": (
            "A background divided by one bold diagonal split from corner to corner, deep "
            "navy #121E3C on one side, crisp hard edge, flat modern color blocking. "),
        "celebration": (
            "A deep navy #121E3C background with elegant abstract confetti-like particles "
            "and light bokeh drifting upward, premium and celebratory but restrained and "
            "tasteful, never cheesy. "),
    }
    return styles[t["bg_style"]] + common


# ---- procedural placeholder backgrounds (local dev, no Nano key) ----------
# These stand in for Pro art so every proof renders and can be audited. They are
# clearly-flat depth fields; they carry NO text (so OCR of a raw background is
# trivially clean) and keep the logo zone calm by construction.

def _grad_v(w, h, top, bottom):
    """Vertical gradient on a w x h canvas. (Square feed passes w == h == SIZE, so
    output is byte-identical to the original single-arg version.)"""
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)
    for y in range(h):
        f = y / h
        col = tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3))
        d.line([(0, y), (w, y)], fill=col)
    return img


def _radial_glow(img, cx, cy, radius, color, strength=0.5):
    glow = Image.new("RGB", img.size, (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 2))
    return Image.blend(img, Image.blend(img, glow, strength), 0.6)


def _lcg(seed):
    """A tiny deterministic PRNG (Math.random is unavailable and re-render must be
    stable). Returns a generator of floats in [0, 1)."""
    s = seed & 0x7FFFFFFF

    def nxt():
        nonlocal s
        s = (1103515245 * s + 12345) & 0x7FFFFFFF
        return s / 0x7FFFFFFF
    return nxt


def _grain(img, amount=8, seed=1, zone=None, zone_amount=2):
    """Fine paper/photo grain for depth. Lighter (zone_amount) inside the logo
    zone so it stays calm. Deterministic by seed. Canvas size is read from the
    image, so it works for the square feed and the tall story alike."""
    rnd = _lcg(seed)
    d = ImageDraw.Draw(img, "RGBA")
    step = 3
    W, H = img.size
    zx = zy = zw = zh = -1
    if zone is not None:
        zx, zy, zw, zh = zone
    for y in range(0, H, step):
        for x in range(0, W, step):
            in_zone = zx <= x <= zx + zw and zy <= y <= zy + zh
            amp = zone_amount if in_zone else amount
            v = int((rnd() - 0.5) * 2 * amp)
            if v:
                col = (255, 255, 255, abs(v) * 6) if v > 0 else (0, 0, 0, abs(v) * 6)
                d.point((x, y), fill=col)
    return img


def _contours(img, color, focal, spacing=34, base_alpha=44, seed=7, zone=None):
    """Draw a clean topographic relief: smooth concentric contour lines from one
    focal point, spacing easing outward, drawn on their own layer then faded near
    the top (text) and over the logo zone so those areas stay calm. Deterministic
    and elegant rather than scribbly."""
    W, H = img.size
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    fx, fy = focal
    r = 70
    i = 0
    reach = max(W, H) * 1.7
    while r < reach:
        # every third line a touch heavier for relief depth
        w = 3 if i % 3 == 0 else 2
        ld.ellipse([fx - r, fy - r, fx + r, fy + r], outline=color + (base_alpha,),
                   width=w)
        r += spacing + int(spacing * 0.10 * i)   # ease spacing outward
        i += 1
    # vertical fade: contours strong at the bottom, fading toward the top text area
    fade = Image.new("L", img.size, 0)
    fd = ImageDraw.Draw(fade)
    for y in range(H):
        fd.line([(0, y), (W, y)], fill=int(40 + 215 * (y / H)))
    # calm the logo zone: knock the fade mask down where the plate will sit
    if zone is not None:
        zx, zy, zw, zh = zone
        zmask = Image.new("L", img.size, 0)
        ImageDraw.Draw(zmask).rounded_rectangle(
            [zx - 24, zy - 24, zx + zw + 24, zy + zh + 24], radius=40, fill=90)
        zmask = zmask.filter(ImageFilter.GaussianBlur(30))
        fade = ImageChops.subtract(fade, zmask)
    layer.putalpha(ImageChops.multiply(layer.split()[3], fade))
    img.alpha_composite(layer)
    return img


def _procedural_background(template):
    """A flat, text-free depth field standing in for Nano Pro art."""
    t = template
    style = t["bg_style"]
    navy_deep = (12, 20, 42)
    if style in ("editorial_navy", "duotone_interior"):
        img = _grad_v(SIZE, SIZE, (22, 37, 72), (9, 15, 32))
        img = _radial_glow(img, int(SIZE * 0.72), int(SIZE * 0.30), 460, (34, 70, 128), 0.55)
        img = _radial_glow(img, int(SIZE * 0.12), int(SIZE * 0.82), 380, (18, 40, 82), 0.4)
        img = img.convert("RGBA")
        _grain(img, amount=7, seed=11, zone=t["logo_zone"])
        img = img.convert("RGB")
    elif style == "red_streak":
        img = _grad_v(SIZE, SIZE, (16, 26, 52), (8, 12, 28))
        d = ImageDraw.Draw(img, "RGBA")
        for i, w in enumerate((70, 40, 18)):
            d.line([(120, SIZE - 120), (SIZE - 160, 200)],
                   fill=(255, 0, 0, 60 + i * 55), width=w)
        img = img.filter(ImageFilter.GaussianBlur(6))
    elif style == "diagonal_split":
        img = Image.new("RGB", (SIZE, SIZE), navy_deep)
        d = ImageDraw.Draw(img)
        d.polygon([(SIZE, 0), (SIZE, SIZE), (int(SIZE * 0.32), SIZE)], fill=(210, 30, 34))
        d.polygon([(0, 0), (SIZE, 0), (int(SIZE * 0.32), SIZE), (0, SIZE)], fill=navy_deep)
    elif style == "celebration":
        img = _grad_v(SIZE, SIZE, (18, 30, 60), (9, 14, 30))
        d = ImageDraw.Draw(img, "RGBA")
        import hashlib
        seed = int(hashlib.sha1(t["id"].encode()).hexdigest(), 16)
        # sky-blue confetti scattered across the frame (decorative, not the accent)
        for _i in range(80):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            px = seed % SIZE
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            py = seed % SIZE
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            rr = 3 + seed % 9
            d.ellipse([px, py, px + rr, py + rr], fill=(120, 200, 240, 160))
        # ONE concentrated solid-red burst as the single accent, tucked in the
        # upper-right above the logo zone (clear of the left text column)
        bx, by = int(SIZE * 0.82), int(SIZE * 0.12)
        for _i in range(22):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            ox = (seed % 150) - 75
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            oy = (seed % 150) - 75
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            rr = 6 + seed % 12
            d.ellipse([bx + ox, by + oy, bx + ox + rr, by + oy + rr],
                      fill=(235, 30, 34, 255))
        img = img.filter(ImageFilter.GaussianBlur(1))
    elif style == "block_cream":
        img = Image.new("RGB", (SIZE, SIZE), (250, 246, 240))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 300, 300], fill=(210, 30, 34))
    elif style == "split_panel":
        split = int(SIZE * 0.52)
        # cream right panel with a soft vertical depth wash
        img = _grad_v(SIZE, SIZE, (252, 249, 244), (243, 236, 226))
        d = ImageDraw.Draw(img)
        # navy left panel with layered depth (glow + faint ghost arc)
        navy_panel = _grad_v(SIZE, SIZE, (24, 39, 74), (10, 17, 36))
        navy_panel = _radial_glow(navy_panel, int(split * 0.35), int(SIZE * 0.28),
                                  420, (36, 72, 132), 0.55)
        nd = ImageDraw.Draw(navy_panel, "RGBA")
        for rr in range(260, 900, 60):
            nd.ellipse([split - 120 - rr, SIZE - rr, split - 120 + rr, SIZE + rr],
                       outline=(60, 100, 165, 22), width=2)
        img.paste(navy_panel.crop((0, 0, split, SIZE)), (0, 0))
        img = img.convert("RGBA")
        _grain(img, amount=6, seed=23)
        d = ImageDraw.Draw(img, "RGBA")
        # crisp red seam with a soft outer glow (the single accent)
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.rectangle([split - 16, 0, split + 16, SIZE], fill=(210, 30, 34, 120))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(12)))
        d.rectangle([split - 5, 0, split + 5, SIZE], fill=(214, 32, 36, 255))
        img = img.convert("RGB")
    elif style == "badge_cream":
        img = Image.new("RGB", (SIZE, SIZE), (250, 246, 240))
        img = _radial_glow(img, SIZE // 2, int(SIZE * 0.42), 300, (225, 220, 210), 0.4)
    elif style == "topo_cream":
        # premium topographic relief: organic navy contour lines from two focal
        # points, dense at the lower right, thinning toward the calm text/logo
        # areas, over a warm cream depth wash with fine paper grain.
        img = _grad_v(SIZE, SIZE, (252, 249, 244), (240, 232, 221)).convert("RGBA")
        _contours(img, (18, 30, 60), focal=(int(SIZE * 0.94), int(SIZE * 0.96)),
                  spacing=34, base_alpha=46, zone=t["logo_zone"])
        _grain(img, amount=3, seed=31, zone=t["logo_zone"])
        img = img.convert("RGB")
    else:  # editorial_cream and any fallback
        img = _grad_v(SIZE, SIZE, (253, 250, 245), (240, 232, 221))
        img = _radial_glow(img, int(SIZE * 0.74), int(SIZE * 0.30), 420,
                           (255, 252, 247), 0.5).convert("RGBA")
        _grain(img, amount=5, seed=19, zone=t["logo_zone"])
        img = img.convert("RGB")
    return img


def _procedural_background_story(template, W=STORY_W, H=STORY_H):
    """A flat, text-free depth field NATIVE to the 9:16 story frame (never a crop
    of the square art). Same palette + accent language as the feed placeholder, but
    composed vertically: the accent lives in the upper or lower third and the middle
    third (where the logo plate lands) is kept calm. Stands in for real Nano Pro 9:16
    art on local dev so every story proof renders and can be audited."""
    t = template
    style = t["bg_style"]
    zone = story_zone(t)
    navy_deep = (12, 20, 42)
    mid_y = H // 2

    if style in ("editorial_navy", "duotone_interior"):
        img = _grad_v(W, H, (22, 37, 72), (9, 15, 32))
        img = _radial_glow(img, int(W * 0.72), int(H * 0.20), 520, (34, 70, 128), 0.55)
        img = _radial_glow(img, int(W * 0.16), int(H * 0.86), 460, (18, 40, 82), 0.4)
        img = img.convert("RGBA")
        _grain(img, amount=7, seed=11, zone=zone)
        img = img.convert("RGB")
    elif style == "red_streak":
        img = _grad_v(W, H, (16, 26, 52), (8, 12, 28))
        d = ImageDraw.Draw(img, "RGBA")
        # one dramatic red streak across the UPPER third, clear of the mid logo band
        for i, w in enumerate((70, 40, 18)):
            d.line([(80, int(H * 0.30)), (W - 120, int(H * 0.06))],
                   fill=(255, 0, 0, 60 + i * 55), width=w)
        img = img.filter(ImageFilter.GaussianBlur(6))
    elif style == "diagonal_split":
        img = Image.new("RGB", (W, H), navy_deep)
        d = ImageDraw.Draw(img)
        # bold diagonal: red wedge anchored to the BOTTOM, navy above (keeps the
        # mid-frame logo band on calm navy)
        d.polygon([(0, H), (W, H), (W, int(H * 0.72))], fill=(210, 30, 34))
    elif style == "celebration":
        img = _grad_v(W, H, (18, 30, 60), (9, 14, 30))
        d = ImageDraw.Draw(img, "RGBA")
        import hashlib
        seed = int(hashlib.sha1((t["id"] + "story").encode()).hexdigest(), 16)
        for _i in range(120):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            px = seed % W
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            py = seed % H
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            rr = 3 + seed % 9
            d.ellipse([px, py, px + rr, py + rr], fill=(120, 200, 240, 150))
        # ONE concentrated solid-red burst in the upper third (the single accent)
        bx, by = int(W * 0.74), int(H * 0.12)
        for _i in range(26):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            ox = (seed % 160) - 80
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            oy = (seed % 160) - 80
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            rr = 6 + seed % 12
            d.ellipse([bx + ox, by + oy, bx + ox + rr, by + oy + rr],
                      fill=(235, 30, 34, 255))
        img = img.filter(ImageFilter.GaussianBlur(1))
    elif style == "block_cream":
        img = Image.new("RGB", (W, H), (250, 246, 240))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, int(W * 0.34), int(H * 0.18)], fill=(210, 30, 34))
    elif style == "split_panel":
        # horizontal band split works better in a tall frame: navy top, cream bottom
        img = _grad_v(W, H, (252, 249, 244), (243, 236, 226)).convert("RGBA")
        seam = int(H * 0.30)
        navy_panel = _grad_v(W, seam, (24, 39, 74), (10, 17, 36))
        img.paste(navy_panel, (0, 0))
        d = ImageDraw.Draw(img, "RGBA")
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).rectangle([0, seam - 16, W, seam + 16],
                                       fill=(210, 30, 34, 120))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(12)))
        d.rectangle([0, seam - 5, W, seam + 5], fill=(214, 32, 36, 255))
        _grain(img, amount=6, seed=23, zone=zone)
        img = img.convert("RGB")
    elif style == "badge_cream":
        img = Image.new("RGB", (W, H), (250, 246, 240))
        img = _radial_glow(img, W // 2, mid_y, 340, (225, 220, 210), 0.4)
    elif style == "topo_cream":
        img = _grad_v(W, H, (252, 249, 244), (240, 232, 221)).convert("RGBA")
        _contours(img, (18, 30, 60), focal=(int(W * 0.94), int(H * 0.97)),
                  spacing=40, base_alpha=46, zone=zone)
        _grain(img, amount=3, seed=31, zone=zone)
        img = img.convert("RGB")
    else:  # editorial_cream and any fallback
        img = _grad_v(W, H, (253, 250, 245), (240, 232, 221))
        img = _radial_glow(img, int(W * 0.74), int(H * 0.18), 480,
                           (255, 252, 247), 0.5).convert("RGBA")
        _grain(img, amount=5, seed=19, zone=zone)
        img = img.convert("RGB")
    return img


def _cover_crop(src, W, H):
    """Cover-crop `src` to exactly W x H: scale so it fills the frame, center-crop
    the overflow, never stretch. Works for square OR portrait targets."""
    sw, sh = src.size
    scale = max(W / sw, H / sh)
    rw, rh = max(1, int(sw * scale)), max(1, int(sh * scale))
    src = src.resize((rw, rh))
    left, top = (rw - W) // 2, (rh - H) // 2
    return src.crop((left, top, left + W, top + H))


def _cache_dir(cache_dir=None):
    return cache_dir or os.path.join(config.LIBRARY_PATH, "welcome_bg")


def ensure_background(template, bg_client=None, cache_dir=None, force=False,
                      prefer=None, fmt="feed"):
    """Return a path to this template's cached background PNG for a given format.

    If a Nano Pro client is available (and the flag/key are set), render real Pro
    art from background_prompt(); otherwise fall back to a procedural depth field.
    prefer='placeholder' forces the procedural background even when a client exists
    (used as the grade-fallback when Pro art will not comply). Either way the result
    is cached on the persistent volume so a re-fill never re-pays generation.
    fmt is 'feed' (square) or 'story' (9:16); story art is regenerated NATIVE to the
    tall frame, never a crop of the square feed art, and cached under its own key.
    Returns (path, mode) where mode is 'pro' or 'placeholder'.
    """
    t = template
    W, H = _fmt_dims(fmt)
    cdir = _cache_dir(cache_dir)
    os.makedirs(cdir, exist_ok=True)

    client = bg_client
    if client is None and prefer != "placeholder":
        # only builds a real client when flag ON and key present; else None
        try:
            from .creative_studio import _default_client
            client = _default_client()
        except Exception:
            client = None
    if prefer == "placeholder":
        client = None

    mode = "pro" if client is not None else "placeholder"
    # feed keeps the historical unsuffixed cache name (no churn); story is suffixed.
    suffix = "" if fmt == "feed" else f"_{fmt}"
    path = os.path.join(cdir, f"{t['id']}_{mode}{suffix}.png")
    if os.path.isfile(path) and not force:
        return path, mode

    if client is not None:
        prompt = background_prompt(t, fmt=fmt)
        img_bytes = client.generate_image(prompt=prompt, model=config.NANO_MODEL)
        with open(path, "wb") as fh:
            fh.write(img_bytes)
        # cover-crop center to the target frame (never stretch), so real Pro art
        # keeps its proportions whether square (feed) or portrait (story).
        src = _cover_crop(Image.open(path).convert("RGB"), W, H)
        src.save(path)
    else:
        bg = (_procedural_background_story(t, W, H) if fmt == "story"
              else _procedural_background(t))
        bg.convert("RGB").save(path)
    return path, mode


# ==========================================================================
# CALM ZONE + LOGO
# ==========================================================================

def _zone_frac(zone, canvas_w=SIZE, canvas_h=SIZE):
    _, _, w, h = zone
    return (w * h) / float(canvas_w * canvas_h)


def calm_zone_ok(img, zone, std_threshold=60.0):
    """True when the logo safe zone carries no busy, high-contrast detail (so a
    composited gym logo reads). Smooth depth gradients are fine; only genuinely
    noisy or cluttered zones fail. Checked on the background BEFORE the plate is
    drawn (a Pro background must earn the zone; procedural ones pass by design)."""
    x, y, w, h = zone
    crop = img.convert("L").crop((x, y, x + w, y + h))
    px = list(crop.getdata())
    if not px:
        return False
    n = len(px)
    mean = sum(px) / n
    var = sum((p - mean) ** 2 for p in px) / n
    return (var ** 0.5) <= std_threshold


def _trim_transparent(logo):
    """Crop a logo to its non-transparent bounding box. Most gym logos ship with a
    large transparent margin baked in; fitting the FULL image into the zone then
    renders the actual mark tiny. Trimming to the real ink first makes the mark fill
    its safe zone. Falls back to the original when there is no alpha or it is empty."""
    try:
        if logo.mode != "RGBA":
            return logo
        alpha = logo.split()[3]
        bbox = alpha.getbbox()          # tight box of every non-zero-alpha pixel
        if bbox and (bbox[2] - bbox[0]) > 0 and (bbox[3] - bbox[1]) > 0:
            return logo.crop(bbox)
    except Exception:
        pass
    return logo


def _fit_into(logo, box_w, box_h, pad_frac=0.06):
    # Trim the baked-in transparent margin FIRST so the real mark fills the zone,
    # then scale to the safe zone with a small breathing pad (0.06, was 0.14 — the
    # logos were reading tiny). Scales up small marks and down large ones alike.
    logo = _trim_transparent(logo)
    pad_w, pad_h = int(box_w * pad_frac), int(box_h * pad_frac)
    avail_w, avail_h = box_w - 2 * pad_w, box_h - 2 * pad_h
    lw, lh = logo.size
    scale = min(avail_w / lw, avail_h / lh)
    return logo.resize((max(1, int(lw * scale)), max(1, int(lh * scale))))


# NOTE: the cream/navy logo PLATE was removed on 2026-08-06 (Blake's ruling: kill
# the plate everywhere). The gym logo now sits directly on the open background. The
# old _draw_zone_plate and its _logo_needs_dark_plate helper are gone; blank review
# templates use _draw_zone_hint below (a fill-less clear-space hint) instead.

def _draw_zone_hint(img, zone, base):
    """Blake killed the logo plate (2026-08-06): a real gym logo now sits directly on
    the open background, no cream/navy box behind it. On the BLANK review template
    (no logo) we still need to show WHERE the logo lands, but NOT a filled box that
    would read as part of the design. So we draw only a thin dashed clear-space frame
    and a faint 'GYM LOGO' label, tinted to the base so it reads without competing.
    Nothing is filled; the background shows through."""
    x, y, w, h = zone
    d = ImageDraw.Draw(img, "RGBA")
    ink = (250, 246, 240) if base == "navy" else (18, 30, 60)
    pad = int(min(w, h) * 0.16)
    # dashed rounded frame so it reads as a placeholder, not a solid plate
    fx0, fy0, fx1, fy1 = x + pad, y + pad, x + w - pad, y + h - pad
    dash, gap = 18, 12
    step = dash + gap
    for sx in range(fx0, fx1, step):
        d.line([(sx, fy0), (min(sx + dash, fx1), fy0)], fill=ink + (70,), width=2)
        d.line([(sx, fy1), (min(sx + dash, fx1), fy1)], fill=ink + (70,), width=2)
    for sy in range(fy0, fy1, step):
        d.line([(fx0, sy), (fx0, min(sy + dash, fy1))], fill=ink + (70,), width=2)
        d.line([(fx1, sy), (fx1, min(sy + dash, fy1))], fill=ink + (70,), width=2)
    label = "GYM LOGO"
    lf = _f(OSWALD, 30)
    lw = _tracked_w(d, label, lf, 5)
    _tracked(d, (x + (w - lw) // 2, y + h // 2 - 18), label, lf, ink + (120,), 5)


def place_gym_logo(img, logo_path, zone, base):
    """Composite the gym's own logo directly into the safe zone, centered, over the
    open background. Placed as-is (never recolored) so the gym's real mark is
    preserved. Blake killed the logo plate everywhere (2026-08-06): there is NO cream
    or navy box behind the logo now. With no logo (a blank review template) a subtle
    dashed clear-space hint shows where the gym's mark will land, without a filled
    plate that would read as part of the design."""
    has_logo = bool(logo_path) and os.path.isfile(logo_path)
    if not has_logo:
        _draw_zone_hint(img, zone, base)
        return
    x, y, w, h = zone
    logo = Image.open(logo_path).convert("RGBA")
    logo = _fit_into(logo, w, h)
    lx = x + (w - logo.size[0]) // 2
    ly = y + (h - logo.size[1]) // 2
    img.alpha_composite(logo, (lx, ly))


def _tinted_wordmark(color, target_h):
    """The REAL LASSO wordmark asset, recolored through its own alpha to `color`
    for contrast on dark or light footers. Shape is the real logo, never generated."""
    if not os.path.isfile(LOGO_WORDMARK):
        return None
    src = Image.open(LOGO_WORDMARK).convert("RGBA")
    w, h = src.size
    scale = target_h / h
    src = src.resize((max(1, int(w * scale)), target_h))
    solid = Image.new("RGBA", src.size, color + (255,))
    solid.putalpha(src.split()[3])
    return solid


# ==========================================================================
# TEXT COMPOSITION
# ==========================================================================

def _fit_headline(d, text, max_w, max_lines, start, floor=64):
    s = start
    while s >= floor:
        fo = _f(ANTON, s)
        ls = _wrap(d, text, fo, max_w)
        if len(ls) <= max_lines:
            return fo, ls
        s -= 4
    fo = _f(ANTON, floor)
    return fo, _wrap(d, text, fo, max_w)


# Story bottom block lives between the logo plate and the footer wordmark. These
# constants and the fitter below are the SINGLE SOURCE OF TRUTH the compositor and
# the story grade guard both use, so they can never disagree about where the gym
# name + owner land (the feed guard pattern, applied to the tall frame).
STORY_FOOTER_TOP = STORY_SAFE_BOTTOM - 46      # the wordmark baseline row
STORY_BOTTOM_RESERVE = 30                      # gap kept clear above the footer
STORY_OWNER_H = 60                             # vertical room the owner line takes


def _story_bottom_fit(d, template, gym_name, owner_name):
    """Fit the story bottom block (gym name, up to 2 lines, + optional owner line) so
    it ALWAYS stays above the footer inside the 85% safe band. Shrinks the gym name
    until the whole block fits; even a long two-line name cannot overrun. Returns
    (gym_font, gym_lines, owner_font_or_None, top_y, bottom_y)."""
    _zx, zy, _zw, zh = story_zone(template)
    col_w = STORY_W - 2 * STORY_MARGIN
    top = zy + zh + 64
    owner = (owner_name or "").strip()
    owner_h = STORY_OWNER_H if owner else 0
    avail = (STORY_FOOTER_TOP - STORY_BOTTOM_RESERVE) - top - owner_h
    gym = (gym_name or "").strip().upper() or "YOUR GYM NAME"
    gf, glines = _fit_headline(d, gym, col_w, 2, 92, floor=44)
    while gf.size > 44 and len(glines) * (gf.size + 10) > avail:
        gf, glines = _fit_headline(d, gym, col_w, 2, gf.size - 6, floor=44)
    gym_h = len(glines) * (gf.size + 10)
    of = _f(MONT, 42) if owner else None
    bottom = top + gym_h + owner_h
    return gf, glines, of, top, bottom


def story_bottom_bounds(template, gym_name, owner_name):
    """The bottom y of the composed story bottom block for a given name. The grade
    guard calls this (with a stress name) to verify the block clears the footer,
    measuring the SAME way the compositor draws it."""
    scratch = ImageDraw.Draw(Image.new("RGBA", (STORY_W, STORY_H)))
    _gf, _gl, _of, _top, bottom = _story_bottom_fit(scratch, template, gym_name,
                                                    owner_name)
    return bottom


def _compose_text(img, template, gym_name, owner_name):
    """Draw eyebrow, WELCOME TO LASSO headline, gym name, owner name, footer, and
    the accent. All left-aligned, asymmetric, house style. Returns on-card text for
    the copy scan / grade Q4."""
    t = template
    base = t["base"]
    ink = _ink_for(base)
    mute = _mute_for(base)
    d = ImageDraw.Draw(img, "RGBA")

    # text column sits opposite the logo zone and stops a gap short of it so
    # composed text can never enter it (the T5-collision class of bug).
    col_x, col_w = text_column(t)

    shadow = (base == "navy") or t["bg_style"] in ("duotone_interior", "red_streak",
                                                    "diagonal_split", "celebration")

    def draw(x, y, text, font, fill):
        if shadow:
            d.text((x + 2, y + 2), text, font=font, fill=(6, 10, 20, 200))
        d.text((x, y), text, font=font, fill=fill)

    y = MARGIN

    # --- accent: red block behind eyebrow (T3) ---
    if t["accent"] == "block":
        ef = _f(OSWALD, 30)
        ew = _tracked_w(d, t["eyebrow"].upper(), ef, 5)
        d.rectangle([col_x - 12, y - 10, col_x + ew + 24, y + 46], fill=RED)
        _tracked(d, (col_x, y), t["eyebrow"].upper(), ef, WHITE, 5)
    else:
        eb_col = SKY if base == "navy" else NAVY
        _tracked(d, (col_x, y), t["eyebrow"].upper(), _f(OSWALD, 30), eb_col, 5)
    y += 74

    # --- accent: red hairline rule (T4, T5) ---
    if t["accent"] == "rule":
        d.rectangle([col_x, y, col_x + 120, y + 8], fill=RED)
        y += 34

    # --- headline: WELCOME TO LASSO, one red word only when accent == word ---
    hf, lines = _fit_headline(d, HEADLINE, col_w, 3, 132)
    red_word = "LASSO" if t["accent"] == "word" else None
    lh = _th(d, "AY", hf) + 6 + int(hf.size * 0.28)
    for i, line in enumerate(lines):
        cx = col_x
        for word in line.split():
            fill = RED if (red_word and word.strip(".,").upper() == red_word) else ink
            if shadow:
                d.text((cx + 3, y + i * lh + 3), word, font=hf, fill=(6, 10, 20, 220))
            d.text((cx, y + i * lh), word, font=hf, fill=fill)
            cx += _tw(d, word + " ", hf)
    y += len(lines) * lh + 26

    # --- gym name (fill field) ---
    gym = (gym_name or "").strip().upper() or "YOUR GYM NAME"
    gf, glines = _fit_headline(d, gym, col_w, 2, 68, floor=40)
    for i, line in enumerate(glines):
        gy = y + i * (gf.size + 8)
        draw(col_x, gy, line, gf, ink)
    y += len(glines) * (gf.size + 8) + 14

    # --- owner name (fill field) ---
    owner = (owner_name or "").strip()
    if owner:
        of = _f(MONT, 42)
        owner_fill = (20, 26, 45) if base == "cream" else (255, 255, 255)
        draw(col_x, y, f"with {owner}", of, owner_fill)
        y += 56

    # --- T5 proof line (verified stat), flowing below the owner line ---
    if t["id"] == "T5":
        pf = _f(OSWALD, 24)
        _tracked(d, (col_x, y + 8), PROOF_STAT, pf, mute, 3)
        y += 44

    # --- footer: real LASSO wordmark + url ---
    fy = SIZE - MARGIN - 6
    wm = _tinted_wordmark(WHITE if base == "navy" else NAVY, 40)
    fx = MARGIN
    if wm is not None:
        img.alpha_composite(wm, (fx, fy - 8))
        fx += wm.size[0] + 20
    url = "LASSOFRAMEWORK.COM"
    uf = _f(OSWALD, 24)
    d.text((SIZE - MARGIN - _tw(d, url, uf), fy + 4), url, font=uf, fill=mute)

    on_card_text = " ".join([t["eyebrow"], HEADLINE, gym, (owner or ""),
                             (PROOF_STAT if t["id"] == "T5" else ""), url])
    return on_card_text


def _compose_text_story(img, template, gym_name, owner_name):
    """Story (9:16) text: the SAME hierarchy as feed (eyebrow, WELCOME TO LASSO,
    gym logo, gym name, owner name, footer) stacked vertically down the tall frame.
    Text is left-aligned in a full-width column; the logo plate is centered in the
    middle third (drawn separately). Everything stays inside the 15..85% safe band
    and clear of the top/bottom 250px. Headline runs larger than feed. Returns the
    on-card text for the copy scan / grade Q4."""
    t = template
    base = t["base"]
    ink = _ink_for(base)
    mute = _mute_for(base)
    d = ImageDraw.Draw(img, "RGBA")

    col_x = STORY_MARGIN
    col_w = STORY_W - 2 * STORY_MARGIN
    zx, zy, zw, zh = story_zone(t)

    shadow = (base == "navy") or t["bg_style"] in ("duotone_interior", "red_streak",
                                                   "diagonal_split", "celebration",
                                                   "split_panel")

    def draw(x, y, text, font, fill):
        if shadow:
            d.text((x + 2, y + 2), text, font=font, fill=(6, 10, 20, 200))
        d.text((x, y), text, font=font, fill=fill)

    # ---------- TOP BLOCK: eyebrow + headline, anchored at the top of the band ----------
    y = STORY_SAFE_TOP
    if t["accent"] == "block":
        ef = _f(OSWALD, 34)
        ew = _tracked_w(d, t["eyebrow"].upper(), ef, 5)
        d.rectangle([col_x - 12, y - 10, col_x + ew + 24, y + 50], fill=RED)
        _tracked(d, (col_x, y), t["eyebrow"].upper(), ef, WHITE, 5)
    else:
        eb_col = SKY if base == "navy" else NAVY
        _tracked(d, (col_x, y), t["eyebrow"].upper(), _f(OSWALD, 34), eb_col, 5)
    y += 84

    if t["accent"] == "rule":
        d.rectangle([col_x, y, col_x + 140, y + 9], fill=RED)
        y += 40

    # headline runs larger than feed (more vertical room) but must clear the logo
    # plate: shrink until the whole block fits in the space above zy (with a gap),
    # not just within the line-count cap. This is the story analogue of the feed
    # text-column guard that stops composed text entering the logo zone.
    head_gap = 34
    avail_h = (zy - head_gap) - y
    # story headline runs LARGER than feed: a two-line fit ("WELCOME TO" / "LASSO")
    # at a bigger size, never below the feed size, still clearing the logo plate.
    hf, lines = _fit_headline(d, HEADLINE, col_w, 2, 152, floor=132)
    while hf.size > 132:
        lh_try = _th(d, "AY", hf) + 8 + int(hf.size * 0.28)
        if len(lines) * lh_try <= avail_h:
            break
        hf, lines = _fit_headline(d, HEADLINE, col_w, 2, hf.size - 6, floor=132)
    red_word = "LASSO" if t["accent"] == "word" else None
    lh = _th(d, "AY", hf) + 8 + int(hf.size * 0.28)
    for i, line in enumerate(lines):
        cx = col_x
        for word in line.split():
            fill = RED if (red_word and word.strip(".,").upper() == red_word) else ink
            if shadow:
                d.text((cx + 3, y + i * lh + 3), word, font=hf, fill=(6, 10, 20, 220))
            d.text((cx, y + i * lh), word, font=hf, fill=fill)
            cx += _tw(d, word + " ", hf)

    # ---------- BOTTOM BLOCK: gym name + owner, height-clamped so it can never run
    # past the footer / out of the 85% safe band, even for a long two-line name ----
    gf, glines, of, y, _bottom = _story_bottom_fit(d, t, gym_name, owner_name)
    for i, line in enumerate(glines):
        draw(col_x, y + i * (gf.size + 10), line, gf, ink)
    y += len(glines) * (gf.size + 10) + 12

    owner = (owner_name or "").strip()
    if of is not None and owner:
        owner_fill = (20, 26, 45) if base == "cream" else (255, 255, 255)
        draw(col_x, y, f"with {owner}", of, owner_fill)
        y += STORY_OWNER_H

    if t["id"] == "T5":
        pf = _f(OSWALD, 26)
        _tracked(d, (col_x, y + 8), PROOF_STAT, pf, mute, 3)

    # ---------- FOOTER: real wordmark + url, inside the bottom safe line ----------
    fy = STORY_SAFE_BOTTOM - 46
    wm = _tinted_wordmark(WHITE if base == "navy" else NAVY, 44)
    fx = STORY_MARGIN
    if wm is not None:
        img.alpha_composite(wm, (fx, fy - 8))
    url = "LASSOFRAMEWORK.COM"
    uf = _f(OSWALD, 26)
    d.text((STORY_W - STORY_MARGIN - _tw(d, url, uf), fy + 4), url, font=uf, fill=mute)

    gym = (gym_name or "").strip().upper() or "YOUR GYM NAME"
    on_card_text = " ".join([t["eyebrow"], HEADLINE, gym, (owner or ""),
                             (PROOF_STAT if t["id"] == "T5" else ""), url])
    return on_card_text


# ==========================================================================
# PUBLIC: make_welcome / render_blank
# ==========================================================================

def _render(template, gym_name, owner_name, logo_path, out_path,
            bg_client=None, cache_dir=None, prefer=None, fmt="feed"):
    W, H = _fmt_dims(fmt)
    bg_path, mode = ensure_background(template, bg_client=bg_client,
                                      cache_dir=cache_dir, prefer=prefer, fmt=fmt)
    img = Image.open(bg_path).convert("RGBA")
    if img.size != (W, H):
        img = img.resize((W, H))
    zone = story_zone(template) if fmt == "story" else template["logo_zone"]
    # gym logo goes down first (directly on the open background, no plate), then text
    place_gym_logo(img, logo_path, zone, template["base"])
    if fmt == "story":
        on_card_text = _compose_text_story(img, template, gym_name, owner_name)
    else:
        on_card_text = _compose_text(img, template, gym_name, owner_name)
    final = img.convert("RGB")
    # GUARD (layer a, render): a story asset MUST be a genuine 9:16 (1080x1920). If
    # anything upstream produced a square (or any off-size) frame, refuse to write a
    # story that would center-crop to garbage. A square can never leave here as a story.
    if fmt == "story" and not is_story_size(final.size):
        raise ValueError(
            f"welcome story render is {final.size}, expected {STORY_SIZE}; "
            f"refusing to write a non-9:16 story (a square feed image would crop). "
            f"template={template['id']}")
    final.save(out_path)
    return out_path, mode, on_card_text


def make_welcome(template_id, gym_name, owner_name, logo_path, format="feed",
                 out_path=None, bg_client=None, cache_dir=None):
    """Compose a finished welcome post: cached background + gym logo + text.

    This is the onboarding entry point. With a warm background cache it renders a
    real gym's card in a fraction of a second and re-pays no Pro generation.
    format is 'feed' (1080x1080) or 'story' (1080x1920) off the SAME design system.
    Returns the output PNG path.
    """
    t = get_template(template_id)
    _fmt_dims(format)  # validate early, fail loud on a bad format
    if out_path is None:
        suffix = "" if format == "feed" else f"_{format}"
        out_path = os.path.join(_cache_dir(cache_dir),
                                f"welcome_{template_id}{suffix}_filled.png")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    path, _mode, _text = _render(t, gym_name, owner_name, logo_path, out_path,
                                 bg_client=bg_client, cache_dir=cache_dir, fmt=format)
    # GUARD (layer a, render): make_welcome MUST return a story that is exactly
    # 1080x1920. Re-open the written file and assert, so a square can never be the
    # thing this function hands back as a story (defense in depth over _render).
    if format == "story":
        with Image.open(path) as _im:
            if not is_story_size(_im.size):
                raise ValueError(
                    f"make_welcome story is {_im.size}, expected {STORY_SIZE}; "
                    f"a non-9:16 story would center-crop and cut off the gym name/logo. "
                    f"template={template_id}")
    return path


def render_blank(template_id, out_path, bg_client=None, cache_dir=None, format="feed"):
    """The empty template: placeholder fills, no gym logo (shows a subtle clear-space
    hint, no plate, so Blake can see where a gym logo lands)."""
    t = get_template(template_id)
    _fmt_dims(format)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    path, mode, _text = _render(t, "YOUR GYM NAME", "Owner Name", None, out_path,
                                bg_client=bg_client, cache_dir=cache_dir, fmt=format)
    return path, mode


# ==========================================================================
# TEST LOGOS (for the 20 filled proofs: one wide, one square, per template)
# ==========================================================================

def make_test_logos(out_dir):
    """Two obviously-placeholder gym logos to prove the safe zone reads on both a
    wide and a square mark. Dark marks on transparent, so they read on the calm
    (light) plate of every template. Returns (wide_path, square_path)."""
    os.makedirs(out_dir, exist_ok=True)
    navy = (18, 30, 60, 255)
    red = (210, 30, 34, 255)
    wide = Image.new("RGBA", (900, 320), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wide)
    wd.rounded_rectangle([6, 6, 894, 314], radius=40, outline=navy, width=10)
    wf = _f(ANTON, 120)
    tw = wd.textbbox((0, 0), "GYM LOGO", font=wf)
    wd.text(((900 - (tw[2] - tw[0])) // 2, 90), "GYM LOGO", font=wf, fill=navy)
    wd.ellipse([820, 40, 860, 80], fill=red)
    wide_path = os.path.join(out_dir, "test_logo_wide.png")
    wide.save(wide_path)

    square = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
    sd = ImageDraw.Draw(square)
    sd.ellipse([10, 10, 490, 490], outline=navy, width=14)
    sf = _f(ANTON, 200)
    tb = sd.textbbox((0, 0), "GL", font=sf)
    sd.text(((500 - (tb[2] - tb[0])) // 2, 120), "GL", font=sf, fill=navy)
    sd.ellipse([360, 90, 410, 140], fill=red)
    square_path = os.path.join(out_dir, "test_logo_square.png")
    square.save(square_path)
    return wide_path, square_path


# ==========================================================================
# slots.json v2
# ==========================================================================

def slots_dict():
    """The slots.json v2 structure: per template, PER FORMAT, the logo safe zone and
    the text-slot layout, in canvas pixel coords. Onboarding and any external tool
    read this to know where each field lands in feed (1080x1080) and story
    (1080x1920)."""
    out = {"canvas": {"w": SIZE, "h": SIZE}, "margin": MARGIN,
           "min_logo_zone_fraction": _MIN_ZONE_FRAC,
           "headline": HEADLINE, "fill_fields": ["gym_name", "owner_name"],
           "formats": {
               "feed": {"w": SIZE, "h": SIZE},
               "story": {"w": STORY_W, "h": STORY_H,
                         "safe_top": STORY_SAFE_TOP, "safe_bottom": STORY_SAFE_BOTTOM,
                         "margin": STORY_MARGIN},
           },
           "kept": [t["id"] for t in active_templates()],
           "templates": {}}
    for t in TEMPLATES:
        zx, zy, zw, zh = t["logo_zone"]
        # matches _compose_text: text sits opposite the zone and stops short of it
        text_side = "left" if zx > SIZE * 0.45 else "right"
        sx, sy, sw, sh = story_zone(t)
        out["templates"][t["id"]] = {
            "name": t["name"], "direction": t["direction"],
            "base": t["base"], "bg_style": t["bg_style"], "accent": t["accent"],
            "eyebrow": t["eyebrow"],
            "feed": {
                "logo_zone": {"x": zx, "y": zy, "w": zw, "h": zh,
                              "fraction": round(_zone_frac(t["logo_zone"]), 4)},
                "text_column": text_side,
            },
            "story": {
                "logo_zone": {"x": sx, "y": sy, "w": sw, "h": sh,
                              "fraction": round(_zone_frac((sx, sy, sw, sh),
                                                           STORY_W, STORY_H), 4)},
                "text_column": "full",
            },
            # back-compat: keep the flat feed keys the v2 readers already use
            "logo_zone": {"x": zx, "y": zy, "w": zw, "h": zh,
                          "fraction": round(_zone_frac(t["logo_zone"]), 4)},
            "text_column": text_side,
        }
    return out


def write_slots_json(path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(slots_dict(), fh, indent=2)
    return path
