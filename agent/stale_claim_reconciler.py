"""Provider-backed recovery for stranded calendar publish claims.

A ``content_calendar`` row stays ``publishing`` when a worker loses the response
to a social publish.  Retrying blindly can duplicate a live post; leaving the
claim forever can consume the account's daily reservation capacity forever.
This module resolves only the two cases Zernio can prove from a complete,
tenant-scoped read:

* the exact platform/content/media post exists and is live -> stamp the row;
* the complete provider window contains no exact post -> release the claim.

Incomplete pagination, missing tenant bindings, conflicting matches, and
non-terminal provider states remain held.  The reconciler never publishes.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config


MAX_PROVIDER_PAGES = 20
PAGE_SIZE = 50
MAX_ROW_AGE_DAYS = 30


def _dt(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _platform_entries(post):
    entries = [x for x in (post.get("platforms") or []) if isinstance(x, dict)]
    return entries or [{}]


def _media_urls(post):
    urls = set()
    for item in post.get("mediaItems") or post.get("media") or []:
        if isinstance(item, dict):
            value = item.get("url") or item.get("sourceUrl")
        else:
            value = item
        if value:
            urls.add(str(value).strip())
    for entry in _platform_entries(post):
        for item in entry.get("customMedia") or []:
            if isinstance(item, dict):
                value = item.get("url") or item.get("sourceUrl")
                if value:
                    urls.add(str(value).strip())
    return urls


def _expected_content(row):
    if str(row.get("format") or "feed").lower() == "story":
        return ""
    body = str(row.get("caption") or "")
    try:
        from .post_quality import split_meta_suffix
        body, _meta = split_meta_suffix(body)
    except Exception:  # the wire applies this best effort too
        pass
    return body.strip()


def _content_matches(expected, actual):
    actual = str(actual or "").strip()
    if actual == expected:
        return True
    # The mentions wire may append validated handles after the stored caption.
    return bool(expected) and actual.startswith(expected + "\n@")


def classify(row, posts):
    """Return ``(state, evidence)`` for one row over a complete provider window.

    ``state`` is live, absent, or ambiguous.  Absence is meaningful only because
    the caller obtained ``posts`` through ``posts_range_complete``.
    """
    platform = str(row.get("account") or "").lower()
    media = str(row.get("image_url") or "").strip()
    content = _expected_content(row)
    matches = []
    incomplete_identity = False
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        if not _content_matches(content, post.get("content")):
            continue
        for entry in _platform_entries(post):
            if str(entry.get("platform") or post.get("platform") or "").lower() != platform:
                continue
            urls = _media_urls(post)
            # A same-content/platform record that omits media could be this row;
            # it makes absence ambiguous rather than authorizing a retry.
            if not media or not urls:
                incomplete_identity = True
                continue
            if media not in urls:
                continue
            matches.append((post, entry))
    if not matches:
        if incomplete_identity:
            return "ambiguous", {"reason": "provider_media_identity_missing"}
        return "absent", {}
    if len(matches) != 1:
        return "ambiguous", {"reason": "multiple_exact_provider_matches"}
    post, entry = matches[0]
    post_id = str(post.get("_id") or post.get("id") or "").strip()
    platform_id = str(entry.get("platformPostId") or "").strip()
    status = str(entry.get("status") or post.get("status") or "").lower()
    published_at = entry.get("publishedAt") or post.get("publishedAt")
    # Zernio has returned both terminal labels on its read APIs.  The normal
    # post-publish confirmation path already treats them identically, so stale
    # recovery must do the same or a provider-confirmed live client post can
    # remain in ``publishing`` and reserve capacity indefinitely.
    if status in ("published", "posted") and post_id and platform_id and _dt(published_at):
        return "live", {"post_id": post_id, "platform_post_id": platform_id,
                        "published_at": _dt(published_at).isoformat()}
    if status in ("failed", "deleted", "rejected") and not platform_id:
        return "absent", {"provider_status": status}
    return "ambiguous", {"reason": "provider_post_not_terminal", "provider_status": status}


def _old_enough(row, kv, now):
    key = f"stuck_publishing_{row.get('id')}"
    try:
        seen = str(kv.get(key, "") or "")
    except Exception:
        return False
    if seen.startswith("alerted:"):
        return True
    first = _dt(seen)
    return bool(first and (now - first).total_seconds() >= 2 * 3600)


def _claim_anchor(row):
    """Best known time boundary for the provider absence read.

    ``scheduled_at`` is display metadata and its write is deliberately best
    effort.  A calendar row can therefore own a real publish claim without it.
    The reservation day is written atomically with the claim and is the better
    anchor for catch-up rows; ``post_date`` is the legacy fallback.  Midnight
    UTC plus the caller's one-day lookback safely covers every supported gym
    timezone without inventing a precise send time.
    """
    reserved = str(row.get("publish_reservation_day") or "")[:10]
    if reserved:
        parsed = _dt(reserved + "T00:00:00Z")
        if parsed:
            return parsed
    scheduled = _dt(row.get("scheduled_at"))
    if scheduled:
        return scheduled
    day = str(row.get("post_date") or "")[:10]
    return _dt(day + "T00:00:00Z") if day else None


def _local_profile_id(gym, platform=None):
    """Legacy-volume fallback matching the publisher's account/base lookup."""
    try:
        from . import db
        base = str(gym or "").strip()
        suffix = "_fb" if str(platform or "").lower() == "facebook" else "_ig"
        for key in (base + suffix, base):
            row = db.gym_get(key) or {}
            value = row.get("zernio_profile_id")
            if value:
                return str(value)
        return None
    except Exception:
        return None


def reconcile(*, store=None, provider=None, kv=None, now=None, alert=None):
    """Reconcile stale Zernio claims fleet-wide without sending social posts."""
    if not (config.publish_enabled() and config.zernio_publish_enabled()):
        return {"published": [], "released": [], "held": [], "provider_reads": 0}
    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    if provider is None:
        from .zernio import ZernioClient
        provider = ZernioClient()
    if kv is None:
        from .calendar_autopublish import _kv_default
        kv = _kv_default()
    if alert is None:
        from .ops_alerts import alert
    now = _dt(now) or datetime.now(timezone.utc)
    result = {"published": [], "released": [], "held": [], "provider_reads": 0}
    try:
        rows = [r for r in (store.publishing_rows() or []) if _old_enough(r, kv, now)]
    except Exception as exc:  # a read failure is never absence evidence
        result["held"].append({"reason": f"calendar_read_{type(exc).__name__}"})
        return result

    grouped = defaultdict(list)
    for row in rows:
        rid, gym = str(row.get("id") or ""), str(row.get("gym_id") or "")
        token = str(row.get("publish_claim_token") or "")
        anchor = _claim_anchor(row)
        if not rid or not gym or not token or not anchor:
            result["held"].append({"id": rid, "reason": "missing_claim_identity"})
            continue
        # GBP gallery photos use /v1/accounts/{id}/gmb-media, not /v1/posts.
        # An empty posts list cannot prove that an accepted gallery upload is
        # absent. Retain its claim until an authoritative gallery readback or a
        # human verifier resolves it; never release it for another upload here.
        if (str(row.get("account") or "").lower() == "googlebusiness"
                and str(row.get("format") or "").lower() == "photo"):
            result["held"].append({"id": rid,
                                   "reason": "gallery_media_requires_readback"})
            continue
        # LASSO can use the direct Meta publisher. Zernio absence says nothing
        # about a direct send, so reconcile its rows only while the Zernio cutover
        # is the configured owner. Client gyms always use the Zernio lane.
        if gym == "lasso" and not config.lasso_via_zernio_enabled():
            result["held"].append({"id": rid, "reason": "non_zernio_publish_route"})
            continue
        if now - anchor > timedelta(days=MAX_ROW_AGE_DAYS):
            result["held"].append({"id": rid, "reason": "outside_provider_window"})
            continue
        try:
            profile = store.gym_zernio_profile_id(gym)
        except Exception:
            profile = None
        # Older client gyms published successfully from their durable worker
        # volume before the shared settings column existed.  The live publisher
        # still resolves those exact tenant-base rows, so the reconciler must do
        # the same or their stale claims can never be repaired.
        profile = profile or _local_profile_id(gym, row.get("account"))
        if not profile:
            result["held"].append({"id": rid, "reason": "no_tenant_profile_binding"})
            continue
        grouped[(gym, str(profile))].append((row, anchor))

    for (gym, profile), tenant_items in grouped.items():
        tenant_rows = [row for row, _anchor in tenant_items]
        start = min(anchor for _row, anchor in tenant_items) - timedelta(days=1)
        try:
            posts = provider.posts_range_complete(
                profile, start, now, page_limit=PAGE_SIZE, max_pages=MAX_PROVIDER_PAGES)
            result["provider_reads"] += 1
        except Exception as exc:
            for row in tenant_rows:
                result["held"].append({"id": row["id"],
                                       "reason": f"provider_read_{type(exc).__name__}"})
            continue
        for row in tenant_rows:
            state, evidence = classify(row, posts)
            try:
                if state == "live":
                    changed = store.reconcile_stale_published(
                        gym, row["id"], row["publish_claim_token"],
                        evidence["post_id"], evidence["published_at"])
                    bucket = "published" if changed else "held"
                    result[bucket].append({"id": row["id"], **evidence,
                                           **({} if changed else {"reason": "claim_changed"})})
                elif state == "absent":
                    changed = store.release_stale_publish_claim(
                        gym, row["id"], row["publish_claim_token"],
                        "provider-complete read proved no post exists; safe retry")
                    bucket = "released" if changed else "held"
                    result[bucket].append({"id": row["id"], **evidence,
                                           **({} if changed else {"reason": "claim_changed"})})
                else:
                    result["held"].append({"id": row["id"], **evidence})
            except Exception as exc:
                result["held"].append({"id": row["id"],
                                       "reason": f"calendar_write_{type(exc).__name__}"})
    if result["published"] or result["released"]:
        alert("stale publish claims reconciled from complete provider evidence: "
              f"{len(result['published'])} stamped published, "
              f"{len(result['released'])} released for retry")
    return result
