"""
Story caption RE-BURN (Task #28 / Dale §5c). A story publishes with an empty body, so its
caption lives only on the burned media. When a client edits a story caption in the portal,
`patch_caption` updates content_calendar.caption but the already-hosted image_url still
carries the OLD (or no) caption. If the row kept its RAW source media url
(content_calendar.source_media_url, written at plan time when AGENT_STORY_SOURCE_MEDIA is
on), we re-burn the NEW caption onto fresh media right away and swap image_url — instead of
waiting for the monthly rebuild.

Best-effort by contract: the caption edit is ALREADY persisted before this runs, so a
re-burn failure NEVER fails the edit (the monthly rebuild remains the backstop). Fully
gated: no-op unless BOTH AGENT_STORY_SOURCE_MEDIA and AGENT_STORY_FORMAT are on and the row
is a story carrying a source_media_url.
"""

import os
import tempfile

from . import config

_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")


def should_reburn(row):
    """True when a story row is eligible for an immediate caption re-burn: both flags on,
    format 'story', and a stored raw source_media_url to burn from."""
    if not (config.story_source_media_enabled() and config.story_format_enabled()):
        return False
    if str((row or {}).get("format") or "").lower() != "story":
        return False
    return bool((row or {}).get("source_media_url"))


def _download(url, logger):
    """Download url to a temp file, returning its path (caller deletes) or None. The
    extension is taken from the url path so the burn picks the photo vs video branch."""
    try:
        import requests
        ext = os.path.splitext(url.split("?")[0])[1].lower() or ".jpg"
        r = requests.get(url, timeout=30)
        if r.status_code >= 400 or not r.content:
            return None
        fd, path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(r.content)
        return path
    except Exception as exc:  # noqa: BLE001
        logger(f"story re-burn: source download failed ({type(exc).__name__})")
        return None


def reburn(source_media_url, caption, gym_name, tenant, *, logger=None):
    """Burn `caption` onto fresh media from source_media_url and host it. Returns the new
    hosted url, or None on any failure (the caller keeps the old media — the edit already
    saved; the rebuild is the backstop). Never raises."""
    log = logger or (lambda m: print(f"[story-reburn] {m}"))
    if not (source_media_url and caption):
        return None
    if not config.hosting_enabled():
        log("hosting off; cannot host the re-burned story (rebuild will catch it)")
        return None
    src = _download(source_media_url, log)
    if not src:
        return None
    try:
        from . import story_image, media_host
        is_video = src.lower().endswith(_VIDEO_EXTS)
        lib = os.path.dirname(src)
        if is_video:
            asset = story_image.get_or_make_story_video(src, caption, gym_name, lib,
                                                        logger=log)
        else:
            asset = story_image.get_or_make_story_image(src, caption, gym_name, lib,
                                                        logger=log)
        if not asset:
            return None
        return media_host.host_media(asset, tenant)
    except Exception as exc:  # noqa: BLE001 - a re-burn must never fail the saved edit
        log(f"story re-burn failed ({type(exc).__name__})")
        return None
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
