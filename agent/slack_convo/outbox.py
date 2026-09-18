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
import hashlib
import json

from . import adapter as _a
from .. import config

# Surfaces where a reply goes TOP LEVEL rather than in a thread: DMs and group DMs (people do
# not thread there), and a portal-bridge ticket, whose Slack home is the group DM this system
# opened for it.
TOP_LEVEL_SURFACES = frozenset({"im", "mpim", "portal_ticket_bridge"})

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


def _customer_fix_reply(ticket, att):
    """Identify customer handoffs even after escalation clears classification.

    The FIXER poll requires classification NULL, so classification alone cannot
    protect held portal incidents. A direct grounded QUESTION keeps its explicit
    classification and remains eligible for the normal answer gates.
    """
    recipient = (att.get("recipient_kind") or ticket.get("identity_kind") or "client")
    if recipient in ("staff", "coach"):
        return False
    classification = str(ticket.get("classification") or "").lower()
    direct_question = (classification == "answerable_question"
                       and ticket.get("status") == "verification"
                       and ticket.get("escalated") is not True
                       and not ticket.get("hold_tier")
                       and not (ticket.get("verification_after") or {}).get("hold"))
    if direct_question and not att.get("fixer"):
        return False
    portal_handoff = (ticket.get("product") == "echo"
                      and portal_deliverable(ticket)
                      and (ticket.get("escalated") is True
                           or bool(ticket.get("hold_tier"))
                           or bool((ticket.get("verification_after") or {}).get("hold"))))
    return classification == "code_fix" or bool(att.get("fixer")) or portal_handoff


def _verified_fix_notice(ticket, att, kind):
    """Only the current fix's resolve notice may tell a customer it is handled."""
    verification = ticket.get("verification_after") or {}
    release = verification.get("fixer") or {}
    deployment = release.get("deployment_check") or {}
    if kind != _a.KIND_STATUS or att.get("resolve_notice") is not True:
        return False
    operation = release.get("ops_action") or {}
    if att.get("ops_action") in {"reset_recreate_budget", "requeue_failed_row"}:
        return (ticket.get("status") == "verification"
                and release.get("postcondition_verified") is True
                and operation.get("identityVerified") is True
                and operation.get("ok") is True
                and operation.get("action") == att.get("ops_action")
                and operation.get("tenantVerified") is True
                and bool(ticket.get("client_id"))
                and operation.get("tenantId") == ticket.get("client_id")
                and bool(release.get("request_key")))
    # A healthy deployment proves the code is live, not that this owner's symptom
    # is gone. The independent business check must identify its observation and
    # bind it to the exact request and commit being released.
    business = release.get("business_postcondition") or {}
    if not isinstance(business, dict):
        return False
    return (kind == _a.KIND_STATUS and att.get("resolve_notice") is True
            and ticket.get("status") == "merged"
            and verification.get("exit_code") == 0
            and verification.get("incomplete") is not True
            and bool(ticket.get("fix_pr_url"))
            and att.get("pr_url") == ticket.get("fix_pr_url")
            and bool(release.get("merged_sha"))
            and deployment.get("verified") is True
            and deployment.get("sha") == release.get("merged_sha")
            and bool(release.get("request_key"))
            and business.get("source") == "independent_business_check"
            and business.get("verified") is True
            and business.get("symptom_resolved") is True
            and bool(str(business.get("check_id") or "").strip())
            and bool(str(business.get("evidence") or "").strip())
            and business.get("request_key") == release.get("request_key")
            and business.get("merged_sha") == release.get("merged_sha"))


def _current_fixer_request_key(bus, ticket):
    """Recompute Scout's requestKey from all requester inbound rows, failing closed.

    A truncated message read cannot prove the current request. Require fewer
    than the normal PostgREST response cap and compare a separate inbound read.
    """
    rows = bus.messages(ticket["id"], limit=1000)
    if not isinstance(rows, list) or len(rows) >= 1000:
        return None
    inbound = [m for m in rows if m.get("direction") == "inbound"]
    if len(inbound) != bus.inbound_count(ticket["id"]):
        return None
    requester = []
    for m in inbound:
        att = m.get("attachments") or {}
        author = m.get("author_type")
        client = author == "client" or not author
        operator_mention = (author in ("staff", "blake")
                            and att.get("surface") == "mention"
                            and att.get("identity_reason") == "operator list")
        if client or operator_mention:
            requester.append([m.get("id"), m.get("created_at"), m.get("body")])
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    requester.sort(key=encode)
    payload = requester or [ticket.get("id"), ticket.get("created_at"),
                            ticket.get("raw_text")]
    return hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()


def _channel_for(kind, identity):
    if kind == _a.KIND_FIXER_REQUEST:
        return config.ops_fix_channel_id()
    return identity.fixer_channel() or config.fixer_channel_id()


def _person_for_card(bus, ticket, identity):
    """m2: a post-time hold card names the person and gym in words, like a draft-time one.

    The outbox has no identity_gate result to hand (it runs long after resolution), so this
    reconstructs the same line from the ticket row itself -- reporter, gym, identity kind --
    rather than falling back to a bare Slack id, which is exactly the unreadable card D53
    was written to get rid of."""
    t = ticket or {}
    uid = str(t.get("slack_user_id") or "") or "?"
    kind = str(t.get("identity_kind") or "unknown")
    email = str(t.get("reporter") or "").strip()
    gym = str(t.get("client_id") or "").strip()
    label = ""
    try:
        if gym:
            rows = bus._get("gyms", {"id": f"eq.{gym}", "select": "name", "limit": "1"})
            label = str((rows or [{}])[0].get("name") or "").strip()
    except Exception:  # noqa: BLE001 - a name lookup never blocks a card
        label = ""
    bits = [f"{kind} {_a._slack_escape(uid)}"]
    if email:
        bits.append(_a._slack_escape(email))
    bits.append(f"gym {_a._slack_escape(label or gym or 'not resolved')}")
    return ", ".join(bits)


def _client_dm_lane_meta_key():
    """The attachments key client_dm_support stamps on every row it writes
    (lane.py's LANE_META), imported by name rather than duplicated as a magic
    string here so the two can never drift apart."""
    from ..client_dm_support.lane import LANE_META
    return LANE_META


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


def _blake_is_member(identity, channel, user):
    """Read every Slack membership page; an incomplete or unsupported read is not proof."""
    if not channel.startswith(("C", "G")) or not user:
        return False
    from slack_sdk import WebClient
    client = WebClient(token=identity.env(identity.bot_token_env), timeout=5)
    cursor = None
    for _ in range(100):
        args = {"channel": channel, "limit": 200}
        if cursor:
            args["cursor"] = cursor
        response = client.conversations_members(**args)
        if not response.get("ok"):
            return False
        if user in (response.get("members") or []):
            return True
        cursor = (response.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return False
    return False


def run_once(bus, post, *, identity, log=print, limit=50, now=None,
             member_check=None):
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
            _dispatch_one(bus, post, row, identity=identity, log=log, summary=summary,
                          now=now, member_check=member_check or
                          (lambda channel, user: _blake_is_member(identity, channel, user)))
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


def _dispatch_one(bus, post, row, *, identity, log, summary, now=None,
                  member_check=None):
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
        try:
            ts = post(channel, row["body"], thread_ts=None, blocks=blocks)
        except Exception as e:  # noqa: BLE001
            # Audit 4, finding 2: a hold card or escalation that fails to post is the case
            # where a human never learns anything -- while the client may already have been
            # acknowledged. It must be LOUD; silence here is the whole failure mode.
            log(f"[slack-convo/outbox] CRITICAL internal {kind} FAILED to reach "
                f"{channel} for ticket {ticket['id']}: {type(e).__name__}: {e}. "
                f"Nobody has been told about this ticket.")
            raise
        bus.mark_message(row["id"], "posted", slack_ts=ts)
        summary["posted"] += 1
        return

    # ---- conversational kinds: the gates ---------------------------------------------
    # Re-read deployment proof at dispatch time. A queued FIXER acknowledgement,
    # held-answer replacement, or stale notice must never reach a client.
    customer_fix = _customer_fix_reply(ticket, att)
    if customer_fix:
        if not _verified_fix_notice(ticket, att, kind):
            _suppress(bus, row, ticket, identity,
                      "customer fix reply requires current PR merged, deployed and verified",
                      log, summary)
            return
        release_key = ((ticket.get("verification_after") or {}).get("fixer") or {}).get("request_key")
        try:
            current_key = _current_fixer_request_key(bus, ticket)
        except Exception as e:  # noqa: BLE001 - unreadable thread must not close a ticket
            log(f"[slack-convo/outbox] request read failed for {ticket['id']}: "
                f"{type(e).__name__}")
            current_key = None
        if not current_key or release_key != current_key or att.get("request_key") != current_key:
            _suppress(bus, row, ticket, identity,
                      "customer fix reply does not match the current requester messages",
                      log, summary)
            return
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
    # 5a. D54 hard lines, re-checked at POST time (a flag can flip, and a row can be written
    # by an older process). A substantive ANSWER to a client posts on its own ONLY with that
    # identity's AUTO_ANSWER flag armed, and NEVER when the draft-time check marked it
    # forbidden. Blake's release tap is still the way any of these actually go out.
    if kind == _a.KIND_ANSWER and recipient_kind not in ("staff", "coach") \
            and not att.get("released_by"):
        # M2 (audit 2): this used to trust the stored marker alone, so a row written by any
        # writer that omits it -- a pre-D54 process, a future one -- posted a hard-line
        # answer to a client with no tap. The body is re-read here, which is what "checked
        # again at post time" has to mean for a rule that is about content.
        # Audit 4, finding 4: this re-read only the DENYLIST against the body, so the claim
        # "both are checked at draft time AND at post time" was false and the whole point of
        # the single may_auto_answer decision (that no path enforces half the rule) did not
        # hold for the post-time path. The ticket carries the question -- raw_text -- so the
        # identical decision can be, and now is, made here too.
        # D72 (2026-09-11): the verdict names the rule and carries a TIER. A row the FIXER
        # authored (attachments.fixer) is checked against the org floor only; an Echo
        # draft gets the structural checks too. Either way a hold is never silent:
        # hold_answer_for_team writes the team card and escalates the ticket.
        # Portal holds wait for verification and Blake's customer handoff.
        fixer_authored = bool(att.get("fixer"))
        verdict = _a.auto_answer_verdict(ticket.get("raw_text") or "", row.get("body"),
                                         grounded_by_fixer=fixer_authored)
        if att.get("auto_answer_forbidden") and verdict.ok:
            # A draft-time check marked this org floor; the marker is a hard signal even
            # when the body re-read passes (an older, stricter rule may have written it).
            verdict = _a.AnswerVerdict(False, _a.HOLD_TIER_ORG_FLOOR,
                                       "draft_time_forbidden_marker")
        if verdict.held:
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": f"{verdict.tier}: {verdict.rule}",
                                          "hold_tier": verdict.tier,
                                          "hold_rule": verdict.rule})
            summary["held"] += 1
            _a.hold_answer_for_team(
                bus, ticket=ticket, ident_name=identity.name, recipient_kind=recipient_kind,
                user=ticket.get("slack_user_id") or "?", account_key=None,
                surface=att.get("surface") or "", body=row.get("body") or "",
                held_message_id=row["id"], verdict=verdict,
                person=_person_for_card(bus, ticket, identity),
                fixer_authored=fixer_authored,
                client_notice=(att.get("surface") != "portal_ticket_bridge"
                               and not portal_deliverable(ticket)), log=log)
            return
        if not config.slack_convo_auto_answer_armed(identity.name):
            flag = f"SLACK_CONVO_{identity.name.upper()}_AUTO_ANSWER"
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": "auto answer not armed",
                                          "hold_tier": _a.HOLD_TIER_UNARMED})
            summary["held"] += 1
            _a.hold_answer_for_team(
                bus, ticket=ticket, ident_name=identity.name, recipient_kind=recipient_kind,
                user=ticket.get("slack_user_id") or "?", account_key=None,
                surface=att.get("surface") or "", body=row.get("body") or "",
                held_message_id=row["id"],
                verdict=_a.AnswerVerdict(False, _a.HOLD_TIER_UNARMED, "auto_answer_not_armed"),
                person=_person_for_card(bus, ticket, identity),
                fixer_authored=fixer_authored, unarmed_flag=flag,
                client_notice=(att.get("surface") != "portal_ticket_bridge"
                               and not portal_deliverable(ticket)), log=log)
            return
    # 5b. GAP 2 (audit of PR #68): a row THIS SPECIFIC LANE wrote must re-verify that
    # lane's OWN full three-flag interlock at dispatch time, not only the general
    # _recipient_armed check above. arming.preflight() checks AGENT_CLIENT_DM_AUTOFIX,
    # AGENT_CLIENT_DM_CLIENT_REPLY and a runtime-derived AGENT_CLIENT_DM_LIVE_ACK --
    # none of which _recipient_armed (SLACK_CONVO_<ID>_CLIENT_REPLY alone) has ever
    # read. Without this, a row written while the lane was briefly LIVE stays in
    # 'ready' after AGENT_CLIENT_DM_AUTOFIX is later revoked (or the ack goes stale),
    # and this same _recipient_armed check -- true the whole time, since it is a
    # different, pre-existing, already-armed flag -- would still release it with zero
    # awareness this lane, or its revocation, exists. Scoped to rows carrying this
    # lane's own provenance marker, so no other identity's or lane's delivery changes.
    if not att.get("released_by") and att.get(_client_dm_lane_meta_key()):
        from ..client_dm_support import arming as _cdm_arm
        lane_arm = _cdm_arm.preflight(identity.name)
        if lane_arm.mode != _cdm_arm.MODE_LIVE:
            bus.mark_message(row["id"], "held",
                             meta_update={"held_why": "client_dm_support lane no longer "
                                                       f"live at dispatch time: "
                                                       f"{lane_arm.reason}"})
            summary["held"] += 1
            _a.write_hold_notice(
                bus, ident_name=identity.name, tid=ticket["id"],
                recipient_kind=recipient_kind, user=ticket.get("slack_user_id") or "?",
                account_key=None, kind=kind, body=row.get("body") or "",
                held_message_id=row["id"], surface=att.get("surface") or "",
                person=_person_for_card(bus, ticket, identity),
                why=f"client_dm_support's own arming no longer holds at dispatch "
                    f"time (re-checked independently of SLACK_CONVO_"
                    f"{identity.name.upper()}_CLIENT_REPLY): {lane_arm.reason}")
            return
    if not att.get("released_by") and not _recipient_armed(identity, recipient_kind):
        bus.mark_message(row["id"], "held", meta_update={"held_why": "flag off at post time"})
        summary["held"] += 1
        _a.write_hold_notice(
            bus, ident_name=identity.name, tid=ticket["id"], recipient_kind=recipient_kind,
            user=ticket.get("slack_user_id") or "?", account_key=None, kind=kind,
            body=row.get("body") or "", held_message_id=row["id"],
            surface=att.get("surface") or "", why="flag off at post time")
        return
    # FIXER customer Slack messages include Blake in the actual conversation. A receipt
    # in #fixer alone is not participation in the client's channel. Read membership at
    # dispatch; Slack read failures, one-to-one DMs and unsupported channel types hold.
    channel = ticket.get("slack_channel_id")
    fixer_customer_slack = bool(customer_fix)
    if fixer_customer_slack:
        try:
            member = bool(channel and channel.startswith(("C", "G")) and member_check and
                          member_check(channel, config.APPROVER_SLACK_ID))
        except Exception as e:  # noqa: BLE001
            log(f"[slack-convo/outbox] membership read failed for {channel}: "
                f"{type(e).__name__}")
            member = False
        if not member:
            _suppress(bus, row, ticket, identity,
                      "FIXER customer Slack reply requires verified Blake membership "
                      "in destination conversation", log, summary)
            return
    # 6. claim, immediately before posting
    if not _claim(bus, row, log):
        summary["skipped"] += 1
        return
    # 7. destination
    channel = ticket.get("slack_channel_id")
    surface = att.get("surface") or ""
    # F1 (audit 8, MAJOR): the audit-7 fix read the surface off the ticket's own inbound row
    # instead of its source -- and a portal ticket's inbound row carries
    # 'portal_ticket_bridge', which is not in this tuple, so the notice STILL posted as a
    # thread reply inside the group DM. Byte-identical behaviour to the bug it replaced, and
    # its test asserted the helper on a Slack MPIM ticket, never the portal case it was for.
    # People do not thread in a DM whatever brought the ticket there.
    thread_ts = (None if surface in TOP_LEVEL_SURFACES
                 else ticket.get("slack_thread_ts"))
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
        _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log)
        # m4: no Slack call happens on this branch -- "posted" here means migration 0310 now
        # lets the client read it in the thread they wrote from. The receipt says exactly
        # that rather than claiming a message was pushed to them.
        _receipt(bus, ticket, row, identity, kind, att,
                 where="released into the portal support thread they wrote from",
                 summary=summary)
        return
    sent_body = row["body"]
    if fixer_customer_slack:
        mention = f"<@{config.APPROVER_SLACK_ID}>"
        if mention not in sent_body:
            sent_body = f"{mention} {sent_body}"
        # Persist the exact body BEFORE posting, so the portal thread and subsequent
        # receipt cannot disagree with what Slack actually received.
        if sent_body != row["body"]:
            stored = bus.set_message_body_if_posting(row["id"], sent_body)
            if not stored or stored.get("body") != sent_body:
                raise RuntimeError("FIXER Slack body update was not confirmed")
            row = {**row, "body": sent_body}
    ts = post(channel, sent_body, thread_ts=thread_ts, blocks=None)
    bus.mark_message(row["id"], "posted", slack_ts=ts)
    summary["posted"] += 1
    _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log)
    _receipt(bus, ticket, row, identity, kind, att, where=f"Slack {channel}", summary=summary)


# Kinds worth a receipt in #fixer. An `ack` ("checking that for you now") is noise; what
# Blake asked to see is anything SUBSTANTIVE that reached the client without him: the answer
# itself, the honest no-draft template, and the resolution notice that closes a ticket.
RECEIPT_KINDS = frozenset({_a.KIND_ANSWER, _a.KIND_TEMPLATE, _a.KIND_STATUS})


def write_receipt(bus, ticket, *, identity, body, kind, where, auto, extra=None):
    """The shared receipt writer, so every path that tells a client something -- this
    module's outbox AND the portal bridge's direct-outreach path (M1) -- produces the same
    card. Written only AFTER a delivery actually succeeded."""
    from datetime import datetime as _dt, timezone as _tz
    sent_at = _dt.now(_tz.utc).isoformat()
    how = "SENT AUTOMATICALLY (no tap)" if auto else "sent"
    meta = {"identity": getattr(identity, "name", ""), "receipt": True,
            "receipt_kind": kind, "auto_answer": bool(auto), "sent_at": sent_at}
    if extra:
        meta.update(extra)
    return bus.record_outbound(
        ticket_id=ticket["id"], author_type="system",
        body=(f"RECEIPT: the client was told this, {how}.\n"
              f"BOT: {getattr(identity, 'name', '?')}   TICKET: {ticket['id']}   "
              f"KIND: {kind}\nWHERE: {where}   WHEN: {sent_at}\n"
              f"STATUS NOW: {(bus.ticket(ticket['id']) or ticket).get('status') or '?'}\n\n"
              f"{body or ''}"),
        delivery_status="ready", kind=_a.KIND_ESCALATION, meta=meta)


def _receipt(bus, ticket, row, identity, kind, att, *, where, summary):
    """D55: a receipt of what the client was ACTUALLY told, posted to #fixer.

    Blake, 2026-09-05: "so Blake never has to wonder whether a ticket actually landed with
    the client". This is written only AFTER the row is marked posted, and it quotes the real
    body that went out with the real timestamp, so it can never claim a delivery that did not
    happen. It is an internal kind: it goes to the fixer channel and never into the person's
    thread. A failure here never fails the post that already succeeded."""
    if kind not in RECEIPT_KINDS:
        return
    if (att or {}).get("recipient_kind") in ("staff", "coach"):
        return  # staff can see their own thread; a receipt would just be an echo
    try:
        sent_at = datetime.now(timezone.utc).isoformat()
        # m4: re-read the ticket so STATUS NOW is the status now, not the one captured
        # before _resolve_on_answer ran a line earlier.
        fresh = None
        try:
            fresh = bus.ticket(ticket["id"])
        except Exception:  # noqa: BLE001
            fresh = None
        status_now = (fresh or ticket).get("status") or "?"
        auto = bool(kind == _a.KIND_ANSWER and not (att or {}).get("released_by"))
        how = ("SENT AUTOMATICALLY (no tap)" if auto else
               ("sent after your tap" if (att or {}).get("released_by") else "sent"))
        # FIXER provenance can remain after a human tap; only the release actor counts.
        if (att or {}).get("released_by") == "fixer":
            how = "sent automatically by FIXER"
        owner = f"<@{config.APPROVER_SLACK_ID}> " if (att or {}).get("fixer") else ""
        bus.record_outbound(
            ticket_id=ticket["id"], author_type="system",
            body=(f"{owner}RECEIPT: the client was told this, {how}.\n"
                  f"BOT: {identity.name}   TICKET: {ticket['id']}   KIND: {kind}\n"
                  f"WHERE: {where}   WHEN: {sent_at}\n"
                  f"STATUS NOW: {status_now}\n\n{row.get('body') or ''}"),
            # C3: an ESCALATION row, because that is a kind the portal already hides from
            # clients. attachments.receipt marks it as a receipt for everything on this side.
            delivery_status="ready", kind=_a.KIND_ESCALATION,
            meta={"identity": identity.name, "receipt_for": row["id"], "receipt": True,
                  "receipt_kind": kind, "auto_answer": auto, "sent_at": sent_at})
    except Exception:  # noqa: BLE001 - never undo a successful post over a receipt
        pass


def _after_answer_posted(bus, ticket, row, kind, summary, att, identity, log=print):
    """Round 2 (audit of PR #107, MAJOR 6): what happens to the ticket once an answer is
    with the person. An answer that promised a HUMAN follow-up does not close the ticket --
    it is routed to the FIXER with adapter.FOLLOW_UP_MARKER (idempotent: the Slack adapter
    may already have done this at draft time). Every other answer resolves as before."""
    if kind == _a.KIND_ANSWER and (att or {}).get("recipient_kind") not in ("staff", "coach") \
            and _a.promises_human_follow_up(row.get("body") or ""):
        _a.route_follow_up_promise(bus, ticket, ident_name=identity.name,
                                   body=row.get("body") or "",
                                   recipient_kind=(att or {}).get("recipient_kind") or "client",
                                   surface=(att or {}).get("surface") or "",
                                   person=_person_for_card(bus, ticket, identity), log=log)
        return
    _resolve_on_answer(bus, ticket, kind, summary, att)


def _resolve_on_answer(bus, ticket, kind, summary, att=None):
    """V-M4: the ticket closes when the person HAS the message, not when we drafted it.

    Two rows close a ticket: the ANSWER that answered it, and the resolve NOTICE a human
    tapped (MINOR 5, audit 7 -- resolve_and_notify used to stamp the ticket itself, before
    delivery, so a failed post left a ticket claiming to be resolved over a failed row)."""
    if (att or {}).get("fixer") and (att or {}).get("resolve_notice"):
        try:
            fresh = bus.ticket(ticket["id"])
            release_key = (((fresh or {}).get("verification_after") or {}).get("fixer") or {}).get("request_key")
            current_key = _current_fixer_request_key(bus, fresh or ticket)
        except Exception:  # noqa: BLE001 - posting never proves a changed request is fixed
            return
        if not current_key or release_key != current_key or (att or {}).get("request_key") != current_key:
            return
    if kind == _a.KIND_ANSWER and ticket.get("status") == "verification":
        bus.set_ticket(ticket["id"], status="resolved")
        summary["resolved"] += 1
    elif kind == _a.KIND_STATUS and (att or {}).get("resolve_notice"):
        if ticket.get("status") != "resolved":
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
    customer_fix = _customer_fix_reply(ticket, {})
    if customer_fix:
        def refuse_fix(reason):
            why = f"Resolve tap on ticket {ticket_id} did NOT go through: {reason}. " \
                  "The ticket is unchanged."
            log(f"[slack-convo/outbox] {why}")
            try:
                bus.record_outbound(
                    ticket_id=ticket_id, author_type="system", body=why,
                    delivery_status="ready", kind=_a.KIND_ESCALATION,
                    meta={"identity": getattr(identity, "name", ""),
                          "resolve_refused": True})
            except Exception:  # noqa: BLE001 - refusal still stands
                pass
            return False

    # MINOR 5's fix moved the resolved stamp to delivery time, which quietly broke what the
    # status check had been doing double duty for: idempotence. A second tap before the
    # notice posts would have written a SECOND notice. The notice row itself is the record of
    # "this tap already happened", so that is what is checked.
    if _resolve_notice_exists(bus, ticket_id):
        return False
    if not portal_deliverable(ticket) and not ticket.get("slack_channel_id"):
        log(f"[slack-convo/outbox] resolve refused: ticket {ticket_id} has no delivery "
            "surface (no portal thread, no group DM)")
        return False
    # Audit 4, finding 9: this marked the ticket resolved and returned True even when the
    # notice would be HELD by the trust ladder -- Blake taps "Resolved, tell them", the tap
    # reports ok, the ticket reads resolved, and the client is never told. If we cannot
    # deliver the notice, we do not claim the resolution.
    recipient_kind = ticket.get("identity_kind") or "client"
    if not _recipient_armed(identity, recipient_kind):
        # Audit 5, finding 4: refusing silently is its own version of the dead button this
        # whole path exists to fix -- Blake taps "Resolved, tell them" and gets nothing at
        # all. The refusal is written back to the fixer channel, naming the flag to flip.
        flag = ("STAFF_REPLY" if recipient_kind in ("staff", "coach") else "CLIENT_REPLY")
        why = (f"Resolve tap on ticket {ticket_id} did NOT go through: the notice to the "
               f"{recipient_kind} would be held because SLACK_CONVO_"
               f"{getattr(identity, 'name', '?').upper()}_{flag} is off, and marking a "
               f"ticket resolved that the person was never told about is the lie this "
               f"button exists to prevent. Arm that flag, or reply to them directly and "
               f"close it by hand. The ticket is unchanged.")
        log(f"[slack-convo/outbox] {why}")
        try:
            bus.record_outbound(
                ticket_id=ticket_id, author_type="system", body=why,
                delivery_status="ready", kind=_a.KIND_ESCALATION,
                meta={"identity": getattr(identity, "name", ""), "resolve_refused": True})
        except Exception:  # noqa: BLE001 - the refusal itself already stands
            pass
        return False
    if customer_fix:
        proof_meta = {"resolve_notice": True, "pr_url": ticket.get("fix_pr_url")}
        if not _verified_fix_notice(ticket, proof_meta, _a.KIND_STATUS):
            return refuse_fix("customer fix has no current merged, deployed and "
                              "independently verified business postcondition")
        try:
            current_key = _current_fixer_request_key(bus, ticket)
        except Exception:  # noqa: BLE001 - a human tap cannot waive unreadable context
            current_key = None
        release_key = ((ticket.get("verification_after") or {}).get("fixer") or {}).get("request_key")
        if not current_key or release_key != current_key:
            return refuse_fix("customer request changed or could not be verified")
        if not str(ticket.get("slack_channel_id") or "").startswith(("C", "G")):
            return refuse_fix("customer fix has no group conversation for Blake to join")
    # MINOR 4 (audit 7): `surface` was the ticket's SOURCE ("website_tab"), which is not one
    # of the surfaces gate 7 knows, so the notice was posted as a THREAD REPLY inside a DM --
    # a place people do not look. The real surface is on the ticket's own inbound rows.
    surface = _surface_of(bus, ticket_id) or (ticket.get("source") or "")
    bus.record_outbound(
        ticket_id=ticket_id, author_type=getattr(identity, "name", "system"),
        body=RESOLVED_NOTICE, delivery_status="ready", kind=_a.KIND_STATUS,
        # Audit 5, finding 3: this hardcoded "client" while the gate above read the ticket's
        # own identity_kind, so a staff ticket with STAFF_REPLY on and CLIENT_REPLY off
        # passed the gate and then held the row -- the exact lie the gate was added to close.
        meta={"identity": getattr(identity, "name", ""), "recipient_kind": recipient_kind,
              "surface": surface, "resolved_by": approved_by, "resolve_notice": True,
              **({"fixer": True, "request_key": current_key,
                  "pr_url": ticket.get("fix_pr_url")} if customer_fix else {})})
    # MINOR 5 (audit 7): the ticket used to be stamped resolved HERE, before the notice had
    # been delivered -- so a post failure left a ticket permanently asserting it was resolved
    # over a row marked failed. Same rule as an answer (V-M4): the ticket closes when the
    # person HAS the message. _resolve_on_answer closes it when this row posts.
    bus.set_ticket(ticket_id, approved_by=approved_by, approved_via="slack_button",
                   approved_at=datetime.now(timezone.utc).isoformat())
    return True


def _recent(bus, ticket_id, limit=200):
    """The NEWEST rows on a ticket, newest first.

    MINOR 1 (audit 8): bus.messages orders created_at.asc, so a client-side scan of its first
    200 rows reads the OLDEST 200 -- the exact bug count_escalation_cards_since was added to
    fix, reintroduced by hand in three helpers at once. For _resolve_notice_exists the
    failure direction was a DUPLICATE resolve notice to a client."""
    try:
        return bus.recent_messages(ticket_id, limit=limit)
    except AttributeError:
        rows = bus.messages(ticket_id, limit=limit) or []
        return list(reversed(rows))


def _resolve_notice_exists(bus, ticket_id):
    """True once a resolve notice has been written for this ticket, in any delivery state.
    Fails CLOSED (True) on a read failure: a duplicate notice to a client is worse than a
    tap that reports nothing happened."""
    try:
        for m in _recent(bus, ticket_id) or []:
            att = m.get("attachments") or {}
            if m.get("direction") == "outbound" and att.get("resolve_notice"):
                return True
        return False
    except Exception:  # noqa: BLE001
        return True


def _surface_of(bus, ticket_id):
    """The surface this ticket's human actually spoke on, from its own inbound rows."""
    try:
        for m in _recent(bus, ticket_id) or []:
            if m.get("direction") == "inbound":
                s = ((m.get("attachments") or {}).get("surface") or "").strip()
                if s:
                    return s
    except Exception:  # noqa: BLE001
        pass
    return ""
