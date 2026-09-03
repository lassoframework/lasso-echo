"""
outbox.py — the Wrangler outbound role. The ONLY code in this package that posts to Slack.

Blake (spec item 8): "Wrangler owns all outbound. The adapter never calls chat.postMessage
itself. It writes the row, Wrangler posts." Ground truth 2026-09-03: no Wrangler process
reads support_messages today, so this in-process loop IS the outbound role for now. Its
whole interface is `run_once(bus, post, ...)`; when a real Wrangler service exists, it takes
over by pointing at the same rows and this loop is disabled by config. Nothing else in the
package would change.

It reads rows in delivery_status='ready' and, for each, re-checks EVERY gate at post time
(not only at write time -- a flag can be flipped between the two):

  1. FIRST CONTACT. The parent ticket must carry at least one inbound human row, or the row
     is suppressed. The bot never speaks first, structurally.
  2. VERIFICATION. A substantive reply (kind=answer) posts only if the parent ticket has
     verification_after populated. Missing -> suppressed, loudly. Never "should be fixed."
  3. TRUST LADDER. A conversational reply to a client posts only if the identity's
     client-reply flag is armed; to staff only if the staff flag is armed. Otherwise the row
     is moved to 'held' (its tap notice was already written by the adapter).
  4. DESTINATION. Internal kinds (escalation, hold_notice) go to the fixer channel and
     fixer_request goes to the ops-fix intake channel the existing worker watches. They
     never enter the person's thread. Conversational kinds go to the ticket's own channel:
     top-level in a DM or group DM (people do not thread there), in-thread for a mention or
     a channel thread.

A post failure marks the row 'failed' and moves on; one bad row never stalls the queue.
"""
from . import adapter as _a
from .. import config


def _channel_for(kind):
    if kind == _a.KIND_FIXER_REQUEST:
        return config.ops_fix_channel_id()
    return config.fixer_channel_id()


def _recipient_armed(identity, recipient_kind):
    if recipient_kind in ("staff", "coach"):
        return config.slack_convo_staff_reply_armed(identity.name)
    return config.slack_convo_client_reply_armed(identity.name)


def run_once(bus, post, *, identity, log=print, limit=50):
    """Process up to `limit` ready rows. post(channel, text, thread_ts=None) -> slack ts.
    Returns a summary dict. Never raises out of the loop."""
    summary = {"posted": 0, "held": 0, "suppressed": 0, "failed": 0, "skipped": 0}
    try:
        rows = bus.outbox("ready", limit=limit)
    except Exception as e:  # noqa: BLE001
        log(f"[slack-convo/outbox] read failed: {type(e).__name__}")
        return summary
    for row in rows:
        try:
            _dispatch_one(bus, post, row, identity=identity, log=log, summary=summary)
        except Exception as e:  # noqa: BLE001 - one row never stalls the queue
            log(f"[slack-convo/outbox] row {row.get('id')} failed: {type(e).__name__}")
            try:
                bus.mark_message(row["id"], "failed")
            except Exception:  # noqa: BLE001
                pass
            summary["failed"] += 1
    return summary


def _dispatch_one(bus, post, row, *, identity, log, summary):
    att = row.get("attachments") or {}
    kind = att.get("kind") or ""
    ticket = bus.ticket(row["ticket_id"])
    if not ticket:
        bus.mark_message(row["id"], "suppressed")
        summary["suppressed"] += 1
        return
    # only rows for THIS identity's tickets; another identity's loop owns the rest
    if (ticket.get("bot_identity") or "") != identity.name:
        summary["skipped"] += 1
        return

    # ---- internal kinds: fixer / ops-fix channels, never the person's thread ---------
    if kind in _a.INTERNAL_KINDS:
        channel = _channel_for(kind)
        if not channel:
            log(f"[slack-convo/outbox] no channel configured for {kind}; row "
                f"{row['id']} marked failed (set AGENT_FIXER_CHANNEL_ID / the ops-fix "
                "channel)")
            bus.mark_message(row["id"], "failed")
            summary["failed"] += 1
            return
        ts = post(channel, row["body"])
        bus.mark_message(row["id"], "posted", slack_ts=ts)
        summary["posted"] += 1
        return

    # ---- conversational kinds: the gates ---------------------------------------------
    # 1. first contact
    if bus.inbound_count(ticket["id"]) < 1:
        log(f"[slack-convo/outbox] SUPPRESSED row {row['id']}: ticket {ticket['id']} has no "
            "inbound human message; the bot never speaks first")
        bus.mark_message(row["id"], "suppressed")
        summary["suppressed"] += 1
        return
    # 2. verification for anything substantive
    if kind == _a.KIND_ANSWER and not ticket.get("verification_after"):
        log(f"[slack-convo/outbox] SUPPRESSED answer row {row['id']}: ticket "
            f"{ticket['id']} has no verification_after")
        bus.mark_message(row["id"], "suppressed")
        summary["suppressed"] += 1
        return
    # 3. trust ladder, re-checked at post time
    recipient_kind = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
    if not _recipient_armed(identity, recipient_kind):
        bus.mark_message(row["id"], "held")
        summary["held"] += 1
        return
    # 4. destination
    channel = ticket.get("slack_channel_id")
    surface = att.get("surface") or ""
    thread_ts = None if surface in (_a.SURFACE_IM, _a.SURFACE_MPIM) else ticket.get("slack_thread_ts")
    if not channel:
        bus.mark_message(row["id"], "failed")
        summary["failed"] += 1
        return
    ts = post(channel, row["body"], thread_ts=thread_ts)
    bus.mark_message(row["id"], "posted", slack_ts=ts)
    summary["posted"] += 1


def release_held(bus, message_id, *, approved_by, log=print):
    """A human tap on a hold notice: flip that held row to ready and stamp the ticket.
    Returns True when a held row was released. Refuses anything not currently held."""
    rows = bus._get("support_messages", {"id": f"eq.{message_id}", "select": "*", "limit": "1"})
    if not rows or rows[0].get("delivery_status") != "held":
        return False
    bus.mark_message(message_id, "ready")
    try:
        from datetime import datetime, timezone
        bus.set_ticket(rows[0]["ticket_id"], approved_by=approved_by,
                       approved_via="slack_button",
                       approved_at=datetime.now(timezone.utc).isoformat())
    except Exception as e:  # noqa: BLE001 - the release itself already happened
        log(f"[slack-convo/outbox] approval stamp failed: {type(e).__name__}")
    return True
