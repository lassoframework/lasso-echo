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

from datetime import datetime, time, timezone

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
    """The STABLE "HH:MM" slot time for one row (see slot_index_for_row)."""
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


def _local_now(now=None):
    """
    Current wall-clock time in config.POSTING_TIMEZONE as a timezone-aware
    datetime. `now` is injectable for tests: pass an ISO string or a datetime.
    Never uses Date.now-style nondeterminism when `now` is supplied.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config.POSTING_TIMEZONE)
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


def _slot_reached(slot_time, now=None):
    """True when the wall-clock "HH:MM" slot_time (in POSTING_TIMEZONE) is <= the
    current local time. `now` is injectable (see _local_now)."""
    local = _local_now(now)
    hh, mm = str(slot_time).split(":")
    slot = time(int(hh), int(mm))
    return local.timetz().replace(tzinfo=None) >= slot


def is_due(row, now=None):
    """
    True when a ROW's OWN stable slot time (slot_time_for_row) is <= the current
    local time. Compares the row's own slot, so a row is NEVER published before its
    slot and its slot never moves when a sibling publishes. `now` is injectable.
    """
    return _slot_reached(slot_time_for_row(row), now)


def _account_for(row, gym_id="lasso"):
    """Map a content_calendar row to its Echo account. The row's `account` column is a
    bare platform ('instagram'/'facebook'); the GYM comes from gym_id. LASSO keeps its
    hardcoded accounts (byte-for-byte the original behavior). A CLIENT gym resolves to
    ITS OWN account (<gym_id>_fb / <gym_id>_ig) so a client post never lands on LASSO's
    pages. Returns None when no such account exists (the row is then skipped, never
    misrouted)."""
    plat = (row.get("account") or "").strip().lower()
    base = (gym_id or "lasso").strip() or "lasso"
    if base == "lasso":
        return get_account("lasso_fb" if plat == "facebook" else "lasso_ig")
    suffix = "_fb" if plat == "facebook" else "_ig"
    return get_account(f"{base}{suffix}")


def scheduled_iso_for_row(row, now=None):
    """The ISO8601 go-live timestamp for a row: its post_date at its OWN stable slot
    time (slot_time_for_row), in POSTING_TIMEZONE. This is what Echo hands Zernio as
    `scheduledFor` so the client sees exactly when the post publishes. Returns '' when
    the row has no post_date."""
    from datetime import date as _date
    from zoneinfo import ZoneInfo
    post_date = (row.get("post_date") or "").strip()
    if not post_date:
        return ""
    slot = slot_time_for_row(row)
    hh, mm = str(slot).split(":")
    try:
        y, m, d = (int(x) for x in post_date.split("-"))
        tz = ZoneInfo(config.POSTING_TIMEZONE)
        return datetime(y, m, d, int(hh), int(mm), tzinfo=tz).isoformat()
    except (ValueError, TypeError):
        return ""


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
                zernio_publish=None):
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

    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    publisher = publisher or meta_publisher.publish
    if zernio_publish is None:
        from . import zernio_publisher
        zernio_publish = zernio_publisher.publish

    rows = store.due_rows(gym_id, run_date) or []

    published = []
    skipped = []
    failed = []
    waiting = []            # slot not arrived yet: left pending for a later run
    published_accounts = set()

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
                    stamper(row_id, scheduled_iso_for_row(row, now))
            except Exception as e:
                print(f"[calendar-autopublish] scheduled_at stamp failed for "
                      f"{row_id}: {type(e).__name__}: {e}")
        # Belt-and-braces: never touch a row already stamped published (the query
        # already excludes these, but a live race could still surface one).
        if row.get("published_at") or row.get("late_post_id"):
            skipped.append(row_id)
            continue

        # CLIENT approval gate: when approved_only (client gyms), a row that the client
        # has not approved yet is left UNTOUCHED (never claimed, never published). LASSO
        # (approved_only=False) is unchanged: it auto-publishes pending rows at slot time.
        if approved_only and (row.get("status") or "").strip().lower() != "approved":
            waiting.append(row_id)
            continue

        # SLOT GATE: publish nothing before the row's OWN stable slot time. A row
        # whose slot has not arrived is left UNTOUCHED (never claimed) so a later
        # tick this same day drips it out. catch_all bypasses the gate so the last
        # slot / the once-a-day draw sweeps every straggler (NO ORPHANS). A row whose
        # slot has passed is never skipped for timing.
        if not catch_all and not is_due(row, now):
            waiting.append(row_id)
            continue

        account = _account_for(row, gym_id)
        if account is None:
            # No mappable account: leave the row untouched (never claimed), skip it.
            skipped.append(row_id)
            continue

        # EXACTLY-ONCE CLAIM: only the winner proceeds to a network call.
        try:
            won = store.mark_publishing(row_id)
        except Exception as e:
            failed.append(row_id)
            print(f"[calendar-autopublish] claim failed for row {row_id}: "
                  f"{type(e).__name__}: {e}")
            continue
        if not won:
            # Another run/worker owns it (or it was already published). Skip.
            skipped.append(row_id)
            continue

        draft = _draft_for(row)
        draft.account_key = account.key
        draft.platform = account.platform

        try:
            # ROUTE BY GYM: LASSO publishes via the Meta-direct lane (unchanged). A
            # CLIENT gym publishes to ITS OWN connected IG/FB via Zernio, scheduled at
            # the row's own slot time so the go-live time is real and visible. The
            # zernio publisher self-gates on AGENT_ZERNIO_PUBLISH + AGENT_PUBLISH_ENABLED
            # (returns would_publish when off), so a client row is never sent live
            # unless both are armed.
            if account.key.startswith("lasso"):
                result = publisher(draft, account)
            else:
                result = zernio_publish(
                    draft, account, scheduled_for=scheduled_iso_for_row(row, now))
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
                # would re-publish next run). Report it loudly instead.
                failed.append(row_id)
                print(f"[calendar-autopublish] published row {row_id} but the "
                      f"mark_published write failed: {type(e).__name__}: {e}")
                continue
            published.append(row_id)
            published_accounts.add(account.key)
        else:
            try:
                store.mark_publish_failed(
                    row_id, revert_status="approved" if approved_only else "pending")
            except Exception as e:
                print(f"[calendar-autopublish] revert failed for row {row_id}: "
                      f"{type(e).__name__}: {e}")
            failed.append(row_id)

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
# LASSO auto-publishes its own calendar via Meta-direct (above). A CLIENT gym's
# path is different: the client APPROVES a post in the portal, and Echo then
# publishes it to the gym's OWN connected IG/FB via Zernio, SCHEDULED at the row's
# slot time. This function is the client counterpart to run_slot_ticks.

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


def publish_client_gyms(run_date, *, store=None, notifier=None, now=None,
                        zernio_publish=None):
    """Publish every client gym's APPROVED, due calendar rows to the gym's OWN IG/FB
    via Zernio, scheduled at each row's slot time. Self-gating: publish_due checks
    AGENT_CALENDAR_AUTOPUBLISH + AGENT_PUBLISH_ENABLED, and the zernio publisher checks
    AGENT_ZERNIO_PUBLISH, so this is a no-op unless all three are armed. A client post
    is due the moment it is approved (we hand Zernio the future slot time), so
    catch_all=True; approved_only=True means an un-approved row is never published.
    Per-gym isolation: one gym's failure never blocks another. Returns per-gym summaries."""
    if not config.calendar_autopublish_enabled() or not config.publish_enabled():
        return []
    if not config.zernio_publish_enabled():
        return []
    out = []
    for base in client_gym_bases():
        try:
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
            summary = publish_due(run_date, gym_id=base, store=store, notifier=notifier,
                                  now=now, catch_all=True,
                                  approved_only=not autonomous,
                                  zernio_publish=zernio_publish)
            summary["gym"] = base
            summary["autonomous"] = autonomous
            out.append(summary)
        except Exception as e:
            print(f"[client-autopublish] gym {base} failed: {type(e).__name__}: {e}")
            out.append({"ok": False, "gym": base, "error": type(e).__name__})
    return out
