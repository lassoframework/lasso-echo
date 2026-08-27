"""publish_guard.py — the ONE publish-boundary rail (Blake's WIRING.md spec, 2026-08-27).

Every outbound social payload passes check() immediately before the publisher
call. A violation means the post is NEVER sent: the caller reverts the row to
pending with a reject_reason and fires ONE deduped alert per (gym, code).

Rails (feed payloads):
  1. empty_caption   — visible_len(caption) == 0. visible_len counts ALPHANUMERIC
                       characters only: an emoji-only caption, '...', dashes, or
                       zero-width junk is NOT a caption.
  2. thin_caption    — a non-empty caption under the A+ floor
                       (post_quality.MIN_CAPTION_CHARS). Consolidates the Wave 5.3
                       inline thin-caption recheck so there is ONE rail implementation.
  3. copy_violation  — copy_gate.violations(caption) (banned dashes etc.).
  4. missing_mention — category in (proof, results) MUST carry at least one
                       @mention from the gym's allowlist
                       (agent.mentions.allowlisted_handles(gym_id)).
  5. multi_ask       — more than ONE distinct ask family per caption
                       (copy_gate.ASK_RE families).
  6. avatar_block    — LASSO avatar rail: post_quality.avatar_breach (HYROX,
                       competitive CrossFit, strength/serious athletes). Hard block.
Always (feed AND story):
  7. media_missing   — media_ready is False. A payload with no ready media never
                       publishes, story or not.

STORY EXEMPTION (verified 2026-08-27): the '26 empty IG captions' in Blake's
2026-08-27 audit are STORY rows — empty-body BY DESIGN (contentType='story',
the caption is burned onto the media by story_image; verified 2026-08-27
against content_calendar via late_post_id). So the caption rails (1-6) apply
to FEED payloads only; a story payload is exempt from every caption rail but
NEVER from media_ready (7).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Violation codes (stable identifiers: reject_reason + the deduped-alert kv keys
# `publish_blocked:<gym>:<code>` key off these).
EMPTY_CAPTION = "empty_caption"
THIN_CAPTION = "thin_caption"
COPY_VIOLATION = "copy_violation"
MISSING_MENTION = "missing_mention"
MULTI_ASK = "multi_ask"
AVATAR_BLOCK = "avatar_block"
MEDIA_MISSING = "media_missing"

ALL_CODES = (EMPTY_CAPTION, THIN_CAPTION, COPY_VIOLATION, MISSING_MENTION,
             MULTI_ASK, AVATAR_BLOCK, MEDIA_MISSING)

# Categories that MUST carry a proof-tag: results/proof posts without a real
# @mention are unverifiable brag copy (the '0 tags in 219 posts' class).
MENTION_REQUIRED_CATEGORIES = ("proof", "results")


def visible_len(text) -> int:
    """The number of ALPHANUMERIC characters in text. Emoji, '...', dashes,
    whitespace, and zero-width characters are not captions: a string of them
    has visible_len 0."""
    return sum(1 for ch in str(text or "") if ch.isalnum())


# Ask FAMILIES: copy_gate.ASK_RE is one alternation; two matches from the SAME
# family (e.g. 'book a call' twice) are one ask, two different families
# ('book a call' + 'DM us') are two asks -> blocked. The family of a match is
# its first keyword, normalized.
_ASK_FAMILY_HEAD = {
    "link": "link_in_bio", "book": "book", "dm": "dm", "message": "dm",
    "comment": "comment", "sign": "signup", "get": "signup", "claim": "claim",
    "reserve": "claim", "try": "try_class", "schedule": "book", "start": "start",
}


def ask_families(caption) -> list:
    """The distinct ask families this caption carries, via copy_gate.ASK_RE."""
    from . import copy_gate
    fams = []
    for m in copy_gate.ASK_RE.finditer(str(caption or "")):
        head = re.split(r"\W+", m.group(0).strip().lower(), maxsplit=1)[0]
        fam = _ASK_FAMILY_HEAD.get(head, head)
        if fam not in fams:
            fams.append(fam)
    return fams


@dataclass
class PublishPayload:
    """The outbound post exactly as the publisher will send it."""
    row_id: str
    gym_id: str
    platform: str
    caption: str
    category: str = ""
    mentions: list = field(default_factory=list)
    media_ready: bool = False
    # A story publishes empty-body by design (see module docstring); the caller
    # sets this from the row's format so the caption rails know to stand down.
    is_story: bool = False


def _normalized_mentions(payload) -> set:
    return {str(h or "").strip().lstrip("@").lower()
            for h in (payload.mentions or []) if str(h or "").strip()}


def check(payload: PublishPayload, *, handles_fn=None) -> list:
    """Every violation code this payload carries (empty list == clear to publish).

    handles_fn(gym_id) -> list of allowlisted handles; defaults to
    agent.mentions.allowlisted_handles. Injectable so tests run offline. A
    handles_fn failure fails CLOSED for proof/results (missing_mention), never
    open: an unverifiable mention is not a mention.
    """
    from . import copy_gate, post_quality

    violations = []
    if not payload.media_ready:
        violations.append(MEDIA_MISSING)

    if payload.is_story:
        # Story: caption rails stand down (empty-body by design). media_ready
        # above still applies.
        return violations

    cap = str(payload.caption or "")
    if visible_len(cap) == 0:
        violations.append(EMPTY_CAPTION)
    elif len(cap.strip()) < post_quality.MIN_CAPTION_CHARS:
        violations.append(THIN_CAPTION)

    if copy_gate.violations(cap):
        violations.append(COPY_VIOLATION)

    if (payload.category or "").strip().lower() in MENTION_REQUIRED_CATEGORIES:
        allowed = None
        try:
            if handles_fn is None:
                from .mentions import allowlisted_handles as handles_fn
            allowed = {str(h or "").strip().lstrip("@").lower()
                       for h in (handles_fn(payload.gym_id) or [])}
        except Exception:
            allowed = set()          # fail closed: unverifiable == not allowlisted
        if not (_normalized_mentions(payload) & allowed):
            violations.append(MISSING_MENTION)

    if len(ask_families(cap)) > 1:
        violations.append(MULTI_ASK)

    if post_quality.avatar_breach(cap):
        violations.append(AVATAR_BLOCK)

    return violations
