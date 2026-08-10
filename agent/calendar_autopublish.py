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

from datetime import datetime, timezone

from . import config
from . import meta_publisher
from .accounts import get_account
from .drafter import Draft, DraftStatus


def _now_iso(now=None):
    if now is not None:
        return now
    return datetime.now(timezone.utc).isoformat()


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


def publish_due(run_date, *, gym_id="lasso", store=None, publisher=None,
                notifier=None, now=None):
    """
    Read gym_id's content_calendar rows dated run_date and publish each unpublished
    one to live IG/FB, EXACTLY ONCE. Returns a summary dict.

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

    published = []
    skipped = []
    failed = []
    published_accounts = set()

    for row in rows:
        row_id = row.get("id")
        # Belt-and-braces: never touch a row already stamped published (the query
        # already excludes these, but a live race could still surface one).
        if row.get("published_at") or row.get("late_post_id"):
            skipped.append(row_id)
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
            "failed": failed, "date": run_date}
