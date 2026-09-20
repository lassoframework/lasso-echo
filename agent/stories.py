"""
Stories: one 9:16 Story draft per account per day, alongside the feed post.

FULLY DORMANT by default behind AGENT_STORIES_ENABLED: with the flag OFF this
module generates NO Story drafts at all. Armed, it drafts one Story per active
account per posting day, PENDING and held for human approval through the same
Slack card flow as every other draft, clearly labeled STORY so it can never be
confused with a feed post.

NO FABRICATION, NO CROPPED FEED CARDS: a Story is only ever built from a GENUINE
9:16 asset. Either a premade *_story sibling next to the day's approved creative,
or a purpose-built 9:16 variant that creative_studio renders from the SAME
approved text (its hook + body lines ride on the feed draft's source_fragments);
aspect is per-use, so the feed target stays 4:5. If the reviewed LASSO studio lane
cannot produce that asset, Echo retains a needs-media Story draft for recovery.
Other creatives without a genuine 9:16 source are skipped. It NEVER reuses or
crops the day's 4:5 / 1:1 feed image into a Story frame.
Stories carry no caption text.

Publishing is unaffected here: this module never posts. A Story publish goes
through meta_publisher, which requires BOTH the approval gate + publish flag AND
AGENT_STORIES_ENABLED before any network call.
"""

import os
import re

from . import config, creative_studio, media_host, ops_alerts, schedule
from .drafter import Draft, DraftStatus, _make_id


def _story_out_path(headline, unique=False):
    """A Story-specific output path so the 9:16 render never overwrites the day's
    4:5 feed image (both slug from the same approved headline)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (headline or "story").lower()).strip("_") or "story"
    if unique:
        import uuid
        slug += "_" + uuid.uuid4().hex
    return os.path.join(config.LIBRARY_PATH, f"nano_story_{slug}.png")


def _is_studio_creative(feed_draft):
    """True when the feed creative is a daily-studio/nano render (its approved
    headline + facts are on source_fragments, so a 9:16 re-render stays honest)."""
    base = os.path.basename(getattr(feed_draft, "creative_path", "") or "")
    return base.startswith("nano_") and bool(feed_draft.source_fragments)


def build_story_draft(account, day_key, *, feed_draft=None,
                      nano_client=None, s3_client=None):
    """
    Build one PENDING Story draft for `account` from the day's feed draft. A Story
    is ONLY ever built from a genuine 9:16 asset (a premade *_story sibling, or a
    purpose-built 9:16 studio render). It NEVER reuses or crops the feed image.

    Returns None (no Story at all) when:
      - AGENT_STORIES_ENABLED is OFF (the default), or
      - the schedule says this day does not post, or
      - there is no PENDING feed draft to anchor the day's approved text/creative, or
      - no genuine 9:16 asset is available (skipped, with one ops alert fired).
    """
    if not config.stories_enabled():
        return None
    if not schedule.should_post_on(day_key):
        return None
    if feed_draft is None or feed_draft.status != DraftStatus.PENDING:
        return None
    if not (feed_draft.creative_public_url or feed_draft.creative_path):
        return None  # nothing approved to anchor to; a Story never fabricates a creative

    draft_id = _make_id(account.key, "story", day_key)
    fragments = list(feed_draft.source_fragments or [])

    # PREMADE story variant first (AGENT_STORY_PREMADE_ENABLED, OFF): a *_story
    # render next to the day's creative (the regen-library convention) is used
    # as-is, nothing generated. This is a genuine 9:16 asset, not a reused feed card.
    if config.story_premade_enabled() and not config.lasso_infographic_quality_enabled(account.key):
        premade = _premade_story_variant(feed_draft)
        if premade is not None:
            hosted = media_host.host_media(premade, account.key, client=s3_client)
            if hosted:
                return _story_draft(account, day_key, draft_id, feed_draft,
                                    premade, hosted, fragments)
            # Genuine 9:16 asset exists but could not be hosted: skip, do not reuse.
            ops_alerts.alert(
                f"story draft skipped for {account.key} on {day_key}: found premade "
                f"9:16 variant {os.path.basename(premade)} but hosting returned no "
                f"public URL. Enable AGENT_HOSTING_ENABLED or add public_url."
            )
            return None

    # Purpose-built 9:16 variant from the SAME approved text. Aspect is passed
    # per-use so the feed's 4:5 target is untouched. Only a daily-studio creative
    # carries the approved headline + facts on source_fragments to re-render safely.
    if (_is_studio_creative(feed_draft) or
            (config.lasso_infographic_quality_enabled(account.key) and
             getattr(feed_draft, "infographic_copy", None))):
        headline, facts = (fragments[0] if fragments else ""), fragments[1:]
        image_copy = getattr(feed_draft, "infographic_copy", {}) or {}
        if config.lasso_infographic_quality_enabled(account.key) and image_copy:
            headline, facts = image_copy["headline"], image_copy["facts"]
        copy_opts = ({"cta": image_copy.get("cta", ""), "footer": image_copy.get("footer")}
                     if image_copy else {})
        if facts:
            art = creative_studio.generate(
                headline, facts, client=nano_client,
                account_key=account.key,
                out_path=_story_out_path(headline, unique=config.lasso_infographic_quality_enabled(account.key)),
                aspect=config.STORY_ASPECT, pixels=config.STORY_PIXELS,
                surface="Story", **copy_opts,
            )
            if art:
                hosted = media_host.host_media(art["path"], account.key,
                                               client=s3_client)
                if hosted:
                    return _story_draft(account, day_key, draft_id, feed_draft,
                                        art["path"], hosted, fragments,
                                        image_engine=art.get("route", ""))
            elif config.lasso_infographic_quality_enabled(account.key) and image_copy:
                # A reviewed LASSO Story must never bypass the pixel-quality gate with
                # an unreviewed local render. Preserve the planned slot as needs-media
                # instead, so it remains recoverable and cannot auto-publish.
                reason = (
                    "Story Studio could not produce a reviewed 9:16 asset. "
                    "This Story is held for purpose-built media; the feed card was not "
                    "cropped or reused.")
                ops_alerts.alert(
                    f"story draft held for {account.key} on {day_key}: {reason}")
                return Draft(
                    draft_id=draft_id, account_key=account.key,
                    platform=account.platform, caption="", hashtags=[],
                    creative_path="", creative_public_url="",
                    scheduled_for=schedule.scheduled_for(day_key, slot="morning"),
                    status=DraftStatus.PENDING, blocked_reason=reason,
                    source_fragments=fragments, infographic_copy=dict(image_copy),
                    is_story=True, day_key=day_key, draft_type="story",
                    needs_media=True, force_approval=True,
                    warnings=[reason])

    # No genuine 9:16 asset available: SKIP the Story for the day. Never reuse or
    # crop the day's feed image into a Story frame.
    #
    # TWO cases, only ONE is an incident (Blake 2026-08-27, the podcast-video launch
    # stormed Slack with per-day story-skips):
    #   * a STUDIO creative that COULD render a 9:16 but the studio came back dark /
    #     the render failed -> a real "studio is down" incident, KEEP the ops alert.
    #   * a VIDEO clip / audiogram / b2b concept card / plain library image that has
    #     no 9:16 sibling BY DESIGN -> normal. The feed still posts; the story is
    #     supplementary. LOG only, never Slack (this was the entire storm).
    _basename = os.path.basename(feed_draft.creative_path or "(no path)")
    if _is_studio_creative(feed_draft):
        ops_alerts.alert(
            f"story draft skipped for {account.key} on {day_key}: the studio render "
            f"came back dark for {_basename} (no purpose-built 9:16 studio asset and "
            f"no premade *_story sibling). A Story is never a cropped feed card."
        )
    else:
        print(f"[stories] skip {account.key} {day_key}: no 9:16 sibling for "
              f"{_basename} (feed still posts; story is supplementary, by design)")
    return None


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
                 creative_public_url, fragments, image_engine=""):
    # Task #28 (§5c): stamp the story's RAW hosted media so content_calendar carries
    # source_media_url and an edited story caption RE-BURNS (portal save + the
    # publish-lane self-heal) instead of shipping the old text. Gated by
    # AGENT_STORY_SOURCE_MEDIA inside the helper; a no-op while it is off.
    from . import story_reburn
    return story_reburn.stamp_source_media(Draft(
        draft_id=draft_id, account_key=account.key, platform=account.platform,
        # Stories carry minimal or no caption; Echo ships none and never invents one.
        caption="", hashtags=[],
        creative_path=creative_path, creative_public_url=creative_public_url,
        # Morning slot from the schedule module, so the Story and the evening feed
        # post land at different times of the same posting day.
        scheduled_for=schedule.scheduled_for(day_key, slot="morning"),
        status=DraftStatus.PENDING,
        infographic_copy=dict(getattr(feed_draft, "infographic_copy", {}) or {}),
        source_fragments=fragments,  # the same approved text the feed creative used
        is_story=True,
        image_engine=image_engine,
    ))
