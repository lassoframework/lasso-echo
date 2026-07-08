"""
Drafter: turns a voice doc + one creative into one post draft.

NO FABRICATION. The default caption generator only recombines text the human
already approved: the brand voice doc and the client-provided note on the
creative. It never invents an offer, a price, a claim, or a fact.

The generator is pluggable. A future LLM generator can slot in here, but it MUST
be constrained to the voice doc + client note and stay inside the same contract.
"""

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from . import config
from . import content_planner
from . import media_host
from . import ops_alerts
from .accounts import Platform
from .voice import load_voice


class DraftStatus(Enum):
    PENDING = "pending"      # waiting for Blake
    BLOCKED = "blocked"      # cannot draft (e.g. no voice doc)
    APPROVED = "approved"
    SKIPPED = "skipped"
    # Replaced by a newer draft for the same account + day + type (idempotent
    # re-run whose content changed). Its card is edited to a superseded state and
    # approving it does nothing (see approvals.handle_action).
    SUPERSEDED = "superseded"
    # A PENDING draft whose posting day has passed (idempotent flag ON only): the
    # next daily run flips it here, edits its card to an expired state, and
    # approving it does nothing (see approvals.handle_action).
    EXPIRED = "expired"


@dataclass
class Draft:
    draft_id: str
    account_key: str
    platform: str
    caption: str
    hashtags: list
    creative_path: str
    creative_public_url: str
    scheduled_for: str
    status: DraftStatus = DraftStatus.PENDING
    blocked_reason: str = ""
    # source spans we composed FROM, kept for the no-fabrication test + audit
    source_fragments: list = field(default_factory=list)
    # carousel support: local slide paths + their public URLs (empty for singles)
    slides: list = field(default_factory=list)
    slide_urls: list = field(default_factory=list)
    # Google Business Profile only: structured CTA button + post topic (Meta paths ignore).
    cta_type: str = ""
    cta_url: str = ""
    topic_type: str = "STANDARD"
    # Stories: True for a 9:16 Story draft so the card and the publisher can never
    # confuse it with a feed post. Feed drafts leave this False.
    is_story: bool = False
    # Idempotent daily drafts (flag AGENT_IDEMPOTENT_DRAFTS_ENABLED, default OFF):
    # the (account, day, type) identity of the draft plus the Slack message that
    # carries its card, so a re-run can find it and a superseding run can edit the
    # old card in place. All four stay empty while the flag is OFF.
    day_key: str = ""
    draft_type: str = ""       # "feed" or "story" (empty while the flag is OFF)
    slack_channel: str = ""
    slack_ts: str = ""
    warnings: list = field(default_factory=list)  # card-time notes (e.g. OCR check)
    # Category rotation (AGENT_CATEGORY_ROTATION, default OFF). Empty while off.
    category: str = ""    # one of content_categories.CATEGORIES, or "" when not set
    sub_topic: str = ""   # platform sub-topic (ads, google, nurture, ...) or ""


def _make_id(account_key, creative_path, scheduled_for):
    h = hashlib.sha1(f"{account_key}|{creative_path}|{scheduled_for}".encode()).hexdigest()
    return h[:10]


def _stem(creative):
    """The creative filename stem, used as the stable rotation key."""
    stem = getattr(creative, "stem", None)
    if stem:
        return stem
    path = getattr(creative, "path", "") or ""
    return os.path.splitext(os.path.basename(path))[0]


def _det_index(key, n):
    """Deterministic index in [0, n) from sha1(key). Stable across re-runs."""
    if n <= 0:
        return 0
    return int(hashlib.sha1((key or "").encode()).hexdigest(), 16) % n


def _pick_cta(voice, creative):
    """
    Pick one CTA from the approved rotation in the voice doc.

    Growth-hint CTAs (save / tag / share / dm / send) are PREFERRED — they drive
    the reach signals that actually grow an account. Selection within the chosen
    pool is deterministic by sha1 of the creative filename stem, so the same card
    always gets the same CTA while different cards rotate through the list.
    Returns "" if the voice doc defines no CTAs.
    """
    if not voice.ctas:
        return ""
    growth = [c for c in voice.ctas
              if any(h in c.lower() for h in TemplateGenerator.GROWTH_CTA_HINTS)]
    pool = growth if growth else list(voice.ctas)
    return pool[_det_index(_stem(creative), len(pool))]


def _caption_has_cta(caption, voice):
    """True if the caption already ends with an approved CTA verbatim, so we
    don't append (and duplicate) one."""
    low = caption.lower()
    return any(c.lower() in low for c in voice.ctas)


def _select_hashtags(voice, creative):
    """
    Select up to HASHTAG_LIMIT (5) hashtags from the approved set in the voice
    doc. Brand-tier tags come first if present, then niche/topic tags rotated
    deterministically per creative. In 2026, 3–5 tags is the whole strategy —
    more does not help (see the bible's hashtag section).
    """
    BRAND_TAGS = {"#LASSOFramework", "#GymMarketingMadeSimple", "#LASSOPinnacle"}
    all_tags = list(voice.hashtags)

    brand = [t for t in all_tags if t in BRAND_TAGS]
    rest = [t for t in all_tags if t not in BRAND_TAGS]

    offset = _det_index(_stem(creative), max(len(rest), 1))
    rotated = rest[offset:] + rest[:offset]

    limit = TemplateGenerator.HASHTAG_LIMIT
    selected = brand + rotated[: max(0, limit - len(brand))]
    return selected[:limit]


# Facebook best practice: at most 2 hashtags, at the end of the caption (the
# composer already appends hashtags at the end, so placement is preserved).
FB_HASHTAG_LIMIT = 2


def variant_hashtags(platform, hashtags):
    """
    Per-platform hashtag selection (Task: platform variants). SELECTION ONLY, never
    new text: every returned tag is one of the approved tags passed in.

    Flag OFF (default) -> the list is returned unchanged, exactly today's behavior.
    Flag ON  -> Instagram keeps up to 5 (the existing cap); a Facebook Page keeps
    at most FB_HASHTAG_LIMIT (2).
    """
    tags = list(hashtags or [])
    if not config.platform_variants_enabled():
        return tags
    if platform == Platform.FACEBOOK_PAGE:
        return tags[:FB_HASHTAG_LIMIT]
    return tags[:TemplateGenerator.HASHTAG_LIMIT]


class TemplateGenerator:
    """
    Deterministic, zero-fabrication caption builder (the safe Stage 1 baseline).
    Caption = client's approved note + one CTA from the voice doc rotation.
    Hashtags are pulled from the approved doc, brand tier always included.

    Upgrade path: a constrained LLM generator can replace this to write a real
    hook / problem / insight / CTA caption, but it must draw ONLY from the voice
    doc + client note and keep this same contract.
    """

    HASHTAG_LIMIT = 5
    GROWTH_CTA_HINTS = ("save", "tag", "share", "dm", "send")

    def build(self, voice, creative):
        fragments = []

        # 1. Client note (the core body — verbatim, no fabrication)
        if creative.client_note:
            fragments.append(creative.client_note.strip())

        caption = "\n\n".join(fragments).strip()

        # 2. CTA from the approved rotation — appended verbatim, but ONLY if the
        #    caption doesn't already carry one.
        cta = _pick_cta(voice, creative)
        if cta and not _caption_has_cta(caption, voice):
            fragments.append(cta)
            caption = "\n\n".join(fragments).strip()

        # 3. Hashtags: brand tier first, rest rotated per creative, capped at 5.
        hashtags = _select_hashtags(voice, creative)

        return caption, hashtags, fragments


def draft_post(account, creative, scheduled_for, voice=None,
               generator=None, voice_path=None):
    """
    Build one Draft for one account. Returns a Draft.

    If the voice doc is missing -> returns a BLOCKED draft. We draft NOTHING.
    """
    if voice is None:
        voice = load_voice(voice_path or config.VOICE_DOC_PATH)

    draft_id = _make_id(account.key, getattr(creative, "path", "none"), scheduled_for)

    if voice is None:
        return Draft(
            draft_id=draft_id,
            account_key=account.key,
            platform=account.platform,
            caption="",
            hashtags=[],
            creative_path="",
            creative_public_url="",
            scheduled_for=scheduled_for,
            status=DraftStatus.BLOCKED,
            blocked_reason="Brand voice doc missing or empty. Not drafting.",
        )

    if creative is None:
        return Draft(
            draft_id=draft_id,
            account_key=account.key,
            platform=account.platform,
            caption="",
            hashtags=[],
            creative_path="",
            creative_public_url="",
            scheduled_for=scheduled_for,
            status=DraftStatus.BLOCKED,
            blocked_reason="No creative available in the library. Not drafting.",
        )

    gen = generator or TemplateGenerator()

    cta_type = cta_url = ""
    topic_type = "STANDARD"

    # Daily content brain: for a LASSO account with the brain armed, compose the
    # caption ONLY from the approved source doc (never the per-creative note, never
    # invented text). A blocked plan blocks the draft. Off / non-LASSO -> unchanged.
    plan_category = ""
    plan_sub_topic = ""
    if config.content_brain_enabled() and account.key.startswith("lasso"):
        plan = content_planner.plan_for(date.today().isoformat())
        if plan.get("blocked"):
            # Surfaced on the Slack card AND (flag ON) as one ops alert.
            ops_alerts.alert(f"content plan blocked for {account.key}: {plan['reason']}")
            return Draft(
                draft_id=draft_id,
                account_key=account.key,
                platform=account.platform,
                caption="",
                hashtags=[],
                creative_path="",
                creative_public_url="",
                scheduled_for=scheduled_for,
                status=DraftStatus.BLOCKED,
                blocked_reason="Content brain: " + plan["reason"],
            )
        if account.platform == Platform.GOOGLE_BUSINESS:
            # GBP variant: trimmed summary, NO hashtags, a structured CTA button + url.
            caption, hashtags, fragments = plan["summary"], [], plan["summary_fragments"]
            cta_type, cta_url = config.GBP_DEFAULT_CTA, config.GBP_CTA_URL
        else:
            caption, hashtags, fragments = plan["caption"], plan["hashtags"], plan["fragments"]
        plan_category = plan.get("category", "")
        plan_sub_topic = plan.get("sub_topic", "")
    else:
        caption, hashtags, fragments = gen.build(voice, creative)

    # Per-platform variant (flag OFF -> unchanged): selection only, from the same
    # approved set. FB keeps at most 2 tags; IG keeps its existing cap of 5.
    hashtags = variant_hashtags(account.platform, hashtags)

    creative_public_url = getattr(creative, "public_url", "")
    slides = list(getattr(creative, "slides", []) or [])
    slide_urls = list(getattr(creative, "slide_urls", []) or [])

    # Scale-harden: when hosting is armed, publish the local creative(s) to S3 and use
    # the hosted URLs (tenant-scoped by account). OFF, or any failure, leaves the
    # existing sidecar URLs untouched -> current behavior is unchanged.
    if config.hosting_enabled():
        hosted = media_host.host_media(creative.path, account.key)
        if hosted:
            creative_public_url = hosted
        if slides:
            hosted_slides = media_host.host_many(slides, account.key)
            if hosted_slides:
                slide_urls = hosted_slides

    return Draft(
        draft_id=draft_id,
        account_key=account.key,
        platform=account.platform,
        caption=caption,
        hashtags=hashtags,
        creative_path=creative.path,
        creative_public_url=creative_public_url,
        scheduled_for=scheduled_for,
        status=DraftStatus.PENDING,
        source_fragments=fragments,
        slides=slides,
        slide_urls=slide_urls,
        cta_type=cta_type,
        cta_url=cta_url,
        topic_type=topic_type,
        category=plan_category,
        sub_topic=plan_sub_topic,
    )
