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


def _order_key(row):
    """
    Deterministic ordering of a day+account's rows so slot assignment is stable
    across re-runs (no schema change, no time-of-day column). Order:
      1. feed before its paired story (a story shares the day; feed goes first),
      2. then stable by id (string compare).
    Returns a sort tuple; ties never occur because ids are unique.
    """
    fmt = (row.get("format") or "feed").strip().lower()
    is_story = 1 if fmt == "story" else 0        # feed(0) sorts before story(1)
    return (is_story, str(row.get("id") or ""))


def assign_slots(rows):
    """
    Given ONE day+account's content_calendar rows, return [(row, slot_time)]
    deterministically ordered, each row assigned an ordinal slot time.

    Ordinals map onto summit_queue.SPRINT_SLOT_TIMES ("07:30","12:30","18:30")
    in config.POSTING_TIMEZONE. If a day carries more rows than there are slot
    times, the mapping WRAPS (ordinal % len) so every row still gets a concrete
    time and none is starved. slot_time is an "HH:MM" string.
    """
    ordered = sorted(rows, key=_order_key)
    n = len(SPRINT_SLOT_TIMES)
    out = []
    for i, row in enumerate(ordered):
        slot_time = SPRINT_SLOT_TIMES[i % n] if n else "00:00"
        out.append((row, slot_time))
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


def is_due(slot_time, now=None):
    """
    True when a row's assigned "HH:MM" slot_time (in POSTING_TIMEZONE) is <= the
    current local time. A row is NEVER published before its slot; a row whose slot
    has passed is NEVER skipped. `now` is injectable (see _local_now).
    """
    local = _local_now(now)
    hh, mm = str(slot_time).split(":")
    slot = time(int(hh), int(mm))
    return local.timetz().replace(tzinfo=None) >= slot


def _account_for(row):
    """Map a content_calendar row's account to an Echo account. 'facebook' -> the
    LASSO FB page, everything else (incl. 'instagram') -> the LASSO IG account."""
    acct = (row.get("account") or "").strip().lower()
    if acct == "facebook":
        return get_account("lasso_fb")
    return get_account("lasso_ig")


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


def _slot_by_row_id(rows):
    """
    Assign each row a slot time, grouped by account so a feed and its paired story
    within an ACCOUNT get spaced ordinals (feed first). Returns {row_id: "HH:MM"}.
    """
    by_account = {}
    for row in rows:
        acct = (row.get("account") or "").strip().lower()
        by_account.setdefault(acct, []).append(row)
    slots = {}
    for acct_rows in by_account.values():
        for row, slot_time in assign_slots(acct_rows):
            slots[row.get("id")] = slot_time
    return slots


def publish_due(run_date, *, gym_id="lasso", store=None, publisher=None,
                notifier=None, now=None):
    """
    Read gym_id's content_calendar rows dated run_date and publish each unpublished
    one to live IG/FB, EXACTLY ONCE. Returns a summary dict.

    TIME-OF-DAY SPACING: a day's rows are not fired all at once. Within a day+account
    each row is assigned a deterministic ordinal slot time (SPRINT_SLOT_TIMES in
    POSTING_TIMEZONE); a row only publishes once its slot time is <= the current local
    time (`now`, injectable). Rows whose slot has not arrived are left pending and
    publish on a later run the same day (the runner already calls this on a cycle).
    Exactly-once is unchanged: a row still publishes at most once.

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

    rows = store.due_rows(gym_id, run_date) or []

    # TIME-OF-DAY SPACING: assign each row a deterministic slot time up front.
    slot_by_id = _slot_by_row_id(rows)

    published = []
    skipped = []
    failed = []
    waiting = []            # slot not arrived yet: left pending for a later run
    published_accounts = set()

    for row in rows:
        row_id = row.get("id")
        # Belt-and-braces: never touch a row already stamped published (the query
        # already excludes these, but a live race could still surface one).
        if row.get("published_at") or row.get("late_post_id"):
            skipped.append(row_id)
            continue

        # SLOT GATE: publish nothing before its assigned slot time. A row whose slot
        # has not arrived is left UNTOUCHED (never claimed) so a later run this same
        # day drips it out. A row whose slot has passed is never skipped for timing.
        slot_time = slot_by_id.get(row_id)
        if slot_time is not None and not is_due(slot_time, now):
            waiting.append(row_id)
            continue

        account = _account_for(row)
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
            result = publisher(draft, account)
        except Exception as e:
            # A real publish error: revert the claim so it retries next run.
            try:
                store.mark_publish_failed(row_id)
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
                store.mark_publish_failed(row_id)
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
