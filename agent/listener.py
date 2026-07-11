"""
Slack control surface: the live listener (Socket Mode).

This is the always-on process that turns the Approve / Edit / Skip buttons on a
card into real actions through the approval gate. Socket Mode means NO public URL
and NO request-URL config: it holds an outbound websocket to Slack, which is the
simplest, safest setup for a single internal workspace.

Needs two tokens (set by hand in Railway, never in chat or code):
  AGENT_SLACK_BOT_TOKEN   xoxb-...   (bot token)
  AGENT_SLACK_APP_TOKEN   xapp-...   (app-level token, scope connections:write)

The approver gate still lives in approvals.handle_action: only the configured
approver's taps do anything. Everyone else is denied.

Run:  python -m agent listen
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

from . import config, ops_alerts, schedule
from .approvals import handle_action
from .accounts import get_account
from .drafter import Draft, DraftStatus, draft_post
from .store import PendingStore
from .runner import run_daily


def _redraft_with_note(old: Draft, note: str) -> Draft:
    """Blake's edit note becomes the new caption, re-held for approval."""
    acct = get_account(old.account_key)
    new = Draft(
        draft_id=old.draft_id + "e",
        account_key=old.account_key,
        platform=old.platform,
        caption=note.strip(),
        hashtags=old.hashtags,
        creative_path=old.creative_path,
        creative_public_url=old.creative_public_url,
        scheduled_for=old.scheduled_for,
        status=DraftStatus.PENDING,
        source_fragments=[note.strip()],
    )
    return new


# The scheduler's fire date persists to /data (the volume on the echo service) so a
# redeploy inside the fire window cannot double-fire even with idempotency disarmed.
_SCHEDULER_STATE_FILE = "scheduler_state.json"


def _scheduler_state_path():
    return os.path.join(os.environ.get("AGENT_SCHEDULER_STATE_DIR", "/data"),
                        _SCHEDULER_STATE_FILE)


def _read_last_run_date():
    """The persisted last fire date, or None when /data is unavailable or empty
    (in-memory tracking then carries the day, exactly the old behavior)."""
    try:
        with open(_scheduler_state_path(), encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("last_run_date")
    except Exception:
        return None


def _write_last_run_date(day):
    """Best-effort persist; a missing /data never breaks the scheduler."""
    try:
        with open(_scheduler_state_path(), "w", encoding="utf-8") as fh:
            json.dump({"last_run_date": day}, fh)
    except Exception as e:
        print(f"[scheduler] could not persist last run date: {type(e).__name__}: {e}")


def _fire_daily(store, today, run=run_daily):
    """
    One scheduled fire, LOUD on every no-card outcome. Any result other than a
    drafted run with at least one card (on a posting day) raises one ops alert, so
    a silent no-card morning is impossible while AGENT_OPS_ALERTS_ENABLED is true.
    A skip day (schedule.should_post_on false) drafting zero cards is EXPECTED and
    does not alert.
    """
    try:
        out = run(store=store)
    except Exception as e:
        print(f"[scheduler] run_daily error: {e}")  # log either way (old behavior)
        ops_alerts.alert("scheduled draft run produced no cards - "
                         f"{type(e).__name__}: {e}")
        return None
    status = (out or {}).get("status")
    drafts = (out or {}).get("drafts") or []
    if status != "drafted":
        ops_alerts.alert(f"scheduled draft run produced no cards - status '{status}' "
                         "(check AGENT_ENABLED and the voice doc)")
    elif not drafts and schedule.should_post_on(today):
        ops_alerts.alert("scheduled draft run produced no cards - drafted 0 drafts "
                         "on a posting day")
    return out


def _daily_scheduler(store):
    """
    Minimal in-process daily trigger. Fires run_daily once per day at the target
    UTC hour. Simple by design. For stricter reliability, run `run-daily` as a
    Railway cron service instead and disable this with AGENT_SCHEDULER_ENABLED=false.
    """
    target_hour = int(os.environ.get("AGENT_DAILY_HOUR_UTC", "14"))  # ~10am ET
    ingest_every = max(1, int(os.environ.get("AGENT_INTAKE_POLL_MINUTES", "5"))) * 60
    opus_every = max(1, int(os.environ.get("AGENT_OPUS_POLL_MINUTES", "60"))) * 60
    podcast_every = max(1, int(os.environ.get("AGENT_PODCAST_POLL_MINUTES", "60"))) * 60
    inbox_every = config.episode_inbox_poll_minutes() * 60
    last_run_date = _read_last_run_date()  # survives a redeploy inside the window
    last_ingest = 0.0
    last_opus = 0.0
    last_podcast = 0.0
    last_inbox = 0.0
    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if now.hour == target_hour and last_run_date != today:
            _fire_daily(store, today)
            last_run_date = today
            _write_last_run_date(today)
            # Daily metrics snapshot AFTER the daily draft: READ-ONLY Graph pulls
            # (views, never impressions), dormant unless AGENT_REPORTING_ENABLED.
            # Failures alert inside snapshot_all; nothing here crashes the loop.
            if config.reporting_enabled():
                try:
                    from . import reporting_live
                    reporting_live.snapshot_all()
                except Exception as e:
                    print(f"[reporting] snapshot pass failed: {type(e).__name__}: {e}")
        # Intake ingest: dormant unless AGENT_INTAKE_ENABLED. Runs INSIDE this
        # listener (the one process with /data + R2); an error never kills the loop.
        if config.intake_enabled() and time.monotonic() - last_ingest >= ingest_every:
            last_ingest = time.monotonic()
            try:
                from . import intake_ingest
                intake_ingest.process_all()
            except Exception as e:
                print(f"[intake] ingest pass failed: {type(e).__name__}: {e}")
        # Opus Clip poll: FULLY INERT unless BOTH AGENT_OPUS_ENABLED and
        # AGENT_OPUS_POLL_ENABLED are armed. Errors alert (inside pull), never crash.
        if (config.opus_enabled() and config.opus_poll_enabled()
                and time.monotonic() - last_opus >= opus_every):
            last_opus = time.monotonic()
            try:
                from . import opus_ingest
                opus_ingest.pull()
            except Exception as e:
                print(f"[opus] poll pass failed: {type(e).__name__}: {e}")
        # Podcast feed poll: FULLY INERT unless AGENT_PODCAST_ENABLED. A new
        # episode is stored exactly once (idempotent by guid); a malformed feed
        # or missing feed url fails LOUD here (log + one ops alert), never
        # silent, and never crashes the loop. Detection only; drafting stays in
        # the daily run's priority chain.
        if config.podcast_enabled() and time.monotonic() - last_podcast >= podcast_every:
            last_podcast = time.monotonic()
            try:
                from . import podcast_feed
                podcast_feed.poll()
            except Exception as e:
                print(f"[podcast] poll pass failed: {type(e).__name__}: {e}")
                ops_alerts.alert(f"podcast feed poll failed: {type(e).__name__}: {e}")
        # Episode inbox watcher: FULLY INERT unless AGENT_EPISODE_INBOX_ENABLED.
        # On each pass: list the watched R2 prefix, guard against in-progress
        # uploads (size stability), claim + Phase 1 clip selection, post ranked
        # plan to Slack #echoclaude. Also runs the Monday 9am nudge check when
        # the flag is armed. Errors alert (inside poll/check_monday_nudge) and
        # never crash the loop.
        if config.episode_inbox_enabled():
            if time.monotonic() - last_inbox >= inbox_every:
                last_inbox = time.monotonic()
                try:
                    from . import episode_inbox
                    episode_inbox.poll()
                except Exception as e:
                    print(f"[inbox] poll pass failed: {type(e).__name__}: {e}")
                    ops_alerts.alert(
                        f"episode inbox poll failed: {type(e).__name__}: {e}"
                    )
            try:
                from . import episode_inbox
                episode_inbox.check_monday_nudge(now=now)
            except Exception as e:
                print(f"[inbox] nudge check failed: {type(e).__name__}: {e}")
        # Card self-expiry sweep (no flag, queue hygiene): hourly, cheap.
        if now.minute == 0:
            try:
                from . import ops_alerts as _oa
                from .runner import expire_past_due
                expire_past_due(store, _oa._default_poster(), now=now)
            except Exception as e:
                print(f"[expiry] sweep failed: {type(e).__name__}: {e}")
        # Heartbeat morning check (no flag, honest observability): one alert
        # per enabled account per day when the daily run missed its window.
        try:
            from . import heartbeat
            heartbeat.check_heartbeats(now=now)
        except Exception as e:
            print(f"[heartbeat] check failed: {type(e).__name__}: {e}")
        # Evening digest: one line per day at AGENT_DIGEST_HOUR_UTC, dormant
        # unless AGENT_DIGEST_ENABLED. Never crashes the loop.
        if config.digest_enabled():
            try:
                from . import digest, ops_alerts
                poster = ops_alerts._default_poster()
                digest.maybe_send(poster, now=now, library_path=config.LIBRARY_PATH)
            except Exception as e:
                print(f"[digest] pass failed: {type(e).__name__}: {e}")
        # Sunday operator report: one weekly card at 6 PM ET, dormant unless
        # AGENT_WEEKLY_REPORT_ENABLED. Never crashes the loop.
        if config.weekly_report_enabled():
            try:
                from . import ops_alerts as _oa2, weekly_report
                weekly_report.maybe_send(_oa2._default_poster(), now=now)
            except Exception as e:
                print(f"[weekly] pass failed: {type(e).__name__}: {e}")
        # Nightly brain: one read-only proposal note, dormant unless
        # AGENT_BRAIN_PROPOSALS_ENABLED. Never crashes the loop.
        if config.brain_proposals_enabled():
            try:
                from . import brain, ops_alerts
                brain.maybe_send(ops_alerts._default_poster(), now=now)
            except Exception as e:
                print(f"[brain] pass failed: {type(e).__name__}: {e}")
        # Nightly store backup: dormant unless AGENT_BACKUP_ENABLED. One ops
        # alert on failure only; never crashes the loop.
        if config.backup_enabled():
            try:
                from . import backup
                backup.maybe_backup(now=now)
            except Exception as e:
                print(f"[backup] pass failed: {type(e).__name__}: {e}")
        time.sleep(60)


def run_listener():
    # Startup config hygiene: placeholder AGENT_OPUS_PROJECT_IDS values (P1
    # pattern / under 6 chars) get ONE warning naming each bad value and are
    # never sent to the API. Ingest revalidates on every pass; this line only
    # makes the misconfiguration visible the moment the service boots.
    from . import opus_ingest
    opus_ingest.validated_project_ids()
    # Facebook connect page: a small HTTP surface INSIDE this process (it needs
    # the /data store for the page token). Dormant unless AGENT_CONNECT_ENABLED;
    # while off, no thread starts and the routes would 404 anyway.
    if config.connect_enabled():
        import threading as _threading
        from . import connect_web
        _threading.Thread(target=connect_web.serve, daemon=True).start()
    import os
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("slack_bolt is not installed. Add it (pip install slack_bolt) and redeploy.")
        return

    bot_token = os.environ.get(config.SLACK_BOT_TOKEN_ENV)
    app_token = os.environ.get("AGENT_SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        print("Missing Slack tokens. Set AGENT_SLACK_BOT_TOKEN (xoxb-) and "
              "AGENT_SLACK_APP_TOKEN (xapp-) in the environment by hand.")
        return

    app = App(token=bot_token)
    store = PendingStore()

    def _act(ack, body, action, client, kind):
        ack()
        draft_id = action.get("value")
        actor = body.get("user", {}).get("id", "")
        channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
        ts = body.get("message", {}).get("ts") or body.get("container", {}).get("message_ts")
        draft = store.get(draft_id)
        if not draft:
            client.chat_postMessage(channel=channel, text=f"Draft {draft_id} not found (it may have expired).")
            return
        if getattr(draft, "draft_type", "") == "claim_promotion":
            # standing claim promotion (podcast Part F): same approver gate,
            # its own write path; the post approval flow stays untouched
            from . import podcast_promote
            res = podcast_promote.handle_promotion_action(kind, draft, actor)
        else:
            res = handle_action(kind, draft, actor_slack_id=actor,
                                account=get_account(draft.account_key))
        if not res.ok:
            client.chat_postMessage(channel=channel, text=f":no_entry: {res.detail}")
            return
        store.remove(draft_id)
        label = {"approve": "Approved", "skip": "Skipped"}[kind]
        try:
            client.chat_update(channel=channel, ts=ts,
                               text=f"{label} by <@{actor}> — {res.detail}",
                               blocks=[{"type": "section", "text": {"type": "mrkdwn",
                                        "text": f":white_check_mark: *{label}* by <@{actor}>\n{res.detail}"}}])
        except Exception:
            client.chat_postMessage(channel=channel, text=f"{label}: {res.detail}")

    @app.action("approve")
    def on_approve(ack, body, action, client):
        _act(ack, body, action, client, "approve")

    @app.action("skip")
    def on_skip(ack, body, action, client):
        _act(ack, body, action, client, "skip")

    @app.action("edit")
    def on_edit(ack, body, action, client):
        ack()
        draft_id = action.get("value")
        trigger_id = body.get("trigger_id")
        client.views_open(trigger_id=trigger_id, view={
            "type": "modal", "callback_id": "edit_submit",
            "private_metadata": draft_id,
            "title": {"type": "plain_text", "text": "Edit caption"},
            "submit": {"type": "plain_text", "text": "Re-hold for approval"},
            "blocks": [{
                "type": "input", "block_id": "note",
                "label": {"type": "plain_text", "text": "New caption"},
                "element": {"type": "plain_text_input", "action_id": "v", "multiline": True},
            }],
        })

    @app.view("edit_submit")
    def on_edit_submit(ack, body, view, client):
        ack()
        draft_id = view["private_metadata"]
        note = view["state"]["values"]["note"]["v"]["value"]
        actor = body.get("user", {}).get("id", "")
        old = store.get(draft_id)
        if not old:
            return
        from .accounts import get_account as _get_account
        from .approvals import _is_approver as _gate
        if not _gate(actor, account=_get_account(old.account_key or "")):
            return
        new = _redraft_with_note(old, note)
        store.remove(draft_id)
        store.put(new)
        from .slack_surface import SlackPoster
        SlackPoster(token=os.environ.get(config.SLACK_BOT_TOKEN_ENV)).post_approval_card(new)

    @app.command("/echo-draft")
    def on_draft_now(ack, respond):
        ack()
        out = run_daily(store=store)
        respond(f"Drafting: {out['status']} ({len(out.get('drafts', []))} card(s)) -> #echoclaude")

    if str(os.environ.get("AGENT_SCHEDULER_ENABLED", "true")).lower() in {"1", "true", "yes", "on"}:
        threading.Thread(target=_daily_scheduler, args=(store,), daemon=True).start()
        print("Daily scheduler started.")

    print("Echo listener online (Socket Mode). Draft-only:", not config.publish_enabled())
    SocketModeHandler(app, app_token).start()
