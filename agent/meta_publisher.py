"""
Meta publisher.

THE DRAFT-ONLY GUARD LIVES HERE, belt-and-suspenders with the approval gate.
If publish_enabled() is False, publish() makes NO network call and returns a
WouldPublish result. Real Meta writes only happen after Blake arms the flag.

Honest limits (documented in AGENT_README.md):
  - Instagram: requires an IG BUSINESS/CREATOR account linked to a Page, and the
    creative must be reachable at a PUBLIC URL (image_url/video_url). Local files
    must be hosted first. Two-step: create container -> publish container.
  - Facebook Page: supported (photo/feed).
  - Personal FB profile: the Graph API CANNOT publish to a personal timeline
    (publish_actions was removed in 2018). publish() raises NotSupported for it.
    In draft-only mode this never triggers; it only matters once publish is armed.
"""

import time
from dataclasses import dataclass

from . import config
from .accounts import Platform


def _is_video(url):
    """True if the path/URL ends in a video extension (.mp4/.mov), case-insensitive.

    Accepts None/empty and returns False. Used to route a video creative to the
    Reels flow (and to label it in the Slack card)."""
    return bool(url) and str(url).lower().endswith((".mp4", ".mov"))


class PublishError(Exception):
    pass


class MediaNotReady(PublishError):
    """The media container never reached FINISHED (or it reported ERROR) inside
    the poll window, so we did NOT publish. This is a KNOWN, RETRYABLE condition
    (Meta processes the container asynchronously): the post did not go out and
    the card should be HELD for a retry, not alarmed as a hard publish failure.
    Subclass of PublishError so anything catching PublishError still catches it."""
    pass


class NotSupported(PublishError):
    pass


class MissingToken(PublishError):
    pass


@dataclass
class PublishResult:
    ok: bool
    mode: str          # "published" or "would_publish"
    media_id: str = ""
    detail: str = ""


# requests is imported lazily so draft-only mode has zero network dependency
def _requests():
    import requests
    return requests


def publish(draft, account, http=None):
    """
    Publish a draft to the right Meta surface. Returns a PublishResult.

    Draft-only short-circuit: if publishing is not armed, we do NOT touch Meta.
    """
    if not config.publish_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="publish flag OFF (draft-only)")

    # Stories sit behind BOTH gates: even with publishing armed, a Story draft makes
    # NO network call until AGENT_STORIES_ENABLED is also armed.
    if getattr(draft, "is_story", False) and not config.stories_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="stories flag OFF (draft only)")

    token = account.get_token()
    if not token:
        raise MissingToken(f"No token set for account '{account.key}'.")

    full_caption = _compose_caption(draft)
    client = http or _requests()

    if getattr(draft, "is_story", False):
        if account.platform == Platform.INSTAGRAM:
            return _publish_instagram_story(client, account, draft, token)
        if account.platform == Platform.FACEBOOK_PAGE:
            return _publish_fb_page_story(client, account, draft, token)
        raise NotSupported(f"Stories are not supported on platform: {account.platform}")

    if account.platform == Platform.INSTAGRAM:
        return _publish_instagram(client, account, draft, full_caption, token)
    if account.platform == Platform.FACEBOOK_PAGE:
        return _publish_fb_page(client, account, draft, full_caption, token)
    if account.platform == Platform.PERSONAL:
        raise NotSupported(
            "Graph API cannot publish to a personal Facebook profile. "
            "Use a Page or an IG Business/Creator account. See AGENT_README.md."
        )
    raise NotSupported(f"Unknown platform: {account.platform}")


def _compose_caption(draft):
    tags = (" " + " ".join(draft.hashtags)) if draft.hashtags else ""
    return (draft.caption + ("\n\n" + " ".join(draft.hashtags) if draft.hashtags else "")).strip()


def _publish_instagram(client, account, draft, caption, token):
    ig_id = account.get_target_id()
    if not ig_id:
        raise PublishError(f"No IG user id for '{account.key}'.")
    # Carousel: 2+ public slide URLs -> multi-child container flow.
    if len(getattr(draft, "slide_urls", []) or []) >= 2:
        return _publish_instagram_carousel(client, ig_id, draft, caption, token)
    # Reel: a video creative -> REELS container flow (dormant in draft-only).
    if _is_video(draft.creative_public_url) or _is_video(draft.creative_path):
        return _publish_instagram_reel(client, account, draft, caption, token, ig_id)
    if not draft.creative_public_url:
        raise PublishError(
            "Instagram needs a PUBLIC media URL. This creative has none. "
            "Host it and set public_url in its sidecar. See AGENT_README.md."
        )
    base = config.GRAPH_API_BASE
    media_param = "video_url" if draft.platform and draft.creative_public_url.lower().endswith((".mp4", ".mov")) else "image_url"
    # step 1: create container
    r1 = client.post(
        f"{base}/{ig_id}/media",
        data={media_param: draft.creative_public_url, "caption": caption, "access_token": token},
        timeout=30,
    )
    _raise_for_status(r1)
    container_id = r1.json().get("id")
    # step 2: the container is processed asynchronously; wait for FINISHED before
    # publishing. Skipping this is what caused subcode 2207027 "The media is not
    # ready for publishing" — we called media_publish before Meta finished.
    _await_container_ready(client, base, container_id, token, label="media",
                           max_tries=IMG_POLL_MAX_TRIES,
                           interval=IMG_POLL_INTERVAL_SEC)
    # step 3: publish the processed container
    r2 = client.post(
        f"{base}/{ig_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    _raise_for_status(r2)
    return PublishResult(ok=True, mode="published", media_id=r2.json().get("id", ""))


def _publish_instagram_carousel(client, ig_id, draft, caption, token):
    """
    IG carousel: create one child container per slide (is_carousel_item=true),
    then a parent container (media_type=CAROUSEL, children=...), then publish it.

    DORMANT in draft-only mode: publish() short-circuits before we ever get here
    while the publish flag is OFF. This path only runs once Blake arms publishing.
    """
    base = config.GRAPH_API_BASE
    child_ids = []
    for url in draft.slide_urls:
        rc = client.post(
            f"{base}/{ig_id}/media",
            data={"image_url": url, "is_carousel_item": "true", "access_token": token},
            timeout=30,
        )
        _raise_for_status(rc)
        child_ids.append(rc.json().get("id"))

    rp = client.post(
        f"{base}/{ig_id}/media",
        data={"media_type": "CAROUSEL", "children": ",".join(child_ids),
              "caption": caption, "access_token": token},
        timeout=30,
    )
    _raise_for_status(rp)
    parent_id = rp.json().get("id")

    r2 = client.post(
        f"{base}/{ig_id}/media_publish",
        data={"creation_id": parent_id, "access_token": token},
        timeout=30,
    )
    _raise_for_status(r2)
    return PublishResult(ok=True, mode="published", media_id=r2.json().get("id", ""))


# Image containers usually finish in well under a second but can lag; poll
# status_code before publishing. 30 tries x 2s ~= 60s ceiling.
IMG_POLL_MAX_TRIES = 30
IMG_POLL_INTERVAL_SEC = 2
# Reels are heavier (video transcode); give them the same ~60s ceiling.
REEL_POLL_MAX_TRIES = 20
REEL_POLL_INTERVAL_SEC = 3


def _await_container_ready(client, base, container_id, token, *, label="media",
                           max_tries, interval, sleep=time.sleep):
    """
    Poll a media container's status_code until FINISHED, then return. Raise
    MediaNotReady on ERROR or if it never finishes within the bounded retries
    (a held-and-retry condition, NOT a hard failure). `sleep` is injectable so a
    test never actually waits. Only runs once publishing is armed (guarded
    upstream). READ-ONLY: one GET per poll, never a write.
    """
    for _ in range(max_tries):
        r = client.get(
            f"{base}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        _raise_for_status(r)
        status = (r.json() or {}).get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise MediaNotReady(
                f"{label} container {container_id} processing failed (status ERROR).")
        sleep(interval)
    raise MediaNotReady(
        f"{label} container {container_id} not FINISHED after {max_tries} tries "
        f"(~{max_tries * interval}s)."
    )


def _publish_instagram_reel(client, account, draft, caption, token, ig_id):
    """
    IG Reel: create a REELS container (video_url + share_to_feed=true), poll the
    container's status_code until FINISHED, then publish it.

    DORMANT in draft-only mode: publish() short-circuits before we ever get here
    while the publish flag is OFF. This path only runs once Blake arms publishing.
    """
    if not draft.creative_public_url:
        raise PublishError(
            "Instagram Reels need a PUBLIC video URL. This creative has none. "
            "Host it and set public_url in its sidecar. See AGENT_README.md."
        )
    base = config.GRAPH_API_BASE
    # step 1: create the REELS container
    r1 = client.post(
        f"{base}/{ig_id}/media",
        data={
            "media_type": "REELS",
            "video_url": draft.creative_public_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        },
        timeout=30,
    )
    _raise_for_status(r1)
    container_id = r1.json().get("id")
    # step 2: a Reel's video is processed asynchronously; wait for FINISHED.
    _await_container_ready(client, base, container_id, token, label="Reel",
                           max_tries=REEL_POLL_MAX_TRIES,
                           interval=REEL_POLL_INTERVAL_SEC)
    # step 3: publish the processed container
    r2 = client.post(
        f"{base}/{ig_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    _raise_for_status(r2)
    return PublishResult(ok=True, mode="published", media_id=r2.json().get("id", ""))


def _publish_instagram_story(client, account, draft, token):
    """
    IG Story: create a STORIES container (image_url or video_url, no caption), then
    publish it. DORMANT until BOTH the publish flag and the stories flag are armed:
    publish() short-circuits upstream before this is ever reached.
    """
    ig_id = account.get_target_id()
    if not ig_id:
        raise PublishError(f"No IG user id for '{account.key}'.")
    if not draft.creative_public_url:
        raise PublishError(
            "An Instagram Story needs a PUBLIC media URL. This creative has none. "
            "Host it first. See AGENT_README.md."
        )
    base = config.GRAPH_API_BASE
    media_param = "video_url" if _is_video(draft.creative_public_url) else "image_url"
    r1 = client.post(
        f"{base}/{ig_id}/media",
        data={"media_type": "STORIES", media_param: draft.creative_public_url,
              "access_token": token},
        timeout=30,
    )
    _raise_for_status(r1)
    container_id = r1.json().get("id")
    r2 = client.post(
        f"{base}/{ig_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    _raise_for_status(r2)
    return PublishResult(ok=True, mode="published", media_id=r2.json().get("id", ""))


def _publish_fb_page_story(client, account, draft, token):
    """
    FB Page photo Story: upload the photo unpublished, then attach it to
    /photo_stories. DORMANT until BOTH the publish flag and the stories flag are
    armed: publish() short-circuits upstream before this is ever reached.
    """
    page_id = account.get_target_id()
    if not page_id:
        raise PublishError(f"No Page id for '{account.key}'.")
    if not draft.creative_public_url:
        raise PublishError(
            "A Facebook Page Story needs a PUBLIC media URL. This creative has none. "
            "Host it first. See AGENT_README.md."
        )
    base = config.GRAPH_API_BASE
    r1 = client.post(
        f"{base}/{page_id}/photos",
        data={"url": draft.creative_public_url, "published": "false",
              "access_token": token},
        timeout=30,
    )
    _raise_for_status(r1)
    photo_id = r1.json().get("id")
    r2 = client.post(
        f"{base}/{page_id}/photo_stories",
        data={"photo_id": photo_id, "access_token": token},
        timeout=30,
    )
    _raise_for_status(r2)
    body = r2.json()
    return PublishResult(ok=True, mode="published",
                         media_id=body.get("post_id") or body.get("id", ""))


def _publish_fb_page(client, account, draft, caption, token):
    page_id = account.get_target_id()
    if not page_id:
        raise PublishError(f"No Page id for '{account.key}'.")
    base = config.GRAPH_API_BASE
    if draft.creative_public_url and (_is_video(draft.creative_public_url)
                                      or _is_video(draft.creative_path)):
        # A reel/video posts to the Page /videos endpoint (file_url), NOT /photos
        # (which rejects mp4 with "Can't Read Files"). description carries the caption.
        r = client.post(
            f"{base}/{page_id}/videos",
            data={"file_url": draft.creative_public_url, "description": caption,
                  "access_token": token},
            timeout=60,
        )
    elif draft.creative_public_url:
        r = client.post(
            f"{base}/{page_id}/photos",
            data={"url": draft.creative_public_url, "caption": caption, "access_token": token},
            timeout=30,
        )
    else:
        r = client.post(
            f"{base}/{page_id}/feed",
            data={"message": caption, "access_token": token},
            timeout=30,
        )
    _raise_for_status(r)
    body = r.json()
    return PublishResult(ok=True, mode="published",
                         media_id=body.get("post_id") or body.get("id", ""))


def _raise_for_status(resp):
    if getattr(resp, "status_code", 200) >= 400:
        raise PublishError(f"Meta API error {resp.status_code}: {resp.text}")
