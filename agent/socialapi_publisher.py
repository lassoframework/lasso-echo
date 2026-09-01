"""
SocialAPI.ai publisher — the second publish lane.

Same contract as meta_publisher: publish(draft, account, http=None) -> PublishResult.
The approval lifecycle calls this exactly like the Meta path; routing to it comes
from the account's publish_route (see approvals._publisher_for). Drafting,
approvals, fabrication/grade gates, trust ladder, calendar are all UNCHANGED.

THE DRAFT-ONLY GUARD LIVES HERE too, belt-and-suspenders with the approval gate:
if publish_enabled() is False, publish() makes NO network call and returns a
would_publish result. Stories additionally require stories_enabled().

Media: SocialAPI ignores a raw public URL, so we fetch the approved creative's
bytes from its R2 public_url and upload them to SocialAPI to get a media_id. R2
stays the source of truth; no new hosting path is introduced.

Idempotency: before publishing we check the posts table for an already-published
row for this draft; if present, publishing is a safe no-op (no double post under
any retry). We also pass the draft id as the vendor Idempotency-Key.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config, db, ops_alerts, socialapi_client, socialapi_store
from .accounts import Platform
# Reuse the Meta lane's retryable signal so approvals' existing "held, retry"
# path covers an async-not-ready post identically (no new UX to learn).
from .meta_publisher import MediaNotReady


@dataclass
class PublishResult:
    ok: bool
    mode: str            # "published" or "would_publish"
    media_id: str = ""   # the platform post id (what postlog stores)
    detail: str = ""
    permalink: str = ""  # public URL to the live post, when the vendor returns one
    # Part of the shared publisher contract, mirroring zernio_publisher and
    # meta_publisher: True ONLY when a vendor-side content dedup says an identical post
    # already exists, which is the one case "published" is honest with no post id of our
    # own and the only case portal_calendar_store.mark_published accepts a blank
    # late_post_id for. This lane polls to a terminal state and normally carries a real
    # id, so nothing sets it today — but the FIELD must exist, because
    # calendar_autopublish reads it via getattr on whichever publisher ran, and a
    # publisher missing it strands any future blank-id result in 'publishing' forever.
    dedup: bool = False


class SocialApiPublishError(Exception):
    pass


_PLATFORM_SLUG = {
    Platform.INSTAGRAM: "instagram",
    Platform.FACEBOOK_PAGE: "facebook",
}

_TERMINAL_OK = {"published"}
_TERMINAL_FAIL = {"failed", "cancelled"}

# Poll budget for an async-processing post before holding the card for retry.
POLL_TRIES = 4
POLL_INTERVAL = 2


def _platform_slug(account):
    slug = _PLATFORM_SLUG.get(account.platform)
    if not slug:
        raise SocialApiPublishError(
            f"SocialAPI lane does not support platform {account.platform!r} "
            f"for account {account.key!r} (IG + FB only)."
        )
    return slug


def _compose_caption(draft):
    """Caption + hashtags, IDENTICAL to the Meta lane so approved text is byte
    for byte the same on either route. Newlines are preserved verbatim."""
    return (draft.caption + ("\n\n" + " ".join(draft.hashtags)
                             if draft.hashtags else "")).strip()


def _media_meta(url):
    """(filename, content_type) inferred from the creative URL extension."""
    import os
    name = os.path.basename((url or "").split("?")[0]) or "creative"
    lower = name.lower()
    if lower.endswith(".png"):
        return name, "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return name, "image/jpeg"
    if lower.endswith(".mp4"):
        return name, "video/mp4"
    if lower.endswith(".mov"):
        return name, "video/quicktime"
    return name, "image/png"


def _already_published(draft):
    """True + the stored post id when this draft already has a published posts
    row. The idempotency backstop: a re-approve never double-posts."""
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT media_id FROM posts WHERE draft_id=? AND account_key=? "
                "AND mode='published' ORDER BY id DESC LIMIT 1",
                (draft.draft_id, draft.account_key)).fetchone()
        if row:
            return True, (row["media_id"] or "")
    except Exception:
        pass
    return False, ""


def _fetch_bytes(url, client):
    """Fetch the approved creative's bytes from its public R2 URL."""
    resp = client.get(url, timeout=60)
    code = getattr(resp, "status_code", 0)
    if not (200 <= code < 300):
        raise SocialApiPublishError(
            ops_alerts.scrub(f"could not fetch creative bytes: HTTP {code}"))
    return resp.content


def _bump_and_guard(account_key):
    """Record a per-account/day publish and fire a loud alert if the count runs
    past the sanity ceiling (one/day is normal; >ceiling means an upstream bug)."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        count = db.counter_bump(f"socialapi_publish_{account_key}", today)
    except Exception:
        return
    ceiling = config.socialapi_max_per_day()
    if count and count > ceiling:
        ops_alerts.alert(
            f"SocialAPI publish count for {account_key} is {count} today "
            f"(ceiling {ceiling}). Something upstream is publishing too often.")


def _interpret(resp):
    """(state, sapi_post_id, platform_post_id, permalink, err) from a post response.
    state is 'published' | 'failed' | 'processing'."""
    status = str(resp.get("status", "")).lower()
    tgts = resp.get("targets") or []
    tgt = tgts[0] if tgts else {}
    tstatus = str(tgt.get("status", "")).lower()
    sapi_post_id = resp.get("id") or ""
    platform_post_id = tgt.get("platform_post_id") or ""
    permalink = tgt.get("permalink") or ""
    err = tgt.get("error") or resp.get("error") or ""
    if status in _TERMINAL_FAIL or tstatus in _TERMINAL_FAIL:
        state = "failed"
    elif status in _TERMINAL_OK or tstatus in _TERMINAL_OK:
        state = "published"
    else:
        state = "processing"
    return state, sapi_post_id, platform_post_id, permalink, err


def _poll_terminal(client, post_id):
    """Poll get_post a few times for a terminal status. Returns the last response."""
    resp = {}
    for i in range(POLL_TRIES):
        resp = socialapi_client.get_post(post_id, http=client)
        state, *_ = _interpret(resp)
        if state in ("published", "failed"):
            return resp
        if i < POLL_TRIES - 1:
            time.sleep(POLL_INTERVAL)
    return resp


def _success(account, post_id, platform_post_id, permalink):
    _bump_and_guard(account.key)
    detail = "published via SocialAPI"
    if platform_post_id:
        detail += f" (platform_post_id={platform_post_id})"
    return PublishResult(ok=True, mode="published", media_id=post_id,
                         detail=detail, permalink=permalink)


def _resolve(resp, draft, account):
    """Turn a (possibly polled) vendor response into a PublishResult, or raise.
    Called AFTER the vendor has accepted the post, so the claim is never released
    here on a processing/failed state except a hard vendor failure (nothing live)."""
    state, sapi_post_id, platform_post_id, permalink, err = _interpret(resp)
    if state == "published":
        db.socialapi_claim_done(draft.draft_id, account.key,
                                sapi_post_id or platform_post_id)
        return _success(account, sapi_post_id or platform_post_id,
                        platform_post_id, permalink)
    if state == "failed":
        # A failed post is NOT live, so releasing lets Blake retry cleanly.
        db.socialapi_claim_release(draft.draft_id, account.key)
        msg = ops_alerts.scrub(str(err or "failed"))
        ops_alerts.alert(
            f"SocialAPI publish FAILED for {account.key} draft {draft.draft_id}: {msg}")
        raise SocialApiPublishError(f"SocialAPI publish failed: {msg}")
    # still processing: keep the claim (with the vendor post id) so a retry POLLS
    # this post instead of re-POSTing it, then hold the card.
    if sapi_post_id:
        db.socialapi_claim_set_post(draft.draft_id, account.key, sapi_post_id)
    raise MediaNotReady(
        f"SocialAPI post {sapi_post_id} for {account.key} is still processing; "
        f"held for retry.")


def publish(draft, account, http=None):
    """Publish one approved draft through SocialAPI. Returns a PublishResult."""
    # 1) Draft-only short-circuit: no network call when publishing is not armed.
    if not config.publish_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="publish flag OFF (draft-only)")

    # 2) Stories sit behind BOTH gates, same as the Meta lane.
    if getattr(draft, "is_story", False) and not config.stories_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="stories flag OFF (draft only)")

    # 3) Fast idempotency: an already-published posts row => no-op.
    done, prior_id = _already_published(draft)
    if done:
        return PublishResult(ok=True, mode="published", media_id=prior_id,
                             detail="already published (idempotent no-op)")

    client = http or socialapi_client._requests()

    # 4) Atomic claim BEFORE any network call. This is the real double-post guard:
    #    the PK on (draft_id, account_key) makes exactly one caller the winner, so
    #    a fast re-tap or a retry during a slow first POST cannot post twice.
    state, claim_post = db.socialapi_claim(draft.draft_id, account.key)
    if state == "done":
        return PublishResult(ok=True, mode="published", media_id=claim_post,
                             detail="already published (idempotent no-op)")
    if state == "in_flight":
        if claim_post:
            # A prior attempt reached the vendor but did not finish: POLL that
            # exact post, never POST a new one.
            return _resolve(_poll_terminal(client, claim_post), draft, account)
        raise MediaNotReady(
            f"another publish for {account.key} draft {draft.draft_id} is in "
            f"flight; held to avoid a double post.")

    # state == "won": we own the claim. Anything that fails BEFORE the vendor
    # accepts the post posted nothing, so we release the claim and let a retry
    # proceed. Once create_post returns, we never release (only poll/finish).
    try:
        slug = _platform_slug(account)
        sapi_account_id = socialapi_store.get_account_id(account.key, slug)
        if not sapi_account_id:
            raise SocialApiPublishError(
                f"{account.key} has no connected SocialAPI {slug} account. "
                f"Run the connect flow (portal social-connect) first.")
        media_url = draft.creative_public_url or ""
        if not media_url:
            raise SocialApiPublishError(
                f"{account.key} draft {draft.draft_id} has no creative_public_url; "
                f"nothing to upload. (Host the creative first.)")
        text = _compose_caption(draft)
        content_type = "stories" if getattr(draft, "is_story", False) else "feed"
        data = _fetch_bytes(media_url, client)
        filename, mime = _media_meta(media_url)
        media_id = socialapi_client.upload_media(data, filename, mime, http=client)
        if not media_id:
            raise SocialApiPublishError("SocialAPI returned no media_id for the upload.")
        resp = socialapi_client.create_post(
            sapi_account_id, text, [media_id], content_type=content_type,
            http=client, idempotency_key=draft.draft_id)
    except MediaNotReady:
        # never raised here, but be explicit: do not release on a hold.
        raise
    except Exception:
        # Nothing was posted to the vendor: release the claim so a retry works.
        db.socialapi_claim_release(draft.draft_id, account.key)
        raise

    # The vendor has the post. From here we only poll/finish; never release
    # except on a hard vendor failure (handled inside _resolve).
    state, sapi_post_id, _plat, _perma, _err = _interpret(resp)
    if state == "processing" and sapi_post_id:
        db.socialapi_claim_set_post(draft.draft_id, account.key, sapi_post_id)
        resp = _poll_terminal(client, sapi_post_id)
    return _resolve(resp, draft, account)
