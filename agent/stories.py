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
the Story reuses the feed image, but see `ensure_story_safe()` below -- Meta does
NOT letterbox a non-9:16 Story image (an earlier version of this docstring
wrongly assumed it does); it zoom-crops to fill, cutting content off the left
and right edges. Confirmed 2026-08-05 against a real published Story
(b2b_five_companies) that had its footer URL cropped. Stories carry no caption
text.

Publishing is unaffected here: this module never posts. A Story publish goes
through meta_publisher, which requires BOTH the approval gate + publish flag AND
AGENT_STORIES_ENABLED before any network call.
"""

import os
import re

from . import config, creative_studio, media_host, ops_alerts, schedule
from .drafter import Draft, DraftStatus, _make_id

STORY_TARGET_W, STORY_TARGET_H = 1080, 1920
_STORY_ASPECT_TOL = 0.01


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
    # Falls through (does not return early) so the guaranteed-9:16 safety net
    # below still checks it -- premade SHOULD already be 9:16, but that is
    # verified, not assumed.
    purpose_built = False
    if config.story_premade_enabled():
        premade = _premade_story_variant(feed_draft)
        if premade is not None:
            hosted = media_host.host_media(premade, account.key, client=s3_client)
            if hosted:
                creative_path, creative_public_url = premade, hosted
                purpose_built = True

    # Purpose-built 9:16 variant from the SAME approved text, when available. Aspect
    # is passed per-use so the feed's 4:5 target is untouched. Any unavailable step
    # (flags off, no key, hosting down) falls back to reusing the feed image as-is.
    # Skipped when the premade branch above already resolved one.
    if not purpose_built and _is_studio_creative(feed_draft):
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

    # Fallback hosting for library creatives: if the feed sidecar had no URL
    # and hosting is on, try uploading now.  Stories need a public URL; unlike
    # feed posts there is no text-only fallback at publish time.
    if not creative_public_url and creative_path:
        hosted = media_host.host_media(creative_path, account.key, client=s3_client)
        if hosted:
            creative_public_url = hosted

    # GUARANTEED 9:16: whatever creative survived the branches above (premade,
    # studio-regenerated, or the plain feed reuse), verify it is actually a
    # 1080x1920 canvas before it can reach Meta, and pad it if not. This is
    # the permanent fix -- it does not depend on any flag or on regen_library's
    # per-concept "story" switch (65 of 67 concepts ship "story": False today,
    # and AGENT_STORY_PREMADE_ENABLED defaults OFF, so this exact path was the
    # one actually being hit in production). A local file already checked
    # above (has_signal path) that is not a real 9:16 canvas gets padded onto
    # a true 1080x1920 frame and RE-hosted under its own URL, so Meta always
    # receives real 9:16 pixels and can never crop content off the edges again.
    if creative_path and os.path.isfile(creative_path):
        safe_path = ensure_story_safe(creative_path)
        if safe_path != creative_path:
            hosted = media_host.host_media(safe_path, account.key, client=s3_client)
            if hosted:
                creative_path, creative_public_url = safe_path, hosted
            else:
                ops_alerts.alert(
                    f"story for {account.key} on {day_key} is not 9:16 and could "
                    f"not be re-hosted after padding (hosting unavailable); "
                    f"publishing the original, un-padded creative -- Meta will "
                    f"crop it. Enable AGENT_HOSTING_ENABLED to close this gap."
                )

    # Hard block: a story without a public URL always raises PublishError inside
    # the approval handler silently.  Surface the failure here instead.
    if not creative_public_url:
        ops_alerts.alert(
            f"story draft blocked for {account.key} on {day_key}: "
            f"no public URL for {os.path.basename(creative_path or '(no path)')}. "
            f"Enable AGENT_HOSTING_ENABLED or add public_url to the creative sidecar."
        )
        return None

    return _story_draft(account, day_key, draft_id, feed_draft,
                        creative_path, creative_public_url, fragments)


def _is_video_path(path):
    return bool(path) and str(path).lower().endswith((".mp4", ".mov"))


def _edge_fill_color(img):
    """The average color of the image's own top row, as the pad tone -- reads
    as an intentional extension of the art rather than a jarring black bar."""
    w, _h = img.size
    row = img.crop((0, 0, w, 1)).convert("RGB")
    px = list(row.getdata())
    if not px:
        return (18, 30, 60)  # house navy fallback; never reached in practice
    n = len(px)
    return (sum(p[0] for p in px) // n, sum(p[1] for p in px) // n,
           sum(p[2] for p in px) // n)


def _story_safe_path(path):
    stem, _ext = os.path.splitext(path)
    return f"{stem}_story_safe.png"


def ensure_story_safe(path):
    """
    Guarantee a genuine 9:16 (1080x1920) canvas before a Story creative can
    reach Meta. THE permanent fix for the class of bug where a Story shows
    content cropped off its edges: Meta does not letterbox a non-9:16 Story
    image, it zoom-crops to fill.

    Deterministic and local (PIL only, no API call, no flag) so it cannot be
    silently disabled by a forgotten flag or a per-concept config default --
    both of which are exactly how this bug reached production (see the
    module docstring). Checks the actual pixels rather than trusting the
    source: an already-9:16 image (premade or studio-regenerated) passes
    through unchanged; anything else is scaled to fill the width (or height,
    for a taller-than-9:16 source) and centered on a new 1080x1920 canvas,
    padded in a color sampled from the image's own top edge.

    Returns the ORIGINAL path unchanged when there is nothing to fix (missing
    file, unreadable, a video, or already 9:16) -- never mutates the source,
    so an asset shared with the feed post is untouched either way. Otherwise
    returns a NEW file path; the caller is responsible for (re-)hosting it.
    """
    if not path or not os.path.isfile(path) or _is_video_path(path):
        return path
    from PIL import Image
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        return path
    w, h = img.size
    if w <= 0 or h <= 0:
        return path
    if abs((w / h) - (STORY_TARGET_W / STORY_TARGET_H)) < _STORY_ASPECT_TOL:
        return path
    img = img.convert("RGB")
    fill = _edge_fill_color(img)
    canvas = Image.new("RGB", (STORY_TARGET_W, STORY_TARGET_H), fill)
    fit_by_height = (w / h) < (STORY_TARGET_W / STORY_TARGET_H)
    if fit_by_height:
        # taller-than-9:16 source: fit to the full target height, pad the sides
        scale = STORY_TARGET_H / h
        new_w = max(1, round(w * scale))
        resized = img.resize((new_w, STORY_TARGET_H))
        canvas.paste(resized, ((STORY_TARGET_W - new_w) // 2, 0))
    else:
        # wider-than-9:16 (or square) source: fit to the full target width,
        # pad top and bottom -- this is the common case (a 4:5 or 1:1 feed card)
        scale = STORY_TARGET_W / w
        new_h = max(1, round(h * scale))
        resized = img.resize((STORY_TARGET_W, new_h))
        canvas.paste(resized, (0, (STORY_TARGET_H - new_h) // 2))
    out_path = _story_safe_path(path)
    canvas.save(out_path)
    return out_path


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
