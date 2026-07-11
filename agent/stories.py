"""
Stories: one 9:16 Story draft per account per day, alongside the feed post.

FULLY DORMANT by default behind AGENT_STORIES_ENABLED: with the flag OFF this
module generates NO Story drafts at all. Armed, it drafts one Story per active
account per posting day, PENDING and held for human approval through the same
Slack card flow as every other draft, clearly labeled STORY so it can never be
confused with a feed post.

NO FABRICATION: a Story only ever reuses the day's approved feed creative. When
that creative is a daily-studio infographic (its approved hook + body lines ride
on the feed draft's source_fragments), Echo requests a purpose-built 9:16 variant
from creative_studio using the SAME approved text; aspect is per-use, so the feed
target stays 4:5. Otherwise (library asset, or generation/hosting unavailable)
the Story reuses the feed image as-is; Meta letterboxes a non-9:16 image in a
Story. Stories carry no caption text.

Publishing is unaffected here: this module never posts. A Story publish goes
through meta_publisher, which requires BOTH the approval gate + publish flag AND
AGENT_STORIES_ENABLED before any network call.
"""

import os
import re

from . import config, creative_studio, media_host, ops_alerts, schedule
from .drafter import Draft, DraftStatus, _make_id


def _story_out_path(headline):
    """A Story-specific output path so the 9:16 render never overwrites the day's
    4:5 feed image (both slug from the same approved headline)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (headline or "story").lower()).strip("_") or "story"
    return os.path.join(config.LIBRARY_PATH, f"nano_story_{slug}.png")


def _is_studio_creative(feed_draft):
    """True when the feed creative is a daily-studio/nano render (its approved
    headline + facts are on source_fragments, so a 9:16 re-render stays honest)."""
    base = os.path.basename(getattr(feed_draft, "creative_path", "") or "")
    return base.startswith("nano_") and bool(feed_draft.source_fragments)


def build_story_draft(account, day_key, *, feed_draft=None,
                      nano_client=None, s3_client=None):
    """
    Build one PENDING Story draft for `account` from the day's feed draft. Returns
    None (fully dormant, no draft at all) when:
      - AGENT_STORIES_ENABLED is OFF (the default), or
      - the schedule says this day does not post, or
      - there is no PENDING feed draft to reuse a creative from (a Story never
        invents its own creative).
    """
    if not config.stories_enabled():
        return None
    if not schedule.should_post_on(day_key):
        return None
    if feed_draft is None or feed_draft.status != DraftStatus.PENDING:
        return None
    if not (feed_draft.creative_public_url or feed_draft.creative_path):
        return None  # nothing approved to reuse; a Story never fabricates a creative

    draft_id = _make_id(account.key, "story", day_key)
    creative_path = feed_draft.creative_path
    creative_public_url = feed_draft.creative_public_url
    fragments = list(feed_draft.source_fragments or [])

    # PREMADE story variant first (AGENT_STORY_PREMADE_ENABLED, OFF): a *_story
    # render next to the day's creative (the regen-library convention) is used
    # as-is, nothing generated. Flag OFF = behavior byte-identical to today.
    if config.story_premade_enabled():
        premade = _premade_story_variant(feed_draft)
        if premade is not None:
            hosted = media_host.host_media(premade, account.key, client=s3_client)
            if hosted:
                creative_path, creative_public_url = premade, hosted
                return _story_draft(account, day_key, draft_id, feed_draft,
                                    creative_path, creative_public_url, fragments)

    # Purpose-built 9:16 variant from the SAME approved text, when available. Aspect
    # is passed per-use so the feed's 4:5 target is untouched. Any unavailable step
    # (flags off, no key, hosting down) falls back to reusing the feed image as-is.
    if _is_studio_creative(feed_draft):
        headline, facts = fragments[0], fragments[1:]
        if facts:
            art = creative_studio.generate(
                headline, facts, client=nano_client,
                account_key=account.key,
                out_path=_story_out_path(headline),
                aspect=config.STORY_ASPECT, pixels=config.STORY_PIXELS,
                surface="Story",
            )
            if art:
                hosted = media_host.host_media(art["path"], account.key,
                                               client=s3_client)
                if hosted:
                    creative_path, creative_public_url = art["path"], hosted
            else:
                ops_alerts.alert(
                    f"story 9:16 render returned nothing for {account.key} "
                    f"(studio dark or Gemini unavailable); reusing feed image."
                )

    return _story_draft(account, day_key, draft_id, feed_draft,
                        creative_path, creative_public_url, fragments)


def _premade_story_variant(feed_draft):
    """A *_story render next to the day's creative (regen-library convention),
    or None. Only ever a sibling of the APPROVED creative, never a new asset."""
    import os as _os
    path = feed_draft.creative_path or ""
    if not path:
        return None
    stem, ext = _os.path.splitext(path)
    for cand_ext in dict.fromkeys([ext, ".png", ".jpg", ".webp"]):
        cand = f"{stem}_story{cand_ext}"
        if cand_ext and _os.path.exists(cand):
            return cand
    return None


def _story_draft(account, day_key, draft_id, feed_draft, creative_path,
                 creative_public_url, fragments):
    return Draft(
        draft_id=draft_id, account_key=account.key, platform=account.platform,
        # Stories carry minimal or no caption; Echo ships none and never invents one.
        caption="", hashtags=[],
        creative_path=creative_path, creative_public_url=creative_public_url,
        # Morning slot from the schedule module, so the Story and the evening feed
        # post land at different times of the same posting day.
        scheduled_for=schedule.scheduled_for(day_key, slot="morning"),
        status=DraftStatus.PENDING,
        source_fragments=fragments,  # the same approved text the feed creative used
        is_story=True,
    )
