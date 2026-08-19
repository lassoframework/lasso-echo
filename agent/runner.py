"""
Daily runner.

Once a day, for each connected account:
  - master flag OFF        -> do nothing.
  - voice doc missing      -> post ONE notice to Slack, draft nothing.
  - otherwise              -> draft exactly ONE post and post an approval card.

Nothing publishes here. This job only drafts and surfaces. Publishing happens
later, only on a human Approve, and only if the publish flag is armed.
"""

import os
from datetime import datetime, timezone

from . import config, ops_alerts, schedule
from .accounts import active_accounts
from .library import VIDEO_EXTS
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


def _autonomous_publish(draft, store, poster):
    """PER-ACCOUNT AUTONOMY future-post path. When the draft's account has autonomy ON,
    auto-approve+publish it through the SAME gated approve path a portal/Slack approve
    uses (approvals.handle_action -> publisher.publish), then persist the resulting
    status. Returns True when it handled the draft (approved), False when autonomy is
    OFF or the attempt could not run, so the caller stores the draft PENDING as before.

    Honesty + safety:
      - The publisher's own AGENT_PUBLISH_ENABLED guard still applies inside publish():
        with it OFF the result is a would_publish (no network write), never a fake live.
      - A MediaNotReady / unauthorized-actor / unknown-account outcome returns False so
        the draft is stored PENDING and can still be approved manually (never lost).
    """
    from . import db as _db
    account_key = getattr(draft, "account_key", "") or ""
    if not _db.is_autonomous(account_key):
        return False
    from .accounts import get_account as _get_acct
    from .approvals import handle_action
    acct = _get_acct(account_key)
    if acct is None:
        return False
    # Act AS the account's own approver (falls back to the global approver), so the
    # autonomous approve stays inside the existing approver gate, never around it.
    try:
        actor = (acct.approver_ids() or [config.APPROVER_SLACK_ID])[0]
    except Exception:
        actor = config.APPROVER_SLACK_ID
    try:
        result = handle_action("approve", draft, actor, account=acct)
    except Exception as e:
        # A real publish failure already alerted inside handle_action; hold the draft
        # PENDING for a manual retry rather than dropping it.
        print(f"[autonomy] approve failed for {account_key} {draft.draft_id}: "
              f"{type(e).__name__}: {e}; holding PENDING")
        return False
    if not getattr(result, "ok", False):
        # e.g. media not ready / not authorized: hold PENDING, do not fake success.
        return False
    # handle_action set draft.status to APPROVED on success; persist that record.
    store.put(draft)
    from . import db
    db.audit("autonomy_autopublish", draft.draft_id, result.detail,
             account_key, getattr(draft, "day_key", ""))
    try:
        preview = (draft.caption or "")[:80].replace("\n", " ")
        poster.post_notice(
            f"Autonomous ({result.detail}): *{account_key}* | "
            f"{preview}{'...' if len(draft.caption or '') > 80 else ''}")
    except Exception:
        pass  # a Slack notice failure never blocks or un-publishes the post
    return True


def _post_and_save(draft, store, poster, idempotent):
    """Post the card, capture its Slack message ref (flag ON), save if not blocked."""
    # Master auto-approve: AGENT_AUTO_APPROVE_ENABLED bypasses the approval card
    # entirely. Drafts publish at schedule time; a lightweight notice goes to Slack
    # so Blake can see what went out without needing to tap anything.
    # WELCOME-ONLY auto-publish: AGENT_WELCOME_AUTOPUBLISH publishes new-client welcome
    # posts (topic_type == "WELCOME") hands-free WITHOUT enabling portfolio-wide
    # auto-approve, so the welcome backlog clears while every other LASSO post still
    # cards for a tap. Same gated publish() path; AGENT_PUBLISH_ENABLED still applies.
    _is_welcome = getattr(draft, "topic_type", "") == "WELCOME"
    if (draft.status.value == "pending"
            and not getattr(draft, "force_approval", False)
            and (config.auto_approve_enabled()
                 or (_is_welcome and config.welcome_autopublish_enabled()))):
        from . import db, postlog
        from .accounts import get_account
        from .meta_publisher import publish
        acct = get_account(draft.account_key)
        if acct:
            result = publish(draft, acct)
            draft.status = DraftStatus.APPROVED
            postlog.log_post(account_key=draft.account_key, platform=draft.platform,
                             caption=draft.caption,
                             media_id=getattr(result, "media_id", ""),
                             mode=result.mode, draft_id=draft.draft_id)
            db.audit("auto_approve", draft.draft_id, "AGENT_AUTO_APPROVE_ENABLED",
                     draft.account_key, draft.day_key)
            preview = (draft.caption or "")[:80].replace("\n", " ")
            poster.post_notice(
                f"Auto-published ({result.mode}): *{draft.account_key}* | "
                f"{preview}{'...' if len(draft.caption or '') > 80 else ''}")
            store.put(draft)
            return
    # Trust ladder wiring (both flags default OFF; nothing changes while off).
    if draft.status.value == "pending" and not getattr(draft, "force_approval", False) and (
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
    # APPROVAL SURFACE ROUTING (Part A, AGENT_PORTAL_SOCIAL_ENABLED, OFF by default).
    # LASSO accounts approve in Slack (a card); client gyms approve in the PORTAL, so
    # their Slack approval CARD is SKIPPED. One draft lifecycle, two surfaces. This
    # never weakens a gate: the client draft is still PENDING + force_approval=True and
    # waits for a human on the portal. ops_alerts (failures) STILL go to Slack for every
    # gym. Flag OFF -> surface is always "slack", byte-for-byte today's behavior.
    from .accounts import get_account as _get_acct
    from .gym_calendar_queue import approval_surface_for as _surface_for
    _surface = _surface_for(_get_acct(draft.account_key))
    if _surface == "portal":
        if idempotent:
            draft.slack_channel = ""
            draft.slack_ts = ""
        # PER-ACCOUNT AUTONOMY: when the gym has flipped "Autonomous" ON, a
        # portal-surface draft that would otherwise wait on the client is instead
        # auto-approved and published through the SAME gated approve path a portal
        # approve uses (approvals.handle_action -> publisher.publish). The publisher's
        # own AGENT_PUBLISH_ENABLED guard still applies inside publish(), so autonomy
        # auto-APPROVES but never bypasses the global publish kill switch.
        #
        # Client calendar drafts carry force_approval=True (they must never be caught
        # by the PORTFOLIO-WIDE AGENT_AUTO_APPROVE / trust-ladder auto-publish above).
        # Per-account autonomy is the OPPOSITE: an explicit, gym-owner-initiated opt-in
        # for THIS one gym, so it deliberately overrides force_approval here. A gym that
        # has NOT flipped autonomy is unchanged: the draft is stored PENDING and waits
        # on the portal. Any failure falls back to storing PENDING (the post is never
        # lost, only held for a manual approve).
        if (draft.status == DraftStatus.PENDING
                and _autonomous_publish(draft, store, poster)):
            return
        store.put(draft)
        return
    resp = poster.post_approval_card(draft) or {}
    # A hard send failure (transport down, rate limit past every retry) must be
    # LOUD for this one account and invisible to the rest of the fan-out: one
    # ops alert, the draft still saved (PENDING, actionable once Slack is back),
    # the loop moves to the next account.
    if draft.status == DraftStatus.PENDING and resp.get("ok") is False:
        ops_alerts.alert(
            f"approval card for {draft.account_key} draft {draft.draft_id} did "
            f"not post ({resp.get('error', 'send failed')} after retries). The "
            "draft is saved and pending; other accounts are unaffected.")
    if idempotent:
        draft.slack_channel = str(resp.get("channel") or "")
        draft.slack_ts = str(resp.get("ts") or "")
    # Blocked drafts are stored too (terminal records): that is what lets the
    # blocked dedupe stop a retry storm from re-carding the same failure.
    store.put(draft)


def _generation_account_for(tenant_key):
    """The registry account that DRAFTS for this tenant. Tries the tenant key as
    given (registry accounts like gritx_ig or lasso_ig), then <tenant>_ig (the
    convention: intake files under the base key, the generation account is _ig).
    Returns None when no registry account exists for the tenant."""
    from .accounts import get_account
    return get_account(tenant_key) or get_account(f"{tenant_key}_ig")


def draft_for_new_upload(tenant_key, filed_assets, poster=None, store=None,
                         voice_path=None, scheduled_for=None):
    """
    AGENT_DRAFT_ON_UPLOAD: draft ONE approval card per newly uploaded asset for a
    tenant, the instant its media is ingested — no waiting for the daily draw.

    Reuses draft_post + _post_and_save, so EVERY gate is identical to the daily
    path: the approval gate, the publish-off default, the fabrication gate, the
    portal-vs-Slack surface routing, and per-gym autonomy. This never publishes on
    its own; it only makes the card appear immediately (a gym with autonomy armed
    still publishes through the SAME gated approve path _post_and_save already uses).

    filed_assets: list of (absolute_path, client_note) for the assets just filed.
    Returns the list of Drafts produced (blocked markers included), or [] when the
    flag is OFF or the tenant cannot be drafted.

    A tenant with no registry account, or an account with no voice doc, is SKIPPED
    with ONE ops alert naming exactly what to add. Nothing is fabricated; nothing
    crashes the ingest loop.
    """
    if not config.draft_on_upload_enabled():
        return []
    if not filed_assets:
        return []
    if not config.master_enabled():
        return []

    account = _generation_account_for(tenant_key)
    if account is None:
        ops_alerts.alert(
            f"draft-on-upload: {len(filed_assets)} new asset(s) filed for "
            f"'{tenant_key}' but no registry account was found (looked for "
            f"'{tenant_key}' and '{tenant_key}_ig'). Add the Account record so "
            "Echo can draft for this gym. The media is safe in the library; "
            "nothing was drafted.")
        return []

    voice = load_voice(account.voice_doc) if account.voice_doc else \
        load_voice(voice_path or config.VOICE_DOC_PATH)
    if voice is None:
        ops_alerts.alert(
            f"draft-on-upload: {account.key} uploaded media but its voice doc is "
            "missing or empty. Nothing was drafted (no fabrication). Add the voice "
            "doc, then re-file or wait for the daily draw.")
        return []

    from .library import Creative
    poster = poster or SlackPoster()
    if store is None:
        from .store import PendingStore
        store = PendingStore()
    when = scheduled_for or datetime.now(timezone.utc).isoformat()
    idempotent = config.idempotent_drafts_enabled()

    # CLIENT gyms ALWAYS card (never portfolio auto-publish): the same
    # force_approval=True gate build_gym_calendar_draft uses. Without it, an
    # upload draft would be caught by the PORTFOLIO-WIDE AGENT_AUTO_APPROVE /
    # trust-ladder auto-publish in _post_and_save and go out with no portal
    # approval (and a Slack leak). LASSO's own accounts keep the default (False)
    # so their existing portfolio auto-approve behavior is unchanged. Per-gym
    # Autonomy still works: it lives inside the portal branch and deliberately
    # overrides force_approval there.
    force_card = not account.key.startswith("lasso")

    produced = []
    for path, note in filed_assets:
        try:
            note = (note or "").strip()
            # A note-less asset would draft a CTA-only card (no client subject).
            # Skip it: it is safely filed, and a later caption or the daily draw
            # can pick it up. Never surface a content-free card, never fabricate.
            if not note:
                continue
            ext = os.path.splitext(path)[1].lower()
            media_type = "video" if ext in VIDEO_EXTS else "image"
            creative = Creative(path=path, media_type=media_type, client_note=note)
            draft = draft_post(account, creative, when, voice=voice)
            draft.force_approval = force_card
            _post_and_save(draft, store, poster, idempotent)
            produced.append(draft)
        except Exception as e:
            # One bad asset never blocks the rest, and never crashes ingest.
            ops_alerts.alert(f"draft-on-upload: failed to draft {path} for "
                             f"{account.key}: {type(e).__name__}: {e}")
    return produced


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

    # WELCOME TRIGGER (AGENT_WELCOME_QUEUE_ENABLED, OFF): once per cycle, scan Stripe
    # for brand-new clients and enqueue any ready welcome (feed + story, hosted). This
    # is the automatic new-client trigger and the 45-day catch-up in one; the drip
    # below serves one/day. Fully guarded: a scan error never takes the draft run down.
    if config.welcome_queue_enabled():
        try:
            from .welcome_queue import scan_and_enqueue
            summary = scan_and_enqueue()
            if summary.get("enqueued"):
                print(f"[welcome-queue] scan enqueued {summary['enqueued']} new "
                      f"welcome(s); {summary.get('needs_confirmation', 0)} need a name, "
                      f"{summary.get('needs_logo', 0)} need a logo")
            elif not summary.get("scanned"):
                print(f"[welcome-queue] scan skipped: {summary.get('reason')}")
        except Exception as e:
            print(f"[welcome-queue] scan failed: {type(e).__name__}: {e}")
            ops_alerts.alert(f"welcome-queue daily scan failed: {type(e).__name__}: {e}. "
                             "The drip continues from what is already queued.")
        # PORTAL source (same gate): portal-added clients have no Stripe record, so the
        # Stripe scan above never welcomes them. Scan the portal gyms table too. Creds
        # absent -> this no-ops and the Stripe path is byte-for-byte unchanged.
        try:
            from .welcome_queue import scan_portal_and_enqueue
            psummary = scan_portal_and_enqueue()
            if psummary.get("enqueued"):
                print(f"[welcome-queue] portal scan enqueued {psummary['enqueued']} new "
                      f"welcome(s); {psummary.get('needs_logo', 0)} need a logo, "
                      f"{psummary.get('deduped_with_stripe', 0)} already covered by Stripe")
            elif not psummary.get("scanned"):
                print(f"[welcome-queue] portal scan skipped: {psummary.get('reason')}")
        except Exception as e:
            print(f"[welcome-queue] portal scan failed: {type(e).__name__}: {e}")
            ops_alerts.alert(f"welcome-queue portal scan failed: {type(e).__name__}: {e}. "
                             "The drip continues from what is already queued.")

    # CLIENT MEDIA SYNC + AUTO-GENERATE (AGENT_CLIENT_MEDIA_SYNC, OFF by default).
    # Armed, once per cycle: for each onboarded client gym, pull its NEWLY uploaded
    # photos/videos out of R2 into its content library, then, IF the gym now has media
    # + approved sources + NO calendar yet, build its DRAFT month from its REAL media
    # via build_client_month. This is the missing link that starts Echo working on a
    # gym the moment it uploads. Client calendars are DRAFTS (paused); NOTHING here
    # publishes. Self-guarded on the flag and fully isolated: a sync error never takes
    # the draft run down, and one gym failing never blocks the others.
    if config.client_media_sync_enabled():
        try:
            from .client_media_sync import scan_and_generate as _cms_scan
            summary = _cms_scan()
            if summary.get("ok"):
                if summary.get("generated") or summary.get("synced"):
                    print(f"[client-media-sync] synced {summary['synced']} media across "
                          f"{summary['scanned']} gym(s); built {summary['generated']} "
                          f"draft calendar(s), {summary.get('awaiting', 0)} awaiting, "
                          f"{summary.get('skipped_existing', 0)} already built")
            else:
                print(f"[client-media-sync] skipped: {summary.get('reason')}")
        except Exception as e:
            print(f"[client-media-sync] scan failed: {type(e).__name__}: {e}")
            ops_alerts.alert(f"client media sync failed: {type(e).__name__}: {e}. "
                             "The draft run is unaffected.")

    # GBP CONNECTION SYNC (AGENT_GBP_CONN_SYNC, OFF by default): refresh each client gym's
    # gym_gbp_connections row from its LIVE Zernio Google Business connection so the publish
    # lane can route (the table has no other writer). Reads Zernio + writes the connection
    # row only — NEVER publishes, never touches content_calendar. Fully isolated: a failure
    # never takes the draft run down.
    if config.gbp_conn_sync_enabled():
        try:
            from .gbp_conn_sync import sync_gbp_connections
            gsum = sync_gbp_connections(alert=ops_alerts.alert)
            if gsum.get("ok"):
                print(f"[gbp-conn-sync] synced {gsum['synced']} connection(s): "
                      f"{gsum['connected']} connected, {gsum['needs_reconnect']} "
                      f"needs_reconnect, {gsum['skipped']} skipped")
            else:
                print(f"[gbp-conn-sync] skipped: {gsum.get('reason')}")
        except Exception as e:
            print(f"[gbp-conn-sync] sync failed: {type(e).__name__}: {e}")
            ops_alerts.alert(f"GBP connection sync failed: {type(e).__name__}: {e}. "
                             "The draft run is unaffected.")

    for account in (accounts or active_accounts()):
        # FLEET ISOLATION (flagless hardening): one account's API error,
        # missing token, or empty library never blocks another account's
        # cycle. An exception logs, alerts once, audits, and moves on.
        try:
            # Cadence gate FIRST: a configured skip day produces no draft and no
            # card for this account (default 7 days/week, no skip days).
            if not schedule.should_post_on(day_key):
                continue
            # Channel ownership guard: a client account with no slack_channel
            # would route its approval cards to the shared default — LASSO's
            # internal channel — silently. That never happens: the account
            # skips its day with one loud alert instead. Fires only when a
            # shared channel is actually configured to leak into
            # (SLACK_CHANNEL_ID set); LASSO accounts (client zero) own the
            # default channel by design and are exempt.
            if (not account.key.startswith("lasso")
                    and not account.slack_channel
                    and config.SLACK_CHANNEL_ID):
                msg = (f"account {account.key} has no slack_channel: its cards "
                       "would route to the shared internal channel. Skipping "
                       "this account; set Account.slack_channel and run "
                       "`python -m agent preflight --account "
                       f"{account.key}`.")
                print(f"[runner] {msg}")
                ops_alerts.alert(msg)
                from . import db as _db
                _db.audit("channel_guard_skip", account.key, msg,
                          account.key, day_key)
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
            # BOOK QUEUE (pre-made drafts from book_queue.py + book_manifest.json).
            # Fires before any campaign builder. On a matching date, returns the
            # exact pre-written Draft; runner auto-publishes it (AGENT_AUTO_APPROVE
            # armed) without needing a Slack tap. No-ops when manifest is absent.
            if account.key in ("lasso_ig", "lasso_fb"):
                from .book_queue import build_book_queue_draft as _bq_draft
                draft = _bq_draft(account, day_key)

            # WELCOME DRIP (AGENT_WELCOME_QUEUE_ENABLED, OFF by default). One queued
            # new-client welcome per day, cross-posted to lasso_ig + lasso_fb. Sits
            # right behind the dated book queue so a book-launch date keeps its slot;
            # fills any non-book-queue day. No-ops when the flag is off or empty.
            if draft is None and account.key in ("lasso_ig", "lasso_fb"):
                from .welcome_queue import build_welcome_queue_draft as _wq_draft
                draft = _wq_draft(account, day_key)

            # DEMO CALENDAR (AGENT_DEMO_CALENDAR_ENABLED, OFF by default). The 30-day
            # LASSO-brand done-for-you demo. Sits behind the dated book queue and the
            # welcome drip so neither loses its slot; fills any other demo date. No-ops
            # when the flag is off, the date has no seeded post, or the manifest is empty.
            if draft is None and account.key in ("lasso_ig", "lasso_fb"):
                from .demo_calendar_queue import build_demo_calendar_draft as _dc_draft
                draft = _dc_draft(account, day_key)

            # QUIET the legacy LASSO daily card when autopublish is on (FIX 2).
            # When AGENT_CALENDAR_AUTOPUBLISH is armed, content_calendar is the source
            # of truth for LASSO and its rows are published directly at the end of the
            # cycle, so the legacy daily rotation/infographic/fallback draft would be a
            # redundant, divergent approval card. Skip building/carding it FOR LASSO
            # ACCOUNTS ONLY. The book queue, welcome drip, and demo calendar above are
            # untouched (they already ran); client-gym drafting below is untouched.
            _skip_legacy_lasso_daily = (
                config.calendar_autopublish_enabled()
                and account.key.startswith("lasso")
                and draft is None)

            # Category frequency + consecutive caps (category_cap.py, both OFF by
            # default). Campaign builders are gated; the fallback never blocks.
            from .category_cap import is_allowed as _cap_allowed, record_win as _record_cap
            _book_n = config.book_campaign_every_n_days()
            _max_consec = config.category_max_consecutive()

            # CATEGORY ROTATION CONTROLLER (AGENT_CATEGORY_ROTATION, OFF by default).
            # When armed, content_categories.schedule_for_day is the sole authority:
            # only the builder whose category matches today's scheduled slot fires.
            # Campaign builders (book, podcast, summit) never pre-empt the schedule.
            # platform / b2b / doctrine days fall through to the creative layer below.
            if (config.category_rotation_enabled() and account.key.startswith("lasso")
                    and not _skip_legacy_lasso_daily):
                from .content_categories import schedule_for_day as _sched_for_day
                _day_sched = _sched_for_day(day_key)
                if _day_sched is not None:
                    _cat = _day_sched[0]
                    if _cat == "podcast":
                        from .podcast_release import build_podcast_slot_draft
                        if _cap_allowed(account.key, "podcast", day_key,
                                        max_consecutive=_max_consec):
                            draft = build_podcast_slot_draft(account, day_key)
                    elif _cat == "book":
                        from .book_campaign import build_book_draft
                        if _cap_allowed(account.key, "book", day_key,
                                        every_n_days=_book_n,
                                        max_consecutive=_max_consec):
                            draft = build_book_draft(account, day_key)
                    elif _cat == "summit":
                        if _cap_allowed(account.key, "summit", day_key,
                                        max_consecutive=_max_consec):
                            draft = build_summit_draft(account, day_key,
                                                       voice=acct_voice)
                    # _cat in ("platform", "b2b", "doctrine"): draft stays None;
                    # the creative rotation / infographic / fallback below fills.
            elif not _skip_legacy_lasso_daily:
                # LEGACY PRIORITY CHAIN (rotation OFF; behavior byte-for-byte identical
                # to what shipped before AGENT_CATEGORY_ROTATION existed). Skipped for
                # LASSO accounts when autopublish is on (content_calendar is authority).
                if account.key.startswith("lasso"):
                    # BOOK CAMPAIGN (AGENT_BOOK_CAMPAIGN_ENABLED, OFF): participates in
                    # rotation via frequency cap and consecutive cap (both default off).
                    from .book_campaign import build_book_draft
                    if _cap_allowed(account.key, "book", day_key,
                                    every_n_days=_book_n, max_consecutive=_max_consec):
                        draft = build_book_draft(account, day_key)
                if draft is None and account.key.startswith("lasso"):
                    # PODCAST SLOT (AGENT_PODCAST_ENABLED, OFF): consecutive cap applied;
                    # no frequency cap (episode queue naturally limits cadence).
                    from .podcast_release import build_podcast_slot_draft
                    if _cap_allowed(account.key, "podcast", day_key,
                                    max_consecutive=_max_consec):
                        draft = build_podcast_slot_draft(account, day_key)
                if draft is None and account.key.startswith("lasso"):
                    draft = build_social_proof_draft(account, day_key,
                                                     voice=acct_voice, poster=poster)
                # Summit campaign next (its own weekly day). Consecutive cap applied.
                if draft is None and account.key.startswith("lasso"):
                    if _cap_allowed(account.key, "summit", day_key,
                                    max_consecutive=_max_consec):
                        draft = build_summit_draft(account, day_key, voice=acct_voice)
            # Creative rotation + variety guard: dormant unless AGENT_ROTATION_ENABLED.
            # Armed, it picks WHICH approved creative today's draft proposes (window,
            # pillar alternation, gate-clean only); None -> the paths below run as today.
            # Skipped for LASSO accounts when autopublish is on (quiet the legacy card).
            if (draft is None and account.key.startswith("lasso")
                    and not _skip_legacy_lasso_daily):
                from .rotation import build_rotated_draft
                draft = build_rotated_draft(account, day_key, acct_voice, acct_lib, poster=poster)
            # For a LASSO account, try the fully-automated infographic path next. It is
            # dormant unless all three flags are armed; None -> fall back to the library
            # path unchanged. (A BLOCKED draft is still a draft: it surfaces, not falls back.)
            # Skipped for LASSO accounts when autopublish is on (quiet the legacy card).
            if (draft is None and account.key.startswith("lasso")
                    and not _skip_legacy_lasso_daily):
                draft = build_daily_infographic_draft(account, day_key)
            # CLIENT SOURCES (AGENT_CLIENT_SOURCES, OFF by default). A client
            # (non-LASSO) account drafts the day from its OWN approved sources +
            # uploaded library, spread across categories. OFF, or no approved
            # source for the day, -> None, and the library pick below runs exactly
            # as today. Book/summit stay LASSO-only (never reached here).
            if (draft is None and config.client_sources_enabled()
                    and not account.key.startswith("lasso")):
                from .client_content import build_client_draft
                draft = build_client_draft(account, day_key, acct_voice, acct_lib,
                                           poster=poster)
            # Library fallback: the last leg of the legacy LASSO daily draft. Skipped
            # for LASSO accounts when autopublish is on so no redundant card is built;
            # client/non-LASSO accounts are unaffected (_skip_legacy_lasso_daily False).
            if draft is None and not _skip_legacy_lasso_daily:
                creative = pick_next(account, acct_lib, used_creatives_for(account.key))
                if creative is not None:
                    from .library_audit import check_creative as _check_creative
                    _issue = _check_creative(creative)
                    if _issue:
                        _msg = (f"creative {creative.stem!r} for {account.key} on "
                                f"{day_key} has an issue: {_issue}. "
                                "Upload or replace it; drafting will attempt a fallback.")
                        print(f"[library] {_msg}")
                        ops_alerts.alert(_msg)
                draft = draft_post(account, creative, schedule.scheduled_for(day_key), voice=acct_voice)
            # Record the category win for cap history (idempotent by day_key, before
            # idempotent reconcile so a same-content re-run still counts correctly).
            if draft is not None:
                _record_cap(account.key, draft.draft_type or "feed", day_key)

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

            # BOOK STORIES QUEUE: pre-made 9:16 story cards for The Full Gym launch.
            # Fires before the auto-generated story; takes its slot on matching dates.
            _book_story_posted = False
            if account.key in ("lasso_ig",):
                from .book_stories_queue import build_book_story_draft as _bsq_draft
                bk_story = _bsq_draft(account, day_key)
                if bk_story is not None:
                    if idempotent:
                        bk_story, _existing_bk = _reconcile(
                            bk_story, day_key, "story", store, poster)
                        if bk_story is None:
                            results.append(_existing_bk)
                    if bk_story is not None:
                        _post_and_save(bk_story, store, poster, idempotent)
                        results.append(bk_story)
                    _book_story_posted = True

            # WELCOME STORY (AGENT_WELCOME_QUEUE_ENABLED, OFF). The 9:16 story for the
            # SAME gym today's welcome feed served on lasso_ig; takes the story slot
            # when the feed was a welcome. The publisher still needs AGENT_STORIES_ENABLED.
            if not _book_story_posted and account.key == "lasso_ig":
                from .welcome_queue import build_welcome_story_draft as _wq_story
                wc_story = _wq_story(account, day_key, feed_draft=feed_draft)
                if wc_story is not None:
                    if idempotent:
                        wc_story, _existing_wc = _reconcile(
                            wc_story, day_key, "story", store, poster)
                        if wc_story is None:
                            results.append(_existing_wc)
                    if wc_story is not None:
                        _post_and_save(wc_story, store, poster, idempotent)
                        results.append(wc_story)
                    _book_story_posted = True

            # DEMO CALENDAR STORY (AGENT_DEMO_CALENDAR_ENABLED, OFF). The 9:16 story for
            # the SAME demo post today's feed served on lasso_ig. Coupled to the demo feed
            # draft id, so it only fires when today's feed was a demo feed. The publisher
            # still needs AGENT_STORIES_ENABLED to actually send it.
            if not _book_story_posted and account.key == "lasso_ig":
                from .demo_calendar_queue import build_demo_calendar_story_draft as _dc_story
                dc_st = _dc_story(account, day_key, feed_draft=feed_draft)
                if dc_st is not None:
                    if idempotent:
                        dc_st, _existing_dc = _reconcile(
                            dc_st, day_key, "story", store, poster)
                        if dc_st is None:
                            results.append(_existing_dc)
                    if dc_st is not None:
                        _post_and_save(dc_st, store, poster, idempotent)
                        results.append(dc_st)
                    _book_story_posted = True

            # Stories: FULLY DORMANT unless AGENT_STORIES_ENABLED. Armed, draft one
            # 9:16 Story per account reusing the day's creative; PENDING, its own
            # approval card, clearly labeled STORY. Book story takes this slot when
            # scheduled; auto-generated story is skipped on book story days.
            if not _book_story_posted:
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
            except Exception as audit_err:
                print(f"[runner] audit write failed (audit table broken?): "
                      f"{type(audit_err).__name__}")
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

    # Review cycle refresh ask: dormant unless AGENT_REVIEW_CYCLE_ENABLED. Armed,
    # one creative refresh ask per account per review cycle (kv deduped inside).
    # An ask error never takes the draft run down.
    if config.review_cycle_enabled():
        from .day30 import maybe_refresh_ask
        for account in (accounts or active_accounts()):
            try:
                maybe_refresh_ask(account.key, poster=poster)
            except Exception as e:
                print(f"[review-cycle] refresh ask failed for {account.key}: "
                      f"{type(e).__name__}: {e}")

    # REAL-CALENDAR MIRROR (AGENT_REAL_CALENDAR_MIRROR, OFF by default). Armed, fold
    # each REAL gym's real drafts into the shared Supabase content_calendar so the
    # client portal serves the gym's actual plan (and demo rows are cleared off a real
    # gym). Flag OFF -> this block is skipped entirely: byte-for-byte today's behavior.
    # Needs the Supabase creds (the portal data plane); no creds -> nothing to mirror.
    # LASSO's own accounts and the demo gym id are never mirrored. Writes calendar rows
    # only; nothing here publishes. A mirror error never takes the draft run down.
    if config.real_calendar_mirror_enabled() and config.portal_calendar_supabase_enabled():
        from .real_calendar_mirror import mirror_to_supabase
        from .portal_calendar_store import SupabaseCalendarStore
        _demo_gym = config.demo_calendar_gym_id()
        _sb = SupabaseCalendarStore()
        for account in (accounts or active_accounts()):
            if account.key.startswith("lasso") or account.key == _demo_gym:
                continue
            # A gym on the CLIENT-MONTH lane (has approved client sources) gets its
            # calendar from build_client_month under its BASE gym_id; mirroring its
            # PendingStore drafts here too would write a SECOND, off-plane copy
            # (gym_id='eng_ig') the portal/publisher never read — junk at best, a
            # double-publish surface at worst.
            try:
                from . import client_sources as _cs
                if _cs.categories_present(account.key):
                    continue
            except Exception:
                pass
            try:
                summary = mirror_to_supabase(account.key, store, _sb)
                if not summary.get("ok"):
                    print(f"[real-mirror] {account.key}: {summary.get('reason')}")
                elif summary.get("upserted") or summary.get("deleted"):
                    print(f"[real-mirror] {account.key}: upserted "
                          f"{summary['upserted']}, deleted {summary['deleted']} demo row(s)")
            except Exception as e:
                print(f"[real-mirror] {account.key}: {type(e).__name__}: {e}")
                ops_alerts.alert(f"real-calendar mirror failed for {account.key}: "
                                 f"{type(e).__name__}: {e}. The draft run is unaffected.")

    # CALENDAR AUTO-PUBLISHER (AGENT_CALENDAR_AUTOPUBLISH, OFF by default; ALSO needs
    # AGENT_PUBLISH_ENABLED). publish_due() self-guards on BOTH flags, so an unguarded
    # call is a safe no-op when off. Armed, it reads THIS day's content_calendar rows for
    # gym_id='lasso' and publishes each unpublished one to live IG/FB EXACTLY ONCE (an
    # atomic status claim per row). Isolated from drafting/approval; a failure here never
    # takes the draft run down. The manual approval path is untouched.
    if config.calendar_autopublish_enabled():
        try:
            from . import calendar_autopublish
            # catch_all=True: this is the ONCE/DAY draw, so it must be a safety net
            # that publishes EVERY remaining due row regardless of slot (NO ORPHANS).
            # Time-of-day spacing is driven by the listener's run_slot_ticks lane;
            # this call guarantees nothing is ever left unpublished for the day even
            # if the listener/slot ticks were missed or the scheduler fired only once.
            summary = calendar_autopublish.publish_due(day_key, notifier=poster,
                                                       catch_all=True)
            if summary.get("ok") and summary.get("published"):
                print(f"[calendar-autopublish] published {len(summary['published'])} "
                      f"row(s) for {day_key}; skipped {len(summary.get('skipped', []))}, "
                      f"failed {len(summary.get('failed', []))}")
            elif not summary.get("ok"):
                print(f"[calendar-autopublish] no-op: {summary.get('reason')}")
        except Exception as e:
            print(f"[calendar-autopublish] cycle failed: {type(e).__name__}: {e}")
            ops_alerts.alert(f"calendar auto-publisher failed for {day_key}: "
                             f"{type(e).__name__}: {e}. The draft run is unaffected.")

    return {"status": "drafted", "drafts": results}
