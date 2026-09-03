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

  0. KNOWN KIND, KNOWN IDENTITY. A row whose kind is not one of ours, or that carries no
     identity stamp, is suppressed, not posted (V-M7: fail closed).
  1. FIRST CONTACT. The parent ticket must carry at least one inbound human row, or the row
     is suppressed. The bot never speaks first, structurally.
  2. VERIFICATION. A substantive reply (kind=answer) posts only if the parent ticket has
     verification_after populated. Missing -> suppressed, loudly. Never "should be fixed."
  3. NO RE-ENTRY. A conversational body carrying the ops-fix trigger prefix is suppressed;
     the bot's own reply must never be readable as a command by the ops-fix worker (RT-m2).
  4. FRESHNESS. A conversational row that sat in 'ready' longer than STALE_AFTER_SECONDS
     (outbox down, channel unset) is suppressed: a stale "checking that now" hours later is
     worse than silence (V-m2). Internal rows never go stale; a human still needs them.
  5. TRUST LADDER. A conversational reply to a client posts only if the identity's
     client-reply flag is armed; to staff only if the staff flag is armed. Otherwise the row
     is moved to 'held' AND a tap notice is written (V-M8), so nothing sits held unseen.
  6. DESTINATION. Internal kinds (escalation, hold_notice) go to the fixer channel and
     fixer_request goes to the ops-fix intake channel the existing worker watches. They
     never enter the person's thread. Conversational kinds go to the ticket's own channel:
     top-level in a DM or group DM (people do not thread there), in-thread for a mention or
     a channel thread.

Every SUPPRESSION writes an escalation row, so a human sees what the bot declined to say
(V-M5). A hold notice posts with a Block Kit button (action id slack_convo_release, value =
the held row id) so the tap actually exists (V-M2 / RT-m5). When an answer posts, its ticket
is marked resolved -- the ticket closes when the person has the answer, not before (V-M4).

A post failure marks the row 'failed' and moves on; one bad row never stalls the queue.
"""
from datetime import datetime, timezone

from . import adapter as _a
from .. import config

STALE_AFTER_SECONDS = 6 * 3600
RELEASE_ACTION_ID = "slack_convo_release"
_REENTRY_PREFIX = "OPS-FIX REQUEST"


def _channel_for(kind):
    if kind == _a.KIND_FIXER_REQUEST:
        return config.ops_fix_channel_id()
    return config.fixer_channel_id()


def _recipient_armed(identity, recipient_kind):
    if recipient_kind in ("staff", "coach"):
        return config.slack_convo_staff_reply_armed(identity.name)
    return config.slack_convo_client_reply_armed(identity.name)


def _parse_ts(value):
    try:
        s = str(value or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _age_seconds(row, now=None):
    att = row.get("attachments") or {}
    born = _parse_ts(att.get("released_at")) or _parse_ts(row.get("created_at"))
    if born is None:
        return 0
    return ((now or datetime.now(timezone.utc)) - born).total_seconds()


def hold_notice_blocks(row):
    """Block Kit for a hold notice: the text plus ONE button whose value is the held row id.
    listener_wiring routes that action id to release_held, operator-gated."""
    att = row.get("attachments") or {}
    mid = att.get("held_message_id") or ""
    blocks = [{"type": "section",
               "text": {"type": "mrkdwn", "text": (row.get("body") or "")[:2900]}}]
    if mid:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": RELEASE_ACTION_ID, "value": str(mid),
            "text": {"type": "plain_text", "text": "Release"}, "style": "primary"}]})
    return blocks


def run_once(bus, post, *, identity, log=print, limit=50, now=None):
    """Process up to `limit` ready rows for THIS identity.
    post(channel, text, thread_ts=None, blocks=None) -> slack ts.
    Returns a summary dict. Never raises out of the loop."""
    summary = {"posted": 0, "held": 0, "suppressed": 0, "failed": 0, "skipped": 0,
               "resolved": 0}
    try:
        rows = bus.outbox("ready", limit=limit, identity=identity.name)
    except TypeError:  # a bus without the identity filter (older fakes)
        rows = bus.outbox("ready", limit=limit)
    except Exception as e:  # noqa: BLE001
        log(f"[slack-convo/outbox] read failed: {type(e).__name__}")
        return summary
    for row in rows:
        try:
            _dispatch_one(bus, post, row, identity=identity, log=log, summary=summary, now=now)
        except Exception as e:  # noqa: BLE001 - one row never stalls the queue
            log(f"[slack-convo/outbox] row {row.get('id')} failed: {type(e).__name__}")
            try:
                bus.mark_message(row["id"], "failed")
            except Exception:  # noqa: BLE001
                pass
            summary["failed"] += 1
    return summary


def _suppress(bus, row, ticket, identity, why, log, summary, *, escalate=True):
    log(f"[slack-convo/outbox] SUPPRESSED row {row['id']}: {why}")
    bus.mark_message(row["id"], "suppressed", meta_update={"suppressed_why": why})
    summary["suppressed"] += 1
    if escalate and ticket:
        # V-M5: a human sees every reply the bot declined to send.
        bus.record_outbound(
            ticket_id=ticket["id"], author_type="system",
            body=(f"SUPPRESSED reply on ticket {ticket['id']} ({identity.name}): {why}. "
                  f"Nothing was posted; a person should look."),
            delivery_status="ready", kind=_a.KIND_ESCALATION,
            meta={"identity": identity.name, "suppressed_message_id": row["id"],
                  "recipient_kind": (row.get("attachments") or {}).get("recipient_kind")})


def _dispatch_one(bus, post, row, *, identity, log, summary, now=None):
    att = row.get("attachments") or {}
    kind = att.get("kind") or ""
    ticket = bus.ticket(row["ticket_id"])
    if not ticket:
        _suppress(bus, row, None, identity, "parent ticket missing", log, summary,
                  escalate=False)
        return
    # only rows for THIS identity; another identity's loop owns the rest
    row_ident = att.get("identity") or ""
    if row_ident and row_ident != identity.name:
        summary["skipped"] += 1
        return
    if (ticket.get("bot_identity") or "") != identity.name:
        summary["skipped"] += 1
        return
    # 0. fail closed on anything we do not recognise
    if kind not in _a.ALL_KINDS:
        _suppress(bus, row, ticket, identity, f"unknown kind {kind!r}", log, summary)
        return
    if not row_ident:
        _suppress(bus, row, ticket, identity, "row carries no identity stamp", log, summary)
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
        blocks = hold_notice_blocks(row) if kind == _a.KIND_HOLD_NOTICE else None
        ts = post(channel, row["body"], thread_ts=None, blocks=blocks)
        bus.mark_message(row["id"], "posted", slack_ts=ts)
        summary["posted"] += 1
        return

    # ---- conversational kinds: the gates ---------------------------------------------
    # 1. first contact
    if bus.inbound_count(ticket["id"]) < 1:
        _suppress(bus, row, ticket, identity,
                  "ticket has no inbound human message; the bot never speaks first",
                  log, summary)
        return
    # 2. verification for anything substantive
    if kind == _a.KIND_ANSWER and not ticket.get("verification_after"):
        _suppress(bus, row, ticket, identity, "answer with no verification_after", log,
                  summary)
        return
    # 3. no re-entry into the ops-fix worker through our own mouth
    if _REENTRY_PREFIX in (row.get("body") or "").upper():
        _suppress(bus, row, ticket, identity, "reply body carries the ops-fix trigger prefix",
                  log, summary)
        return
    # 4. freshness
    if _age_seconds(row, now) > STALE_AFTER_SECONDS:
        _suppress(bus, row, ticket, identity,
                  f"row sat in ready for over {STALE_AFTER_SECONDS // 3600}h", log, summary)
        return
    # 5. trust ladder, re-checked at post time; held rows always get a card (V-M8)
    recipient_kind = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
    if not _recipient_armed(identity, recipient_kind):
        bus.mark_message(row["id"], "held", meta_update={"held_why": "flag off at post time"})
        summary["held"] += 1
        _a.write_hold_notice(
            bus, ident_name=identity.name, tid=ticket["id"], recipient_kind=recipient_kind,
            user=ticket.get("slack_user_id") or "?", account_key=None, kind=kind,
            body=row.get("body") or "", held_message_id=row["id"],
            surface=att.get("surface") or "", why="flag off at post time")
        return
    # 6. destination
    channel = ticket.get("slack_channel_id")
    surface = att.get("surface") or ""
    thread_ts = None if surface in (_a.SURFACE_IM, _a.SURFACE_MPIM) else ticket.get("slack_thread_ts")
    if not channel:
        bus.mark_message(row["id"], "failed")
        summary["failed"] += 1
        return
    ts = post(channel, row["body"], thread_ts=thread_ts, blocks=None)
    bus.mark_message(row["id"], "posted", slack_ts=ts)
    summary["posted"] += 1
    if kind == _a.KIND_ANSWER and ticket.get("status") == "verification":
        # V-M4: the ticket closes when the person HAS the answer, not when we drafted it.
        bus.set_ticket(ticket["id"], status="resolved")
        summary["resolved"] += 1


def release_held(bus, message_id, *, approved_by, identity=None, log=print):
    """A human tap on a hold notice: flip that held row to ready and stamp the ticket.
    Returns True when a held row was released. Refuses anything not currently held, any
    kind that is not a reply or fixer request, and (when `identity` is given) any row
    another bot wrote (V-m10). The release is stamped so the freshness clock restarts."""
    row = bus.message(message_id)
    if not row or row.get("delivery_status") != "held":
        return False
    att = row.get("attachments") or {}
    kind = att.get("kind") or ""
    if kind not in (_a.CONVERSATIONAL_KINDS | {_a.KIND_FIXER_REQUEST}):
        log(f"[slack-convo/outbox] release refused: row {message_id} kind {kind!r}")
        return False
    if identity is not None and (att.get("identity") or "") != identity.name:
        log(f"[slack-convo/outbox] release refused: row {message_id} belongs to "
            f"{att.get('identity') or '?'} not {identity.name}")
        return False
    bus.mark_message(message_id, "ready",
                     meta_update={"released_at": datetime.now(timezone.utc).isoformat(),
                                  "released_by": approved_by})
    try:
        bus.set_ticket(row["ticket_id"], approved_by=approved_by, approved_via="slack_button",
                       approved_at=datetime.now(timezone.utc).isoformat())
    except Exception as e:  # noqa: BLE001 - the release itself already happened
        log(f"[slack-convo/outbox] approval stamp failed: {type(e).__name__}")
    return True
