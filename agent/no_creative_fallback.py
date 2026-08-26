"""
no_creative_fallback.py — the client Organic Social calendar's no-creative fallback.

When a scheduled calendar post has NO usable creative image (image_url missing / None,
the Gemini or Nano render failed or was skipped, or the DAM had nothing), the calendar
must NOT show a blank / broken card and must NOT fabricate a photo. Instead it degrades
to a clean, on-brand WEBSITE STYLE INFOGRAPHIC built from the post's OWN approved text
(caption + pillar) using the HOUSE PIL renderer — the same clean, professional data-viz
look already locked in (no AI-looking illustrated scenes; the creative_studio banned
list stands). Pure typographic / graphic infographic on the brand palette.

Two hard guarantees, mirrored from the rest of Echo:

  - OFF BY DEFAULT (`config.no_creative_fallback_enabled()`). Flag OFF -> behavior is
    unchanged: an image_url present is returned as-is, an absent one stays absent (the
    caller keeps its current empty-state behavior). No fallback card is ever drawn.
  - NO FABRICATION. The infographic renders ONLY the post's approved caption / pillar
    text. It invents no facts, offers, prices, or stats. If there is NO caption and no
    pillar to render, it returns None and lets the upstream BLOCK / show its empty
    state — it never emits a blank card and never makes up copy.

Pure PIL: no API key, no model-rendered (garble-prone) text. The card is rendered
locally, then HOSTED to object storage so the client portal (a separate service with no
local filesystem access to the worker's disk) can display it: display_image_for returns
the PUBLIC hosted url, never a local path. Hosting is content-addressed (media_host),
so an identical render for the same text is deduped, not re-uploaded; a short in-process
cache also skips re-rendering the same post on repeated page loads. If hosting is off or
returns nothing, display_image_for returns None (a clean empty state beats an unshowable
local path). Nothing here publishes and no gate is weakened; this only decides a calendar
card's DISPLAY image.

The brand fonts (summit_render.ANTON / OSWALD / MONT) load from repo-bundled files under
agent/assets/fonts/ (resolved relative to the package), so they ship on every service. A
render or font failure never raises into the web request: it degrades to None (the empty
state), so a missing font can never 500 a portal page.

HARD COPY RULES (grep-asserted in tests): no em / en / hyphen dashes and never the word
"vendor" in any on-image text.
"""

import os
import re
import tempfile

from . import config, summit_render as sr

# House palette + text helpers from the summit compositor (single source of truth for
# the LASSO house style: cream/navy/red/sky, the brand fonts, wrap/fit/headline).
CREAM = sr.CREAM
NAVY = sr.NAVY
RED = sr.RED
SKY = sr.SKY
WHITE = sr.WHITE
MUTE_CREAM = sr.MUTE_CREAM
MUTE_NAVY = sr.MUTE_NAVY

ANTON = sr.ANTON
OSWALD = sr.OSWALD
OSWALD_B = sr.OSWALD_B
MONT = sr.MONT
MONT_SB = sr.MONT_SB

_f = sr._f
_tw = sr._tw
_tracked = sr._tracked
_wrap = sr._wrap
_fit = sr._fit
_headline = sr._headline

FEED = 1080
STORY_W, STORY_H = 1080, 1920
MARGIN = sr.MARGIN
STORY_SAFE_TOP = 250
STORY_SAFE_BOT = 320

# Banned on-image words (mirrors creative_studio._BANNED_HEADLINE_WORDS).
_BANNED_WORDS = ("vendor",)


def _clean(text):
    """Strip every dash-family character from approved copy (the brand no-dash rule),
    collapse the doubled spaces a removed dash leaves, and trim. Delegates to
    copy_gate.scrub (single house-style gate)."""
    if not text:
        return ""
    from . import copy_gate
    return copy_gate.scrub(str(text))


def _check_hard_rules(text):
    """Raise ValueError for a banned word in on-image copy. The dash rule is handled by
    _clean; this catches remaining bans so a banned word can never render on a card."""
    low = str(text or "").lower()
    for word in _BANNED_WORDS:
        if word in low:
            raise ValueError(
                f"no_creative_fallback: banned word {word!r} in on-image text. "
                "Remove it before rendering.")


def _has_usable_image(image_url):
    """True iff image_url is a present, non-empty, usable URL string. A missing / None
    value, an empty or whitespace string, or a non-string is NOT usable (the render
    failed, was skipped, or the DAM had nothing) and triggers the fallback."""
    return isinstance(image_url, str) and bool(image_url.strip())


def _approved_text(caption, pillar):
    """The post's OWN approved text, cleaned. Returns (eyebrow, headline, deck) where:
      eyebrow  the pillar as a short ALL CAPS label (empty when no pillar)
      headline the FIRST sentence / line of the caption (the card's hero)
      deck     one short following line of context (empty when the caption is one line)
    All three come ONLY from the approved caption / pillar — nothing is invented. When
    there is no caption text, the pillar alone becomes the headline (still the post's
    own approved text). Returns (None, None, None) when there is nothing to render."""
    cap = _clean(caption)
    pil = _clean(pillar)
    if not cap and not pil:
        return None, None, None

    eyebrow = pil.upper() if pil else ""

    if cap:
        # First sentence / first line is the hero; the next non-empty chunk is the deck.
        # Split on a sentence boundary (period + space) or a newline; keep it verbatim.
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", cap) if p.strip()]
        headline = parts[0] if parts else cap
        deck = parts[1] if len(parts) > 1 else ""
    else:
        # No caption: the pillar carries the card as the headline (approved text only).
        headline = pil
        deck = ""
        eyebrow = ""  # pillar is now the headline; don't repeat it as the eyebrow

    return eyebrow, headline, deck


def _render_infographic(eyebrow, headline, deck, out_path, is_story=False):
    """Render a clean website-style infographic from approved text to out_path.

    Pure typographic house card: cream canvas, navy ink, ONE red accent (a short rule
    under the eyebrow), the pillar eyebrow, the caption headline (hero), and one deck
    line. No illustrated scenes, no figures, no photos, no AI-looking art (the
    creative_studio banned list stands): type + one geometric accent only.

    feed  -> 1080x1080; story -> 1080x1920 with top / bottom safe bands.
    Returns out_path.
    """
    from PIL import Image, ImageDraw

    # Every on-image string passes the hard-rules check (dashes already stripped by
    # _approved_text via _clean; this catches the banned word).
    for s in (eyebrow, headline, deck):
        _check_hard_rules(s)

    w = STORY_W if is_story else FEED
    h = STORY_H if is_story else FEED
    cw = w - 2 * MARGIN

    img = Image.new("RGB", (w, h), CREAM)
    d = ImageDraw.Draw(img)
    ink, mute = NAVY, MUTE_CREAM

    # Story frames start below the top safe band; feed starts at the margin.
    y = STORY_SAFE_TOP if is_story else MARGIN

    # Eyebrow: the pillar as a small tracked ALL CAPS label.
    if eyebrow:
        _tracked(d, (MARGIN, y), eyebrow, _f(OSWALD_B, 30), mute, 5)
        y += 58

    # The ONE red element: a short accent rule under the eyebrow (grade Q3, exactly one
    # red). Deliberate and designed, never a second accent anywhere else on the card.
    d.rectangle([MARGIN, y, MARGIN + 120, y + 8], fill=RED)
    y += 40

    # Headline (the hero): the caption's first line, big and left-aligned. No red word
    # (the single red already lives in the accent rule), so red_tokens is empty.
    max_lines = 6 if is_story else 4
    start = 108 if is_story else 92
    hf, lines = _fit(d, headline.upper(), cw, max_lines, start)
    y = _headline(d, MARGIN, y + 6, lines, hf, set(), ink)
    y += 24

    # Deck: one short following line of context, small, directly below the headline.
    if deck:
        df = _f(MONT_SB, 36 if is_story else 33)
        for ln in _wrap(d, deck, df, cw)[: (3 if is_story else 2)]:
            d.text((MARGIN, y), ln, font=df, fill=mute)
            y += 46

    # Footer wordmark, typeset small once (house convention), bottom-left.
    fy = (h - STORY_SAFE_BOT) if is_story else (h - MARGIN - 20)
    ex = _tracked(d, (MARGIN, fy), "LASSO", _f(ANTON, 34), ink, 8)
    d.text((ex + 14, fy + 8), "GYM MARKETING MADE SIMPLE", font=_f(OSWALD, 20), fill=mute)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path)
    return out_path


# A short in-process cache so repeated /social page loads do not re-render + re-host the
# same card. Keyed by the post's IDENTITY (id, caption, pillar, format): a changed
# caption / pillar renders a fresh card, an unchanged one reuses the last hosted url.
# Process-local + tiny; it holds only public urls (never secrets) and never persists.
_URL_CACHE = {}
_URL_CACHE_MAX = 512


def _cache_key(post, is_story):
    return (
        str(post.get("id") or ""),
        _clean(post.get("caption")),
        _clean(post.get("pillar")),
        "story" if is_story else "feed",
    )


def _tenant_for(post, tenant):
    """The hosting tenant (media_host scopes every key to it, so one gym's fallback
    card never collides with another's). Prefer an explicit tenant, else the post's
    account_key, else a stable shared bucket segment."""
    return str(tenant or post.get("account_key") or "no_creative").strip() or "no_creative"


def display_image_for(post, *, out_dir=None, renderer=None, host=None, tenant=None):
    """Decide a calendar card's DISPLAY image, degrading a missing creative to a clean
    website-style infographic built from the post's OWN approved text and HOSTED so the
    client portal (a service with no access to the worker's local disk) can display it.

    `post` is a mapping carrying (any of): id, caption, pillar, format ('feed' |
    'story'), image_url (or image_public_url), account_key. Returns:

      - the existing image_url when it is present and usable  (no render, unchanged)
      - a PUBLIC hosted url for an infographic rendered from caption / pillar, when the
        flag is ON, the image is absent, AND there is approved text to render
      - None when: the flag is OFF (fallback disabled, current behavior); there is no
        caption / pillar text to render; hosting is off or returns nothing; or the
        render / font step fails. In every None case the caller keeps its existing
        empty-state (a clean blank beats a blank card, a fabricated photo, or an
        unshowable local path).

    NO FABRICATION: the infographic text comes only from the post's approved caption /
    pillar. Nothing here publishes and no gate is weakened.

    Injection seams for tests: `renderer` (defaults to the house PIL renderer above),
    `host` (defaults to agent.media_host.host_media, the same content-addressed hosting
    primitive summit_rebuild uses), `tenant` (hosting scope), `out_dir` (temp render dir).
    """
    image_url = post.get("image_url")
    if image_url is None:
        image_url = post.get("image_public_url")

    # An existing, usable image is returned unchanged: no render, no host, ever.
    if _has_usable_image(image_url):
        return image_url

    # Flag OFF -> no fallback. Behavior is unchanged: the caller keeps its current
    # empty-state handling for a missing image.
    if not config.no_creative_fallback_enabled():
        return None

    eyebrow, headline, deck = _approved_text(post.get("caption"), post.get("pillar"))
    # No approved text -> block, never a blank card and never invented copy.
    if not headline:
        return None

    is_story = str(post.get("format") or "feed").strip().lower() == "story"

    # Repeated page loads of the same unchanged post reuse the last hosted url instead
    # of re-rendering + re-hosting blindly.
    ck = _cache_key(post, is_story)
    cached = _URL_CACHE.get(ck)
    if cached:
        return cached

    render = renderer or _render_infographic
    host_media = host or _default_host

    slug = re.sub(r"[^a-z0-9]+", "_", headline.lower()).strip("_")[:60] or "infographic"
    suffix = "story" if is_story else "feed"
    filename = f"no_creative_{slug}_{suffix}.png"

    # Render locally (to out_dir when given, else a throwaway temp dir), then host and
    # return the PUBLIC url. A render / font failure NEVER raises into the caller (a web
    # request): it degrades to None (empty state). The local file is only an input to
    # hosting; the portal is served the hosted url, never a local path.
    tmp = None
    try:
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, filename)
        else:
            tmp = tempfile.mkdtemp(prefix="echo_no_creative_")
            out_path = os.path.join(tmp, filename)
        try:
            rendered = render(eyebrow, headline, deck, out_path, is_story=is_story)
        except Exception:
            # A missing font or any PIL error must never 500 a portal page.
            return None
        if not rendered:
            return None

        hosted = host_media(rendered, _tenant_for(post, tenant))
        # Hosting off / no creds / upload failed -> None (empty state), never a local
        # path the portal cannot show.
        if not hosted:
            return None

        if len(_URL_CACHE) >= _URL_CACHE_MAX:
            _URL_CACHE.clear()  # tiny, simple bound: drop the whole cache when full
        _URL_CACHE[ck] = hosted
        return hosted
    finally:
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _default_host(local_path, tenant):
    """Default hosting seam: media_host.host_media (content-addressed R2 upload). When
    hosting is off (config.hosting_enabled() False) or no creds are present, host_media
    returns None on its own, so display_image_for degrades to the empty state."""
    from . import media_host
    return media_host.host_media(local_path, tenant)
