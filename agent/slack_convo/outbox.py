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
     is moved to 'held' AND a tap notice is written (V-M8), so nothing sits held unseen. A
     row Blake has explicitly released (release_held stamped released_by) skips this recheck
     once -- his tap IS the approval the trust ladder exists to collect (N2).
  6. CLAIM. The row is moved ready/held->posting with a conditional PATCH only one caller
     can win, immediately before post() (N4). This is what makes two consumers of the same
     row safe.
  7. DESTINATION. Internal kinds (escalation, hold_notice) go to the fixer channel and
     fixer_request goes to the ops-fix intake channel the existing worker watches. They
     never enter the person's thread. Conversational kinds go to the ticket's own channel:
     top-level in a DM or group DM (people do not thread there), in-thread for a mention or
     a channel thread.

Every SUPPRESSION writes an escalation row, so a human sees what the bot declined to say
(V-M5). A hold notice posts with a Block Kit button (action id slack_convo_release, value =
the held row id) so the tap actually exists (V-M2 / RT-m5), rendered across as many sections
as the full body needs (RA-M2: Blake must review exactly the text that will post on release,
never a truncated prefix of it). When an answer posts, its ticket is marked resolved -- the
ticket closes when the person has the answer, not before (V-M4).

A post failure marks the row 'failed' and moves on; one bad row never stalls the queue.

HARDENING (2026-09-03 re-audit wave 2):
  N2  Blake's tap on a held row used to be swallowed silently: release_held flipped it to
      'ready', but gate 5 re-read the SAME flag that had held it, found it still off (that
      IS why it was held), and held it again -- writing a fresh card each time, forever. A
      released row now skips the trust-ladder recheck once.
  N4  Read-then-post-then-mark had no claim step: two consumers (a redeploy overlap, a
      second Wrangler pointed at the same rows per D2) could post the same row twice, and a
      mark_message failure after a successful post left the row in 'ready' to be reposted
      forever. Every row is now claimed (ready/held -> posting, a conditional PATCH only one
      caller can win) immediately before post(); any row still in 'posting' at the START of
      a run_once call is orphaned from a crashed prior attempt (claim -> post -> mark is
      synchronous within one call, so nothing legitimate is ever mid-flight across calls)
      and is swept back to 'ready'.
  RA-M2  hold_notice_blocks used to show only the first 2900 characters while release posted
      the full row -- an injected tail could sit invisible to the reviewer. Now it renders
      as many sections as the body needs; what Blake reviews is what posts.
  RA-m5  fixer_request always used the shared ops-fix worker channel (ground truth: only one
      worker exists, and it only trusts Echo's bot_id -- a cross-identity ruling for Blake,
      D10). escalation / hold_notice now honour the identity's OWN fixer_channel_env when
      set, instead of always the global default, so a second identity's holds do not land
      in Echo's channel.
"""
from datetime import datetime, timezone

from . import adapter as _a
from .. import config

STALE_AFTER_SECONDS = 6 * 3600
RELEASE_ACTION_ID = "slack_convo_release"
RESOLVE_ACTION_ID = "slack_convo_resolve"
_REENTRY_PREFIX = "OPS-FIX REQUEST"
_BLOCK_TEXT_CHARS = 2900

# D48 (Blake, 2026-09-05): a ticket the person submitted IN THE PORTAL has a second, real
# delivery surface that is not Slack -- the /my/support/[ticketId] thread they submitted it
# from. Migration 0310 already decides what a client may read there: an outbound row that is
# delivery_status='posted' and not an internal kind. So for these tickets "post" means
# "release the row into the thread they are already looking at", and gate 7 below marks it
# posted instead of failing it for having no Slack channel. Restricted to the two sources a
# client actually submits through the portal UI (a website_intake / engage_tenant_event
# ticket has no portal reader), and to tickets carrying a client_id, without which 0310's
# own predicate can never match the reader to the row.
PORTAL_THREAD_SOURCES = frozenset({"portal_form", "website_tab"})

RESOLVED_NOTICE = (
    "Update from the LASSO team: this one is handled. If that is not what you needed, "
    "reply here and we will pick it back up.")


def portal_deliverable(ticket):
    """True when the portal support thread is a real delivery surface for this ticket."""
    t = ticket or {}
    return (str(t.get("source") or "") in PORTAL_THREAD_SOURCES
            and bool(str(t.get("client_id") or "").strip()))


def _channel_for(kind, identity):
    if kind == _a.KIND_FIXER_REQUEST:
        return config.ops_fix_channel_id()
    return identity.fixer_channel() or config.fixer_channel_id()


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
    born = (_parse_ts(att.get("claimed_at")) or _parse_ts(att.get("released_at"))
           or _parse_ts(row.get("created_at")))
    if born is None:
        return 0
    return ((now or datetime.now(timezone.utc)) - born).total_seconds()


def hold_notice_blocks(row):
    """Block Kit for a hold notice: the FULL text across as many sections as it needs (RA-M2
    -- what Blake reviews before tapping must be everything that will post, never a
    truncated prefix), plus ONE button whose value is the held row id. listener_wiring
    routes that action id to release_held, operator-gated."""
    body = row.get("body") or ""
    att = row.get("attachments") or {}
    mid = att.get("held_message_id") or ""
    chunks = [body[i:i + _BLOCK_TEXT_CHARS] for i in range(0, len(body), _BLOCK_TEXT_CHARS)] or [""]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]
    if mid:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": RELEASE_ACTION_ID, "value": str(mid),
            "text": {"type": "plain_text", "text": "Release"}, "style": "primary"}]})
    return blocks


def escalation_blocks(row, ticket):
    """Block Kit for an escalation card: the full text, plus ONE button that closes the loop
    back to the person who wrote in (D48).

    An escalation used to be the end of the line for the submitter. The card reached #fixer,
    Blake dealt with it, and nothing ever went back -- no acknowledgement, no "this is
    handled". The button is the missing half: listener_wiring routes it (operator-gated) to
    resolve_and_notify below, which writes the person a status row and closes the ticket.

    Rendered only when there IS somewhere to send that notice (a portal thread or an already
    opened group DM) and the ticket is not already closed; a button that could only no-op is
    worse than no button."""
    body = row.get("body") or ""
    tid = str((ticket or {}).get("id") or "")
    chunks = [body[i:i + _BLOCK_TEXT_CHARS] for i in range(0, len(body), _BLOCK_TEXT_CHARS)] or [""]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": c}} for c in chunks]
    reachable = portal_deliverable(ticket) or bool((ticket or {}).get("slack_channel_id"))
    if tid and reachable and (ticket or {}).get("status") != "resolved":
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": RESOLVE_ACTION_ID, "value": tid,
            "text": {"type": "plain_text", "text": "Resolved, tell them"}}]})
    return blocks


CLAIM_TIMEOUT_SECONDS = 90


def _claim(bus, row, log):
    """Ready/held -> posting, a conditional PATCH only one caller can win (N4). Also stamps
    claimed_at (a second, non-atomic write, but safe: we already exclusively own the row --
    the CAS matched only for us) so _recover_stale_claims can tell a row that just started
    posting from one truly orphaned by a crash (D26)."""
    try:
        if not bus.claim_message(row["id"]):
            return False
    except AttributeError:
        return True  # a bus without claim support (older fakes): best effort, no CAS
    except Exception as e:  # noqa: BLE001
        log(f"[slack-convo/outbox] claim failed for row {row['id']}: {type(e).__name__}")
        return False
    try:
        bus.mark_message(row["id"], "posting",
                         meta_update={"claimed_at": datetime.now(timezone.utc).isoformat()})
    except Exception:  # noqa: BLE001 - best-effort stamp; the claim itself already succeeded
        pass
    return True


def _recover_stale_claims(bus, identity, log, now=None):
    """A row still 'posting' after CLAIM_TIMEOUT_SECONDS is orphaned -- either a crash
    between claim and mark in THIS process, or (D26, the scenario the claim step exists for)
    a redeploy overlap / second consumer per D2 that crashed mid-post. Swept back to 'ready'
    so it is retried rather than stuck forever.

    D26 (2026-09-03, MAJOR): this used to sweep EVERY 'posting' row unconditionally, with no
    staleness check. Under exactly the multi-consumer scenario it exists to protect against,
    a row genuinely mid-flight in process A (a slow Slack API call) would be un-claimed by
    process B's very next 5-second sweep and re-posted while A was still in flight -- a
    duplicate post, the opposite of the guarantee. The timeout is generous over realistic
    post() latency so a live post is never stolen out from under it."""
    try:
        stuck = bus.outbox("posting", limit=200, identity=identity.name)
    except TypeError:
        try:
            stuck = bus.outbox("posting", limit=200)
        except Exception:  # noqa: BLE001
            return 0
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for row in stuck:
        if _age_seconds(row, now) < CLAIM_TIMEOUT_SECONDS:
            continue  # plausibly still in flight; do not steal it
        try:
            bus.mark_message(row["id"], "ready", meta_update={"reclaimed_stale_posting": True})
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def run_once(bus, post, *, identity, log=print, limit=50, now=None):
    """Process up to `limit` ready rows for THIS identity.
    post(channel, text, thread_ts=None, blocks=None) -> slack ts.
    Returns a summary dict. Never raises out of the loop."""
    summary = {"posted": 0, "held": 0, "suppressed": 0, "failed": 0, "skipped": 0,
               "resolved": 0, "reclaimed": 0}
    summary["reclaimed"] = _recover_stale_claims(bus, identity, log, now=now)
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
        channel = _channel_for(kind, identity)
        if not channel:
            log(f"[slack-convo/outbox] no channel configured for {kind}; row "
                f"{row['id']} marked failed (set AGENT_FIXER_CHANNEL_ID / the ops-fix "
                "channel)")
            bus.mark_message(row["id"], "failed")
            summary["failed"] += 1
            return
        if not _claim(bus, row, log):
            summary["skipped"] += 1
            return
        if kind == _a.KIND_HOLD_NOTICE:
            blocks = hold_notice_blocks(row)
        elif kind == _a.KIND_ESCALATION:
            blocks = escalation_blocks(row, ticket)
        else:
            blocks = None
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
    # 5. trust ladder, re-checked at post time; held rows always get a card (V-M8). A row
    # Blake has explicitly released is the one exception: his tap already IS the approval.
    recipient_kind = att.get("recipient_kind") or ticket.get("identity_kind") or "client"
    if not att.get("released_by") and not _recipient_armed(identity, recipient_kind):
        bus.mark_message(row["id"], "held", meta_update={"held_why": "flag off at post time"})
        summary["held"] += 1
        _a.write_hold_notice(
            bus, ident_name=identity.name, tid=ticket["id"], recipient_kind=recipient_kind,
            user=ticket.get("slack_user_id") or "?", account_key=None, kind=kind,
            body=row.get("body") or "", held_message_id=row["id"],
            surface=att.get("surface") or "", why="flag off at post time")
        return
    # 6. claim, immediately before posting
    if not _claim(bus, row, log):
        summary["skipped"] += 1
        return
    # 7. destination
    channel = ticket.get("slack_channel_id")
    surface = att.get("surface") or ""
    thread_ts = None if surface in (_a.SURFACE_IM, _a.SURFACE_MPIM) else ticket.get("slack_thread_ts")
    if not channel:
        # D48: no Slack thread is not automatically a dead end. A portal-submitted ticket
        # is delivered to the thread the person wrote it in; only a ticket with neither
        # surface fails. Before this, EVERY conversational row on a portal ticket that had
        # not been group-DMed died here -- marked failed, silently, with the person who
        # submitted the form never told anything at all (found live 2026-09-05 on three
        # escalated portal tickets).
        if not portal_deliverable(ticket):
            bus.mark_message(row["id"], "failed")
            summary["failed"] += 1
            return
        bus.mark_message(row["id"], "posted", meta_update={"delivered_via": "portal_thread"})
        summary["posted"] += 1
        _resolve_on_answer(bus, ticket, kind, summary)
        return
    ts = post(channel, row["body"], thread_ts=thread_ts, blocks=None)
    bus.mark_message(row["id"], "posted", slack_ts=ts)
    summary["posted"] += 1
    _resolve_on_answer(bus, ticket, kind, summary)


def _resolve_on_answer(bus, ticket, kind, summary):
    """V-M4: the ticket closes when the person HAS the answer, not when we drafted it."""
    if kind == _a.KIND_ANSWER and ticket.get("status") == "verification":
        bus.set_ticket(ticket["id"], status="resolved")
        summary["resolved"] += 1


def release_held(bus, message_id, *, approved_by, identity=None, log=print):
    """A human tap on a hold notice: flip that held row to ready and stamp the ticket.
    Returns True when a held row was released. Refuses anything not currently held, any
    kind that is not a reply or fixer request, and (when `identity` is given) any row
    another bot wrote (V-m10). The release is stamped (N2: read by gate 5 above to skip the
    trust-ladder recheck exactly once for this row) and restarts the freshness clock."""
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


def resolve_and_notify(bus, ticket_id, *, approved_by, identity, log=print):
    """A human tap on an escalation card: tell the person it is handled, and close the
    ticket (D48).

    The notice is written as a normal conversational row, so it goes out through every gate
    this module already enforces (first contact, trust ladder, freshness, claim) and lands
    wherever that ticket's person actually is: the group DM if one was opened, the portal
    support thread otherwise. Nothing is posted from here directly.

    Refuses, returning False, when: the ticket is gone, it belongs to another bot (the same
    cross-identity rule release_held holds), it is already resolved (the tap is idempotent --
    a second press must not write a second notice), or there is nowhere to deliver."""
    ticket = bus.ticket(ticket_id)
    if not ticket:
        log(f"[slack-convo/outbox] resolve refused: no ticket {ticket_id}")
        return False
    if identity is not None and (ticket.get("bot_identity") or "") != identity.name:
        log(f"[slack-convo/outbox] resolve refused: ticket {ticket_id} belongs to "
            f"{ticket.get('bot_identity') or '?'} not {identity.name}")
        return False
    if ticket.get("status") == "resolved":
        return False
    if not portal_deliverable(ticket) and not ticket.get("slack_channel_id"):
        log(f"[slack-convo/outbox] resolve refused: ticket {ticket_id} has no delivery "
            "surface (no portal thread, no group DM)")
        return False
    bus.record_outbound(
        ticket_id=ticket_id, author_type=getattr(identity, "name", "system"),
        body=RESOLVED_NOTICE, delivery_status="ready", kind=_a.KIND_STATUS,
        meta={"identity": getattr(identity, "name", ""), "recipient_kind": "client",
              "surface": (ticket.get("source") or ""), "resolved_by": approved_by})
    bus.set_ticket(ticket_id, status="resolved", approved_by=approved_by,
                   approved_via="slack_button",
                   approved_at=datetime.now(timezone.utc).isoformat())
    return True
