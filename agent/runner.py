"""
Daily runner.

Once a day, for each connected account:
  - master flag OFF        -> do nothing.
  - voice doc missing      -> post ONE notice to Slack, draft nothing.
  - otherwise              -> draft exactly ONE post and post an approval card.

Nothing publishes here. This job only drafts and surfaces. Publishing happens
later, only on a human Approve, and only if the publish flag is armed.
"""

from datetime import datetime, timezone

from . import config, ops_alerts, schedule
from .accounts import active_accounts
from .daily_studio import build_daily_infographic_draft
from .social_proof import build_social_proof_draft
from .summit import build_summit_draft
from .drafter import DraftStatus, draft_post
from .library import pick_next
from .postlog import used_creatives_for
from .slack_surface import SlackPoster
from .stories import build_story_draft
from .voice import load_voice


def _same_content(a, b):
    """True when two drafts for the same (account, day, type) carry the same
    content, i.e. a re-run produced nothing genuinely new."""
    return (a.caption == b.caption
            and list(a.hashtags or []) == list(b.hashtags or [])
            and a.creative_path == b.creative_path
            and a.creative_public_url == b.creative_public_url
            and list(a.slides or []) == list(b.slides or [])
            and list(a.slide_urls or []) == list(b.slide_urls or []))


def _reconcile(draft, day_key, draft_type, store, poster):
    """
    Idempotency check for one freshly built PENDING draft (flag ON only).
    Returns (draft_to_post, existing_returned):
      - no existing PENDING draft for (account, day, type) -> (draft, None): post it.
      - existing draft with the SAME content -> (None, existing): zero new drafts,
        zero new cards; the existing draft is the run's result.
      - existing draft with DIFFERENT content (genuinely new, e.g. flags changed)
        -> (draft, None) after superseding the old one: its store record flips to
        SUPERSEDED and its Slack card is edited in place (header rewritten, buttons
        removed), so only the new card can be approved.
    """
    draft.day_key = day_key
    draft.draft_type = draft_type
    # EMPTY CAPTION GUARD (the 39ceaaf63d class): a feed draft with nothing to
    # say is not approvable material; it blocks instead of growing buttons.
    if (draft.status == DraftStatus.PENDING and not getattr(draft, "is_story", False)
            and not (draft.caption or "").strip()):
        draft.status = DraftStatus.BLOCKED
        draft.blocked_reason = "empty caption: nothing approved to say"
    if draft.status != DraftStatus.PENDING:
        # BLOCKED DEDUPE (retry-storm root): the same failing slot cards ONCE.
        # A repeat of an already-recorded block for (account, day, type) posts
        # no new card; recovery to PENDING supersedes normally below.
        finder = getattr(store, "find_for_day", None)
        existing = finder(draft.account_key, day_key, draft_type) if finder else None
        if (existing is not None and existing.status == DraftStatus.BLOCKED
                and existing.blocked_reason == draft.blocked_reason):
            print(f"[reconcile] {draft.account_key} {day_key} {draft_type}: "
                  "same block repeated; no new card")
            return None, existing
        return draft, None
    existing = store.find_pending(draft.account_key, day_key, draft_type)
    if existing is None:
        return draft, None
    if _same_content(existing, draft):
        return None, existing
    existing.status = DraftStatus.SUPERSEDED
    store.put(existing)
    poster.mark_superseded(existing)
    # Draft ids hash account + creative + schedule, not content, so the superseding
    # draft can collide with the record it replaces. Suffix until unique so the old
    # SUPERSEDED record (and its card's buttons) keep pointing at the OLD draft.
    while store.get(draft.draft_id) is not None:
        draft.draft_id += "r"
    return draft, None


def expire_past_due(store, poster, now=None):
    """
    CARD SELF-EXPIRY (no flag: queue hygiene, always on, like the heartbeat).
    Any PENDING draft whose scheduled post time has passed can no longer be
    approved as that slot's post: it flips to EXPIRED, its Slack card is edited
    in place (label rewritten, buttons removed), and it drops from the pending
    queue with one log line. This kills the zombie-queue class permanently and
    retroactively: the first sweep after deploy expires every stale card already
    in the store. Safety direction is one way only: expiry can never publish,
    and approvals already refuse an EXPIRED draft.
    """
    from datetime import datetime as _dt, timezone as _tz
    now = now or _dt.now(_tz.utc)
    today = now.date().isoformat()
    expired = []
    pending = getattr(store, "list_pending", None)
    if pending is None:
        return expired  # a store without a queue has nothing to expire
    for d in pending():
        past_due = False
        sched = (d.scheduled_for or "").strip()
        if sched:
            try:
                when = _dt.fromisoformat(sched)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_tz.utc)
                past_due = when < now
            except ValueError:
                past_due = bool(d.day_key and d.day_key < today)
        elif d.day_key:
            past_due = d.day_key < today
        if not past_due:
            continue
        d.status = DraftStatus.EXPIRED
        store.put(d)
        try:
            poster.mark_expired(d)
        except Exception:
            pass  # a missing Slack ref never blocks the sweep
        print(f"[expiry] {d.draft_id} ({d.account_key}, scheduled "
              f"{sched or d.day_key}) EXPIRED: past its slot, dropped from the queue")
        expired.append(d)
    if expired:
        print(f"[expiry] sweep expired {len(expired)} past-due card(s)")
    return expired


def _expire_stale(day_key, store, poster):
    """Kept for the existing call site; the real sweep is expire_past_due."""
    return expire_past_due(store, poster)


def _post_and_save(draft, store, poster, idempotent):
    """Post the card, capture its Slack message ref (flag ON), save if not blocked."""
    # Trust ladder wiring (both flags default OFF; nothing changes while off).
    if draft.status.value == "pending" and (
            config.trust_dryrun_enabled() or config.trust_autopublish_enabled()):
        from . import db
        from .accounts import get_account
        from .trust import auto_eligibility
        acct = get_account(draft.account_key)
        eligible, why = auto_eligibility(acct, draft) if acct else (False, "no account")
        if eligible and config.trust_autopublish_enabled():
            # GATED AUTOPUBLISH: calendar-routine only, level 1+, never a first
            # post, never book/comments/stories. The publisher's own draft-only
            # guard (AGENT_PUBLISH_ENABLED) still applies inside publish().
            from . import postlog
            from .meta_publisher import publish
            result = publish(draft, acct)
            draft.status = DraftStatus.APPROVED
            postlog.log_post(account_key=draft.account_key, platform=draft.platform,
                             caption=draft.caption,
                             media_id=getattr(result, "media_id", ""),
                             mode=result.mode, draft_id=draft.draft_id)
            db.audit("trust_autopublish", draft.draft_id, why, draft.account_key,
                     draft.day_key)
            poster.post_notice(
                f"AUTO PUBLISHED under trust for {draft.account_key} "
                f"({result.mode}): {why}. Draft {draft.draft_id}.")
            store.put(draft)
            return
        if eligible and config.trust_dryrun_enabled():
            draft.warnings = list(getattr(draft, "warnings", []) or []) + [
                "would auto-publish at current trust (dry run: still needs your tap)"]
            db.audit("trust_dryrun", draft.draft_id, why, draft.account_key,
                     draft.day_key)
    resp = poster.post_approval_card(draft) or {}
    if idempotent:
        draft.slack_channel = str(resp.get("channel") or "")
        draft.slack_ts = str(resp.get("ts") or "")
    # Blocked drafts are stored too (terminal records): that is what lets the
    # blocked dedupe stop a retry storm from re-carding the same failure.
    store.put(draft)


def _trust_startup_warning():
    if config.trust_autopublish_enabled():
        print("[trust] WARNING: AGENT_TRUST_AUTOPUBLISH is ARMED. Calendar routine "
              "posts on level 1+ accounts publish without a tap. Everything else "
              "still cards.")


def run_daily(poster=None, voice_path=None, library_path=None,
              scheduled_for=None, accounts=None, store=None):
    """
    Returns a list of Draft objects produced this run (one per account, or a
    blocked marker). Side effects: posts approval cards to Slack AND saves each
    non-blocked draft to the pending store so the listener can act on it later.
    """
    _trust_startup_warning()
    results = []

    if not config.master_enabled():
        # agent disarmed. say nothing publicly; just report state to the caller.
        return {"status": "disabled", "drafts": []}

    poster = poster or SlackPoster()
    voice = load_voice(voice_path or config.VOICE_DOC_PATH)

    if voice is None:
        poster.post_notice(":warning: Brand voice doc missing or empty. "
                           "Drafting nothing until it's in place.")
        return {"status": "no_voice", "drafts": []}

    if store is None:
        from .store import PendingStore
        store = PendingStore()

    when = scheduled_for or datetime.now(timezone.utc).isoformat()
    day_key = when[:10]  # YYYY-MM-DD, the day this post is for
    lib = library_path or config.LIBRARY_PATH

    # Card self-expiry (no flag): past-due pending cards drop before drafting.
    # Anchored to THIS run's reference time so simulated/scheduled runs agree.
    try:
        run_now = datetime.fromisoformat(when)
        if run_now.tzinfo is None:
            run_now = run_now.replace(tzinfo=timezone.utc)
    except ValueError:
        run_now = datetime.now(timezone.utc)
    expire_past_due(store, poster, now=run_now)

    # Idempotent daily drafts: OFF by default = behavior below is exactly today's.
    idempotent = config.idempotent_drafts_enabled()

    for account in (accounts or active_accounts()):
        # FLEET ISOLATION (flagless hardening): one account's API error,
        # missing token, or empty library never blocks another account's
        # cycle. An exception logs, alerts once, audits, and moves on.
        try:
            # Cadence gate FIRST: a skip day (default Saturday) produces no draft and no
            # card for this account.
            if not schedule.should_post_on(day_key):
                continue
            # Heartbeat (no flag, honest observability): this account's daily
            # run happened today. The morning check alerts on its absence.
            from .heartbeat import record_heartbeat
            record_heartbeat(account.key, day_key)

            # Multi-client resolution: an account with its own voice doc or library uses
            # them; empty fields (client zero, the LASSO accounts) fall back to the
            # globals above, so existing behavior is byte-for-byte identical.
            acct_voice = load_voice(account.voice_doc) if account.voice_doc else voice
            if acct_voice is None:
                poster.post_notice(f":warning: Voice doc missing for {account.key}. "
                                   "Drafting nothing for this account.")
                continue
            acct_lib = account.library_prefix or lib

            draft = None
            # Social proof card FIRST, but only on the weekly proof day and only when
            # its flag is armed with approved (permissioned + verified) entries. It is
            # dormant otherwise; None -> the normal paths below run untouched.
            if account.key.startswith("lasso"):
                # BOOK CAMPAIGN LEADS THE CALENDAR (AGENT_BOOK_CAMPAIGN_ENABLED, OFF):
                # armed, the day's book post takes posting priority and the normal
                # pillars below fill around it. Every draft still cards to Blake.
                from .book_campaign import build_book_draft
                draft = build_book_draft(account, day_key)
            if draft is None and account.key.startswith("lasso"):
                # PODCAST SLOT (AGENT_PODCAST_ENABLED, OFF): a newly detected
                # episode's release card takes the day's feed slot AFTER the
                # book campaign queue and BEFORE pillar rotation. Cards once
                # per episode, max one podcast draft per day, always held for
                # the tap. Dormant = None and the chain below runs unchanged.
                from .podcast_release import build_podcast_slot_draft
                draft = build_podcast_slot_draft(account, day_key)
            if draft is None and account.key.startswith("lasso"):
                draft = build_social_proof_draft(account, day_key, voice=acct_voice, poster=poster)
            # Summit campaign next (its own weekly day, inside the same daily cadence,
            # never additional). Dormant unless armed; auto-stops after 2026-11-08.
            if draft is None and account.key.startswith("lasso"):
                draft = build_summit_draft(account, day_key, voice=acct_voice)
            # Creative rotation + variety guard: dormant unless AGENT_ROTATION_ENABLED.
            # Armed, it picks WHICH approved creative today's draft proposes (window,
            # pillar alternation, gate-clean only); None -> the paths below run as today.
            if draft is None and account.key.startswith("lasso"):
                from .rotation import build_rotated_draft
                draft = build_rotated_draft(account, day_key, acct_voice, acct_lib, poster=poster)
            # For a LASSO account, try the fully-automated infographic path next. It is
            # dormant unless all three flags are armed; None -> fall back to the library
            # path unchanged. (A BLOCKED draft is still a draft: it surfaces, not falls back.)
            if draft is None and account.key.startswith("lasso"):
                draft = build_daily_infographic_draft(account, day_key)
            if draft is None:
                creative = pick_next(account, acct_lib, used_creatives_for(account.key))
                # Schedule the fallback draft to the same cadence slot.
                draft = draft_post(account, creative, schedule.scheduled_for(day_key), voice=acct_voice)

            existing = None
            if idempotent:
                draft, existing = _reconcile(draft, day_key, "feed", store, poster)
                if draft is None:
                    # Re-run, nothing new: the existing PENDING draft IS the result.
                    # No new draft, no new card.
                    results.append(existing)
            if draft is not None:
                _post_and_save(draft, store, poster, idempotent)
                results.append(draft)
            feed_draft = draft if draft is not None else existing

            # Stories: FULLY DORMANT unless AGENT_STORIES_ENABLED. Armed, draft one
            # 9:16 Story per account reusing the day's creative; PENDING, its own
            # approval card, clearly labeled STORY. Nothing publishes here.
            story = build_story_draft(account, day_key, feed_draft=feed_draft)
            if story is not None:
                if idempotent:
                    story, existing_story = _reconcile(story, day_key, "story", store, poster)
                    if story is None:
                        results.append(existing_story)
                if story is not None:
                    _post_and_save(story, store, poster, idempotent)
                    results.append(story)

        except Exception as e:
            print(f"[runner] {account.key} failed this cycle: "
                  f"{type(e).__name__}: {e}")
            ops_alerts.alert(f"account {account.key} failed its draft cycle: "
                             f"{type(e).__name__}: {e}. Other accounts continue.")
            try:
                from . import db as _db
                _db.audit("account_error", account.key,
                          f"{type(e).__name__}: {e}", account.key, day_key)
            except Exception:
                pass
            continue
    # Creative runway: dormant unless AGENT_RUNWAY_ENABLED. Armed, one line per
    # account with the day's cards (days of approved content left + projected
    # zero date); a runway error never takes the draft run down.
    if config.runway_enabled():
        from .runway import daily_runway
        for account in active_accounts():
            try:
                daily_runway(account.key, account.library_prefix or lib, day_key,
                             poster=poster)
            except Exception as e:
                print(f"[runway] {account.key}: {type(e).__name__}: {e}")

    # Token watchdog: dormant unless AGENT_TOKEN_WATCHDOG_ENABLED. Armed, one
    # READ-ONLY expiry check per daily cycle; a near-expiry token posts one ops
    # alert. A watchdog error never takes the draft run down.
    if config.token_watchdog_enabled():
        from .token_watchdog import check_tokens
        try:
            check_tokens(poster=poster)
        except Exception as e:
            print(f"[token-watchdog] check failed: {type(e).__name__}: {e}")

    return {"status": "drafted", "drafts": results}
