"""
Scheduled calendar AUTO-PUBLISHER.

Each daily cycle this reads THAT day's content_calendar rows for one gym and
publishes each unpublished row to the real IG/FB surface. It posts to LIVE
social, so the top priority is EXACTLY-ONCE: a row is published at most one time,
even across a re-run or a second concurrent worker.

Two hard gates guard it:
  1. AGENT_CALENDAR_AUTOPUBLISH (config.calendar_autopublish_enabled), default OFF.
  2. AGENT_PUBLISH_ENABLED (config.publish_enabled) — the global publish kill switch.
Either OFF => publish_due() returns {"ok": False, ...} and publishes NOTHING.

Exactly-once design (the claim):
  - due_rows() returns rows dated the run date only (never a past/future date),
    unpublished, with an image.
  - Before the network call, mark_publishing(id) ATOMICALLY flips status
    'pending' -> 'publishing' and returns True only if THIS call won the claim.
    A False means another run/worker already has it, so we SKIP.
  - On a real 'published' result, mark_published(id, media_id, now) records it.
  - A publisher exception or ordinary non-published result is ambiguous after the
    network boundary, so the owned claim stays 'publishing' for reconciliation.
    Only a pre-network 'would_publish' result (or an explicitly proven no-post
    rejection) reverts the claim for a safe retry. A row that already has
    published_at is NEVER re-published.

Nothing here logs a token or secret. The manual approval path is untouched.
"""

import inspect
import os
import re
from datetime import datetime, time, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from . import config
from . import meta_publisher
from .accounts import get_account
from .drafter import Draft, DraftStatus
from .media_types import is_video_url          # ONE video definition (audit D1)
from .summit_queue import SPRINT_SLOT_TIMES


# LEGACY IGFILL MEDIA (2026-10-04 hard block): legacy client_infographic_fill
# cards are hosted under paths like .../igfill_2026-09-10_<archetype>.png. Same
# pattern as client_media_sync.GENERATED_DERIVATIVE_PREFIXES and
# infographic_photo_maintenance._IGFILL. Separately branded Astra fallback media
# (no_media_/seed_) deliberately does NOT match this pattern and stays publishable.
_LEGACY_IGFILL_MEDIA = re.compile(r"(?:^|/)igfill_\d{4}-\d{2}-\d{2}(?:[_-]|\.)", re.I)


def _row_has_legacy_igfill_media(row):
    """True when the media the row would ACTUALLY ship points at legacy
    client_infographic_fill media (igfill_YYYY-MM-DD_...), regardless of pillar.
    Row-type-aware: a FEED row is judged by its FEED image_url ONLY -- a stale
    igfill provenance on source_media_url must NOT block a feed whose live
    image_url was already swapped to a client-real photo. A STORY row is judged
    by its source_media_url PLUS the actually-delivered story image_url."""
    from urllib.parse import urlparse

    def _legacy(url):
        url = str(url or "").strip()
        return bool(url) and bool(_LEGACY_IGFILL_MEDIA.search(urlparse(url).path))

    if _is_story_row(row):
        return _legacy(row.get("source_media_url")) or _legacy(row.get("image_url"))
    return _legacy(row.get("image_url"))


def _alert_confirmed(result):
    """True ONLY when ops_alerts.alert CONFIRMED delivery. alert() returns None
    when the flag is off, a gate suppressed the line, or the Slack post failed
    or raised -- none of those may KV-stamp a dedupe key, or one transient
    delivery failure would permanently suppress the notice (retry un-stamped).
    A Slack-style response dict with ok=False is also not a confirmed delivery."""
    if not result:
        return False
    if isinstance(result, dict) and result.get("ok") is False:
        return False
    return True


def _stamp_after_confirmed_alert(db, key, result):
    """KV-stamp a dedupe key only AFTER a confirmed delivered alert. Best
    effort; a stamp failure never blocks the lane (the alert itself already
    fired, and the repeat gate keeps any re-fire from storming)."""
    try:
        if _alert_confirmed(result):
            db.kv_set(key, "1")
    except Exception:
        pass


def _rail_fail_closed_alert(kind, row_id, gym_id, detail=""):
    """One deduped internal ops alert when a safety rail cannot evaluate for a row:
    the row is SKIPPED (fail-closed), and the rail failure must be visible to ops
    instead of silent. An alert failure never blocks the lane."""
    try:
        from . import db, ops_alerts
        key = f"rail_failclosed_{kind}_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        _stamp_after_confirmed_alert(db, key, ops_alerts.alert(
            f"{gym_id}: row {row_id} SKIPPED (fail-closed) — the {kind} safety "
            f"rail could not be evaluated ({detail or 'import/config error'}). "
            "A row is NEVER published past an unevaluable safety rail; fix the rail."))
    except Exception:
        pass  # an alert failure must never block the publish lane


def _legacy_igfill_blocked_alert(row_id, gym_id):
    """One deduped internal ops alert per row blocked by the legacy-igfill media
    hard block, so the media can be swapped for client-real media instead of
    silently stranding the slot. Fires for a legacy-media row EVEN when the
    pillar also carries a review suffix -- both hold reasons are true and the
    media swap alert must not be swallowed by the pillar block."""
    try:
        from . import db, ops_alerts
        key = f"legacy_igfill_blocked_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        _stamp_after_confirmed_alert(db, key, ops_alerts.alert(
            f"{gym_id}: row {row_id} BLOCKED at the publish boundary — it points "
            "at legacy client_infographic_fill media (igfill_YYYY-MM-DD_...), which "
            "must never auto-publish even when the pillar has no review suffix and "
            "even in approved/autonomous lanes. Swap in client-real media."))
    except Exception:
        pass  # an alert failure must never block the publish lane


def _now_iso(now=None):
    value = now if now is not None else datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _calendar_state_conflict_submission_key(gym_id, row_id):
    """One deterministic FIXER submission identity for one calendar state conflict.

    The key describes the durable domain event, not a transient exception string.
    Retrying the same provider-confirmed post therefore reaches the same FIXER ticket
    rather than opening another autonomous repair.
    """
    return str(uuid5(
        NAMESPACE_URL,
        f"lasso:fixer:calendar-published-state-conflict:v1:{gym_id}:{row_id}",
    ))


def _submit_published_state_conflict(*, gym_id, row, error,
                                     resolve_client_id=None, event_builder=None,
                                     sender=None):
    """Create a row-first FIXER ticket for a proven calendar state conflict.

    This is intentionally narrower than a publish failure.  It runs only after the
    provider returned ``published`` and the calendar store rejected its guarded
    ``publishing -> published`` transition with the store's explicit 409 conflict.
    Network, ledger, and provider uncertainty have no ticket path here.  The ticket
    transport is best effort and cannot alter the claimed calendar row.
    """
    try:
        from .portal_calendar_store import PortalStoreError
        if not isinstance(error, PortalStoreError) or error.status != 409:
            return False
        row_id = str((row or {}).get("id") or "")
        row_gym_id = str((row or {}).get("gym_id") or "")
        gym_id = str(gym_id or "")
        # ``due_rows`` is tenant-filtered in production.  Keep the assertion here so
        # an injected or future caller cannot bind a row from another tenant.
        if not row_id or not gym_id or row_gym_id != gym_id:
            return False
        if resolve_client_id is None:
            from .fixer_business_seed import resolve_portal_client_id
            resolve_client_id = resolve_portal_client_id
        if event_builder is None:
            from .fixer_business_seed_client import calendar_row_event, send
            event_builder = calendar_row_event
            if sender is None:
                sender = send
        if sender is None:
            return False
        client_id = resolve_client_id(gym_id)
        event = event_builder(
            submission_key=_calendar_state_conflict_submission_key(gym_id, row_id),
            client_id=client_id,
            row_id=row_id,
            expected_status="published",
        )
        return bool(getattr(sender(event), "ok", False))
    except Exception:  # noqa: BLE001 - intake failure must never corrupt calendar state
        return False


def _stable_hash(s):
    """A deterministic non-negative int from a string, stable across processes
    (Python's built-in hash() is salted per process, so it is NOT used here)."""
    import hashlib
    return int(hashlib.sha1(str(s).encode("utf-8")).hexdigest(), 16)


def slot_index_for_row(row, n=None):
    """
    The STABLE slot ordinal for ONE row, a deterministic function of the ROW
    ITSELF (its format + id) and NEVER of its position within the day's due set.
    So when an earlier row publishes and leaves due_rows, a remaining row's slot
    does NOT move earlier.

    Mapping (n = number of slot times, default len(SPRINT_SLOT_TIMES) = 3):
      - a STORY -> the middle slot (midday), so it lands after its paired feed.
      - a FEED  -> a non-middle slot chosen by a stable hash of its id, spread
        across the remaining (earlier + later) slots. With n=3 that is AM or PM.
    A single-slot config (n=1) collapses everyone onto slot 0.
    """
    if n is None:
        n = len(SPRINT_SLOT_TIMES)
    if n <= 1:
        return 0
    mid = n // 2
    fmt = (row.get("format") or "feed").strip().lower()
    if fmt == "story":
        return mid
    # Feed: pick from the non-middle slots by a stable hash so multiple feeds on
    # one day spread out (and never all land on the story's midday slot).
    non_mid = [i for i in range(n) if i != mid]
    return non_mid[_stable_hash(row.get("id")) % len(non_mid)]


def slot_time_for_row(row, n=None):
    """The STABLE "HH:MM" slot time for one row (see slot_index_for_row).

    2x CADENCE (CADENCE_SPEC.md D6): a FEED row stamped with a cadence slot_index
    (0 or 1 — written only by a 2x plan) gets the DETERMINISTIC pair from
    config.cadence_slot_times() (default 07:30 / 18:30) instead of the id-hash,
    which could collide both of a day's feeds onto one slot. Applies while
    ECHO_CADENCE_2X_ENABLED or LASSO's durable 3x cadence is armed; otherwise
    the pre-cadence hash path is unchanged. Durable LASSO Stories with a slot
    index follow their matching feed by 15 minutes."""
    fmt = (row.get("format") or "feed").strip().lower()
    si = row.get("slot_index")
    lasso_paired = (
        str(row.get("gym_id") or "").strip().lower() == "lasso"
        and (config.lasso_three_feed_enabled()
             or _lasso_summit_daily_enabled(row.get("gym_id"),
                                            row.get("post_date"))))
    if fmt == "story" and si in (0, 1, 2) and lasso_paired:
        feed_slot = config.cadence_slot_times()[int(si)] if si in (0, 1) else "12:00"
        hour, minute = (int(part) for part in feed_slot.split(":"))
        # A configured late feed must never wrap its Story to the next day's
        # early hours, where a same-day slot comparison would send it early.
        story_minute = min(hour * 60 + minute + 15, 23 * 60 + 59)
        return f"{story_minute // 60:02d}:{story_minute % 60:02d}"
    # LASSO Summit daily runway: the extra FEED owns a third, distinct local
    # slot.  Scope this by both tenant and the row's explicit calendar day so
    # enabling the campaign cannot change clients or spill beyond its window.
    # The Summit-only third Story follows its noon feed at 12:15.
    if (fmt == "feed" and si == 2
            and _lasso_three_feed_enabled(row.get("gym_id"),
                                          row.get("post_date"))):
        return "12:00"
    if (fmt == "feed" and si in (0, 1)
            and (config.cadence_2x_enabled() or lasso_paired)):
        return config.cadence_slot_times()[int(si)]
    if n is None:
        n = len(SPRINT_SLOT_TIMES)
    if not SPRINT_SLOT_TIMES:
        return "00:00"
    return SPRINT_SLOT_TIMES[slot_index_for_row(row, n) % len(SPRINT_SLOT_TIMES)]


def _lasso_summit_daily_enabled(account_key, day_key):
    """Fail-closed adapter for the LASSO-only, date-scoped third feed flag."""
    if str(account_key or "").strip().lower() != "lasso" or not day_key:
        return False
    enabled = getattr(config, "lasso_summit_daily_enabled", None)
    if not callable(enabled):
        return False
    try:
        return bool(enabled(str(day_key)[:10]))
    except (TypeError, ValueError):
        return False


def _lasso_three_feed_enabled(account_key, day_key):
    """LASSO's durable third feed, with the dated Summit flag as compatibility."""
    if str(account_key or "").strip().lower() != "lasso" or not day_key:
        return False
    try:
        if config.lasso_three_feed_enabled():
            return True
    except Exception:
        pass
    return _lasso_summit_daily_enabled(account_key, day_key)


def _publish_capacity(gym_id, row, store, local_claim_day):
    """Effective atomic publish capacity for this row on its actual local day."""
    from .cadence import resolve_posts_per_day
    try:
        capacity = resolve_posts_per_day(gym_id, store, day=local_claim_day)
    except TypeError:
        # Compatibility for narrow injected resolvers predating the dated API.
        # The production resolver accepts `day` and always takes the path above.
        capacity = resolve_posts_per_day(gym_id, store)
    fmt = (row.get("format") or "feed").strip().lower()
    is_feed = fmt == "feed"
    row_day = str(row.get("post_date") or "")[:10]
    if (str(gym_id or "").strip().lower() == "lasso"
            and fmt in ("feed", "story")
            and _lasso_immediate_backlog_day(local_claim_day)
            and _lasso_three_feed_enabled(gym_id, local_claim_day)
            and (row_day == local_claim_day
                 or ("2026-10-02" <= row_day <= "2026-10-05"
                     and row_day < local_claim_day))):
        # The RPC independently enforces 3 current + 12 backlog rows per
        # account and format on the actual local publish day (Oct 5-6 only).
        # Backlog is strictly before the publish day, so Oct 5 is never both.
        return 15
    if (str(gym_id or "").strip().lower() == "lasso"
            and fmt in ("feed", "story")
            and _lasso_incident_catchup_day(local_claim_day)
            and _lasso_three_feed_enabled(gym_id, local_claim_day)
            and str(row.get("post_date") or "")[:10] in
                {local_claim_day, "2026-10-02", "2026-10-03",
                 "2026-10-04", "2026-10-05"}):
        # The RPC independently enforces 3 current + 2 outage rows per account
        # and format on the actual local publish day. This is not a general 5x.
        return 5
    # Both the durable cadence and the dated Summit cadence pair each feed
    # with a Story, so their publish capacity must agree with the planner.
    if (fmt in ("feed", "story")
            and _lasso_three_feed_enabled(gym_id, local_claim_day)):
        return max(capacity, 3)
    return capacity if is_feed else min(capacity, 2)


def _lasso_immediate_backlog_day(day):
    return "2026-10-05" <= str(day or "")[:10] <= "2026-10-06"


def _lasso_incident_catchup_day(day):
    return "2026-10-06" <= str(day or "")[:10] <= "2026-10-11"


def _client_publish_limits(gym_id, run_date, configured_cap):
    """Keep LASSO's feed/Story pairs whole without changing client caps."""
    if (str(gym_id or "").strip().lower() != "lasso"
            or not _lasso_three_feed_enabled(gym_id, run_date)):
        return CLIENT_CATCHUP_DAYS, configured_cap
    if _lasso_immediate_backlog_day(run_date):
        # Oct 2 remains in the query through Oct 6. The RPC gates the twelve
        # extra backlog pairs per account, format and actual local day, and
        # never counts an Oct 5 current-day row as backlog on Oct 5.
        from datetime import date as _date
        lookback = (_date.fromisoformat(str(run_date)[:10])
                    - _date(2026, 10, 2)).days
        return max(CLIENT_CATCHUP_DAYS, lookback), 50
    if _lasso_incident_catchup_day(run_date):
        # Oct 2 remains in the query through Oct 11. The RPC gates the two
        # extra outage pairs per account, format and actual local day.
        from datetime import date as _date
        lookback = (_date.fromisoformat(str(run_date)[:10])
                    - _date(2026, 10, 2)).days
        return max(CLIENT_CATCHUP_DAYS, lookback), 20
    # Three feed and three paired Story posts on each of IG and FB.
    return CLIENT_CATCHUP_DAYS, 12


def _paired_lasso_feed_published(story, store):
    """Require one exact, live IG feed before its durable paired Story.

    A logical_post_id is authoritative when present. Older rows without one
    use the same date, account and slot index; ambiguity or a failed complete
    read holds the Story. A publishing claim alone never counts as delivery.
    """
    if (story.get("gym_id") != "lasso"
            or (story.get("format") or "").strip().lower() != "story"
            or story.get("slot_index") not in (0, 1, 2)
            or not story.get("post_date")):
        return False
    logical_id = story.get("logical_post_id")
    try:
        if logical_id:
            rows = store.list_active_logical_post_rows("lasso", logical_id)
        else:
            day = str(story["post_date"])[:10]
            rows = store.rows_in_range_complete("lasso", day, day)
    except Exception:
        return False
    if not isinstance(rows, list):
        return False
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("gym_id") == "lasso"
               and str(row.get("account") or "").strip().lower() ==
                   str(story.get("account") or "").strip().lower()
               and (row.get("format") or "feed").strip().lower() == "feed"
               and str(row.get("post_date") or "")[:10] ==
                   str(story["post_date"])[:10]
               and row.get("slot_index") == story["slot_index"]
               and (row.get("variant_status") or "active") == "active"
               and (not logical_id or row.get("logical_post_id") == logical_id)]
    return (len(matches) == 1
            and matches[0].get("status") == "published"
            and bool(matches[0].get("published_at"))
            and matches[0].get("late_post_id") is not None)


def _paired_lasso_story_prepared(feed, store):
    """Fail closed unless the DB proves one exact usable Story for this feed."""
    try:
        reader = getattr(store, "lasso_paired_story_ready_for_feed")
        return reader(feed["id"]) is True
    except Exception:
        return False


# The outgoing source fields a leased paired LASSO feed must still share with
# the local due_rows snapshot after the owned claim (see publish_due). The
# claim RPC pins OWNERSHIP, not outgoing content: caption/media patched while
# pending between the paired-Story proof and the claim would otherwise send
# content the prepared Story was never bound to.
_PAIRED_FEED_SOURCE_FIELDS = (
    "caption", "image_url", "source_media_url", "source_media_asset_id",
    "thumbnail_url", "account", "format", "post_date", "slot_index",
    "logical_post_id", "pillar", "scheduled_at", "media_not_ready_reason")


def assign_slots(rows):
    """
    Given a day+account's content_calendar rows, return [(row, slot_time)] where
    each row's slot is its OWN stable slot (slot_time_for_row), independent of the
    other rows present. Order the result by slot then id for a stable read.
    """
    out = [(row, slot_time_for_row(row)) for row in rows]
    out.sort(key=lambda pair: (pair[1], str(pair[0].get("id") or "")))
    return out


def _local_now(now=None, tz_name=None):
    """
    Current wall-clock time in tz_name (default config.POSTING_TIMEZONE) as a
    timezone-aware datetime. `now` is injectable for tests: pass an ISO string or a
    datetime. Never uses Date.now-style nondeterminism when `now` is supplied.
    tz_name is the PER-GYM posting timezone (Blake 2026-08-25): a Denver gym's slots
    are Denver wall-clock, not Eastern.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name or config.POSTING_TIMEZONE)
    if now is None:
        return datetime.now(tz)
    if isinstance(now, datetime):
        dt = now
    else:
        dt = datetime.fromisoformat(str(now))
    # Compare in the posting timezone. A naive `now` is read AS local time; an
    # aware `now` is converted into it.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _slot_reached(slot_time, now=None, tz_name=None):
    """True when the wall-clock "HH:MM" slot_time (in the gym's posting timezone) is
    <= the current local time. `now` is injectable (see _local_now)."""
    local = _local_now(now, tz_name)
    hh, mm = str(slot_time).split(":")
    slot = time(int(hh), int(mm))
    return local.timetz().replace(tzinfo=None) >= slot


def is_due(row, now=None, tz_name=None):
    """
    True when a ROW's OWN stable slot time (slot_time_for_row) is <= the current
    local time IN THE GYM'S POSTING TIMEZONE (tz_name; default the global one).
    Compares the row's own slot, so a row is NEVER published before its slot and its
    slot never moves when a sibling publishes. `now` is injectable.
    """
    return _slot_reached(slot_time_for_row(row), now, tz_name)


def _account_for(row, gym_id="lasso"):
    """Map a content_calendar row to its Echo account. The row's `account` column is a
    bare platform ('instagram'/'facebook'); the GYM comes from gym_id. LASSO keeps its
    hardcoded accounts (byte-for-byte the original behavior). A CLIENT gym resolves to
    ITS OWN account (<gym_id>_fb / <gym_id>_ig) so a client post never lands on LASSO's
    pages. Returns None when no such account exists (the row is then skipped, never
    misrouted)."""
    plat = (row.get("account") or "").strip().lower()
    # BELT-AND-SUSPENDERS (Dale/ENG 2026-08-22): this is the IG/FB lane. A row for any
    # OTHER platform (googlebusiness, published by its own worker) must be SKIPPED, never
    # silently mapped to _ig. Previously any non-'facebook' account fell through to _ig,
    # so a googlebusiness row posted its Google caption to Instagram.
    if plat not in ("instagram", "facebook"):
        return None
    base = (gym_id or "lasso").strip() or "lasso"
    if base == "lasso":
        return get_account("lasso_fb" if plat == "facebook" else "lasso_ig")
    suffix = "_fb" if plat == "facebook" else "_ig"
    return get_account(f"{base}{suffix}")


def _reburn_stale_story(row, account, store):
    """A due, approved STORY whose burned media no longer carries its CURRENT (edited)
    caption is re-burned NOW from its raw source_media_url and its image_url swapped, so an
    edited-caption story still posts instead of stranding silently forever (Dale/ENG
    2026-08-22: an edited story caption held on 'approved' and never published). Returns the
    updated row (fresh image_url that carries the caption) on success, else None (the caller
    then holds + alerts, as before). Best-effort: never raises. Requires source_media_url on
    the row (story_reburn.should_reburn); rows built before AGENT_STORY_SOURCE_MEDIA lack it
    and still hold, but every new build persists it so edited stories self-heal at publish."""
    try:
        from . import story_reburn
        if not story_reburn.should_reburn(row):
            return None
        name = (getattr(account, "display_name", "") or "").strip()
        for suf in (" IG", " FB", " Instagram", " Facebook", " Facebook Page"):
            if name.endswith(suf):
                name = name[: -len(suf)].strip()
        from . import visual_writer_prepare
        receipt_evidence = None
        if visual_writer_prepare.enabled():
            reburned = story_reburn.reburn_with_evidence(
                row.get("source_media_url"), row.get("caption") or "", name, account.key)
            if not isinstance(reburned, tuple) or len(reburned) != 2:
                return None
            new_url, receipt_evidence = reburned
        else:
            new_url = story_reburn.reburn(row.get("source_media_url"), row.get("caption") or "",
                                          name, account.key)
        if not new_url:
            return None
        patch = getattr(store, "patch_image_url", None)
        if patch is None:
            return None
        patch_args = {"expected_row": row}
        if receipt_evidence is not None:
            patch_args["render_evidence"] = receipt_evidence.as_dict()
        persisted = patch(row.get("gym_id"), row.get("id"), new_url, **patch_args)
        # The store may reject this update (for example, its status filter may
        # exclude an approved row). Do not publish media that only changed in
        # this in-memory copy; the next tick would still see the stale URL.
        if (not isinstance(persisted, dict)
                or persisted.get("id") != row.get("id")
                or persisted.get("gym_id") != row.get("gym_id")
                or persisted.get("status") != row.get("status")
                or persisted.get("caption") != row.get("caption")
                or persisted.get("source_media_url") != row.get("source_media_url")
                or persisted.get("image_url") != new_url
                or persisted.get("published_at") is not None
                or persisted.get("late_post_id") is not None):
            return None
        updated = dict(row)
        updated["image_url"] = new_url
        return updated
    except Exception as e:  # noqa: BLE001 - a self-heal must never crash the publish lane
        print(f"[calendar-autopublish] story self-heal reburn failed for "
              f"{row.get('id')}: {type(e).__name__}: {e}")
        return None


def _normalize_feed_image(row, account, store):
    """PUBLISH-TIME aspect preflight for a FEED row (ENG/Dale 2026-08-24: raw portrait
    client photos at ratio 0.56-0.67 were REJECTED by Zernio with a 400 'Aspect ratio
    outside Instagram's allowed range (0.75 to 1.91)', so every publish reverted to
    'approved' and the post stranded forever while the portal still read 'Published').

    If the row's hosted image is outside IG/FB's accepted feed ratio, re-frame it into an
    in-spec 1080x1350 card, re-host, and swap image_url so the platform accepts it. Works
    from the hosted url alone (fetch -> reframe -> host), so it heals rows built before
    AGENT_FEED_AUTOFIT.

    Return contract (fail SAFE, never fail-open onto a KNOWN-bad image):
      - the row (unchanged or with a swapped in-spec image_url) => OK to publish. This is
        the common case: video, in-spec image, or an unknown aspect we could not determine
        (hosting off, fetch failed) — those pass through as before and self-heal next tick.
      - None => HOLD: the image is CONFIRMED out-of-aspect and we could NOT re-frame/re-host
        it, so publishing it now would 400 at Zernio and strand the row. The caller leaves it
        approved (never claimed) and alerts, instead of shipping a known-bad image. This is
        the fix for the auditor's fail-open gap: once we KNOW it is bad, we never send it.
    Best effort: never raises. Feed only; a story is framed by its own burner."""
    url = (row.get("image_url") or "").strip()
    if not url or is_video_url(url):
        return row                                        # video/no-image: not our job
    # PHASE 1 (fail-open): determine the aspect. If we cannot even read it, pass through
    # unchanged (unknown, not known-bad) — identical to the historical behavior; it will
    # self-heal on a later tick once R2/hosting recovers.
    try:
        from . import feed_image, media_host, visual_writer_prepare
        if not config.hosting_enabled():
            return row
        evidence_enabled = visual_writer_prepare.enabled()
        # Receipt mode reads the exact bounded object, including its query,
        # through the same reader used by preparation.
        img = (visual_writer_prepare._bytes_for_url(url) if evidence_enabled
               else media_host.download_bytes(url))
        if not img:
            return row
        import io
        from PIL import Image
        with Image.open(io.BytesIO(img)) as im:
            w, h = im.size
        if not feed_image.needs_autofit(w, h):
            return row                                    # already in-spec: publish as-is
    except Exception as e:  # noqa: BLE001 - could not determine aspect -> fail open
        print(f"[calendar-autopublish] feed preflight could not read aspect for "
              f"{row.get('id')}: {type(e).__name__}: {e}; posting as-is")
        return row
    # PHASE 2 (fail-safe): the image is CONFIRMED out-of-aspect. Re-frame + re-host, or HOLD.
    safe = out = None
    try:
        import tempfile
        import hashlib
        import uuid
        fd, out = tempfile.mkstemp(prefix="feedfit_", suffix="__feed.jpg")
        os.close(fd)
        safe = out
        safe = feed_image.make_feed_safe_from_bytes(img, out)
        rendered_bytes = None
        if evidence_enabled:
            if not safe or os.path.getsize(safe) > visual_writer_prepare.MAX_VISUAL_BYTES:
                return None
            with open(safe, "rb") as rendered:
                rendered_bytes = rendered.read()
            if (not rendered_bytes
                    or visual_writer_prepare._bytes_for_url(url) != img):
                return None
        hosted = media_host.host_media(safe, account.key) if safe else None
        if not hosted or hosted == url:
            print(f"[calendar-autopublish] feed preflight could NOT re-host an out-of-aspect "
                  f"image for row {row.get('id')} ({w}x{h}); HOLDING (not sending a 400).")
            return None                                   # HOLD: never ship a known-bad image
        patch = getattr(store, "patch_image_url", None)
        if patch is None:
            return None
        patch_args = {"expected_row": row}
        if evidence_enabled:
            delivered_bytes = visual_writer_prepare._bytes_for_url(hosted)
            if delivered_bytes != rendered_bytes:
                return None
            patch_args["render_evidence"] = {
                "source_exact_url": url, "delivered_exact_url": hosted,
                "source_fingerprint": "md5:" + hashlib.md5(img).hexdigest(),
                "delivered_fingerprint": "md5:" + hashlib.md5(delivered_bytes).hexdigest(),
                "source_byte_length": len(img), "delivered_byte_length": len(delivered_bytes),
                "operation": "rehost", "evidence_ref": "feed_autofit:" + str(uuid.uuid4()),
                "observed_by": "calendar_autopublish.feed_autofit",
                "rendered_by": "calendar_autopublish.feed_autofit",
            }
        persisted = patch(row.get("gym_id"), row.get("id"), hosted, **patch_args)
        source = (row.get("source_media_url") or url) if evidence_enabled else row.get("source_media_url")
        stable = ("id", "gym_id", "status", "format", "caption", "account", "post_date",
                  "source_media_asset_id", "drive_file_id", "variant_status",
                  "scheduled_at", "slot_index", "publish_claim_token", "publish_reservation_day")
        required = ("id", "gym_id", "status", "format", "caption", "account", "post_date",
                    "image_url", "source_media_url", "published_at", "late_post_id",
                    "media_not_ready_reason")
        if (not isinstance(persisted, dict)
                or any(key not in persisted for key in required)
                or any(persisted.get(key) != row.get(key) for key in stable)
                or persisted.get("source_media_url") != source
                or persisted.get("image_url") != hosted
                or persisted.get("published_at") is not None
                or persisted.get("late_post_id") is not None
                or persisted.get("media_not_ready_reason") is not None):
            return None
        if evidence_enabled and (
                persisted.get("byte_hash") != "derived:" + patch_args["render_evidence"]["delivered_fingerprint"]
                or not isinstance(persisted.get("visual_group_key"), str)
                or not persisted["visual_group_key"].startswith("vg_")
                or (row.get("visual_group_key")
                    and persisted["visual_group_key"] != row["visual_group_key"])):
            return None
        print(f"[calendar-autopublish] feed preflight reframed out-of-aspect image for "
              f"row {row.get('id')} ({w}x{h}) -> in-spec 1080x1350")
        return persisted
    except Exception as e:  # noqa: BLE001 - known-bad image + reframe failed -> HOLD
        print(f"[calendar-autopublish] feed preflight failed to fix out-of-aspect row "
              f"{row.get('id')}: {type(e).__name__}: {e}; HOLDING (not sending a 400).")
        return None
    finally:
        for path in {safe, out} - {None}:
            try:
                os.remove(path)
            except OSError:
                pass


def _alert_feed_needs_reframe(row_id, gym_id):
    """Alert a human ONCE when a feed row is held because its image is confirmed out-of-aspect
    and could not be re-framed/re-hosted this tick (so we refuse to ship a known-400 image).
    Deduped in the shared kv so the ~1-min retry does not spam. Best effort; never raises."""
    try:
        from . import db, ops_alerts
        key = f"feed_reframe_alerted_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        db.kv_set(key, "1")
        ops_alerts.alert(
            f"calendar row {row_id} (gym {gym_id}) is HELD: its feed image is outside "
            "Instagram's accepted aspect ratio and Echo could not re-frame/re-host it this "
            "run (likely a transient hosting/R2 issue). It will retry and self-heal, but if "
            "it persists a human should check the source image / hosting.")
    except Exception:
        pass


def _pub_count_today(gym_id, run_date):
    """How many rows this gym has already published today (kv counter). Best effort -> 0."""
    try:
        from . import db
        return int(db.kv_get(f"clientpub_{gym_id}_{run_date}") or 0)
    except Exception:
        return 0


def _bump_pub_count(gym_id, run_date):
    """Increment the per-gym per-day publish counter (kv). Best effort; never raises."""
    try:
        from . import db
        key = f"clientpub_{gym_id}_{run_date}"
        db.kv_set(key, str(int(db.kv_get(key) or 0) + 1))
    except Exception:
        pass


def _alert_daily_cap_hit(gym_id, run_date):
    """ONE ops alert per gym per DAY, the FIRST time AGENT_CLIENT_DAILY_PUBLISH_CAP
    throttles that gym (DEFECT 3, audit 2026-08-30): the cap silently left rows in
    'waiting' with nothing telling anyone, so at 100 gyms a gym recovering from a
    stall sat invisibly throttled. Deduped in kv per (gym, run_date) so the ~1-min
    retry cadence never storms — same stamp shape as onboarding_watch.run's `stamp`
    (a kv.get check, then kv.set after alerting). A new run_date re-arms it."""
    try:
        from . import db, ops_alerts
        key = f"dailycap_alerted_{gym_id}_{run_date}"
        if db.kv_get(key):
            return
        db.kv_set(key, "1")
        ops_alerts.alert(
            f"{gym_id}: hit its daily publish cap for {run_date}. The rest of "
            "today's due rows are held (not lost) and will drip out on later "
            "days; this is expected during a stall recovery, but flag it if "
            "the gym should never be capped this low.")
    except Exception:
        pass  # an alert failure must never block the publish lane


def _alert_slot_capacity_hit(gym_id, row, count, limit):
    """Tell ops once per gym/day/platform/format when approved rows exceed cadence."""
    try:
        from . import db, ops_alerts
        day = str(row.get("post_date") or "")[:10]
        platform = str(row.get("account") or "").lower()
        fmt = str(row.get("format") or "feed").lower()
        key = f"slotcap_alerted_{gym_id}_{day}_{platform}_{fmt}"
        if db.kv_get(key):
            return
        db.kv_set(key, "1")
        ops_alerts.alert(
            f"{gym_id}: approved {platform}/{fmt} calendar rows exceed the "
            f"{day} cadence ({count}/{limit} already publishing or published). "
            "Extra rows are held for calendar review, not sent.")
    except Exception:  # noqa: BLE001 - alerting cannot bypass the hold
        pass


def scheduled_iso_for_row(row, now=None, tz_name=None):
    """The ISO8601 go-live timestamp for a row: its post_date at its OWN stable slot
    time (slot_time_for_row), in the GYM'S posting timezone (tz_name; default the
    global POSTING_TIMEZONE). This is the display stamp the client sees for exactly
    when the post publishes. Returns '' when the row has no post_date."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo
    post_date = (row.get("post_date") or "").strip()
    if not post_date:
        return ""
    slot = slot_time_for_row(row)
    hh, mm = str(slot).split(":")
    try:
        y, m, d = (int(x) for x in post_date.split("-"))
        tz = ZoneInfo(tz_name or config.POSTING_TIMEZONE)
        return datetime(y, m, d, int(hh), int(mm), tzinfo=tz).isoformat()
    except (ValueError, TypeError):
        return ""


def _is_story_row(row):
    """True when the row is a STORY (framed by its own burner, not the feed preflight)."""
    return (row.get("format") or "feed").strip().lower() == "story"


def _story_media_is_stale(row):
    """True when this row is a STORY whose rendered media does NOT carry the row's
    CURRENT caption (the caption was edited after the media was burned), so publishing
    it would ship a stale/blank story. False for a feed row, a story with a matching
    caption, or a story whose media was not caption-burned by story_image (raw baseline).
    A raw same-object Story under the armed visual writer and Story-format guards is
    stale by definition; an error evaluating those armed guards fails closed. Never
    raises (a guard must never crash the lane)."""
    if (row.get("format") or "feed").strip().lower() != "story":
        return False
    source_url = str(row.get("source_media_url") or "").strip()
    image_url = str(row.get("image_url") or "").strip()
    same_object = bool(source_url and source_url == image_url)
    if same_object:
        # A visual-writer receipt may attest a raw same-object story, but that
        # provenance cannot prove the current caption was burned into the media.
        # With Story formatting enabled, re-burn it before the publisher boundary.
        try:
            from . import visual_writer_prepare
            if config.story_format_enabled() and visual_writer_prepare.enabled():
                return True
        except Exception:
            # A guard evaluator/import/config error must not turn an explicitly
            # armed raw Story into a publishable one. Read the direct flags only as
            # a conservative fallback for this exceptional path; ordinary flag-off
            # rows retain the legacy filename evaluator below.
            story_format = os.environ.get("AGENT_STORY_FORMAT", "").lower()
            writer_guard = os.environ.get("AGENT_VISUAL_GLOBAL_WRITER_PREP", "").lower()
            if (story_format in ("1", "true", "yes", "on")
                    and writer_guard in ("1", "true", "yes", "on")):
                return True
    try:
        from . import story_image
        return not story_image.story_media_carries_caption(
            row.get("image_url") or "", row.get("caption") or "")
    except Exception:
        return False  # legacy filename guard remains fail-open when unarmed


def _alert_story_needs_render(row_id, gym_id):
    """One ops alert per row when a story is HELD because its saved caption is not on
    its media yet (needs a build re-render). Deduped in the shared kv so a repeated
    tick never storms the channel."""
    try:
        from . import db, ops_alerts
        key = f"story_stale_alerted_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        db.kv_set(key, "1")
        ops_alerts.alert(
            f"{gym_id}: story {row_id} held — its saved caption is not on the media "
            "yet. It publishes once the calendar rebuild re-renders the story with the "
            "new caption (never shipped captionless).")
    except Exception:
        pass  # an alert failure must never block the publish lane


def _alert_meta_leak_held(row_id, gym_id):
    """One deduped ops alert per row when a due caption is HELD because it is ALL
    edit-rationale scaffolding (a bracketed [why]/[reason] block with no real caption
    body left to publish). Stripping would ship an empty caption, so a human must
    rewrite it. Deduped in the shared kv so the ~1-min retry never storms."""
    try:
        from . import db, ops_alerts
        key = f"metaleak_held_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        db.kv_set(key, "1")
        ops_alerts.alert(
            f"{gym_id}: row {row_id} HELD at the publish boundary — its caption is "
            "only an internal edit-rationale block ([why]/[reason] scaffolding) with "
            "no real caption body to publish. A human needs to rewrite the caption; "
            "it retries every tick once fixed.")
    except Exception:
        pass  # an alert failure must never block the publish lane


def _note_meta_stripped(row_id, gym_id):
    """One deduped ops notice per row when the final gate SELF-HEALED a caption by
    stripping a trailing edit-rationale block before the network call (the CrossFit
    ENG '[why]' leak class). The post still publishes — this is visibility, not a
    hold — so a human can trace which lane keeps producing scaffolding."""
    try:
        from . import db, ops_alerts
        key = f"metaleak_stripped_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        db.kv_set(key, "1")
        ops_alerts.alert(
            f"{gym_id}: row {row_id} carried an internal edit-rationale block "
            "([why]/[reason] scaffolding) after the real caption. Echo stripped it "
            "at the publish boundary and published the clean caption body.")
    except Exception:
        pass  # an alert failure must never block the publish lane


def _alert_meta_reapproval_held(row_id, gym_id, persisted):
    """Tell ops why proof-gated cleanup did not publish this approved row."""
    try:
        from . import db, ops_alerts
        key = f"metaleak_reapproval_{gym_id}_{row_id}"
        if not db.kv_is_durable():
            print(f"[calendar-autopublish] row {row_id}: meta reapproval alert "
                  "suppressed because KV is not durable")
            return
        if db.kv_get(key):
            return
        detail = (
            "Echo removed the internal block and reset the row to pending"
            if persisted else
            "Echo could not safely persist the cleaned caption"
        )
        result = ops_alerts.alert(
            f"{gym_id}: row {row_id} HELD before publish because removing its "
            "[why]/[reason] edit-rationale block changes the approved creative. "
            f"{detail}. A human must review and approve the cleaned caption before "
            "it can publish.")
        if not _alert_confirmed(result):
            return
        db.kv_set(key, "1")
    except Exception:
        pass  # an alert failure must never weaken the publish hold


def _alert_meta_autonomous_cleanup_held(row_id, gym_id):
    """Report a refused autonomous cleanup without claiming human review is due."""
    try:
        from . import db, ops_alerts
        key = f"metaleak_auto_cleanup_held_{gym_id}_{row_id}"
        if not db.kv_is_durable():
            print(f"[calendar-autopublish] row {row_id}: autonomous cleanup alert "
                  "suppressed because KV is not durable")
            return
        if db.kv_get(key):
            return
        result = ops_alerts.alert(
            f"{gym_id}: row {row_id} HELD before publish because its internal "
            "[why]/[reason] block could not be cleaned under the current "
            "autonomy and exact-caption check. No post was sent.")
        if _alert_confirmed(result):
            db.kv_set(key, "1")
    except Exception:
        pass


_META_REAPPROVAL_REQUIRED = object()
_CAPTION_REAPPROVAL_REQUIRED = object()


def _strip_or_hold_meta(row, gym_id, store, *, autonomous_lane=False):
    """FINAL GATE for internal edit-rationale blocks (CrossFit ENG live FB post,
    2026-08-23 00:02 ET: a caption published ending with '[why] Removed word parents
    and added people ...'). Runs UNCONDITIONALLY on every row about to publish —
    unlike the publish_guard recheck this is not behind AGENT_CALENDAR_GRADE, because
    this class of leak must never be publishable under any flag combination.

    With durable approval proof armed, stripping changes digest-bound creative. The
    cleaned caption is therefore persisted through the current-mode Manual RPC,
    which checks the exact status and caption, clears proof, and resets the row to
    pending. The row is held for fresh human approval even when the patch succeeds.
    A missing, stale, or failed patch also holds. With proof disabled, the
    legacy status-preserving self-heal is retained. An all-meta caption
    returns None for the existing rewrite hold."""
    from . import post_quality
    body, meta = post_quality.split_meta_suffix(row.get("caption") or "")
    if not meta:
        return row
    if not (body or "").strip():
        return None
    from .copy_gate import format_caption
    try:
        body = format_caption(body)
    except ValueError:
        return None
    if config.approval_proof_enabled():
        if autonomous_lane:
            try:
                cleaner = getattr(store, "patch_caption_autonomous_clean", None)
                patched = (cleaner(row.get("gym_id") or gym_id, row.get("id"),
                                   row.get("status"), row.get("caption"), body)
                           if callable(cleaner) else None)
            except Exception as e:  # noqa: BLE001 - no persisted cleanup, no send
                patched = None
                print(f"[calendar-autopublish] autonomous meta cleanup failed for "
                      f"{row.get('id')}: {type(e).__name__}: {e}")
            if patched is not None:
                _note_meta_stripped(row.get("id"), gym_id)
                return patched
            _alert_meta_autonomous_cleanup_held(row.get("id"), gym_id)
            return _META_REAPPROVAL_REQUIRED
        persisted = False
        try:
            patcher = getattr(store, "patch_caption_manual_format", None)
            if callable(patcher):
                persisted = patcher(row.get("gym_id") or gym_id,
                                    row.get("id"), row.get("status"),
                                    row.get("caption"), body) is not None
        except Exception as e:  # noqa: BLE001 - the hold remains fail closed
            print(f"[calendar-autopublish] proof meta-strip caption patch failed for "
                  f"{row.get('id')}: {type(e).__name__}: {e}")
        _alert_meta_reapproval_held(row.get("id"), gym_id, persisted)
        return _META_REAPPROVAL_REQUIRED
    patched = None
    try:
        patcher = getattr(store, "patch_caption_preserve_status", None)
        if patcher is not None:
            patched = patcher(row.get("gym_id") or gym_id, row.get("id"), body)
    except Exception as e:  # noqa: BLE001 - persistence is best effort here
        print(f"[calendar-autopublish] meta-strip caption patch failed for "
              f"{row.get('id')}: {type(e).__name__}: {e}")
    row = dict(patched or row)
    row["caption"] = body   # what we SEND is clean even when the patch failed
    _note_meta_stripped(row.get("id"), gym_id)
    return row


def _note_caption_formatted(row_id, gym_id):
    """One deduped ops notice per row when the publish boundary SELF-HEALED a legacy
    approved caption by auto-formatting it (semicolons -> commas). The post still
    publishes — this is visibility, not a hold."""
    try:
        from . import db, ops_alerts
        key = f"capformat_healed_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        _stamp_after_confirmed_alert(db, key, ops_alerts.alert(
            f"{gym_id}: row {row_id} carried a pre-copy-rail caption with "
            "semicolons. Echo auto-formatted it at the publish boundary "
            "(semicolons became commas and sentence gaps were standardized). "
            "The caption is ready for the remaining "
            "publish checks."))
    except Exception:
        pass  # an alert failure must never block the publish lane


def _alert_caption_format_held(row_id, gym_id):
    """One deduped ops alert per row when a due caption is HELD at the publish
    boundary because it cannot be safely auto-formatted. A Story may already
    have the words burned into its media, or a URL may contain the semicolon.
    A human must correct the copy and media; the row retries once fixed."""
    try:
        from . import db, ops_alerts
        key = f"capformat_held_{gym_id}_{row_id}"
        if db.kv_get(key):
            return
        _stamp_after_confirmed_alert(db, key, ops_alerts.alert(
            f"{gym_id}: row {row_id} HELD at the publish boundary — its caption "
            "cannot be safely auto-formatted. Check protected URL semicolons, "
            "put numbered list items on separate lines, and re-render Story media if applicable; "
            "the row retries once fixed."))
    except Exception:
        pass  # an alert failure must never block the publish lane


def _format_caption_at_publish(row, gym_id, store):
    """Format legacy non-Story copy at the final publish boundary.

    Approved rows and media-held rows are excluded from the bulk correction,
    but may later reach this lane with old sentence spacing. Format the exact
    caption sent to the network. With approval proof enabled, persist through a
    current-mode guarded RPC: a Manual edit invalidates proof and returns the
    row to pending review; an Auto edit clears stale proof while preserving its
    autonomous path. Story text may be burned into media, so a Story with a semicolon
    holds for correction rather than silently changing its saved caption.
    A protected URL containing a semicolon also holds instead of being damaged.
    Ambiguous inline numbered lists wait for items to be placed on separate
    lines, preserving copy and approval rather than guessing sentence ends.
    """
    caption = row.get("caption") or ""
    if _is_story_row(row):
        # A Story caption may already be burned into the image or video. Never
        # send the old media while its text still contains a semicolon.
        return None if ";" in caption else row
    from .copy_gate import format_caption
    try:
        clean = format_caption(caption, reject_ambiguous_lists=True)
    except ValueError:
        return None
    if clean == caption:
        return row
    patched = None
    try:
        row_gym_id = row.get("gym_id") or gym_id
        if config.approval_proof_enabled():
            manual_patcher = getattr(store, "patch_caption_manual_format", None)
            if callable(manual_patcher):
                patched = manual_patcher(
                    row_gym_id, row.get("id"), row.get("status"), caption, clean)
            if patched is not None:
                # Manual approval is for the exact caption. The current-mode
                # CAS clears its proof and demotes approved -> pending; make
                # the owner review the formatted wording on a later tick.
                if row.get("status") == "approved":
                    _alert_caption_format_reapproval(row.get("id"), gym_id)
                    return _CAPTION_REAPPROVAL_REQUIRED
                return patched
            # A null Manual result may mean Auto, but can also mean a stale
            # row or unresolved mode. The autonomous RPC repeats both the mode
            # and exact-caption CAS, so only a confirmed Auto update may send.
            auto_patcher = getattr(store, "patch_caption_autonomous_clean", None)
            if callable(auto_patcher):
                patched = auto_patcher(row_gym_id, row.get("id"),
                                       row.get("status"), caption, clean)
            if patched is None:
                return None
        else:
            patcher = getattr(store, "patch_caption_preserve_status", None)
            if patcher is not None:
                patched = patcher(row_gym_id, row.get("id"), clean)
    except Exception as e:  # noqa: BLE001 - persistence is best effort here
        print(f"[calendar-autopublish] caption format patch failed for "
              f"{row.get('id')}: {type(e).__name__}: {e}")
        if config.approval_proof_enabled():
            return None
    row = dict(patched or row)
    row["caption"] = clean  # what we SEND is clean even when the patch failed
    if ";" in caption:
        _note_caption_formatted(row.get("id"), gym_id)
    else:
        print(f"[calendar-autopublish] formatted legacy caption spacing for "
              f"{gym_id}/{row.get('id')}; awaiting remaining publish checks")
    return row


def _alert_caption_format_reapproval(row_id, gym_id):
    """Tell ops that formatting invalidated a Manual approval and needs review."""
    try:
        from . import db, ops_alerts
        key = f"capformat_reapproval_{gym_id}_{row_id}"
        if not db.kv_is_durable():
            print(f"[calendar-autopublish] row {row_id}: caption format reapproval "
                  "alert suppressed because KV is not durable")
            return
        if db.kv_get(key):
            return
        _stamp_after_confirmed_alert(db, key, ops_alerts.alert(
            f"{gym_id}: row {row_id} was returned to pending review because "
            "publish-time caption formatting changed the approved text. Review "
            "and approve the formatted caption before it can publish."))
    except Exception:
        pass


def _planned_mentions(caption, gym_id, category):
    """Every @handle the OUTBOUND caption will carry: the @handles already in the
    caption text PLUS the allowlisted handles the zernio publisher appends for
    this category when AGENT_MENTIONS is armed (zernio_publisher.publish). Used
    by the publish_guard mention rail. Best effort: a read failure returns only
    the in-caption handles (the rail then fails closed on proof/results)."""
    import re as _re
    handles = _re.findall(r"@([A-Za-z0-9_.]+)", str(caption or ""))
    if config.mentions_enabled() and (category or "").strip():
        try:
            from .tag_allowlist import handles_for_category
            for h in handles_for_category(gym_id, (category or "").strip().lower()):
                if h not in handles:
                    handles.append(h)
        except Exception:
            pass
    return handles


def _revert_to_pending(store, row_id, reject_reason="", revert_status="pending",
                       gym_id=None, expected_claim_token=None):
    """Revert a row out of the 'publishing' claim after a PRE-NETWORK block.

    Returns True only when the store confirms the pre-network rollback.

    Why a failure here must never be swallowed: the atomic claim flips a row to
    'publishing' BEFORE the guard runs, and mark_publishing only ever re-claims a
    pending/approved row. So a revert that fails silently strands the row forever --
    nothing retries it, and sweep_stuck_publishing deliberately only ALERTS, because
    in the general case the post may already be live and a blind revert could
    double-post.

    This path is the one case where that ambiguity does not exist: the caption
    cooldown and publish_guard both block BEFORE any network call, so the post
    provably never went out and reverting is always safe. That is worth shouting
    about rather than passing over -- it is exactly how rows d4574f62 and f75c19e9
    (gym lasso) were stranded when the store was unreachable during the 2026-09-02
    outage, one of them for five days.
    """
    try:
        try:
            reverted = store.mark_publish_failed(row_id, revert_status=revert_status,
                                                 reject_reason=reject_reason,
                                                 gym_id=gym_id,
                                                 expected_claim_token=expected_claim_token)
        except TypeError:
            # Only legacy injectable stores lack the tenant-aware signature.
            # Never retry a real Supabase store without its tenant filter.
            from .portal_calendar_store import SupabaseCalendarStore
            if isinstance(store, SupabaseCalendarStore):
                raise
            try:
                reverted = store.mark_publish_failed(
                    row_id, revert_status=revert_status,
                    reject_reason=reject_reason)
            except TypeError:
                reverted = store.mark_publish_failed(
                    row_id, revert_status=revert_status)
        # SupabaseCalendarStore returns None when the conditional update matched no
        # row. The row may still be publishing, or its status/tenant/provider fields
        # may have changed. Neither case confirms a rollback.
        if not reverted:
            raise RuntimeError("publish rollback was not confirmed")
        return True
    except Exception as e:  # noqa: BLE001 - a stranded row must never be silent
        try:
            from . import ops_alerts
            ops_alerts.alert(
                f"calendar row {row_id} has a STRANDED or changed publish claim: it was blocked "
                f"BEFORE any publish attempt ({reject_reason or 'caption cooldown'}) "
                f"but the conditional revert to {revert_status} was unconfirmed "
                f"({type(e).__name__}). This worker's post did NOT go out. Inspect the "
                "current tenant, status and provider fields before any manual recovery.")
        except Exception:
            pass
        return False


def _drive_asset_usable_at_send(row, gym_id):
    """Fetch current review evidence for a Drive-backed calendar row, fail closed."""
    asset_id = str(row.get("source_media_asset_id") or "").strip()
    if not asset_id:
        # Older staged rows may have lost the asset ID. Hold rows whose surviving
        # metadata still identifies the Drive lane; a CDN URL without such a marker
        # cannot establish origin and needs an inventory reconciliation.
        from urllib.parse import urlparse
        markers = ("drive", "gym_media", "gym-media", "gymmedia")
        for field in ("draft_type", "source_type", "media_source", "source",
                      "source_fragments"):
            value = row.get(field)
            values = value if isinstance(value, (list, tuple)) else (value,)
            if any(any(marker in str(item or "").lower() for marker in markers)
                   for item in values):
                return False
        for field in ("source_media_url", "image_url", "thumbnail_url"):
            url = str(row.get(field) or "")
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            path = parsed.path.lower()
            if (host in {"drive.google.com", "docs.google.com"}
                    or host.endswith(".drive.google.com")
                    or any(f"/{marker}/" in f"{path}/" for marker in markers)
                    or any(path.rsplit("/", 1)[-1].startswith(f"{marker}_")
                           for marker in markers)):
                return False
        return True
    try:
        from . import media_source_store, gym_media_selector
        asset = media_source_store.default_store().get_asset(asset_id)
        return (bool(asset) and str(asset.get("gym_id")) == str(gym_id)
                and gym_media_selector.is_usable(asset))
    except Exception as e:  # noqa: BLE001 - unavailable review evidence holds send
        print(f"[calendar-autopublish] media review read failed for row "
              f"{row.get('id')}: {type(e).__name__}")
        return False


def _alert_publish_blocked(gym_id, row_id, code, reverted=True,
                           revert_status="pending"):
    """ONE deduped ops alert per (gym, violation code): kv key
    publish_blocked:<gym>:<code> fires once and stays quiet until the state
    changes (_clear_publish_blocked re-arms it when a row for the gym passes
    the guard). Best effort; never raises into the lane.

    `reverted` reports what actually happened. It used to claim "reverted to pending"
    unconditionally, which read as a completed action even on the nights the revert
    threw and the row was left stranded in 'publishing'."""
    try:
        from . import db, ops_alerts
        key = f"publish_blocked:{gym_id}:{code}"
        if db.kv_get(key):
            return
        db.kv_set(key, str(row_id or "1"))
        _state = (f"reverted to {revert_status} with reject_reason" if reverted else
                  "REVERT FAILED -- the row may be stranded in 'publishing' or "
                  "changed; inspect it before retry")
        ops_alerts.alert(
            f"publish guard: row {row_id} (gym {gym_id}) blocked at the publish "
            f"boundary ({code}); {_state}. Further "
            f"'{code}' blocks for this gym stay quiet until a post publishes clean.")
    except Exception:
        pass


def _alert_ambiguous_publish(gym_id, row_id, detail):
    """A network attempt may have reached the provider. Keep its owned claim held."""
    try:
        from . import ops_alerts
        ops_alerts.alert(
            f"calendar row {row_id} (gym {gym_id}) has an AMBIGUOUS publish outcome: "
            f"{detail}. The row remains in 'publishing' and will NOT retry automatically. "
            "Reconcile with the provider before releasing the claim.")
    except Exception:
        pass


def _result_proves_no_post(result):
    """Explicit publisher contract for a provider rejection before creation.

    False by default. A generic ok=False, a timeout, or an unknown mode is not proof.
    Publisher adapters may opt in only when the provider contract guarantees no post
    exists, using `definitive_no_post=True` on the result object.
    """
    return getattr(result, "definitive_no_post", False) is True


def _clear_publish_blocked(gym_id):
    """Re-arm the deduped publish-blocked alerts for a gym (called when a row
    passes the guard: the state changed). Best effort; never raises."""
    try:
        from . import db, publish_guard
        for code in publish_guard.ALL_CODES:
            if db.kv_get(f"publish_blocked:{gym_id}:{code}"):
                db.kv_set(f"publish_blocked:{gym_id}:{code}", "")
    except Exception:
        pass


# ---- LASSO-via-Zernio cutover (AGENT_LASSO_VIA_ZERNIO) ------------------------
# WHY (Blake 2026-08-27): metrics_sync ingests Zernio analytics; LASSO's
# Meta-direct-published posts read there as an external/second publisher and taint
# LASSO's own months for the learning loop. One publish path = one guard set =
# A-gate parity. Armed, LASSO's calendar rows publish through the SAME zernio lane
# as the client gyms (publish_client_gyms below) and every Meta-direct lasso lane
# stands down. Flag OFF (the default) is byte-for-byte today's routing.

# The hold/missing/alert helpers now live in the SHARED choke point
# (agent/lasso_zernio_route.py) so EVERY LASSO publish lane holds identically and
# speaks with ONE deduped alert. These thin aliases keep this module's internal
# callers (and any test that patches them here) byte-for-byte unchanged.
from . import lasso_zernio_route as _lzr

_LASSO_ZERNIO_HOLD_KEY = _lzr.HOLD_KEY
_lasso_zernio_missing = _lzr.missing


def _alert_lasso_zernio_hold(missing):
    return _lzr.alert_hold(missing)


def _clear_lasso_zernio_hold():
    return _lzr.clear_hold()


def _draft_for(row):
    """Build a PENDING Draft from a content_calendar row for meta_publisher.publish."""
    fmt = (row.get("format") or "feed").strip().lower()
    is_story = fmt == "story"
    return Draft(
        draft_id=str(row.get("id") or ""),
        account_key="",  # filled by the caller once the account is resolved
        platform="",     # filled by the caller
        caption=row.get("caption") or "",
        hashtags=[],
        creative_path="",
        creative_public_url=row.get("image_url") or "",
        scheduled_for=row.get("post_date") or "",
        status=DraftStatus.PENDING,
        is_story=is_story,
        day_key=row.get("post_date") or "",
        draft_type=("story" if is_story else "feed"),
    )


def publish_due(run_date, *, gym_id="lasso", store=None, publisher=None,
                notifier=None, now=None, catch_all=False, approved_only=False,
                zernio_publish=None, catchup_days=0, daily_cap=None):
    """
    Read gym_id's content_calendar rows dated run_date and publish each unpublished
    one to live IG/FB, EXACTLY ONCE. Returns a summary dict.

    TIME-OF-DAY SPACING: a day's rows are not fired all at once. Each row has a
    STABLE slot time derived from the row itself (slot_time_for_row), NOT from its
    position in the shrinking due set, so a row's slot never moves when a sibling
    publishes. A row publishes only once its own slot time is <= the current local
    time (`now`, injectable). Rows whose slot has not arrived are left pending
    (never claimed) for a later tick the same day.

    NO ORPHANS: pass catch_all=True to publish ALL remaining unpublished due rows
    for the day regardless of slot. The listener calls this at the LAST slot and the
    once/day run_daily draw also calls it, so every due row is published that day
    even if a mid-day tick was missed or the scheduler only fired once.

    Exactly-once is unchanged: a row publishes at most once across every slot tick
    and the catch-all (the atomic mark_publishing claim guards it).

    Both gates must be armed (AGENT_CALENDAR_AUTOPUBLISH and AGENT_PUBLISH_ENABLED)
    or this is a no-op. `store`, `publisher`, and `notifier` are injectable so every
    path is unit tested with zero network. `run_date` is 'YYYY-MM-DD'.
    """
    if not config.calendar_autopublish_enabled():
        return {"ok": False, "reason": "calendar autopublish flag OFF",
                "date": run_date}
    if not config.publish_enabled():
        return {"ok": False, "reason": "publish flag OFF (draft-only)",
                "date": run_date}

    from .publish_billing_gate import publishing_blocked
    if publishing_blocked(gym_id):
        return {"ok": False, "held": True, "billing_held": True, "date": run_date,
                "reason": "Echo access revoked or subscription canceled", "published": []}

    # LASSO-VIA-ZERNIO CUTOVER HOLD (AGENT_LASSO_VIA_ZERNIO): when the flag is
    # armed but the 'lasso' gyms row lacks its Zernio profile id or selected FB
    # page, the WHOLE lasso lane HOLDS here — no row is read, claimed, or
    # published, ONE deduped alert fires, and there is NO Meta-direct fallback
    # (that would recreate the second-publisher taint in Zernio analytics that
    # this flag exists to kill). Rows stay pending/approved untouched and publish
    # on the first tick after `python -m agent lasso-zernio-setup` completes.
    if (gym_id or "lasso").strip() == "lasso" and config.lasso_via_zernio_enabled():
        _missing = _lasso_zernio_missing()
        if _missing:
            _alert_lasso_zernio_hold(_missing)
            return {"ok": False, "held": True, "date": run_date,
                    "reason": ("lasso-via-zernio setup incomplete: "
                               + ", ".join(_missing))}
        _clear_lasso_zernio_hold()

    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    publisher = publisher or meta_publisher.publish
    if zernio_publish is None:
        from . import zernio_publisher
        zernio_publish = zernio_publisher.publish
    from . import forward_media_guard as _forward_media_guard
    if _forward_media_guard.enabled():
        from . import zernio_publisher
        # The trusted send scope is meaningful only when its wrapped lower
        # publisher is the callable used. Test hooks or future callers may not
        # substitute an arbitrary callback and ignore that scope when armed.
        if (publisher is not meta_publisher.publish
                or zernio_publish is not zernio_publisher.publish):
            return {"ok": False, "held": True, "date": run_date,
                    "reason": "unverified provider callback", "published": []}

    # A reviewed managed Story unlocks only the exact dated incident feed hold.
    # This CAS never changes captions, visuals, claims or another hold reason.
    if (gym_id == "lasso" and config.lasso_three_feed_enabled()
            and hasattr(store, "_client")):
        try:
            from .jobs import lasso_backlog_feed_hold_release
            for paired_account in ("instagram", "facebook"):
                lasso_backlog_feed_hold_release.run(
                    account=paired_account, store=store, today=run_date)
        except Exception as exc:
            print(f"[lasso-backlog-feed-release] held: {type(exc).__name__}")

    # Repaired Stories stay held until their exact feed has a real publish
    # receipt. Release through the database's source-proof RPC before due_rows
    # filters media-held rows. A failed release never blocks the feed lane.
    if (gym_id == "lasso" and config.lasso_three_feed_enabled()
            and hasattr(store, "_client")):
        try:
            from .jobs.lasso_daily_paired_stories import release_ready_holds
            release_ready_holds(store, run_date, catchup_days=catchup_days)
        except Exception as exc:
            print(f"[lasso-paired-story-release] held: {type(exc).__name__}")

    # catchup_days (client lane): also pick up recent-past rows the client approved
    # AFTER their day passed, so a late approval publishes instead of stranding.
    try:
        rows = store.due_rows(gym_id, run_date, catchup_days=catchup_days) or []
    except TypeError:
        rows = store.due_rows(gym_id, run_date) or []      # older store/test fakes

    published = []
    skipped = []
    failed = []
    waiting = []            # slot not arrived yet: left pending for a later run
    forward_media_holds = {}
    recovery_required = []  # pre-network block claimed a row, but rollback was unconfirmed
    published_accounts = set()

    # ANTI-FLOOD (2026-08-24): when a client gym's publishing is repaired after a stall
    # (e.g. Pierce's Zernio profile was linked, or ENG's images were un-blocked), a
    # catch_all sweep would otherwise fire EVERY stranded approved row at once — dumping
    # a week of posts onto the gym's feed in one minute. daily_cap bounds how many this
    # gym publishes per calendar day, so a backlog DRIPS out over days instead. Counts
    # rows already published today (kv) plus rows published in this run. None => no cap.
    cap_used = _pub_count_today(gym_id, run_date) if daily_cap else 0

    # PER-GYM TIMEZONE (Blake 2026-08-25): a gym's slots are ITS OWN wall clock, not
    # Eastern. Resolved once per run; unset gyms fall back to the global tz so nothing
    # changes until a per-gym value is set (python -m agent set-timezone).
    gym_tz = config.posting_timezone_for(gym_id)
    gym_local_today = _local_now(now, gym_tz).date().isoformat()

    for row in rows:
        row_id = row.get("id")
        # SHOW THE TIME: stamp the row's deterministic go-live time (scheduled_at) so
        # the portal can display exactly when the post publishes — including rows still
        # waiting on the client's approval. Display metadata only (never a status or
        # publish write); best effort, never blocks the lane; idempotent (the slot is a
        # pure function of the row).
        if not row.get("scheduled_at"):
            try:
                stamper = getattr(store, "stamp_scheduled", None)
                if stamper is not None:
                    stamper(row_id, scheduled_iso_for_row(row, now, gym_tz))
            except Exception as e:
                print(f"[calendar-autopublish] scheduled_at stamp failed for "
                      f"{row_id}: {type(e).__name__}: {e}")
        # Belt-and-braces: never touch a row already stamped published (the query
        # already excludes these, but a live race could still surface one).
        if row.get("published_at") or row.get("late_post_id"):
            skipped.append(row_id)
            continue

        # SAMPLE RAIL (onboarding_demo): a seeded SAMPLE row shows a brand-new gym what
        # its calendar will look like while intake lands. It is NOT the gym's content
        # and must never reach a real feed. Checked BEFORE the approval gate, the slot
        # gate and the claim, so it holds regardless of status, autonomy, catch_all or
        # a client tapping approve on it by mistake — marking alone is not trusted.
        try:
            from . import onboarding_demo as _demo
            if _demo.is_sample_row(row):
                skipped.append(row_id)
                continue
        except Exception as _e:  # noqa: BLE001 - a rail that cannot load must not publish
            skipped.append(row_id)
            _rail_fail_closed_alert("sample-rail", row_id, gym_id,
                                    type(_e).__name__)
            continue

        # CLIENT-SAFE REVIEW HARD BLOCK (2026-09-11): a row Echo generated
        # without a client's own real photo/voice behind it must NEVER auto-
        # publish, on ANY account, regardless of trust level, approved_only,
        # catch_all, or a status of 'approved' reached by any path (a client
        # mistap included). Unconditional and explicit — it does not rely on
        # approved_only/trust already covering this case, even though they
        # independently do today; a future change to either must not be able
        # to silently open this lane. Checked BEFORE the approval gate,
        # exactly like the sample rail above. Covers BOTH fallback lanes:
        #   - no_media_astra_seed.py: pillar is an EXACT match
        #     (NEEDS_CLIENT_SAFE_REVIEW_PILLAR, a throwaway label with no
        #     other meaning).
        #   - client_infographic_fill.py: pillar is the source's REAL category
        #     (service/about/offer/...) with a SUFFIX appended
        #     (_NEEDS_CLIENT_SAFE_REVIEW_SUFFIX), since that category is
        #     otherwise meaningful and must not be replaced outright — so this
        #     checks endswith(), not equality.
        # LEGACY IGFILL MEDIA HARD BLOCK (2026-10-04): a row pointing at legacy
        # client_infographic_fill media (feed image_url or story source_media_url
        # matching igfill_YYYY-MM-DD_...) must NEVER auto-publish on ANY account,
        # regardless of trust level, approved_only, catch_all, or a status of
        # 'approved' reached by ANY path (manual tap or an autonomous lane). The
        # 2026-09-11 pillar check above misses rows whose pillar carries no review
        # suffix — Swift River rows e11f7bec-.../8de2ef1e-... (dated Sep 25,
        # published Oct 1) shipped exactly that way. Checked BEFORE the approval
        # gate, the slot gate and the claim, like the pillar block. Separately
        # branded Astra fallback media (no_media_/seed_) is NOT legacy igfill and
        # stays publishable. A rail that cannot evaluate is FAIL-CLOSED: skip +
        # internal ops alert, never publish.
        try:
            pillar = str(row.get("pillar") or "")
            from . import no_media_astra_seed as _nmas
            from . import client_infographic_fill as _cif
            needs_review = (pillar == _nmas.NEEDS_CLIENT_SAFE_REVIEW_PILLAR or
                            pillar.endswith(_cif._NEEDS_CLIENT_SAFE_REVIEW_SUFFIX))
            legacy_media = _row_has_legacy_igfill_media(row)
        except Exception as _e:  # noqa: BLE001 - a rail that cannot load must not publish
            skipped.append(row_id)
            _rail_fail_closed_alert("client-safe-review", row_id, gym_id,
                                    type(_e).__name__)
            continue
        if needs_review or legacy_media:
            skipped.append(row_id)
            if legacy_media:
                _legacy_igfill_blocked_alert(row_id, gym_id)
            continue

        # CLIENT approval gate: when approved_only (client gyms), a row that the client
        # has not approved yet is left UNTOUCHED (never claimed, never published). LASSO
        # (approved_only=False) is unchanged: it auto-publishes pending rows at slot time.
        if approved_only and (row.get("status") or "").strip().lower() != "approved":
            waiting.append(row_id)
            continue

        # SLOT GATE, gym-local and DATE-AWARE: publish nothing before the row's OWN
        # stable slot time in the GYM'S timezone. A row whose slot has not arrived is
        # left UNTOUCHED (never claimed) so a later tick drips it out; catch_all
        # bypasses the gate (LASSO's last-slot straggler sweep). Date-awareness
        # (Blake 2026-08-25, per-gym tz): the row's post_date is compared against the
        # gym's LOCAL calendar day — a past-local-date row (catchup) is always due; a
        # FUTURE-local-date row always waits, so a Pacific gym's "today (ET)" rows can
        # no longer fire the evening before its local date; a same-local-day row waits
        # for its slot on the gym's own wall clock.
        row_date = str(row.get("post_date") or run_date)[:10]
        past_date = row_date < gym_local_today
        future_date = row_date > gym_local_today
        # Paired LASSO Stories wait for their feed-following slot even during
        # the last-slot catch-all sweep. A future local date always waits.
        paired_lasso_story = (
            str(gym_id or "").strip().lower() == "lasso"
            and (row.get("format") or "feed").strip().lower() == "story"
            and row.get("slot_index") in (0, 1, 2)
            and _lasso_three_feed_enabled(gym_id, row_date))
        if ((paired_lasso_story and
             (future_date or (not past_date and not is_due(row, now, gym_tz))))
                or (not paired_lasso_story and not catch_all and
                    (future_date or (not past_date and not is_due(row, now, gym_tz))))):
            waiting.append(row_id)
            continue
        if paired_lasso_story and not _paired_lasso_feed_published(row, store):
            waiting.append(row_id)
            continue
        paired_lasso_feed = (
            str(gym_id or "").strip().lower() == "lasso"
            and (row.get("format") or "feed").strip().lower() == "feed"
            and row.get("slot_index") in (0, 1, 2)
            and row_date >= "2026-10-02"
            and _lasso_three_feed_enabled(gym_id, row_date))
        if paired_lasso_feed and not _paired_lasso_story_prepared(row, store):
            # A due feed without its reviewed Story is an actionable stall.
            # Keep it unclaimed; use the existing persisted threshold/dedupe
            # so repeated minute ticks produce one operational alert per day.
            _note_repeat_failure(row_id, gym_id, RuntimeError(
                "paired Story source proof unavailable; feed remains held"))
            waiting.append(row_id)
            continue
        # The Story artifact binds a source proof over THIS caption. The
        # cleanup gates below (_strip_or_hold_meta / _format_caption_at_publish)
        # can legitimately change it, so the proof is re-measured against the
        # final cleaned row before any claim/network call — but ONLY when the
        # caption actually moved, so a clean row pays no extra RPC.
        paired_proven_caption = row.get("caption") if paired_lasso_feed else None

        account = _account_for(row, gym_id)
        if account is None:
            # No mappable account: leave the row untouched (never claimed), skip it.
            # ALERT for an IG/FB row (audit 2026-08-25 MAJOR): an APPROVED post that can
            # never route (registry drift — the Pierce onboarding stall class) used to
            # skip silently on every tick forever. Non-IG/FB rows (googlebusiness) are
            # another lane's job and stay silent. Deduped per row in kv.
            plat = (row.get("account") or "").strip().lower()
            if plat in ("instagram", "facebook"):
                try:
                    from . import db as _db, ops_alerts as _oa
                    if not _db.kv_get(f"noaccount_alerted_{row_id}"):
                        _db.kv_set(f"noaccount_alerted_{row_id}", "1")
                        _oa.alert(
                            f"calendar row {row_id} (gym {gym_id}, {plat}) cannot "
                            f"publish: no registry account '{gym_id}_"
                            f"{'fb' if plat == 'facebook' else 'ig'}' exists. The post "
                            "is skipped every tick until the account is registered.")
                except Exception:  # noqa: BLE001 - alerting never blocks the lane
                    pass
            skipped.append(row_id)
            continue

        # INTERNAL EDIT-RATIONALE FINAL GATE (CrossFit ENG, 2026-08-23): a caption
        # carrying a bracketed meta block ([why]/[reason]/...) never reaches the
        # network. Clean suffix -> stripped and published in the legacy lane, or
        # reset to pending + held for fresh approval when proof is armed. All-meta ->
        # held + one alert. BEFORE the story-stale check on purpose: a cleaned story
        # in the legacy lane then mismatches its burned media and is re-rendered.
        cleaned = _strip_or_hold_meta(row, gym_id, store,
                                      autonomous_lane=not approved_only)
        if cleaned is _META_REAPPROVAL_REQUIRED:
            waiting.append(row_id)
            continue
        if cleaned is None:
            waiting.append(row_id)
            _alert_meta_leak_held(row_id, gym_id)
            continue
        row = cleaned

        # Legacy non-Story copy is formatted before sending, including approved
        # rows that the safe pending-row backfill intentionally left alone.
        # Story rows with semicolons and unformattable URLs hold with an alert.
        formatted = _format_caption_at_publish(row, gym_id, store)
        if formatted is _CAPTION_REAPPROVAL_REQUIRED:
            waiting.append(row_id)
            continue
        if formatted is None:
            waiting.append(row_id)
            _alert_caption_format_held(row_id, gym_id)
            continue
        row = formatted

        # SOURCE-INTEGRITY HOLD (PR29705 review, lead counterexample). Cleanup
        # above may have changed the caption AFTER the paired-Story source proof
        # was measured. The prepared Story was bound to the PRE-cleanup caption,
        # and _paired_lasso_story_prepared proves by feed ID only -- a second
        # ID-only proof can return true while the outgoing caption differs from
        # what the Story was bound to (e.g. the persistence patch failed and the
        # DB still holds the old text). So a changed caption is ALWAYS held
        # here, waiting and unclaimed, via the existing repeated-failure note;
        # the next preparation tick repairs against the persisted cleanup.
        # Unchanged caption: no second RPC, no behavior change.
        if (paired_lasso_feed
                and str(row.get("caption") or "") != str(paired_proven_caption or "")):
            _note_repeat_failure(row_id, gym_id, RuntimeError(
                "paired Story source proof invalid after caption cleanup; "
                "feed remains held"))
            waiting.append(row_id)
            continue

        # STORY CAPTION MUST BE ON THE MEDIA (Dale, 2026-08-17): a story publishes with
        # an EMPTY body, so its caption lives only on the rendered media. When a client
        # EDITS a story caption in the portal, content_calendar.caption changes but the
        # already-hosted image_url still carries the OLD (or no) caption. Publishing it
        # now would ship a story whose words do not match the saved caption (Dale saw a
        # captionless story). We HOLD such a row (never claimed, left for a build
        # re-render) rather than ship a stale/blank story. Schema-free: the burned story
        # media's filename embeds the caption key, so a mismatch is detectable from the
        # row alone. A non-story row, or a story whose media was NOT caption-burned by
        # us (raw baseline), is never affected.
        if _story_media_is_stale(row):
            # SELF-HEAL (Dale/ENG 2026-08-22): rather than hold this edited-caption story
            # silently forever, re-burn the CURRENT caption onto fresh media now and swap
            # the image_url. If it now carries the caption, publish it this tick. Only when
            # the re-burn cannot run (no source_media_url) or still mismatches do we hold +
            # alert (the old behavior, but now the exception, not the rule).
            healed = _reburn_stale_story(row, account, store)
            if healed is None or _story_media_is_stale(healed):
                waiting.append(row_id)
                _alert_story_needs_render(row_id, gym_id)
                continue
            row = healed  # freshly re-burned; falls through to the exactly-once claim below

        # ANTI-FLOOD CAP: once this gym has hit its per-day publish limit, leave the rest
        # UNTOUCHED (never claimed) so the backlog drips out on later days instead of
        # flooding the feed. Applies only when a daily_cap is set (the client lane).
        if daily_cap and (cap_used + len(published)) >= int(daily_cap):
            waiting.append(row_id)
            _alert_daily_cap_hit(gym_id, run_date)
            continue

        # FEED ASPECT PREFLIGHT: a feed photo outside IG/FB's accepted ratio is re-framed
        # to an in-spec 1080x1350 card BEFORE the network call, so Zernio never 400s on
        # aspect ratio (ENG/Dale 2026-08-24). No-op for a story (framed by its burner) and
        # for an already-in-spec image. Done before the claim so a re-host failure never
        # burns the exactly-once claim. A None return means the image is CONFIRMED
        # out-of-aspect and could NOT be fixed this tick -> HOLD (leave approved + alert)
        # rather than ship a known-bad image that would 400 and strand the row anyway.
        if not _is_story_row(row):
            fixed = _normalize_feed_image(row, account, store)
            if fixed is None:
                waiting.append(row_id)
                _alert_feed_needs_reframe(row_id, gym_id)
                continue
            row = fixed

        # ATOMIC DAY CAPACITY + ROW CLAIM. The actual gym-local publish day is
        # used, so catch-up rows dated on different prior days share today's
        # capacity. Postgres serializes distinct rows/workers in one transaction.
        # This applies to both manual approval and autonomous client lanes.
        claim_token = None
        try:
            claim_slot = getattr(store, "claim_publish_slot", None)
            # DURABLE APPROVAL PROOF (draft, AGENT_APPROVAL_PROOF default OFF):
            # when armed, EVERY lane passes require_proof to the atomic claim.
            # The RPC then re-reads the gym's CURRENT autonomy from the
            # authoritative DB inside the claim transaction: a definitively
            # autonomous gym keeps today's behavior exactly; any other gym
            # (Manual, newly flipped Auto->Manual, or an unresolved/ambiguous
            # lookup) must carry a fresh VERIFIED human approval (human kind +
            # nonempty trusted actor, stamped only via the portal's
            # calendar_stamp_verified_approval) whose canonical digest matches
            # the locked row's exact publish-relevant content
            # (caption/account/format/date, the FINAL image_url and the
            # rendered/source identity; the publisher-stamped scheduled_at is
            # deliberately not bound). A post-approval auto-fit reframe or
            # story reburn changes image_url and therefore fails the row
            # CLOSED into fresh review -- changed pixels never publish under
            # an old approval. Enforcement lives in the DB claim, not a
            # Python pre-read, so a mid-flight Auto->Manual flip is caught
            # atomically. When the store cannot carry the requirement (legacy
            # injected store, unapplied migration), fail CLOSED: hold the row
            # rather than publish under an unproved approval.
            require_proof = config.approval_proof_enabled()
            if callable(claim_slot):
                # Refresh after preflight: a long render/reframe can cross the
                # gym's midnight before this atomic reservation.
                reservation_day = _local_now(now, gym_tz).date().isoformat()
                if require_proof:
                    try:
                        signature = inspect.signature(claim_slot)
                        supports_proof = (
                            "require_proof" in signature.parameters
                            or any(p.kind is inspect.Parameter.VAR_KEYWORD
                                   for p in signature.parameters.values())
                        )
                    except (TypeError, ValueError):
                        supports_proof = False
                    if supports_proof:
                        won = claim_slot(row_id, gym_id, reservation_day, gym_tz,
                                         _publish_capacity(gym_id, row, store,
                                                           reservation_day),
                                         approved_only, require_proof=True)
                    else:
                        # A store whose claim cannot carry the proof requirement
                        # must never claim in Manual mode while armed.
                        won = None
                else:
                    won = claim_slot(row_id, gym_id, reservation_day, gym_tz,
                                     _publish_capacity(gym_id, row, store,
                                                       reservation_day), approved_only)
            else:
                if require_proof:
                    # The legacy mark_publishing fallback cannot verify durable
                    # human-approval proof atomically; hold the row.
                    won = None
                else:
                    # Legacy injectable test stores have no RPC. The production
                    # Supabase store always exposes claim_publish_slot and fails
                    # closed if its migration has not been applied.
                    won = store.mark_publishing(row_id)
        except Exception as e:
            failed.append(row_id)
            print(f"[calendar-autopublish] claim failed for row {row_id}: "
                  f"{type(e).__name__}: {e}")
            # DEFECT 4 (audit 2026-08-30): this branch used to be print-only, unlike
            # the network-publish exception path below, so a row whose atomic claim
            # kept throwing (e.g. a flaky store connection) looped every ~1-min tick
            # forever with no human ever told. Route it through the SAME counter.
            _note_repeat_failure(row_id, gym_id, e)
            continue
        if not won:
            # Either this row was claimed elsewhere or the local-day platform
            # cadence is full. Leave it untouched for calendar review.
            (waiting if callable(claim_slot) else skipped).append(row_id)
            continue
        if require_proof:
            # The proof RPC returns the exact locked creative. A token alone
            # only proves ownership and cannot make the prefetched row safe to
            # send after a concurrent edit. Refuse every incomplete result.
            locked_fields = ("account", "format", "post_date", "caption",
                             "image_url", "byte_hash", "source_media_asset_id",
                             "source_media_url")
            if (not isinstance(won, dict)
                    or won.get("id") != row_id
                    or won.get("gym_id") != gym_id
                    or won.get("status") != "publishing"
                    or not won.get("publish_claim_token")
                    or any(field not in won for field in locked_fields)
                    or not str(won.get("image_url") or "").strip()
                    or type(won.get("autonomous_at_claim")) is not bool):
                failed.append(row_id)
                recovery_required.append(row_id)
                _alert_ambiguous_publish(gym_id, row_id,
                                         "claim returned no verified locked creative")
                continue
            row = won
            claim_token = str(row["publish_claim_token"])
            # Account routing was selected before the claim. If the locked
            # account changed, hold for a fresh preflight on the next tick.
            locked_account = _account_for(row, gym_id)
            if locked_account is None or locked_account.key != account.key:
                failed.append(row_id)
                reverted = _revert_to_pending(
                    store, row_id, reject_reason="account_changed_at_claim",
                    gym_id=gym_id, expected_claim_token=claim_token,
                    revert_status="approved" if approved_only else "pending")
                if not reverted:
                    recovery_required.append(row_id)
                continue
            account = locked_account
            # A concurrent edit can introduce internal rationale or forbidden
            # punctuation after preflight. Recheck the claimed version before
            # any idempotency stamp or provider call.
            from . import post_quality
            _, locked_meta = post_quality.split_meta_suffix(row.get("caption") or "")
            if locked_meta or ";" in (row.get("caption") or ""):
                reason = ("locked_creative_meta_leak" if locked_meta
                          else "locked_creative_caption_format")
                reverted = _revert_to_pending(
                    store, row_id, reject_reason=reason, gym_id=gym_id,
                    expected_claim_token=claim_token,
                    revert_status="approved" if approved_only else "pending")
                if not reverted:
                    recovery_required.append(row_id)
                _alert_publish_blocked(gym_id, row_id, reason,
                                       reverted=reverted)
                failed.append(row_id)
                continue
        else:
            # Legacy UUID and injectable boolean claims retain their behavior.
            claim_token = won if isinstance(won, str) else None

        # LEASED-ROW SOURCE REVALIDATION (paired LASSO feed, owned string-token
        # claim only; legacy bool-claim test stores skip this gate entirely).
        # due_rows is a SNAPSHOT: caption/media can be patched while pending
        # between the paired-Story proof above and this claim, and the claim
        # RPC pins ownership, not outgoing content. Re-fetch the leased row and
        # require the SAME claim token with status 'publishing', field-for-field
        # equality of the outgoing source with the snapshot, and Story readiness
        # measured against the LEASED feed row -- all BEFORE any content-ledger
        # stamp or network call. Anything less rolls back with the owned token
        # and the existing repeated-failure note; a failed rollback is recovery
        # work, exactly like the other pre-network blocks.
        if paired_lasso_feed and claim_token:
            leased = None
            try:
                _get_row = getattr(store, "get_row", None)
                if callable(_get_row):
                    leased = _get_row(gym_id, row_id)
            except Exception:  # noqa: BLE001 - an unreadable lease fails closed
                leased = None
            lease_ok = (
                isinstance(leased, dict)
                and str(leased.get("publish_claim_token") or "") == str(claim_token)
                and str(leased.get("status") or "") == "publishing"
                and all(leased.get(_f) == row.get(_f)
                        for _f in _PAIRED_FEED_SOURCE_FIELDS))
            if not lease_ok or not _paired_lasso_story_prepared(leased, store):
                _note_repeat_failure(row_id, gym_id, RuntimeError(
                    "leased paired feed changed after the Story source proof; "
                    "feed remains held"))
                _reverted = _revert_to_pending(
                    row_id=row_id, store=store, gym_id=gym_id,
                    expected_claim_token=claim_token,
                    reject_reason="leased_feed_source_mismatch")
                if not _reverted:
                    recovery_required.append(row_id)
                failed.append(row_id)
                continue

        # CONTENT IDEMPOTENCY, WRITTEN BEFORE THE NETWORK CALL (2026-09-05 incident).
        #
        # Tough Temple was double posted on a live client account: six publishes in 40
        # seconds, including a re-publish of a day that had already gone out 19 hours
        # earlier. Fleet wide the same signature covered 84 extra publishes across 10
        # gyms (eng 23, lasso 19, piercefitness 15). Every pair carried a DIFFERENT
        # late_post_id, so Zernio accepted each as a separate post and they are live.
        #
        # The row-level claim above (mark_publishing) did not and could not stop it,
        # because these are DIFFERENT ROWS: same gym, same account, same post_date, same
        # caption, different time_slot. The planner wrote one caption into two slots and
        # the publisher correctly published both. A row-id key is the wrong key. The one
        # that matters is the CONTENT going to an ACCOUNT.
        #
        # The lasso case shows the window is not a day: the same caption went out on
        # 08-14, 08-18 and 09-01.
        #
        # Written BEFORE the call, deliberately. If the process dies between this stamp
        # and the network call, the row never publishes again. That is the correct
        # direction: a post that silently fails to go out is recoverable by a human, a
        # post that goes out twice on a client's account is not.
        _content_key = _published_content_key(account, row)
        if _content_key:
            try:
                _seen = _kv_default().get(_content_key, "")
            except Exception as exc:  # an unreadable ledger is not proof of a duplicate
                skipped.append(row_id)
                print(f"[calendar-autopublish] content ledger unreadable for {row_id} "
                      f"({type(exc).__name__}); refusing to publish; duplicate cleanup not attempted")
                _release_content_ledger_claim(
                    store, gym_id, row, "content_ledger_unreadable", claim_token)
                continue
            # Same ROW re-entering this path is the row-claim's business, not a content
            # duplicate: mark_publishing already owns exactly-once for one row. What this
            # guard exists to catch is a DIFFERENT row carrying the same words to the same
            # account, which is exactly the Tough Temple shape.
            _seen_row = str(_seen).split("|", 1)[0] if _seen else ""
            if _seen and _seen_row and _seen_row == str(row_id):
                _seen = ""
            if _seen:
                _seen = str(_seen).split("|", 1)[-1]
                skipped.append(row_id)
                _duplicate_marked = _mark_duplicate_content(
                    store, gym_id, row_id, _seen, claim_token)
                _idx_alert = (
                    f"DUPLICATE CONTENT REFUSED: {gym_id} {account.platform} row "
                    f"{row_id} ({row.get('post_date')}) carries a caption already "
                    f"claimed or published to this account on {_seen}. Not sent. "
                    + ("The duplicate row is soft-deleted so it cannot be retried. "
                       if _duplicate_marked else
                       "Cleanup was not confirmed; the row requires reconciliation. ")
                    + f"This is the guard added after the "
                    f"2026-09-05 Tough Temple double post.")
                print(f"[calendar-autopublish] {_idx_alert}")
                try:
                    from .ops_alerts import alert as _oa
                    _oa(_idx_alert)
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                _kv_default().set(_content_key, f"{row_id}|{_now_stamp()}")
            except Exception as e:  # noqa: BLE001
                # Could not stamp: refuse rather than risk a repeat. Fail closed.
                skipped.append(row_id)
                print(f"[calendar-autopublish] content stamp failed for {row_id} "
                      f"({type(e).__name__}); refusing to publish rather than risk a "
                      f"duplicate")
                _release_content_ledger_claim(
                    store, gym_id, row, "content_stamp_failed", claim_token)
                continue

        draft = _draft_for(row)
        draft.account_key = account.key
        draft.platform = account.platform

        # PUBLISH-TIME RECHECK (AGENT_CALENDAR_GRADE, default OFF)
        # Re-validates the OUTBOUND caption immediately before the network call.
        # CONSOLIDATED (Blake's WIRING.md, 2026-08-27): the former inline
        # thin-caption floor + avatar rail now live in publish_guard.check —
        # ONE rail implementation for empty/thin captions, copy violations,
        # proof-without-mention, multi-ask, the avatar rail, and media_ready.
        # Stories stay exempt from the caption rails (empty-body BY DESIGN; the
        # '26 empty IG captions' in the 2026-08-27 audit were story rows —
        # verified against content_calendar via late_post_id). A violation
        # reverts the row to pending with a reject_reason and ONE deduped alert
        # per (gym, code); the caption_ledger cooldown recheck is unchanged.
        if config.calendar_grade_enabled():
            from agent import caption_ledger as _cl, ops_alerts as _oa
            from agent import publish_guard as _pg
            _cap = draft.caption or ""
            # is_blocked = the fuzzy cooldown PLUS the hard 180-day verbatim
            # rule (report-card build 2026-08-28). Same-date records are the
            # row's own staging stamp / its cross-post siblings and never
            # block (caption_ledger same-date rule).
            # Match planner and publisher semantics: the cooldown switch owns
            # this ledger, and stories have no outbound caption. Calendar grade
            # alone must not activate a stale ledger or self-block a story.
            if (config.caption_cooldown_enabled() and not _is_story_row(row)
                    and _cl.is_blocked(gym_id, _cap, row.get("post_date", ""),
                                       db=None)):
                _reverted = _revert_to_pending(row_id=row_id, store=store,
                                               gym_id=gym_id,
                                               expected_claim_token=claim_token,
                                               reject_reason="caption cooldown")
                if _reverted:
                    _oa.alert(
                        f"publish recheck: row {row_id} caption on cooldown, "
                        f"reverted to pending"
                    )
                else:
                    recovery_required.append(row_id)
                # a failed revert already alerted (loudly, and with the fact that
                # the post never went out) inside _revert_to_pending
                failed.append(row_id)
                continue
            _payload = _pg.PublishPayload(
                row_id=str(row_id), gym_id=gym_id, platform=draft.platform,
                caption=_cap, category=(row.get("category") or ""),
                mentions=_planned_mentions(_cap, gym_id, row.get("category")),
                media_ready=bool((row.get("image_url") or "").strip()),
                is_story=_is_story_row(row),
                post_date=str(row.get("post_date") or "")[:10])
            _viols = _pg.check(_payload)
            if _viols:
                _reason = "publish_guard: " + ", ".join(_viols)
                _reverted = _revert_to_pending(row_id=row_id, store=store,
                                               gym_id=gym_id,
                                               expected_claim_token=claim_token,
                                               reject_reason=_reason)
                if not _reverted:
                    recovery_required.append(row_id)
                for _code in _viols:
                    _alert_publish_blocked(gym_id, row_id, _code,
                                           reverted=_reverted)
                failed.append(row_id)
                continue
            # Guard passed: the block state changed, so re-arm the deduped
            # alerts for this gym (a future violation alerts again).
            _clear_publish_blocked(gym_id)

        # Review evidence can change after staging or even after this row was
        # claimed. Read it freshly at the last pre-network boundary for BOTH
        # external publishers. Calendar approval cannot substitute for asset review.
        if not _drive_asset_usable_at_send(row, gym_id):
            _reason = "media_asset_review_required"
            _reverted = _revert_to_pending(
                store, row_id, reject_reason=_reason, gym_id=gym_id,
                expected_claim_token=claim_token,
                revert_status="approved" if approved_only else "pending")
            if not _reverted:
                recovery_required.append(row_id)
            _alert_publish_blocked(gym_id, row_id, _reason, reverted=_reverted,
                                   revert_status="approved" if approved_only else "pending")
            failed.append(row_id)
            continue

        # Client-specific media reuse rules apply across EVERY outbound platform,
        # even when a planner used a legacy/small-library fallback.
        from .media_reuse_policy import publish_hold_reason
        _reuse_reason = publish_hold_reason(
            row, gym_id, store, now=now,
            library_path=(account.library_path()
                          if callable(getattr(account, "library_path", None))
                          else getattr(account, "library_path", None)))
        if _reuse_reason:
            _reverted = _revert_to_pending(
                store, row_id, reject_reason=_reuse_reason, gym_id=gym_id,
                expected_claim_token=claim_token,
                revert_status="approved" if approved_only else "pending")
            if not _reverted:
                recovery_required.append(row_id)
            _alert_publish_blocked(gym_id, row_id, _reuse_reason, reverted=_reverted,
                                   revert_status="approved" if approved_only else "pending")
            failed.append(row_id)
            continue

        # Default-OFF byte authority: a failed check is a proven pre-network
        # hold. Only release a lease whose persisted token this run owns.
        from . import forward_media_guard as _fmg
        from .forward_media_publish import authorize as _authorize_media
        if _fmg.enabled():
            try:
                if draft.creative_public_url != row.get("image_url"):
                    raise _fmg.ForwardMediaVerificationHold("outgoing draft media differs from row")
                _authorize_media(store, row, claim_token)
            except _fmg.ForwardMediaVerificationHold as exc:
                reason = ("forward_media_duplicate" if isinstance(
                    exc, _fmg.ForwardMediaDuplicateHold) else "forward_media_verification")
                forward_media_holds[row_id] = reason
                reverted = False
                if claim_token:
                    reverted = _revert_to_pending(
                        store, row_id, reject_reason=reason, gym_id=gym_id,
                        expected_claim_token=claim_token,
                        revert_status="approved" if approved_only else "pending")
                if not reverted:
                    recovery_required.append(row_id)
                _alert_publish_blocked(gym_id, row_id, reason, reverted=reverted)
                failed.append(row_id)
                continue

        # CAPTION TRACE (pure logging, WIRING.md 2026-08-27): stage-by-stage
        # visible-length for the outbound caption, so a caption that goes
        # missing between the row and the API call is grep-able as
        # "CAPTION LOST <stage>". A STORY's caption travels ON its media
        # (the API body is empty by design), so its traced value is the burned
        # caption — never a false LOST.
        from .caption_trace import trace_publish as _trace_publish
        with _trace_publish(row_id, getattr(account, "platform", "")) as _tr:
            _tr.t("row_loaded", row.get("caption") or "")
            _tr.t("caption_resolved", draft.caption or "")
            _tr.t("platform_payload_built", draft.caption or "")
            try:
                # ROUTE BY GYM: LASSO publishes via the Meta-direct lane (unchanged). A
                # CLIENT gym publishes to ITS OWN connected IG/FB via Zernio. The zernio
                # publisher self-gates on AGENT_ZERNIO_PUBLISH + AGENT_PUBLISH_ENABLED
                # (returns would_publish when off), so a client row is never sent live
                # unless both are armed.
                #
                # CLIENT LANE = PUBLISH NOW, ALWAYS (audit 2026-08-25 CRITICAL). The lane
                # only reaches here once the row's own slot has ARRIVED (the slot gate above;
                # publish_client_gyms no longer bypasses it with catch_all), so firing
                # immediately IS firing at the slot time — for manual approvals AND
                # autonomous gyms alike. Handing Zernio a FUTURE scheduledFor is what broke
                # trust twice: (a) pre-approved posts swept at the day's first tick fired at
                # ~midnight instead of their slot (Dale: "the times ECHO lists as publish
                # time are not accurate"), and (b) a scheduled hand-off was immediately
                # marked 'published' with a published_at that was a lie, hours before the
                # post existed on the feed, with no reconcile if Zernio dropped it. Publish
                # now at slot time makes published_at truthful and needs no reconcile.
                _tr.t("api_request", draft.caption or "")
                # LASSO routing is FLAG-SPLIT (AGENT_LASSO_VIA_ZERNIO): flag OFF
                # (default) keeps LASSO on the Meta-direct publisher, byte-for-byte.
                # Flag ON sends a lasso row through the SAME zernio publisher as a
                # client row — this single choke point makes a Meta-direct publish
                # of a lasso calendar row IMPOSSIBLE under the flag no matter which
                # caller reached here (WHY: a Meta-direct post reads as an external
                # second publisher in Zernio analytics and taints metrics_sync's
                # LASSO months for the learning loop).
                # A successful precheck grants no ambient provider permission.
                # Bind the exact claimed row/token to this one lower invocation;
                # the scope re-reads authority and closes on every exit.
                from contextlib import nullcontext
                from .forward_media_publish import authorized_send as _authorized_send
                with (_authorized_send(store, row, claim_token)
                      if _fmg.enabled() else nullcontext()):
                    if account.key.startswith("lasso") and \
                            not config.lasso_via_zernio_enabled():
                        result = publisher(draft, account)
                    else:
                        result = zernio_publish(draft, account, scheduled_for=None)
            except Exception as e:
                if isinstance(e, _fmg.ForwardMediaVerificationHold):
                    forward_media_holds[row_id] = (
                        "forward_media_duplicate" if isinstance(
                            e, _fmg.ForwardMediaDuplicateHold)
                        else "forward_media_verification")
                # A deterministic Zernio preflight refusal (missing/expired account,
                # profile/page/media) happens before create_post is called, so it is
                # safe to release the owned claim for a later tick after repair. Keep
                # post-create exceptions held: a timeout or malformed 2xx may have
                # created a real post and an automatic retry could duplicate it.
                if getattr(e, "definitive_no_post", False):
                    _reason = f"provider preflight proved no post: {type(e).__name__}: {e}"
                    _reverted = _revert_to_pending(
                        store, row_id, reject_reason=_reason, gym_id=gym_id,
                        expected_claim_token=claim_token,
                        revert_status="approved" if approved_only else "pending")
                    if not _reverted:
                        recovery_required.append(row_id)
                    _alert_publish_blocked(
                        gym_id, row_id, _reason, reverted=_reverted,
                        revert_status="approved" if approved_only else "pending")
                    failed.append(row_id)
                    _note_repeat_failure(row_id, gym_id, e)
                    continue
                # Once a publisher is called, an exception is ambiguous: a timeout may
                # arrive after the provider accepted the post. Retrying can create a
                # duplicate, so retain the owned claim for reconciliation.
                failed.append(row_id)
                recovery_required.append(row_id)
                print(f"[calendar-autopublish] publish failed for row {row_id}: "
                      f"{type(e).__name__}: {e}")
                _alert_ambiguous_publish(gym_id, row_id,
                                         f"publisher raised {type(e).__name__}")
                _note_repeat_failure(row_id, gym_id, e)
                continue

        ok = getattr(result, "ok", False)
        mode = getattr(result, "mode", "")
        # ONLY a real 'published' counts. 'would_publish' means a gate was off inside
        # publish() before the network call, so it can safely revert the claim.
        if ok and mode == "published":
            try:
                # Only a Zernio 409 content-hash dedup may be stamped published with no
                # post id — Zernio told us the content is already live but named no id.
                # Every other lane must carry a real id or the store refuses (see
                # mark_published). The kwarg is passed ONLY in that case so the call
                # keeps its historic 3-arg shape for every other store implementation.
                _mp_kwargs = ({"allow_missing_post_id": True}
                              if getattr(result, "dedup", False) else {})
                if claim_token:
                    _mp_kwargs["expected_claim_token"] = claim_token
                store.mark_published(row_id, getattr(result, "media_id", ""),
                                     _now_iso(now), **_mp_kwargs)
            except Exception as e:
                # The post went out but we could not record it. Do NOT revert (that
                # would re-publish next run — it already published live). DEFECT 2
                # (audit 2026-08-30): this used to be print-only despite the comment
                # already saying "report it loudly instead" — for up to 2h (until
                # sweep_stuck_publishing's STALE_PUBLISHING_SECONDS backstop fires) a
                # LIVE post showed as neither published nor failed. Alert directly
                # here instead of waiting on the sweep. The exactly-once claim above
                # (mark_publishing already flipped this row out of pending/approved)
                # means this same row can never re-enter this branch, so one direct
                # alert per row cannot storm even across 100 gyms.
                failed.append(row_id)
                print(f"[calendar-autopublish] published row {row_id} but the "
                      f"mark_published write failed: {type(e).__name__}: {e}")
                try:
                    from . import ops_alerts as _oa
                    _oa.alert(
                        f"calendar row {row_id} (gym {gym_id}) PUBLISHED live but "
                        f"the mark_published write failed: {type(e).__name__}: {e}. "
                        "It will show stuck in 'publishing' in the portal until the "
                        "2h stale sweep catches it or a human fixes it by hand — it "
                        "is NOT reverted (that would republish a post already live).")
                except Exception:
                    pass
                # A generic write outage is operational noise, not a safe autonomous
                # code-fix request.  The store's 409 is different: it proves the live
                # provider result and the calendar's guarded terminal transition are
                # inconsistent.  Seed that exact row for FIXER without changing the
                # retained claim when intake is unavailable.
                _submit_published_state_conflict(
                    gym_id=gym_id, row=row, error=e)
                continue
            published.append(row_id)
            published_accounts.add(account.key)
            if daily_cap:
                _bump_pub_count(gym_id, run_date)
        elif mode == "would_publish" or _result_proves_no_post(result):
            # `would_publish` is the publisher's pre-network kill-switch contract.
            # A provider rejection may also opt into definitive_no_post only when its
            # API guarantees that no post exists. Both are safe to retry.
            _reason = ("publisher gate prevented network attempt" if mode == "would_publish"
                       else f"provider rejection proved no post: {getattr(result, 'detail', '')}")
            reverted = _revert_to_pending(
                store, row_id, reject_reason=_reason, gym_id=gym_id,
                expected_claim_token=claim_token,
                revert_status="approved" if approved_only else "pending")
            if not reverted:
                recovery_required.append(row_id)
            failed.append(row_id)
            _note_repeat_failure(
                row_id, gym_id,
                RuntimeError(f"soft publish failure: ok={ok!r} mode={mode!r}"))
        else:
            # A normal return is still ambiguous unless the adapter explicitly proves
            # no post exists. Keep the claim so a later tick cannot resend it.
            failed.append(row_id)
            recovery_required.append(row_id)
            detail = f"publisher returned ok={ok!r} mode={mode!r}"
            _alert_ambiguous_publish(gym_id, row_id, detail)
            _note_repeat_failure(row_id, gym_id, RuntimeError(detail))

    # ONE lightweight Slack "posted" notice, matching the auto-approve notice style.
    # Only sent when something actually published. Never carries a token or secret.
    if notifier is not None and published:
        accts = ", ".join(sorted(published_accounts))
        try:
            notifier.post_notice(
                f"Calendar auto-published ({len(published)}): {accts} | {run_date}")
        except Exception as e:
            print(f"[calendar-autopublish] Slack notice failed: "
                  f"{type(e).__name__}: {e}")

    return {"ok": True, "published": published, "skipped": skipped,
            "failed": failed, "waiting": waiting,
            "held": bool(recovery_required or forward_media_holds),
            "forward_media_holds": forward_media_holds,
            "recovery_required": recovery_required, "date": run_date}


REPEAT_FAILURE_ALERT_AT = 5     # consecutive failures before a human is alerted

# CLIENT catch-up window: a gym owner who approves a post AFTER its day passed still
# gets it published (up to this many days late) instead of stranding it forever.
CLIENT_CATCHUP_DAYS = 7


def _note_repeat_failure(row_id, gym_id, exc, now=None):
    """Count consecutive publish failures per row (kv) and ALERT a human when a row
    keeps failing — the lane retries every ~1 min, so without this a broken row
    (bad payload, missing page, dead account) fails silently forever behind a print.

    DEDUPED PER (row, failure reason) PER DAY (topfuel_fb 'no Facebook page selected',
    2026-08-27): a stuck row that needs a HUMAN action (pick a page, reconnect) used
    to be able to re-alert on every attempt; now it alerts once when it crosses the
    threshold and then at most once per UTC day per distinct reason while it stays
    stuck. A NEW failure reason on the same row alerts on its own (it is new signal).
    The RETRY behavior is untouched — the row keeps retrying every run; only the
    Slack noise is capped. Best effort: never raises, never blocks the lane. The
    counter is cleared lazily (a published row simply stops being counted)."""
    try:
        import hashlib
        from datetime import datetime, timezone
        from . import db, ops_alerts
        key = f"pubfail_{row_id}"
        n = int(db.kv_get(key) or 0) + 1
        db.kv_set(key, str(n))
        if n < REPEAT_FAILURE_ALERT_AT:
            return
        reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        rhash = hashlib.sha256(reason.encode("utf-8", "replace")).hexdigest()[:12]
        day = (now or datetime.now(timezone.utc)).date().isoformat()
        dedup_key = f"pubfail_alerted_{row_id}_{rhash}_{day}"
        if db.kv_get(dedup_key):
            return
        db.kv_set(dedup_key, "1")
        ops_alerts.alert(
            f"calendar row {row_id} (gym {gym_id}) has failed to publish "
            f"{n} times in a row: {reason}. "
            "It will keep retrying, but a human should look — this is usually "
            "a payload/connection problem, not a blip.")
    except Exception:
        pass


# ---- listener slot-fire lane -------------------------------------------------
# The scheduler loop fires run_daily (the DRAFT draw) once a day. That is far too
# coarse for time-of-day spacing and would ORPHAN every later-slot row. So the
# always-on listener loop also calls run_slot_ticks() on its ~1-min cadence: as
# each SPRINT_SLOT_TIME is reached it publishes that slot's due rows, deduped per
# (slot, day) via a kv marker so a slot fires at most once a day. The LAST slot
# runs with catch_all=True so every straggler for the day is swept (NO ORPHANS).

def _kv_default():
    """The real kv (agent.db) as a tiny get/set object. Injectable for tests."""
    from . import db

    class _KV:
        def get(self, key, default=""):
            return db.kv_get(key, default)

        def set(self, key, value):
            db.kv_set(key, value)

    return _KV()


def _now_stamp():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _published_content_key(account, row):
    """The idempotency key for CONTENT reaching ONE account, or "" when it cannot be
    formed.

    Scoped to the account, not the gym: the same caption legitimately cross-posts to a
    gym's Instagram and its Facebook, and blocking that would be a regression. Two rows
    carrying the same words to the SAME account is the defect.

    Uses the caption ledger's own verbatim normalisation so whitespace and case changes
    cannot slip a repeat past the guard. A story row has an empty body by design and is
    exempt: keying stories on content would collapse every story a gym ever posts."""
    try:
        caption = str(row.get("caption") or "").strip()
        fmt = str(row.get("format") or "feed").strip().lower()
        if not caption or fmt == "story":
            return ""
        from .caption_ledger import verbatim_hash
        platform = str(getattr(account, "platform", "") or "").strip().lower()
        key_acct = str(getattr(account, "key", "") or "").strip().lower()
        if not (platform or key_acct):
            return ""
        return f"published_content_{key_acct}_{platform}_{verbatim_hash(caption)}"
    except Exception:  # noqa: BLE001 - an unformable key means no guard, never a crash
        return ""


def _release_content_ledger_claim(store, gym_id, row, reason,
                                  expected_claim_token=None):
    """Retry after a PRE-NETWORK ledger fault without revoking a manual approval."""
    row_id = row["id"]
    previous = "approved" if row.get("status") == "approved" else "pending"
    try:
        fn = getattr(store, "release_content_ledger_claim", None)
        if not fn:
            updated = None
        else:
            try:
                updated = fn(
                    gym_id, row_id, previous, reason,
                    expected_claim_token=expected_claim_token)
            except TypeError:
                # Legacy injected test stores predate owned claim UUIDs. Production
                # Supabase transitions must never fall back to a tokenless write.
                from .portal_calendar_store import SupabaseCalendarStore
                if isinstance(store, SupabaseCalendarStore):
                    raise
                updated = fn(gym_id, row_id, previous, reason)
        if (isinstance(updated, dict) and str(updated.get("id")) == str(row_id)
                and str(updated.get("gym_id")) == str(gym_id)
                and updated.get("status") == previous):
            return True
    except Exception:
        pass
    try:
        from .ops_alerts import alert
        alert(f"calendar row {row_id} (gym {gym_id}) remains claimed after "
              f"a PRE-NETWORK ledger fault ({reason}); release was not confirmed. "
              "No publish was attempted by this invocation. Reconciliation required.")
    except Exception:
        pass
    return False


def _mark_duplicate_content(store, gym_id, row_id, seen_at,
                            expected_claim_token=None):
    """Take a refused duplicate OUT of the publish lane, reversibly.

    mark_publishing already flipped the row to 'publishing'; leaving it there would strand
    it exactly like the row this build spent the morning un-sticking. 'deleted' is the
    schema's soft delete, is excluded from LIVE_CALENDAR_STATUSES and from the publisher's
    own approved_only filter, and is reversible with one UPDATE."""
    # Portal set_status deliberately excludes publishing rows. Its empty result
    # used to be mistaken for success here; the fallback also clamps deleted to
    # pending. Neither method expresses this publisher-owned transition.
    reason = f"duplicate content refused: previous content claim {seen_at}"
    try:
        fn = getattr(store, "mark_duplicate_content", None)
        if not fn:
            updated = None
        else:
            try:
                updated = fn(
                    gym_id, row_id, reason,
                    expected_claim_token=expected_claim_token)
            except TypeError:
                # Keep bounded fake adapters usable without weakening the real store.
                from .portal_calendar_store import SupabaseCalendarStore
                if isinstance(store, SupabaseCalendarStore):
                    raise
                updated = fn(gym_id, row_id, reason)
        if not (isinstance(updated, dict) and str(updated.get("id")) == str(row_id)
                and str(updated.get("gym_id")) == str(gym_id)
                and updated.get("status") == "deleted"):
            print(f"[calendar-autopublish] duplicate cleanup not confirmed for {row_id}")
            return False
    except Exception as exc:  # cleanup failure never falls through to publish
        print(f"[calendar-autopublish] duplicate cleanup failed for {row_id}: {type(exc).__name__}")
        return False
    try:
        from . import db as _db
        _db.audit("duplicate_content_refused", gym_id, reason, gym_id)
    except Exception:  # noqa: BLE001
        pass
    return True


def _slot_fire_key(run_date, slot_time):
    return f"calendar_slotfire_{run_date}_{slot_time}"


def run_slot_ticks(run_date, *, gym_id="lasso", store=None, publisher=None,
                   notifier=None, now=None, kv=None):
    """
    Called on each listener loop tick. For every SPRINT_SLOT_TIME already reached
    (in POSTING_TIMEZONE at `now`) that has NOT yet fired today, publish that slot's
    due rows exactly once (kv-deduped per slot+day). The last slot fires with
    catch_all=True so nothing is orphaned. Self-guards on both publish flags via
    publish_due(). Returns a list of per-slot summaries (empty when nothing fired).

    `now`, `store`, `publisher`, `notifier`, and `kv` are injectable for tests.
    """
    if not config.calendar_autopublish_enabled():
        return []
    # LASSO-VIA-ZERNIO (AGENT_LASSO_VIA_ZERNIO): when armed, the zernio client lane
    # (publish_client_gyms) OWNS LASSO's calendar rows on the same ~1-min listener
    # cadence, so this Meta-direct slot lane stands down ENTIRELY for the lasso gym —
    # exactly ONE lane can ever claim a lasso row (no double publish, no lane race).
    # Slot-fire kv markers are not burned, so disarming the flag restores this lane
    # cleanly. Any other gym_id (none today) is untouched.
    if (gym_id or "lasso").strip() == "lasso" and config.lasso_via_zernio_enabled():
        return []
    if kv is None:
        kv = _kv_default()

    fired = []
    slots = list(SPRINT_SLOT_TIMES or [])
    if _lasso_three_feed_enabled(gym_id, run_date):
        # The direct-publisher fallback also needs ticks at the paired times.
        # Its old final 18:30 catch-all cannot send the 18:45 Story early.
        slots = sorted(set(slots) | {
            slot_time_for_row({"gym_id": "lasso", "format": fmt,
                               "slot_index": si, "post_date": run_date})
            for fmt in ("feed", "story") for si in (0, 1, 2)
        })
    last_slot = slots[-1] if slots else None
    for slot_time in slots:
        if not _slot_reached(slot_time, now):
            continue                              # this slot has not arrived yet
        key = _slot_fire_key(run_date, slot_time)
        try:
            already = kv.get(key, "")
        except Exception:
            already = ""                          # a kv hiccup must not orphan a slot
        if already == "done":
            continue                              # this slot already fired today
        summary = publish_due(run_date, gym_id=gym_id, store=store,
                              publisher=publisher, notifier=notifier, now=now,
                              catch_all=(slot_time == last_slot))
        # Mark fired ONLY on an armed (ok) run so a flag-off no-op does not burn the
        # slot; a later armed tick can then still fire it.
        if summary.get("ok"):
            try:
                kv.set(key, "done")
            except Exception as e:
                print(f"[calendar-autopublish] slot-fire kv write failed "
                      f"({slot_time}): {type(e).__name__}: {e}")
        fired.append(summary)
    return fired


# ---- client-gym publish lane (Zernio) ---------------------------------------
# LASSO auto-publishes its own calendar via Meta-direct (above) — unless
# AGENT_LASSO_VIA_ZERNIO is armed, in which case LASSO joins THIS lane like an
# eighth client gym and the Meta-direct lasso lanes stand down. A CLIENT gym's
# path: the client APPROVES a post in the portal, and Echo then publishes it to
# the gym's OWN connected IG/FB via Zernio at the row's slot time. This function
# is the client counterpart to run_slot_ticks.

def client_gym_bases():
    """Distinct client-gym tenant bases (non-LASSO) from the account registry:
    eng_ig / eng_fb -> 'eng'. LASSO is excluded (it has its own Meta-direct lane).

    ECHO CLIENTS ONLY (2026-09-11 incident): a dynamic-registry base is returned only
    when echo_clients says the gym is an Echo client (an echo_gym_settings row); the
    registry itself was polluted with ~110 ads-only gyms by autoregister sweeping
    echo_intake_tokens. Hardcoded ACCOUNTS bases are trusted without a plane read.
    Fails closed: an unreadable client universe yields the hardcoded bases only."""
    from .accounts import all_accounts
    from .account_key_resolve import resolve as resolve_key
    from . import echo_clients
    from .account_key_doctor import _is_internal_base
    seen, bases = set(), []
    for a in all_accounts():
        k = a.key or ""
        if k.startswith("lasso"):
            continue
        base = k
        for suf in ("_ig", "_fb"):
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        base = resolve_key(base)
        if base and not _is_internal_base(base) and base not in seen:
            seen.add(base)
            bases.append(base)
    return echo_clients.only_client_bases(bases)


# Stale-'publishing' ALERT sweep (audit MEDIUM): a worker that dies between the
# atomic claim and the publish result leaves its row in 'publishing' forever —
# silent, unrecoverable, and invisible to the client. This sweep NEVER auto-reverts
# (in the mark_published-write-failure case the post actually went out; a revert
# would double-publish). It only ALERTS a human, once per row: first sighting
# records the time in kv; a row still stuck past the threshold alerts and is
# marked so it never re-alerts.
#
# THAT LAST SENTENCE WAS THE BUG (AUD-106, 2026-09-05). "Marked so it never re-alerts"
# and "cleared on recovery" are different promises, and only the first was implemented.
# The marker was written as the literal string "alerted" and cleared NOWHERE, so a row
# that stays stuck is muted permanently after one alert. Caught red-handed: the LASSO
# Instagram row d4574f62 carried stuck_publishing_d4574f62... = 'alerted' in production
# while STILL sitting in status 'publishing' with published_at NULL, post_date 2026-08-28
# -- eight days stranded, one alert, then silence. That is the same shape as the four
# safety nets that already shipped inert.
#
# Two changes: the marker now records WHEN the alert fired (a timestamp, never a magic
# word, so the value is comparable with the first-sighting stamp written above it), and a
# row still stuck after STALE_PUBLISHING_REALERT_SECONDS alerts again with its age. A row
# that leaves 'publishing' stops appearing in publishing_rows(), so its key simply goes
# unread; nothing has to clear it for the alert to be correct on the next genuine stall.

STALE_PUBLISHING_SECONDS = 2 * 3600   # 2h: far beyond the seconds-wide claim window
STALE_PUBLISHING_REALERT_SECONDS = 24 * 3600   # re-alert daily while it is still stuck


def sweep_stuck_publishing(*, store=None, kv=None, now=None, alert=None):
    """Alert (once per row) on any row stuck in 'publishing' past the threshold.
    Read-only on the calendar; never reverts, never publishes. Returns the row ids
    alerted this pass. All I/O injectable for tests."""
    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    if kv is None:
        kv = _kv_default()
    if alert is None:
        from .ops_alerts import alert as _alert
        alert = _alert
    now_dt = _local_now(now)
    alerted = []
    try:
        rows = store.publishing_rows() or []
    except Exception as e:
        print(f"[calendar-autopublish] stale-publishing sweep read failed: "
              f"{type(e).__name__}: {e}")
        return alerted
    for row in rows:
        rid = row.get("id")
        if not rid:
            continue
        key = f"stuck_publishing_{rid}"
        try:
            seen = kv.get(key, "")
        except Exception:
            seen = ""
        if seen.startswith("alerted"):
            # ALREADY ALERTED, BUT NOT FOREVER. Re-alert on a daily cadence while the row
            # is still stuck, so a stall that nobody actioned resurfaces instead of going
            # silent. Legacy value: a bare "alerted" with no timestamp (what the old code
            # wrote) is treated as "alerted just now" and re-alerts one interval later,
            # rather than being trusted forever or re-alerting instantly on every sweep.
            stamp = seen.partition(":")[2]
            try:
                last = datetime.fromisoformat(stamp) if stamp else now_dt
            except ValueError:
                last = now_dt
            # A stamp written by an older build (or by a test) can be naive while
            # _local_now is aware; subtracting the two raises TypeError, which the caller
            # would swallow and the row would go silent again -- the exact failure this
            # fix exists to remove.
            if (last.tzinfo is None) != (now_dt.tzinfo is None):
                last = last.replace(tzinfo=now_dt.tzinfo) if last.tzinfo is None \
                    else last.astimezone(None).replace(tzinfo=None)
            if not stamp:
                try:
                    kv.set(key, f"alerted:{now_dt.isoformat()}")
                except Exception:
                    pass
                continue
            if (now_dt - last).total_seconds() < STALE_PUBLISHING_REALERT_SECONDS:
                continue
            alert(f"calendar row {rid} (gym {row.get('gym_id')}, {row.get('account')}, "
                  f"{row.get('post_date')}) is STILL stuck in 'publishing'. It was first "
                  f"alerted on {stamp[:19]} and nothing has changed since. This row "
                  "cannot publish and cannot be seen by the client. Check the account's "
                  "feed: if the post is live, mark the row published by hand; if not, "
                  "flip it back to approved.")
            try:
                kv.set(key, f"alerted:{now_dt.isoformat()}")
            except Exception:
                pass
            alerted.append(rid)
            continue
        if not seen:
            try:
                kv.set(key, now_dt.isoformat())   # first sighting: start the clock
            except Exception:
                pass
            continue
        try:
            first = datetime.fromisoformat(seen)
        except ValueError:
            continue
        if (now_dt - first).total_seconds() < STALE_PUBLISHING_SECONDS:
            continue
        alert(f"calendar row {rid} (gym {row.get('gym_id')}, {row.get('account')}, "
              f"{row.get('post_date')}) has been stuck in 'publishing' for over "
              f"{STALE_PUBLISHING_SECONDS // 3600}h — a worker likely died mid-"
              "publish. NOT auto-reverted (the post may have gone out; a revert "
              "could double-post). Check the account's feed: if the post is live, "
              "mark the row published by hand; if not, flip it back to approved.")
        try:
            kv.set(key, f"alerted:{now_dt.isoformat()}")
        except Exception:
            pass
        alerted.append(rid)
    return alerted


def sweep_expired_rows(*, store=None, kv=None, now=None, alert=None,
                       catchup_days=None):
    """Alert ONCE PER GYM PER DAY on approved/pending rows that have aged past the
    catch-up window and can therefore never publish.

    THE GAP THIS CLOSES: due_rows only looks back `catchup_days` (7). A row older than
    that is never read, never claimed, never failed and carries no reject_reason — it
    just silently stops existing to the publisher. Live at the time of writing: 11
    APPROVED LASSO posts (2026-08-07 to 08-11) and 26 GritX rows died exactly this way,
    with nothing anywhere saying so. A client approved content that never went out and
    nobody found out.

    Read-only on the calendar: it never publishes, never reverts, never denies — a
    human decides whether to re-date or drop them. One digest line per gym per day
    (kv-deduped) so this can never become a storm. All I/O injectable."""
    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    if kv is None:
        kv = _kv_default()
    if alert is None:
        from .ops_alerts import alert as _alert
        alert = _alert
    days = CLIENT_CATCHUP_DAYS if catchup_days is None else int(catchup_days)
    now_dt = _local_now(now)
    cutoff = (now_dt.date() - timedelta(days=days)).isoformat()
    try:
        rows = store.expired_rows(cutoff) or []
    except Exception as e:  # noqa: BLE001 - a read failure must never crash the run
        print(f"[calendar-autopublish] expired-row sweep read failed: "
              f"{type(e).__name__}: {e}")
        return []
    by_gym = {}
    for row in rows:
        by_gym.setdefault(str(row.get("gym_id") or "?"), []).append(row)
    alerted = []
    today = now_dt.date().isoformat()
    for gym, gym_rows in sorted(by_gym.items()):
        # SELF-HEAL FIRST (Blake 2026-08-31: "the only human thing should be the gym
        # approving the post"): re-date expired rows into the next open future days
        # instead of asking a human to. Only when it cannot (flag off, no open days,
        # store error) does the old ask-a-human digest fire.
        if config.expired_auto_redate_enabled():
            try:
                moved, retired = _auto_redate_expired(gym, gym_rows, store, kv, now_dt)
            except Exception as e:  # noqa: BLE001 - self-heal never crashes the sweep
                print(f"[calendar-autopublish] expired auto-redate failed for {gym}: "
                      f"{type(e).__name__}: {e}")
                moved, retired = [], []
            handled = {r.get("id") for r in moved} | {r.get("id") for r in retired}
            gym_rows = [r for r in gym_rows if r.get("id") not in handled]
            if moved or retired:
                bits = []
                if moved:
                    span = f"{moved[0]['new_date']}..{moved[-1]['new_date']}" \
                        if len(moved) > 1 else moved[0]["new_date"]
                    approval_note = (
                        "approval proof cleared; any approved row returned to pending"
                        if config.approval_proof_enabled()
                        else "existing approval status preserved"
                    )
                    bits.append(f"re-dated {len(moved)} expired row(s) into open "
                                f"day(s) {span} ({approval_note})")
                if retired:
                    bits.append(f"retired {len(retired)} expired row(s) (unapproved "
                                "twice-expired, or redundant because every upcoming "
                                "day already has content)")
                suffix = ("Check current gym mode and approval state before release."
                          if config.approval_proof_enabled() else "No action needed.")
                alert(f"{gym}: {'; '.join(bits)}. {suffix}")
            if not gym_rows:
                alerted.append(gym)
                continue
        key = f"expired_rows_{gym}_{today}"
        try:
            if kv.get(key, ""):
                continue                          # already said today
        except Exception:  # noqa: BLE001
            pass
        oldest = min(str(r.get("post_date") or "") for r in gym_rows)
        approved = sum(1 for r in gym_rows
                       if str(r.get("status") or "").lower() == "approved")
        alert(f"{gym}: {len(gym_rows)} calendar row(s) ({approved} already APPROVED) "
              f"are past the {days}-day catch-up window and can never publish. Oldest "
              f"{oldest}. They were never read, claimed or failed, so nothing else "
              f"reports them. Re-date them to publish, or deny them to clear the book.")
        try:
            kv.set(key, "alerted")
        except Exception:  # noqa: BLE001
            pass
        alerted.append(gym)
    return alerted


REDATE_HORIZON_DAYS = 31        # plan-horizon law: never stage past today+31
REDATE_MAX_PER_GYM = 12         # per sweep, so a huge backlog drips over passes


def _auto_redate_expired(gym, gym_rows, store, kv, now_dt):
    """Move a gym's expired waiting rows onto the next OPEN future days (one per
    (account, format) per day, tomorrow .. today+31), preserving status so an approved
    post still publishes without re-approval. Each row is re-dated ONCE (kv marker); a
    row that expires a SECOND time was re-dated into the future and STILL never went
    out (held/failed repeatedly) or was never approved — an APPROVED second expiry gets
    one more chance forward (the gym said yes; we never drop it silently), while an
    UNAPPROVED second expiry is retired ('killed' + reject_reason) so the book stays
    clean. Returns (moved, retired) lists of {id, new_date} dicts.

    SIBLINGS MOVE TOGETHER (cross-day media guard, Blake 2026-08-31): a feed, its FB
    mirror and its paired story share ONE photo and ONE date by design. The old
    per-row walk re-dated each independently — the feed to the first open feed day,
    the mirror to a DIFFERENT open facebook day, the story to yet another — which put
    the same photo on multiple different days (the exact repeat a client spotted).
    Expired rows are now grouped by (original post_date, photo) and the whole group
    lands on ONE new date where every member's (account, format) slot is open.

    MEDIA-AWARE: when the group's photo ALREADY sits on another active future day
    (a rebuild or backfill re-picked it after these rows expired), moving the group
    would plant a cross-day duplicate — the exact repeat a client spotted. Blake
    2026-10-04: NO cross-day repeat, globally, no exceptions. The whole group stays
    put: UNAPPROVED members are retired as redundant under existing rules, while
    APPROVED members are left at their expired date (NOT patched, NOT killed) so the
    human digest names them; the gym's approval is preserved and its word is never
    silently dropped."""
    list_month = getattr(store, "list_month", None)
    patch = getattr(store, "patch_post_date", None)
    if list_month is None or patch is None:
        return [], []
    from . import media_guard
    today = now_dt.date()
    horizon = [today + timedelta(days=i) for i in range(1, REDATE_HORIZON_DAYS + 1)]
    months = sorted({d.isoformat()[:7] for d in horizon})
    occupied = set()
    media_days = {}      # photo key -> future dates it already occupies (active rows)
    for month in months:
        for r in (list_month(gym, month) or []):
            if str(r.get("status") or "").lower() in ("denied", "killed", "deleted"):
                continue
            occupied.add((str(r.get("account") or "").lower(),
                          str(r.get("format") or "feed").lower(),
                          str(r.get("post_date") or "")[:10]))
            mk = media_guard.media_key(r.get("image_url"))
            if mk:
                media_days.setdefault(mk, set()).add(
                    str(r.get("post_date") or "")[:10])

    moved, retired = [], []

    def _retire(rid):
        try:
            if store.set_status(gym, rid, "killed") is not None:
                retired.append({"id": rid, "new_date": ""})
        except Exception:  # noqa: BLE001
            pass

    # Group same-date siblings sharing one photo; an imageless row is its own group.
    groups = {}
    for row in gym_rows:
        pd = str(row.get("post_date") or "")[:10]
        mk = media_guard.media_key(row.get("image_url"))
        gkey = (pd, mk) if mk else (pd, f"row:{row.get('id')}")
        groups.setdefault(gkey, []).append(row)

    for (pd, gmk), members in sorted(groups.items(),
                                     key=lambda kv_: (kv_[0][0], str(kv_[0][1]))):
        if len(moved) >= REDATE_MAX_PER_GYM:
            break
        movers = []
        for row in sorted(members, key=lambda r: str(r.get("id") or "")):
            rid = row.get("id")
            status = str(row.get("status") or "").lower()
            already = ""
            try:
                already = kv.get(f"redated_{rid}", "")
            except Exception:  # noqa: BLE001
                pass
            if already and status != "approved":
                # second expiry, never approved: retire it so the book clears itself.
                _retire(rid)
                continue
            movers.append(row)
        if not movers:
            continue
        photo_key = gmk if not str(gmk).startswith("row:") else ""
        if photo_key and media_guard.enabled() and media_days.get(photo_key):
            # FAIL CLOSED (Blake 2026-10-04): the photo already lives on an active
            # future day, so moving ANY member of this group would plant a cross-day
            # duplicate — globally forbidden, approvals included. Retire the
            # unapproved members as redundant; leave approved members at their
            # expired date (no date patch, no kill) so the human digest names them.
            left = []
            for row in movers:
                if str(row.get("status") or "").lower() == "approved":
                    left.append(row)
                else:
                    _retire(row.get("id"))
            if left:
                print(f"[calendar-autopublish] {gym}: leaving {len(left)} APPROVED "
                      f"expired row(s) at their date (photo already booked on "
                      f"{sorted(media_days[photo_key])}); no re-date, human digest "
                      "will surface them")
            continue
        slots = [(str(r.get("account") or "").lower(),
                  str(r.get("format") or "feed").lower()) for r in movers]
        slot_day = next((d for d in horizon
                         if all((a, f, d.isoformat()) not in occupied
                                for a, f in slots)), None)
        if slot_day is None:
            # BOOK FULL: every day in the plan horizon already carries content for
            # these (account, format) slots — the expired group is REDUNDANT by
            # definition (nothing upcoming lacks a post), so keeping it can only rot.
            # Retire it (killed + reason) instead of bouncing it to a human digest
            # forever; the info alert names the count so nothing disappears silently.
            for row in movers:
                _retire(row.get("id"))
            continue
        for row in movers:
            rid = row.get("id")
            acct = str(row.get("account") or "").lower()
            fmt = str(row.get("format") or "feed").lower()
            try:
                if patch(rid, slot_day.isoformat()) is None:
                    continue                      # raced (claimed/published): skip
            except Exception:  # noqa: BLE001
                continue
            occupied.add((acct, fmt, slot_day.isoformat()))
            moved.append({"id": rid, "new_date": slot_day.isoformat()})
            try:
                kv.set(f"redated_{rid}", "1")
            except Exception:  # noqa: BLE001
                pass
        if photo_key:
            media_days.setdefault(photo_key, set()).add(slot_day.isoformat())
    return moved, retired


def publish_client_gyms(run_date, *, store=None, notifier=None, now=None,
                        zernio_publish=None):
    """Publish every client gym's APPROVED, due calendar rows to the gym's OWN IG/FB
    via Zernio, firing each row AT its own slot time with publishNow (2026-08-25: no
    future scheduledFor hand-offs — those fired pre-approved rows at ~midnight and
    stamped published_at before the post was live). Self-gating: publish_due checks
    AGENT_CALENDAR_AUTOPUBLISH + AGENT_PUBLISH_ENABLED, and the zernio publisher checks
    AGENT_ZERNIO_PUBLISH, so this is a no-op unless all three are armed. The ~1-min
    listener cadence drips each gym's day out slot by slot; a past-date approved row
    (catchup_days) is swept immediately. approved_only=True means an un-approved row is
    never published. Per-gym isolation: one gym's failure never blocks another.
    Returns per-gym summaries."""
    if not config.calendar_autopublish_enabled() or not config.publish_enabled():
        return []
    if not config.zernio_publish_enabled():
        return []
    bases = client_gym_bases()
    # LASSO-VIA-ZERNIO (AGENT_LASSO_VIA_ZERNIO): LASSO's own calendar rows join this
    # lane and publish through Zernio exactly like a client gym — same guard set
    # (slot gate, autonomy, catchup window, daily cap, exactly-once claim; the
    # billing gate fail-opens for lasso by design). The Meta-direct lasso lanes
    # (run_slot_ticks + the runner's once/day publish_due) stand down under the
    # flag, so this lane is the ONLY owner of a lasso row. Setup incomplete =>
    # publish_due HOLDS the lasso pass with one deduped alert (never a drop, never
    # a Meta-direct fallback). client_gym_bases itself stays lasso-free so every
    # other consumer (metrics_sync, inbox_alerts, jobs) is unchanged.
    if config.lasso_via_zernio_enabled():
        bases = ["lasso"] + [b for b in bases if b != "lasso"]
    out = []
    for base in bases:
        try:
            # BILLING GATE (Blake 2026-08-25): a gym whose subscription shows CANCELED
            # in Stripe holds ALL publishing (rows stay approved; nothing goes live).
            # Fail-open by design: only POSITIVE evidence of cancellation blocks — a
            # missing customer id or a flaky Stripe read never stops a paying gym.
            # kv-cached (~6h) so the ~1-min tick never hammers Stripe; alerts once.
            try:
                from .publish_billing_gate import publishing_blocked
                if publishing_blocked(base):
                    out.append({"ok": True, "gym": base, "billing_held": True,
                                "published": [], "failed": [], "waiting": []})
                    continue
            except Exception:  # noqa: BLE001 - the gate itself must never block the lane
                pass
            # The shared plane is authoritative when configured. A stale local ON
            # must never override the owner's newer Manual setting on another host.
            autonomous = False
            try:
                if config.portal_calendar_supabase_enabled() or store is not None:
                    if store is None:
                        from .portal_calendar_store import SupabaseCalendarStore
                        autonomy_store = SupabaseCalendarStore()
                    else:
                        autonomy_store = store
                    shared_mode = autonomy_store.gym_autonomy(base)
                    autonomous = shared_mode is True
                    if base == "lasso" and shared_mode is None:
                        # The internal LASSO cutover predates shared settings and
                        # its setup stamps local autonomy. An explicit shared OFF
                        # still wins; client gyms never use this legacy fallback.
                        from . import db as _db
                        autonomous = bool(_db.is_autonomous(base))
                else:
                    from . import db as _db
                    autonomous = bool(_db.is_autonomous(base))
            except Exception:
                autonomous = False
            # SLOT-GATED (audit 2026-08-25 CRITICAL): catch_all=False — a client row
            # publishes when ITS OWN slot arrives, not at the day's first sweep.
            # catch_all=True here made every pre-approved row fire at ~midnight (the
            # first tick of its post_date). No orphans: a same-day row whose slot has
            # passed is is_due on every later tick, and a PAST-DATE row (catchup_days)
            # is always due — the lane runs every ~1 min, so nothing is stranded.
            catchup_days, daily_cap = _client_publish_limits(
                base, run_date, config.client_daily_publish_cap())
            summary = publish_due(run_date, gym_id=base, store=store, notifier=notifier,
                                  now=now, catch_all=False,
                                  approved_only=not autonomous,
                                  zernio_publish=zernio_publish,
                                  catchup_days=catchup_days,
                                  daily_cap=daily_cap)
            summary["gym"] = base
            summary["autonomous"] = autonomous
            out.append(summary)
        except Exception as e:
            print(f"[client-autopublish] gym {base} failed: {type(e).__name__}: {e}")
            out.append({"ok": False, "gym": base, "error": type(e).__name__})
    return out
