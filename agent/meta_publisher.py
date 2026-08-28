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


# ---- content-hash dedup at the Meta boundary (LASSO IG triple-publish 2026-08-27) --
# The same (account, caption-hash, media) is NEVER sent to Meta twice within 24h
# without an explicit human release. Root cause it guards: the daily draw refired
# after mid-draw deploys and re-sent identical posts ('Honest numbers or no
# numbers' x3, the Pierce welcome x2). This is an exactly-once correctness rail
# (like the calendar lane's atomic claim), not a new capability, so it is not
# flag-gated. The stamp lives in the durable kv (/data), written only AFTER a
# REAL publish; would_publish never stamps.

DEDUP_WINDOW_HOURS = 24


def _dedup_key(account_key, draft):
    import hashlib
    from .caption_ledger import caption_hash
    cap_h = caption_hash(getattr(draft, "caption", "") or "")
    media = (getattr(draft, "creative_public_url", "") or "")
    media_h = hashlib.sha1(media.encode("utf-8", "replace")).hexdigest()[:10]
    return f"metapub_{account_key}_{cap_h}_{media_h}"


def _recent_duplicate(account_key, draft):
    """The prior media_id when this exact (account, caption, media) already
    published within DEDUP_WINDOW_HOURS, else None. Best effort: an unreadable
    stamp reads as no-duplicate (never blocks a legit publish on a kv hiccup)."""
    import json as _json
    from datetime import datetime, timezone
    try:
        from . import db
        raw = db.kv_get(_dedup_key(account_key, draft), "")
        if not raw:
            return None
        rec = _json.loads(raw)
        ts = datetime.fromisoformat(rec.get("ts", ""))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < DEDUP_WINDOW_HOURS * 3600:
            return rec.get("media_id", "") or "dedup"
    except Exception:
        return None
    return None


def _stamp_published(account_key, draft, media_id):
    """Record a REAL publish for the 24h dedup window. Best effort."""
    import json as _json
    from datetime import datetime, timezone
    try:
        from . import db
        db.kv_set(_dedup_key(account_key, draft), _json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(),
             "media_id": media_id or ""}))
    except Exception:
        pass


def release_dedup(account_key, draft):
    """EXPLICIT HUMAN RELEASE: clear the 24h dedup stamp so this exact content
    may be deliberately re-published (e.g. a human deleted the live post and
    wants it re-sent). Never called by any automated lane."""
    from . import db
    db.kv_set(_dedup_key(account_key, draft), "")


def publish(draft, account, http=None):
    """
    Publish a draft to the right Meta surface. Returns a PublishResult.

    Draft-only short-circuit: if publishing is not armed, we do NOT touch Meta.
    Content-hash dedup: an identical (account, caption, media) already published
    within 24h is NOT re-sent — the prior media_id is returned with a 'dedup'
    detail (same posture as the zernio 409 handling).
    """
    if not config.publish_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="publish flag OFF (draft-only)")

    # Stories sit behind BOTH gates: even with publishing armed, a Story draft makes
    # NO network call until AGENT_STORIES_ENABLED is also armed.
    if getattr(draft, "is_story", False) and not config.stories_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="stories flag OFF (draft only)")

    prior = _recent_duplicate(account.key, draft)
    if prior is not None:
        return PublishResult(
            ok=True, mode="published", media_id="" if prior == "dedup" else prior,
            detail="meta dedup: identical (account, caption, media) published "
                   "within 24h; NOT re-sent (release_dedup() is the explicit "
                   "human override)")

    result = _publish_gated(draft, account, http)
    if result.ok and result.mode == "published":
        _stamp_published(account.key, draft, result.media_id)
        # CAPTION LEDGER (AGENT_CAPTION_COOLDOWN, default OFF): a real publish
        # through ANY lane enters the 180-day verbatim ledger. Best effort,
        # never fatal (the post is already live; calendar-lane rows are also
        # stamped by the store's mark_published, where the upsert is idempotent).
        if config.caption_cooldown_enabled() and not getattr(draft, "is_story", False):
            try:
                from .caption_ledger import record_published
                from datetime import date as _date
                gym = account.key
                for _suf in ("_ig", "_fb"):
                    if gym.endswith(_suf):
                        gym = gym[: -len(_suf)]
                        break
                cap = getattr(draft, "caption", "") or ""
                day = (getattr(draft, "day_key", "") or "")[:10] or \
                    _date.today().isoformat()
                if cap.strip():
                    record_published(gym, cap, day)
            except Exception:
                pass
    return result


def _publish_gated(draft, account, http=None):
    """The real publish dispatch (post-gates, post-dedup)."""
    token = account.get_token()
    if not token:
        raise MissingToken(f"No token set for account '{account.key}'.")

    full_caption = _compose_caption(draft)
    client = http or _requests()

    # BELT AND BRACES (report-card build 2026-08-28, parity with
    # zernio_publisher's identical rail): a FEED payload with an empty /
    # invisible body must never reach the Graph API — an emoji-only or '...'
    # caption is not a caption. Raising (not would_publish) makes the caller's
    # revert + alert path own it. Stories are exempt (empty body by design;
    # their caption is burned on the media). This is an exactly-once
    # correctness rail like the 24h dedup above, not a new capability, so it
    # is not flag-gated.
    if not getattr(draft, "is_story", False):
        from .publish_guard import visible_len
        if visible_len(full_caption) == 0:
            raise ValueError(
                f"{account.key}: refusing to publish a FEED post with an empty "
                "(zero visible characters) caption; a feed caption must carry "
                "real words.")
        # VERBATIM DEDUP BELT (AGENT_CAPTION_COOLDOWN, default OFF): a FEED
        # caption already used for this gym on a DIFFERENT date within 180 days
        # never ships twice, no matter which lane reached this publisher.
        # Same-date records (the row's own staging stamp, the FB cross-post,
        # the paired story) never block. Fail-open on ledger errors.
        if config.caption_cooldown_enabled():
            try:
                from .caption_ledger import is_verbatim_blocked, VERBATIM_BLOCK_DAYS
                from datetime import date as _date
                gym = account.key
                for _suf in ("_ig", "_fb"):
                    if gym.endswith(_suf):
                        gym = gym[: -len(_suf)]
                        break
                day = (getattr(draft, "day_key", "") or "")[:10] or \
                    _date.today().isoformat()
                cap = getattr(draft, "caption", "") or ""
                blocked = bool(cap.strip()) and is_verbatim_blocked(gym, cap, day)
            except Exception:
                blocked = False  # a kv hiccup never blocks a legit publish
            if blocked:
                raise ValueError(
                    f"{account.key}: refusing to publish a FEED post whose "
                    f"caption already ran within {VERBATIM_BLOCK_DAYS} days "
                    "(verbatim duplicate); redraft it, a caption never ships "
                    "twice.")

    if getattr(draft, "is_story", False):
        if account.platform == Platform.INSTAGRAM:
            return _publish_instagram_story(client, account, draft, token)
        if account.platform == Platform.FACEBOOK_PAGE:
            return _publish_fb_page_story(client, account, draft, token)
        raise NotSupported(f"Stories are not supported on platform: {account.platform}")

    if account.platform == Platform.INSTAGRAM:
        result = _publish_instagram(client, account, draft, full_caption, token)
    elif account.platform == Platform.FACEBOOK_PAGE:
        result = _publish_fb_page(client, account, draft, full_caption, token)
    elif account.platform == Platform.PERSONAL:
        raise NotSupported(
            "Graph API cannot publish to a personal Facebook profile. "
            "Use a Page or an IG Business/Creator account. See AGENT_README.md."
        )
    else:
        raise NotSupported(f"Unknown platform: {account.platform}")

    # Cross-post to Stories on the same account when AGENT_STORY_CROSSPOST_ENABLED=true.
    # Non-fatal: a story failure never overrides the main publish result.
    if result.ok and result.mode == "published" and config.story_crosspost_enabled():
        try:
            _crosspost_story(client, account, draft, token)
        except Exception as _se:
            print(f"[meta] story crosspost skipped ({account.key}): {_se}", flush=True)

    return result


def delete_media(account_key, media_id, http=None):
    """Best-effort delete of a just-published post, for the 5-minute chat undo.
    Returns True ONLY on a confirmed delete. The Graph API can delete a Facebook
    Page post (DELETE /{post-id}); it cannot delete published Instagram media, so IG
    and personal profiles return False and the caller tells Blake to remove it by
    hand. Guarded by AGENT_PUBLISH_ENABLED so a draft-only environment never calls
    Meta."""
    if not config.publish_enabled():
        return False
    from .accounts import get_account
    account = get_account(account_key)
    if account is None:
        return False
    token = account.get_token()
    if not token or not media_id:
        return False
    if account.platform != Platform.FACEBOOK_PAGE:
        return False  # IG published media / personal profiles: no API delete
    client = http or _requests()
    try:
        resp = client.delete(f"{config.GRAPH_API_BASE}/{media_id}",
                             params={"access_token": token}, timeout=30)
        return getattr(resp, "status_code", 500) < 300
    except Exception:
        return False


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
    # step 2: wait for FINISHED before publishing
    _await_container_ready(client, base, container_id, token, label="media",
                           max_tries=IMG_POLL_MAX_TRIES,
                           interval=IMG_POLL_INTERVAL_SEC,
                           grace=POST_FINISH_GRACE_SEC)
    # step 3: publish with retry for 9007
    r2 = _publish_container(client, base, ig_id, container_id, token)
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

    # Wait for the CAROUSEL parent to finish processing, then publish with the shared
    # 9007 retry, or Meta returns "media not ready" (same fix as feed/story/reel).
    _await_container_ready(client, base, parent_id, token, label="carousel",
                           max_tries=IMG_POLL_MAX_TRIES, interval=IMG_POLL_INTERVAL_SEC,
                           grace=POST_FINISH_GRACE_SEC)
    r2 = _publish_container(client, base, ig_id, parent_id, token)
    return PublishResult(ok=True, mode="published", media_id=r2.json().get("id", ""))


# Image containers usually finish in well under a second but can lag; poll
# status_code before publishing. 30 tries x 2s ~= 60s ceiling.
IMG_POLL_MAX_TRIES = 30
IMG_POLL_INTERVAL_SEC = 2
# Reels are heavier (video transcode); give them more room (~90s).
REEL_POLL_MAX_TRIES = 30
REEL_POLL_INTERVAL_SEC = 3
# After status turns FINISHED, IG sometimes still returns 9007 for a moment.
# Sleep this many seconds before calling media_publish, then retry on 9007.
POST_FINISH_GRACE_SEC = 5
POST_FINISH_RETRIES = 3
POST_FINISH_RETRY_SEC = 5


def _publish_container(client, base, ig_id, container_id, token,
                       _sleep=time.sleep):
    """
    Call /{ig_id}/media_publish and return the response. Retries up to
    POST_FINISH_RETRIES times on error 9007 / subcode 2207027 ("The media is
    not ready for publishing") — IG sometimes lags briefly after FINISHED.
    Raises MediaNotReady if all retries are exhausted, PublishError otherwise.
    """
    for attempt in range(POST_FINISH_RETRIES + 1):
        r = client.post(
            f"{base}/{ig_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=30,
        )
        if getattr(r, "status_code", 200) < 400:
            return r
        body = {}
        try:
            body = r.json() or {}
        except Exception:
            pass
        err = body.get("error", {})
        is_not_ready = (
            err.get("error_subcode") == 2207027
            or err.get("code") == 9007
        )
        if is_not_ready and attempt < POST_FINISH_RETRIES:
            _sleep(POST_FINISH_RETRY_SEC)
            continue
        if is_not_ready:
            raise MediaNotReady(
                f"container {container_id} still not publishable after "
                f"{POST_FINISH_RETRIES} retries: {r.text}"
            )
        raise PublishError(f"Meta API error {r.status_code}: {r.text}")
    return r  # unreachable but satisfies linters


def _await_container_ready(client, base, container_id, token, *, label="media",
                           max_tries, interval, grace=0, sleep=time.sleep):
    """
    Poll a media container's status_code until FINISHED, then return. Raise
    MediaNotReady on ERROR or if it never finishes within the bounded retries
    (a held-and-retry condition, NOT a hard failure). `sleep` is injectable so a
    test never actually waits. Only runs once publishing is armed (guarded
    upstream). READ-ONLY: one GET per poll, never a write.

    `grace`: extra seconds to sleep after FINISHED before returning. Even after
    status is FINISHED, IG can still return 9007 on the media_publish call for a
    brief window. A small grace sleep eliminates most of those races.
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
            if grace:
                sleep(grace)
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
    # step 2: wait for FINISHED + grace buffer before publishing
    _await_container_ready(client, base, container_id, token, label="Reel",
                           max_tries=REEL_POLL_MAX_TRIES,
                           interval=REEL_POLL_INTERVAL_SEC,
                           grace=POST_FINISH_GRACE_SEC)
    # step 3: publish with retry for 9007 (IG can still lag briefly after FINISHED)
    r2 = _publish_container(client, base, ig_id, container_id, token)
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
    # STORIES containers are processed asynchronously just like feed media: publishing
    # immediately returns 9007 "media not ready". Poll to FINISHED, then publish through
    # the shared 9007 retry (same as the feed path). This is what stopped posts going out.
    _await_container_ready(client, base, container_id, token, label="story",
                           max_tries=IMG_POLL_MAX_TRIES, interval=IMG_POLL_INTERVAL_SEC,
                           grace=POST_FINISH_GRACE_SEC)
    r2 = _publish_container(client, base, ig_id, container_id, token)
    return PublishResult(ok=True, mode="published", media_id=r2.json().get("id", ""))


def _publish_fb_page_story(client, account, draft, token):
    """
    FB Page Story. Video -> /video_stories (file_url). Image -> upload unpublished
    then /photo_stories. DORMANT until BOTH the publish flag and the stories flag are
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
    if _is_video(draft.creative_public_url):
        r = client.post(
            f"{base}/{page_id}/video_stories",
            data={"file_url": draft.creative_public_url, "access_token": token},
            timeout=60,
        )
        _raise_for_status(r)
        return PublishResult(ok=True, mode="published", media_id=r.json().get("id", ""))
    # Photo story: upload unpublished then attach
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


def _crosspost_story(client, account, draft, token):
    """Post the same creative as a Story right after the main reel/image publish.
    Called only when AGENT_STORY_CROSSPOST_ENABLED=true; errors are caught upstream."""
    if account.platform == Platform.INSTAGRAM:
        r = _publish_instagram_story(client, account, draft, token)
        print(f"[meta] ig story crossposted: {r.media_id}", flush=True)
    elif account.platform == Platform.FACEBOOK_PAGE:
        r = _publish_fb_page_story(client, account, draft, token)
        print(f"[meta] fb story crossposted: {r.media_id}", flush=True)


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
