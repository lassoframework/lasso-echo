"""
Drafter: turns a voice doc + one creative into one post draft.

NO FABRICATION. The default caption generator only recombines text the human
already approved: the brand voice doc and the client-provided note on the
creative. It never invents an offer, a price, a claim, or a fact.

The generator is pluggable. A future LLM generator can slot in here, but it MUST
be constrained to the voice doc + client note and stay inside the same contract.
"""

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from . import config
from .voice import load_voice


class DraftStatus(Enum):
    PENDING = "pending"      # waiting for Blake
    BLOCKED = "blocked"      # cannot draft (e.g. no voice doc)
    APPROVED = "approved"
    SKIPPED = "skipped"


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


def _make_id(account_key, creative_path, scheduled_for):
    h = hashlib.sha1(f"{account_key}|{creative_path}|{scheduled_for}".encode()).hexdigest()
    return h[:10]


def _pick_cta(voice, creative_path):
    """
    Pick one CTA from the approved rotation in the voice doc.
    Uses a deterministic index based on the creative filename so the same card
    always gets the same CTA (stable across re-runs), but different cards rotate
    through the full list.
    Returns an empty string if no CTAs are defined in the voice doc.
    """
    if not voice.ctas:
        return ""
    # Use the creative filename as a stable rotation key
    key = creative_path or ""
    idx = hash(key) % len(voice.ctas)
    return voice.ctas[idx]


def _select_hashtags(voice, creative_path):
    """
    Select 8 to 11 hashtags from the approved set in the voice doc.
    Always includes the 3 brand-tier tags if present, then fills from the rest.
    Uses a deterministic rotation so different cards get varied niche/topic tags.
    """
    BRAND_TAGS = {"#LASSOFramework", "#GymMarketingMadeSimple", "#LASSOPinnacle"}
    all_tags = list(voice.hashtags)

    brand = [t for t in all_tags if t in BRAND_TAGS]
    rest = [t for t in all_tags if t not in BRAND_TAGS]

    # Rotate the non-brand tags deterministically per creative
    key = creative_path or ""
    offset = hash(key) % max(len(rest), 1)
    rotated = rest[offset:] + rest[:offset]

    # Fill to 11 total (3 brand + up to 8 from rest)
    selected = brand + rotated[: max(0, 11 - len(brand))]
    return selected[:11]


class TemplateGenerator:
    """
    Deterministic, zero-fabrication caption builder (the safe Stage 1 baseline).
    Caption = client's approved note + one CTA from the voice doc rotation.
    Hashtags are pulled from the approved doc, brand tier always included.

    Upgrade path: a constrained LLM generator can replace this to write a real
    hook / problem / insight / CTA caption, but it must draw ONLY from the voice
    doc + client note and keep this same contract.
    """

    def build(self, voice, creative):
        fragments = []

        # 1. Client note (the core body — verbatim, no fabrication)
        if creative.client_note:
            fragments.append(creative.client_note.strip())

        # 2. CTA from the approved rotation in the voice doc
        cta = _pick_cta(voice, getattr(creative, "path", ""))
        if cta:
            fragments.append(cta)

        caption = "\n\n".join(fragments).strip()

        # 3. Hashtags: brand tier always present, rest rotated per creative
        hashtags = _select_hashtags(voice, getattr(creative, "path", ""))

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
    caption, hashtags, fragments = gen.build(voice, creative)

    return Draft(
        draft_id=draft_id,
        account_key=account.key,
        platform=account.platform,
        caption=caption,
        hashtags=hashtags,
        creative_path=creative.path,
        creative_public_url=getattr(creative, "public_url", ""),
        scheduled_for=scheduled_for,
        status=DraftStatus.PENDING,
        source_fragments=fragments,
    )
