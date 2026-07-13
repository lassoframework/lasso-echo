"""
Client content: a client (non-LASSO) account drafts a full, varied month from its
OWN approved source docs (client_sources) paired with its uploaded library.

Behind AGENT_CLIENT_SOURCES (config.client_sources_enabled), OFF by default. When
OFF a client account behaves exactly as before (a library pick, or a blocked card
when the library is thin). When ON, this builder fills the daily slot: it spreads
across the account's categories (offer / service / testimonial / faq / about /
promo) the same way LASSO's doctrine spreads across pillars, pairs the day's fact
with an image from the account's uploaded library, and holds the draft for the tap.

Two laws are absolute here:
  1. The fabrication gate is the SOLE authority on claims. A client caption only
     ever states a fact present in THAT account's APPROVED sources (raw or its
     dash/vendor-cleaned form). A pending source never clears a claim. LASSO's
     global stats never clear a client's claim.
  2. Book and summit are LASSO-only and are never reached from here.

Thin-library grace (a caption-ready day with no image) lands in Part 4; Part 3
drafts only when the account has both an approved source for the day AND an image.
"""

import os
from datetime import date

from . import client_sources, config, media_host, rotation, schedule
from .content_categories import filter_platform_copy
from .drafter import (Draft, DraftStatus, _make_id, _pick_cta, _select_hashtags,
                      variant_hashtags)
from .library import list_creatives


def _day_ordinal(day_key):
    """A stable integer per calendar day, for deterministic category/source/image
    rotation that never drifts across re-runs."""
    return date.fromisoformat(str(day_key)[:10]).toordinal()


def category_for_day(account_key, day_key, present=None):
    """The client category this day draws from, spread evenly across the
    categories the account actually has approved content in. None when the account
    has no approved sources at all."""
    present = present if present is not None \
        else client_sources.categories_present(account_key)
    if not present:
        return None
    return present[_day_ordinal(day_key) % len(present)]


def _source_for_day(account_key, day_key, category, present):
    """One approved source in the day's category, rotated across the days this
    category comes up so the same fact does not repeat back to back."""
    items = client_sources.approved_sources(account_key, category=category)
    if not items:
        return None
    cycle = _day_ordinal(day_key) // max(1, len(present))
    return items[cycle % len(items)]


def _image_key(creative):
    return os.path.basename(creative.path)


def pick_image(account_key, day_key, library_path):
    """An image from the account's uploaded library, preferring one not served
    inside the no-repeat window; falls back to the least-recently-served image so
    a stocked library always yields something. None when there are no images."""
    imgs = [c for c in list_creatives(library_path) if c.media_type == "image"]
    excl = rotation.style_exclusions(library_path)
    imgs = [c for c in imgs if _image_key(c) not in excl]
    if not imgs:
        return None
    served = rotation.load_served().get(account_key, [])
    last_served = {}
    for e in served:                       # oldest..newest, so newest date wins
        last_served[e["key"]] = e["date"]
    window_start = rotation._days_ago(day_key, config.ROTATION_WINDOW_DAYS)
    fresh = [c for c in imgs if last_served.get(_image_key(c), "") < window_start]
    pool = fresh if fresh else imgs
    pool.sort(key=lambda c: (last_served.get(_image_key(c), ""), _image_key(c)))
    return pool[0]


def compose_caption(account, source, voice, creative_key):
    """Caption from the approved fact (dash/vendor cleaned) + one CTA from the
    account's approved voice doc. Returns (caption, hashtags). The claim content
    is unchanged by cleaning; cleaning only enforces the copy law."""
    body = filter_platform_copy(source.text).strip()
    cta = _pick_cta(voice, _CtaKey(creative_key))
    caption = body
    if cta:
        cta = filter_platform_copy(cta).strip()
        if cta and cta.lower() not in caption.lower():
            caption = (body + "\n\n" + cta).strip()
    hashtags = variant_hashtags(account.platform,
                                _select_hashtags(voice, _CtaKey(creative_key)))
    return caption, hashtags


class _CtaKey:
    """Minimal stand-in so drafter's CTA/hashtag rotation (which keys off a
    creative's stem) works for a source-driven draft."""

    def __init__(self, stem):
        self.stem = stem
        self.path = stem


def _alert_needs_media(account_key, day_key, category):
    """One ops alert per account per day when a caption is ready but no image is
    available. Deduped so a re-run never storms the channel."""
    from . import db, ops_alerts
    key = f"needs_media_alerted_{account_key}_{day_key}"
    if db.kv_get(key):
        return
    db.kv_set(key, "1")
    ops_alerts.alert(
        f"{account_key} {day_key}: caption ready ({category}) but the library has "
        "no image. Held as needs-media; add a photo to publish. Not blocked.")
    db.audit("client_needs_media", account_key,
             f"{category}: caption ready, no image", account_key, day_key)


def classify(draft):
    """The day's state for a client draft: 'ready' (caption + creative, held for
    the tap), 'needs-media' (caption ready, no image yet), or 'blocked' (nothing
    to say and nothing to show)."""
    if draft is None or draft.status == DraftStatus.BLOCKED:
        return "blocked"
    if getattr(draft, "needs_media", False):
        return "needs-media"
    return "ready"


def build_client_draft(account, day_key, voice, library_path, poster=None,
                       s3_client=None, template_fn=None):
    """
    The day's client draft, sourced from the account's approved sources + library.
    Returns None only when the client-sources flag is off, the voice doc is
    missing, or the account has no approved source for the day (the caller then
    falls back to the library pick, which blocks with a clear reason when the
    library is also empty — so a day is blocked ONLY when there is neither
    approved text nor a usable creative).

    Thin-library grace: when the account HAS an approved source for the day but
    NO image, the day is still caption-ready. If a source-backed template card can
    be produced (template_fn wired + generation armed) it fills the slot; otherwise
    the draft is held as needs-media with one ops alert. Never a hard blocked card.

    Never fabricates: the caption's fact comes verbatim from one approved source
    and is re-checked against the fabrication gate before it can ship.
    """
    if not config.client_sources_enabled():
        return None
    if voice is None:
        return None
    present = client_sources.categories_present(account.key)
    if not present:
        return None                        # no approved sources: caller falls back
    category = category_for_day(account.key, day_key, present)
    source = _source_for_day(account.key, day_key, category, present)
    if source is None:
        return None
    # Fabrication gate: the fact must be an approved claim for THIS account. It is,
    # by construction (it is an approved source), but we never skip the check.
    claims = client_sources.approved_claims(account.key)
    if not rotation.is_gate_clean(source.text, approved_claims=claims):
        return None

    scheduled_for = schedule.scheduled_for(day_key)
    fragments = [source.text, f"cite:{source.citation}"]

    image = pick_image(account.key, day_key, library_path)
    if image is not None:
        caption, hashtags = compose_caption(account, source, voice,
                                            _image_key(image))
        public_url = getattr(image, "public_url", "")
        if config.hosting_enabled():
            hosted = media_host.host_media(image.path, account.key)
            if hosted:
                public_url = hosted
        rotation.record_served(account.key, _image_key(image), category, day_key)
        return Draft(
            draft_id=_make_id(account.key, image.path, scheduled_for),
            account_key=account.key,
            platform=account.platform,
            caption=caption,
            hashtags=hashtags,
            creative_path=image.path,
            creative_public_url=public_url,
            scheduled_for=scheduled_for,
            status=DraftStatus.PENDING,
            source_fragments=fragments,
            day_key=day_key,
            category=category,
        )

    # THIN-LIBRARY GRACE: caption is ready, but there is no image.
    caption, hashtags = compose_caption(account, source, voice,
                                        f"src_{source.id}")
    # Option A: a source-backed template card, when a generator is wired + armed.
    template_url = template_fn(account, source, day_key) if template_fn else None
    if template_url:
        return Draft(
            draft_id=_make_id(account.key, f"tmpl_{source.id}", scheduled_for),
            account_key=account.key,
            platform=account.platform,
            caption=caption,
            hashtags=hashtags,
            creative_path="",
            creative_public_url=template_url,
            scheduled_for=scheduled_for,
            status=DraftStatus.PENDING,
            source_fragments=fragments + ["template_card"],
            day_key=day_key,
            category=category,
        )
    # Option B: mark the day needs-media (held, one ops alert). NOT blocked.
    _alert_needs_media(account.key, day_key, category)
    return Draft(
        draft_id=_make_id(account.key, f"needsmedia_{source.id}", scheduled_for),
        account_key=account.key,
        platform=account.platform,
        caption=caption,
        hashtags=hashtags,
        creative_path="",
        creative_public_url="",
        scheduled_for=scheduled_for,
        status=DraftStatus.PENDING,
        source_fragments=fragments,
        day_key=day_key,
        category=category,
        needs_media=True,
        warnings=["needs-media: caption ready, add an image to publish"],
    )
