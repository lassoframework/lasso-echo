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
import hashlib
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import config
from .media_types import VIDEO_EXTS as _VIDEO_EXTS   # ONE definition (audit D1)


def should_reburn(row):
    """True when a story row is eligible for an immediate caption re-burn: both flags on,
    format 'story', and a stored raw source_media_url to burn from."""
    if not (config.story_source_media_enabled() and config.story_format_enabled()):
        return False
    if str((row or {}).get("format") or "").lower() != "story":
        return False
    return bool((row or {}).get("source_media_url"))


def stamp_source_media(draft):
    """Record on a STORY draft the RAW hosted media its caption is (or would be) burned
    onto, so `_real_row` persists content_calendar.source_media_url and a later caption
    edit can RE-BURN immediately (portal save + the publish-lane self-heal).

    Same convention as the client lane (client_month_run._maybe_format_story): the value
    is the story's own PRE-BURN hosted url — never the paired feed card, which may be the
    square autofit and would come back cropped. In LASSO's own lanes (the nano 9:16 story
    render, the summit sprint's paired *_story render) the hosted render IS that raw
    media, so source_media_url == the story's creative_public_url at stamp time.

    Same gate as the client lane too: a no-op unless AGENT_STORY_SOURCE_MEDIA is on (the
    column exists), so a pre-migration insert never carries an unknown column. Returns the
    draft for call-site chaining; never raises."""
    try:
        if draft is None or not config.story_source_media_enabled():
            return draft
        if not getattr(draft, "is_story", False):
            return draft
        src = (getattr(draft, "source_media_url", "") or "").strip()
        if not src:
            src = (getattr(draft, "creative_public_url", "") or "").strip()
        if src:
            draft.source_media_url = src
    except Exception:  # noqa: BLE001 - a metadata stamp must never sink a build
        pass
    return draft


def _download(url, logger):
    """Read only a bounded object from our configured media bucket.

    Return a temp path for the burn; the caller removes it. External and
    redirected URLs are ineligible for an automatic re-burn.
    """
    path = None
    try:
        from . import visual_writer_prepare
        if not visual_writer_prepare._own_media_url(url):
            logger("story re-burn: source URL is outside the configured media bucket")
            return None
        data = visual_writer_prepare._bytes_for_url(url)
        if not data or len(data) > visual_writer_prepare.MAX_VISUAL_BYTES:
            return None
        ext = os.path.splitext(urlsplit(url).path)[1].lower() or ".jpg"
        fd, path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path
    except Exception as exc:  # noqa: BLE001
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        logger(f"story re-burn: source download failed ({type(exc).__name__})")
        return None


@dataclass(frozen=True)
class ReburnEvidence:
    """Observed input/render/output bytes; an owner DB receipt is still required."""

    source_exact_url: str
    delivered_exact_url: str
    source_fingerprint: str
    delivered_fingerprint: str
    source_byte_length: int
    delivered_byte_length: int
    operation: str = "reburn"
    evidence_ref: str = ""
    observed_by: str = "story_reburn"
    rendered_by: str = "story_reburn"

    def as_dict(self):
        return vars(self).copy()


def _reburn(source_media_url, caption, gym_name, tenant, *, logger=None, evidence=False):
    """Burn `caption` onto fresh media from source_media_url and host it. Returns the new
    hosted url, or None on any failure. Evidence mode returns a URL and byte
    observations only after reading both stored objects and checking the
    rendered file against the hosted output. Never raises."""
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
        if evidence:
            from . import visual_writer_prepare
            if os.path.getsize(asset) > visual_writer_prepare.MAX_VISUAL_BYTES:
                return None
            with open(src, "rb") as fh:
                source_bytes = fh.read()
            with open(asset, "rb") as fh:
                rendered_bytes = fh.read()
            if (not source_bytes or not rendered_bytes or
                    visual_writer_prepare._bytes_for_url(source_media_url) != source_bytes):
                log("story re-burn: source object readback did not match render input")
                return None
        url = media_host.host_media(asset, tenant)
        if not url or not evidence:
            return url
        delivered_bytes = visual_writer_prepare._bytes_for_url(url)
        if delivered_bytes != rendered_bytes:
            log("story re-burn: hosted object readback did not match render output")
            return None
        return url, ReburnEvidence(
            source_exact_url=source_media_url, delivered_exact_url=url,
            source_fingerprint="md5:" + hashlib.md5(source_bytes).hexdigest(),
            delivered_fingerprint="md5:" + hashlib.md5(delivered_bytes).hexdigest(),
            source_byte_length=len(source_bytes),
            delivered_byte_length=len(delivered_bytes),
            evidence_ref="story_reburn:" + str(uuid.uuid4()),
        )
    except Exception as exc:  # noqa: BLE001 - a re-burn must never fail the saved edit
        log(f"story re-burn failed ({type(exc).__name__})")
        return None
    finally:
        try:
            os.remove(src)
        except OSError:
            pass


def reburn(source_media_url, caption, gym_name, tenant, *, logger=None):
    """Legacy best-effort URL contract used while the writer flag is off."""
    return _reburn(source_media_url, caption, gym_name, tenant, logger=logger)


def reburn_with_evidence(source_media_url, caption, gym_name, tenant, *, logger=None):
    """Return (URL, ReburnEvidence) only for verified input and hosted bytes."""
    return _reburn(source_media_url, caption, gym_name, tenant,
                   logger=logger, evidence=True)
