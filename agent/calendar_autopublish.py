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
  - On failure OR a 'would_publish' result (a gate was off inside publish),
    mark_publish_failed(id) reverts the claim to 'pending' so it retries next run
    and records nothing. A row that already has published_at is NEVER re-published.

Nothing here logs a token or secret. The manual approval path is untouched.
"""

import os
from datetime import datetime, time, timedelta, timezone

from . import config
from . import meta_publisher
from .accounts import get_account
from .drafter import Draft, DraftStatus
from .summit_queue import SPRINT_SLOT_TIMES


def _now_iso(now=None):
    if now is not None:
        return now
    return datetime.now(timezone.utc).isoformat()


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
    which could collide both of a day's feeds onto one slot. Applies only while
    ECHO_CADENCE_2X_ENABLED is armed; flag off (or no slot_index on the row) is
    the pre-cadence hash path, byte-for-byte. Stories keep their midday slot."""
    fmt = (row.get("format") or "feed").strip().lower()
    si = row.get("slot_index")
    if (fmt == "feed" and si in (0, 1) and config.cadence_2x_enabled()):
        return config.cadence_slot_times()[int(si)]
    if n is None:
        n = len(SPRINT_SLOT_TIMES)
    if not SPRINT_SLOT_TIMES:
        return "00:00"
    return SPRINT_SLOT_TIMES[slot_index_for_row(row, n) % len(SPRINT_SLOT_TIMES)]


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
        new_url = story_reburn.reburn(row.get("source_media_url"), row.get("caption") or "",
                                      name, account.key)
        if not new_url:
            return None
        patch = getattr(store, "patch_image_url", None)
        if patch is not None:
            patch(row.get("gym_id"), row.get("id"), new_url)
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
    in-spec 1080x1080 card, re-host, and swap image_url so the platform accepts it. Works
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
    if not url or url.lower().endswith((".mp4", ".mov", ".webm")):
        return row                                        # video/no-image: not our job
    # PHASE 1 (fail-open): determine the aspect. If we cannot even read it, pass through
    # unchanged (unknown, not known-bad) — identical to the historical behavior; it will
    # self-heal on a later tick once R2/hosting recovers.
    try:
        from . import feed_image, media_host
        if not config.hosting_enabled():
            return row
        img = media_host.download_bytes(url)
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
    try:
        import tempfile
        out = os.path.join(
            tempfile.gettempdir(),
            f"feedfit_{account.key}_{row.get('id') or 'row'}__feed.jpg")
        safe = feed_image.make_feed_safe_from_bytes(img, out)
        hosted = media_host.host_media(safe, account.key) if safe else None
        if safe:
            try:
                os.remove(safe)
            except OSError:
                pass
        if not hosted or hosted == url:
            print(f"[calendar-autopublish] feed preflight could NOT re-host an out-of-aspect "
                  f"image for row {row.get('id')} ({w}x{h}); HOLDING (not sending a 400).")
            return None                                   # HOLD: never ship a known-bad image
        patch = getattr(store, "patch_image_url", None)
        if patch is not None:
            patch(row.get("gym_id"), row.get("id"), hosted)
        updated = dict(row)
        updated["image_url"] = hosted
        print(f"[calendar-autopublish] feed preflight reframed out-of-aspect image for "
              f"row {row.get('id')} ({w}x{h}) -> in-spec 1080x1080")
        return updated
    except Exception as e:  # noqa: BLE001 - known-bad image + reframe failed -> HOLD
        print(f"[calendar-autopublish] feed preflight failed to fix out-of-aspect row "
              f"{row.get('id')}: {type(e).__name__}: {e}; HOLDING (not sending a 400).")
        return None


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
    Never raises (a guard must never crash the lane)."""
    if (row.get("format") or "feed").strip().lower() != "story":
        return False
    try:
        from . import story_image
        return not story_image.story_media_carries_caption(
            row.get("image_url") or "", row.get("caption") or "")
    except Exception:
        return False  # fail OPEN to the existing behavior; never block on the guard


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


def _alert_publish_blocked(gym_id, row_id, code):
    """ONE deduped ops alert per (gym, violation code): kv key
    publish_blocked:<gym>:<code> fires once and stays quiet until the state
    changes (_clear_publish_blocked re-arms it when a row for the gym passes
    the guard). Best effort; never raises into the lane."""
    try:
        from . import db, ops_alerts
        key = f"publish_blocked:{gym_id}:{code}"
        if db.kv_get(key):
            return
        db.kv_set(key, str(row_id or "1"))
        ops_alerts.alert(
            f"publish guard: row {row_id} (gym {gym_id}) blocked at the publish "
            f"boundary ({code}); reverted to pending with reject_reason. Further "
            f"'{code}' blocks for this gym stay quiet until a post publishes clean.")
    except Exception:
        pass


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
        except Exception:  # noqa: BLE001 - a rail that cannot load must not publish
            pass

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
        if not catch_all and (future_date or
                              (not past_date and not is_due(row, now, gym_tz))):
            waiting.append(row_id)
            continue

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
        # to an in-spec 1080x1080 card BEFORE the network call, so Zernio never 400s on
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

        # EXACTLY-ONCE CLAIM: only the winner proceeds to a network call.
        try:
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
            # Another run/worker owns it (or it was already published). Skip.
            skipped.append(row_id)
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
            if _cl.is_blocked(gym_id, _cap, row.get("post_date", ""),
                              db=None):
                try:
                    store.mark_publish_failed(
                        row_id, revert_status="pending")
                except Exception:
                    pass
                _oa.alert(
                    f"publish recheck: row {row_id} caption on cooldown, "
                    f"reverted to pending"
                )
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
                try:
                    store.mark_publish_failed(row_id, revert_status="pending",
                                              reject_reason=_reason)
                except TypeError:
                    # older store/test fakes without the reject_reason kwarg
                    try:
                        store.mark_publish_failed(row_id, revert_status="pending")
                    except Exception:
                        pass
                except Exception:
                    pass
                for _code in _viols:
                    _alert_publish_blocked(gym_id, row_id, _code)
                failed.append(row_id)
                continue
            # Guard passed: the block state changed, so re-arm the deduped
            # alerts for this gym (a future violation alerts again).
            _clear_publish_blocked(gym_id)

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
                if account.key.startswith("lasso") and \
                        not config.lasso_via_zernio_enabled():
                    result = publisher(draft, account)
                else:
                    result = zernio_publish(draft, account, scheduled_for=None)
            except Exception as e:
                # A real publish error: revert the claim so it retries next run. A CLIENT
                # row (approved_only) reverts to 'approved' so a transient failure never
                # forces the client to re-approve; LASSO reverts to 'pending' (unchanged).
                try:
                    store.mark_publish_failed(
                        row_id, revert_status="approved" if approved_only else "pending")
                except Exception as re:
                    print(f"[calendar-autopublish] revert failed for row {row_id}: "
                          f"{type(re).__name__}: {re}")
                failed.append(row_id)
                print(f"[calendar-autopublish] publish failed for row {row_id}: "
                      f"{type(e).__name__}: {e}")
                _note_repeat_failure(row_id, gym_id, e)
                continue

        ok = getattr(result, "ok", False)
        mode = getattr(result, "mode", "")
        # ONLY a real 'published' counts. 'would_publish' means a gate was off inside
        # publish() -> treat as NOT published and revert the claim (retryable).
        if ok and mode == "published":
            try:
                store.mark_published(row_id, getattr(result, "media_id", ""),
                                     _now_iso(now))
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
                continue
            published.append(row_id)
            published_accounts.add(account.key)
            if daily_cap:
                _bump_pub_count(gym_id, run_date)
        else:
            try:
                store.mark_publish_failed(
                    row_id, revert_status="approved" if approved_only else "pending")
            except Exception as e:
                print(f"[calendar-autopublish] revert failed for row {row_id}: "
                      f"{type(e).__name__}: {e}")
            failed.append(row_id)
            # DEFECT 1 (audit 2026-08-30): a SOFT failure (publisher returned
            # normally with ok=False or mode != 'published', e.g. 'would_publish')
            # used to fall through here with no counter and no alert at all — only
            # the neighbouring EXCEPTION branch above called _note_repeat_failure,
            # so a row stuck soft-failing (never raising) looped every ~1-min tick
            # forever, completely invisibly. Feed it into the SAME strike counter,
            # naming ok/mode so the eventual alert says what actually happened.
            _note_repeat_failure(
                row_id, gym_id,
                RuntimeError(f"soft publish failure: ok={ok!r} mode={mode!r}"))

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
            "failed": failed, "waiting": waiting, "date": run_date}


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
    slots = SPRINT_SLOT_TIMES or []
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
    eng_ig / eng_fb -> 'eng'. LASSO is excluded (it has its own Meta-direct lane)."""
    from .accounts import all_accounts
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
        if base and base not in seen:
            seen.add(base)
            bases.append(base)
    return bases


# Stale-'publishing' ALERT sweep (audit MEDIUM): a worker that dies between the
# atomic claim and the publish result leaves its row in 'publishing' forever —
# silent, unrecoverable, and invisible to the client. This sweep NEVER auto-reverts
# (in the mark_published-write-failure case the post actually went out; a revert
# would double-publish). It only ALERTS a human, once per row: first sighting
# records the time in kv; a row still stuck past the threshold alerts and is
# marked so it never re-alerts.

STALE_PUBLISHING_SECONDS = 2 * 3600   # 2h: far beyond the seconds-wide claim window


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
        if seen == "alerted":
            continue                              # already alerted, human owns it
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
            kv.set(key, "alerted")
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
            # PER-GYM AUTONOMY (never portfolio-wide): the gym owner's own Autonomous
            # toggle. TWO sources, either arms it for THIS gym only: the portal's
            # Supabase echo_gym_settings row (the toggle in the client's calendar UI)
            # or Echo's own kv flag (POST /portal/<token>/autonomy). Autonomous ON =>
            # this gym's PENDING rows publish on their own at slot time (approved_only
            # off); every other gym still requires the client's approval. Any read
            # error defaults to NOT autonomous — approval required is the safe side.
            autonomous = False
            try:
                from . import db as _db
                autonomous = bool(_db.is_autonomous(base))
                if not autonomous and store is not None:
                    autonomous = bool(store.gym_autonomy(base))
                elif not autonomous and store is None:
                    from .portal_calendar_store import SupabaseCalendarStore
                    autonomous = bool(SupabaseCalendarStore().gym_autonomy(base))
            except Exception:
                autonomous = False
            # SLOT-GATED (audit 2026-08-25 CRITICAL): catch_all=False — a client row
            # publishes when ITS OWN slot arrives, not at the day's first sweep.
            # catch_all=True here made every pre-approved row fire at ~midnight (the
            # first tick of its post_date). No orphans: a same-day row whose slot has
            # passed is is_due on every later tick, and a PAST-DATE row (catchup_days)
            # is always due — the lane runs every ~1 min, so nothing is stranded.
            summary = publish_due(run_date, gym_id=base, store=store, notifier=notifier,
                                  now=now, catch_all=False,
                                  approved_only=not autonomous,
                                  zernio_publish=zernio_publish,
                                  catchup_days=CLIENT_CATCHUP_DAYS,
                                  daily_cap=config.client_daily_publish_cap())
            summary["gym"] = base
            summary["autonomous"] = autonomous
            out.append(summary)
        except Exception as e:
            print(f"[client-autopublish] gym {base} failed: {type(e).__name__}: {e}")
            out.append({"ok": False, "gym": base, "error": type(e).__name__})
    return out
