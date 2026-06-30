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

import threading
import time
from datetime import datetime, timezone

from . import config
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


def _daily_scheduler(store):
    """
    Minimal in-process daily trigger. Fires run_daily once per day at the target
    UTC hour. Simple by design. For stricter reliability, run `run-daily` as a
    Railway cron service instead and disable this with AGENT_SCHEDULER_ENABLED=false.
    """
    import os
    target_hour = int(os.environ.get("AGENT_DAILY_HOUR_UTC", "14"))  # ~10am ET
    last_run_date = None
    while True:
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if now.hour == target_hour and last_run_date != today:
            try:
                run_daily(store=store)
            except Exception as e:
                print(f"[scheduler] run_daily error: {e}")
            last_run_date = today
        time.sleep(60)


def run_listener():
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
        if actor != config.APPROVER_SLACK_ID:
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
