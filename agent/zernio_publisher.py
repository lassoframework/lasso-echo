"""
Zernio client-publish lane — publish a CLIENT gym's approved post to the gym's OWN
connected IG/FB via Zernio (POST /v1/posts). This is the missing link that makes
"client approves -> posts to his own pages" real: LASSO stays on meta_direct, but a
client gym that connected through Zernio publishes through Zernio, to the account it
actually connected.

Same contract as meta_publisher / socialapi_publisher:
    publish(draft, account, client=None, ...) -> PublishResult(ok, mode, media_id, ...)

TWO hard gates, both must be armed or NOTHING goes live (returns would_publish, no
network call): config.publish_enabled() (the global kill switch) AND
config.zernio_publish_enabled(). Belt-and-suspenders with the approval gate.

Scheduling: pass scheduled_for (ISO8601) to SCHEDULE the post at that time via
Zernio's scheduledFor (the client then sees exactly when it will go live); omit it
to publish immediately. The calendar lane computes scheduled_for from the row's slot
time so every post has a real, visible go-live time.

Resolution per gym (never a LASSO fallback — a client publishes to ITS OWN account):
  - profile_id: the gym's stored Zernio profile (gyms.zernio_profile_id), by account
    key then by tenant base (eng_ig -> eng);
  - account_id: the CONNECTED IG/FB account under that profile (zernio.account_id_for);
  - page_id (Facebook only): the gym's chosen page (gyms.zernio_default_fb_page_id).
A missing profile / account / (FB) page is a HARD, honest failure (no post, no fake),
never a silent send to the wrong account.

Nothing here logs a token or secret.
"""

from dataclasses import dataclass

from . import config, zernio
from .accounts import Platform


@dataclass
class PublishResult:
    ok: bool
    mode: str            # "published" or "would_publish"
    media_id: str = ""   # the Zernio post id (what postlog / late_post_id stores)
    detail: str = ""
    permalink: str = ""
    # True only for a Zernio 409 content-hash dedup: Zernio told us this exact content
    # ALREADY exists on the account. That is the ONE case where "published" is honest
    # without a post id of our own, so it is the only case mark_published accepts a
    # blank late_post_id for. Never set on a normal create.
    dedup: bool = False


class ZernioPublishError(Exception):
    pass


_PLATFORM = {
    Platform.INSTAGRAM: "instagram",
    Platform.FACEBOOK_PAGE: "facebook",
}


def _tenant_bases(account_key):
    """Keys to try when resolving the gym record: the account key as given, then the
    tenant base (strip a trailing _ig / _fb). eng_ig -> ['eng_ig', 'eng']."""
    keys = [account_key]
    for suffix in ("_ig", "_fb"):
        if account_key.endswith(suffix):
            keys.append(account_key[: -len(suffix)])
    return keys


def _default_profile_resolver(account_key):
    """The gym's stored Zernio profile id (gyms.zernio_profile_id), trying the account
    key then the tenant base. Read-only; never provisions."""
    from . import db
    for key in _tenant_bases(account_key):
        row = db.gym_get(key)
        if row and row.get("zernio_profile_id"):
            return str(row["zernio_profile_id"])
    return None


def _default_page_resolver(account_key):
    """The gym's chosen Facebook page id (gyms.zernio_default_fb_page_id), or None."""
    from . import db
    for key in _tenant_bases(account_key):
        row = db.gym_get(key)
        if row and row.get("zernio_default_fb_page_id"):
            return str(row["zernio_default_fb_page_id"])
    return None


def publish(draft, account, client=None, scheduled_for=None,
            profile_resolver=None, page_resolver=None):
    """Publish (or schedule) `draft` to `account`'s own connected Zernio account.

    All external calls go through `client` (a ZernioClient), injectable for tests.
    Returns a PublishResult; raises ZernioPublishError only on a genuinely
    unresolvable setup (missing profile / account / FB page) so the caller can hold
    and surface it, never send to the wrong place.
    """
    # DRAFT-ONLY GUARDS (both must be armed). No network call otherwise.
    if not config.publish_enabled() or not config.zernio_publish_enabled():
        return PublishResult(ok=True, mode="would_publish",
                             detail="draft-only (publish or zernio-publish flag OFF)")

    platform = _PLATFORM.get(getattr(account, "platform", ""), "")
    if not platform:
        raise ZernioPublishError(f"unsupported platform for {account.key}")

    # NO PHOTO = NO POST (the "approved with no image" class, closed at the wire).
    # zernio.create_post simply OMITS mediaItems when the url list is empty, and Zernio
    # cheerfully publishes the caption as a TEXT-ONLY post to the gym's real feed. Every
    # upstream rail leaks this one: portal_social._handle_approve_supabase never reads
    # image_url, the store's set_status does not either, due_rows' "image_url is not
    # null" filter does NOT catch the EMPTY STRING several lanes write, and the one real
    # rail (publish_guard MEDIA_MISSING) is reachable only under calendar_grade_enabled.
    # So the refusal belongs HERE, the one wire every client gym publishes through
    # (preflight: every client row is Zernio-routed), mirroring meta_publisher's
    # long-standing "Instagram needs a PUBLIC media URL" raise.
    #
    # SCOPE — this is a blanket refusal ON PURPOSE, and it is still precise: the ONLY
    # formats reaching this function are IG/FB feed, story and reel (the _PLATFORM map
    # above admits instagram + facebook_page and hard-raises on anything else), and all
    # three REQUIRE media on the platform side. The one legitimately media-less Echo
    # format, a Google Business text post, never touches this function: GBP is
    # Platform.GOOGLE_BUSINESS, approvals._publisher_for routes it to gbp_publisher, and
    # its payload goes out through zernio.create_post_raw (a different call) — so no GBP
    # row can be caught by this guard. Raised BEFORE any network call so a photo-less
    # post costs nothing and the caller's revert + alert path owns it.
    url = (getattr(draft, "creative_public_url", "") or "").strip()
    media_urls = [url] if url else []
    if not media_urls:
        raise ZernioPublishError(
            f"{account.key}: refusing to publish a {platform} post with NO media "
            "(empty creative url). Zernio would publish it as a caption-only text "
            "post to the gym's real feed. Attach the image or hold the row.")

    profile_resolver = profile_resolver or _default_profile_resolver
    page_resolver = page_resolver or _default_page_resolver
    client = client or zernio.ZernioClient()

    profile_id = profile_resolver(account.key)
    if not profile_id:
        raise ZernioPublishError(
            f"{account.key}: no Zernio profile id stored; the gym must connect first.")

    accounts_json = client.list_accounts(profile_id)
    account_id = zernio.account_id_for(accounts_json, platform)
    if not account_id:
        raise ZernioPublishError(
            f"{account.key}: no connected {platform} account under the gym's Zernio "
            "profile; reconnect required.")

    page_id = None
    if platform == "facebook":
        page_id = page_resolver(account.key)
        if not page_id:
            raise ZernioPublishError(
                f"{account.key}: no Facebook page selected; the gym must pick a page.")

    # STORY: the draft's own type decides the Zernio contentType (IG/FB Story vs feed).
    story = bool(getattr(draft, "is_story", False)) or (
        (getattr(draft, "draft_type", "") or "").strip().lower() == "story")

    # STORIES CARRY NO CAPTION: platforms do not display caption text on a story, and
    # sending the paired feed's caption made the story byte-identical to the feed on
    # the same account — Zernio's 24h content-hash dedup then 409'd and the story was
    # NEVER created while Echo marked it published (Dale's missing IG story, 2026-08-13).
    body = "" if story else (getattr(draft, "caption", "") or "")

    # EDIT-RATIONALE STRIP, AT THE WIRE (CrossFit ENG, live FB post 2026-08-23 00:02
    # ET: a caption published ending "[why] Removed word parents and added people ...",
    # called out by a member in the comments). Layer 3 of the four-layer strip lives in
    # calendar_autopublish._strip_or_hold_meta — INSIDE publish_due, which three live
    # lanes never enter: approvals.approve (the Slack/portal approve card), runner's
    # LASSO auto-approve (live under AGENT_AUTO_APPROVE_ENABLED) and chat_publish's
    # approve-in-chat. Each of those calls a publisher DIRECTLY, so a "[why] ..." block
    # could still ride a caption onto a real feed. Moving the same split to the wire
    # closes all three at once.
    #
    # Same function, never a second regex: post_quality.split_meta_suffix is the single
    # shared definition of what a meta block IS (the stage gate, the drafter's parser,
    # layer 3 and the book sweep all read it), so this layer can never disagree with
    # them. IDEMPOTENT by construction — a caption layer 3 already cleaned carries no
    # label, split_meta_suffix finds no match and returns it byte-identical.
    # An ALL-META caption strips to an empty body and is deliberately NOT published:
    # the empty-body refusal below raises, and the caller's revert + alert path holds
    # the row for a human (never silently emptied). Stories send body="" by design and
    # are untouched.
    if body:
        from .post_quality import split_meta_suffix
        _clean, _meta = split_meta_suffix(body)
        if _meta:
            print(f"[zernio-publish] {account.key}: stripped an internal "
                  "edit-rationale block off the caption at the publish wire")
            body = _clean

    # BELT AND BRACES (publish_guard wiring, 2026-08-27): a FEED payload with an
    # empty/invisible body must never reach the API — an emoji-only or '...'
    # caption is not a caption (publish_guard.visible_len counts alphanumerics
    # only). Raising (not returning would_publish) makes the caller's revert +
    # alert path own it. STORIES deliberately send body="" (see the STORIES
    # CARRY NO CAPTION note above) and are exempt.
    if not story:
        from .publish_guard import visible_len
        if visible_len(body) == 0:
            raise ValueError(
                f"{account.key}: refusing to publish a FEED post with an empty "
                "(zero visible characters) body; stories send an empty body by "
                "design, a feed caption must carry real words.")
        # VERBATIM DEDUP BELT (AGENT_CAPTION_COOLDOWN, default OFF): a FEED
        # caption already used for this gym on a DIFFERENT date within 180 days
        # (caption_ledger.VERBATIM_BLOCK_DAYS) never ships through this wire
        # either — the same rail meta_publisher carries, so the guarantee holds
        # no matter which lane reached a publisher. Same-date records (the
        # row's own staging stamp, the FB cross-post, the paired story) never
        # block; ledger errors fail open (a kv hiccup never blocks a legit
        # publish). Raising makes the caller's revert + alert path own it.
        if config.caption_cooldown_enabled():
            _dup = False
            try:
                from .caption_ledger import is_verbatim_blocked, VERBATIM_BLOCK_DAYS
                from datetime import date as _date
                _gym = account.key
                for _suf in ("_ig", "_fb"):
                    if _gym.endswith(_suf):
                        _gym = _gym[: -len(_suf)]
                        break
                _day = (getattr(draft, "day_key", "") or "")[:10] or \
                    _date.today().isoformat()
                _dup = bool(body.strip()) and is_verbatim_blocked(_gym, body, _day)
            except Exception:
                _dup = False
            if _dup:
                raise ValueError(
                    f"{account.key}: refusing to publish a FEED post whose "
                    f"caption already ran within {VERBATIM_BLOCK_DAYS} days "
                    "(verbatim duplicate); redraft it, a caption never ships "
                    "twice.")

    # AGENT_MENTIONS: append validated @handle mentions (newline-separated) to the
    # caption when the flag is ON. @handles in caption text are rendered by Zernio
    # as live mentions. Stories carry no caption, so mentions are skipped for stories.
    if body and config.mentions_enabled():
        _category = (getattr(draft, "category", "") or "").strip().lower()
        _gym_id = (getattr(draft, "gym_id", "") or
                   getattr(draft, "account_key", account.key) or account.key)
        # Strip tenant suffix (eng_ig -> eng) to match gym_tag_allowlist gym_id
        for _suf in ("_ig", "_fb"):
            if _gym_id.endswith(_suf):
                _gym_id = _gym_id[: -len(_suf)]
                break
        if _category:
            try:
                from .tag_allowlist import handles_for_category
                _handles = handles_for_category(_gym_id, _category)
                if _handles:
                    body = body.rstrip() + "\n" + "\n".join(f"@{h}" for h in _handles)
            except Exception:
                pass  # mention failures are non-fatal; the post still goes out

    # PAST SLOT -> publish NOW: Zernio treats a missing scheduledFor + publishNow=true
    # as immediate. Handing it a past timestamp risks a 400/undefined behavior, and a
    # row approved after its slot passed should just go out (catch_all semantics).
    if scheduled_for and _is_past(scheduled_for):
        scheduled_for = None

    try:
        resp = client.create_post(account_id, body,
                                  media_urls=media_urls, scheduled_for=scheduled_for,
                                  page_id=page_id, platform=platform, story=story)
    except zernio.ZernioError as exc:
        # 409 = Zernio's 24h content-hash dedup: this exact content already posted to
        # this account. That IS success for our exactly-once goal — mark it published
        # (carrying Zernio's existingPostId when it names one) instead of reverting
        # to approved and retrying the same duplicate forever. Stories can no longer
        # trip this against their paired feed (empty body above); a remaining 409 is
        # a genuine same-content duplicate.
        if getattr(exc, "status", None) == 409:
            existing = _existing_post_id(getattr(exc, "detail", ""))
            return PublishResult(ok=True, mode="published", media_id=existing,
                                 detail="zernio dedup: identical post already exists",
                                 dedup=True)
        raise
    post_id = zernio.post_id_of(resp)
    # NO POST ID = NOT A PUBLISH (published-but-not-posted, the recurring class).
    # A 2xx whose body we cannot parse tells us Zernio answered, NOT that it accepted
    # the post. Returning ok=True here stamped status='published' with an empty
    # late_post_id: unverifiable by publish_confirm, invisible to any reconcile, and
    # indistinguishable in the portal from a post that really went live. Refuse it.
    # The caller reverts the claim and retries next run, which is SAFE because Zernio's
    # own 24h content-hash dedup answers a genuine double-send with a 409 that IS
    # handled as published above. Failing closed can at worst re-ask; the old behaviour
    # could silently lose a post forever.
    if not str(post_id or "").strip():
        raise ZernioPublishError(
            f"{account.key}: Zernio returned no post id for this create "
            "(2xx with an unparseable body). Refusing to mark it published — a post "
            "we cannot identify cannot be verified or reconciled. The row reverts and "
            "retries; Zernio's 24h dedup makes the retry safe.")
    return PublishResult(ok=True, mode="published", media_id=post_id,
                         detail="scheduled" if scheduled_for else "published now")


def _existing_post_id(detail):
    """Zernio's existingPostId out of a 409 body (best effort; '' when absent)."""
    import json as _json
    try:
        data = _json.loads(detail or "")
        return str(data.get("existingPostId") or "")
    except (ValueError, TypeError):
        return ""


def _is_past(iso_ts):
    """True when the ISO8601 timestamp is at/before now. A naive timestamp is assumed
    UTC. Unparseable -> False (keep the schedule; Zernio will validate)."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts <= datetime.now(timezone.utc)
