"""
outreach.py — ticket-initiated outreach: the ONE outbound-first exception in this adapter.

Blake's ruling (2026-09-03, item 3, verbatim): "TICKET-INITIATED OUTREACH. When a ticket
arrives from a non-Slack source (portal form, engage tenant_events, website intake) and
the client resolves to a known Slack user, the owning agent opens a group DM with the
client, me, and the agent. Reuse the group-DM-includes-Blake guard. First message: plain
language, no dashes, restates what they asked and that the agent is on it. The group DM
thread becomes the ticket thread. Never opens a DM for an unresolved identity. Never
opens a DM for a ticket the client did not create. This is the only outbound the agents
may initiate."

This REVERSES D6 ("the bot never calls conversations.open; first contact is structural")
for exactly this one path, on purpose, per this ruling. Everywhere else in this adapter
D6 stands unchanged: the adapter still never calls conversations.open for a Slack-sourced
ticket. See D35 in docs/slack_convo/DECISIONS.md for the exact reasoning and the
conservative choices made where the ruling was ambiguous.

REUSED, NOT REINVENTED: the group-DM-includes-Blake pattern already proven in the portal
(`lasso-ops-portal/src/lib/replies/digest-dm.ts`: `resolveDigestDestination` /
`openEchoGroupDm` / `postAsEchoApp` / `sendDigestDm`). Same shape here: a group DM of
exactly [BLAKE_SLACK_USER_ID, client_slack_user_id], opened with the OWNING agent's own
bot token (never Blake's, never a bare 1:1 client DM) so Slack adds that bot as the third
member automatically -- "conversations.open" with two human user ids on a bot token opens
a 3-party MPIM with the calling bot in it, exactly the digest-dm.ts trick.

REFUSAL PATHS (Blake's own words, restated as hard gates -- both have tests):
  1. never opens a DM for an unresolved identity: `who.kind` must be CLIENT. UNKNOWN
     (including the ambiguous multi-gym case, which identity_gate.py already resolves to
     UNKNOWN) and BOT both refuse. STAFF/COACH also refuse here -- see (2).
  2. never opens a DM for a ticket the client did not create: the ticket's `reporter` must
     be the SAME person who will receive the DM (never staff-filed-on-behalf-of). A
     STAFF/COACH-resolved identity can never be the recipient of an outreach DM by
     definition (a staff member is not "the client"), and a client identity whose email or
     slack_user_id does not match the ticket's own `reporter`/`slack_user_id` refuses too.
"""
from dataclasses import dataclass

from . import identity_gate as _ig
from .adapter import _slack_escape, KIND_OUTREACH_REQUEST

# Same operator id as CLAUDE.md / the portal's digest-dm.ts (Approver Slack id, LASSO
# co-founder Blake Ruff). Included on every outreach group DM, no exceptions.
BLAKE_SLACK_USER_ID = "U06EPUUCL13"

# D34 (conservative choice, logged): the ruling names three non-Slack sources by example
# ("portal form, engage tenant_events, website intake"). Rather than treat "anything that
# is not 'slack_conversation'" as non-Slack (which could silently include a future source
# nobody has reasoned about yet), this is an explicit allowlist. A new non-Slack source
# must be added here deliberately -- fail closed on an unrecognised source, never open a
# DM speculatively.
NON_SLACK_SOURCES = frozenset({"portal_form", "engage_tenant_event", "website_intake"})


class OutreachRefused(Exception):
    """Raised by nothing in this module today (eligible()/initiate() both return a result
    object instead of raising) -- reserved for a future caller that wants a hard failure
    rather than a checked result. Present so a caller who chooses to raise on refusal has
    one canonical exception type to raise, instead of inventing its own per call site."""


@dataclass
class OutreachResult:
    opened: bool
    channel_id: str = ""
    reason: str = ""


def _base_eligible(ticket, who):
    """Every eligibility gate EXCEPT provenance (D42). Pure, no Slack call, no bus write.
    Returns (ok: bool, reason: str). Split out (D45, Blake's ruling 2026-09-04) so a
    ticket that fails only the provenance check can still be offered to a human via
    `request_approval()` instead of being refused outright with no path forward at all.

    `ticket` is a support_tickets row (dict-like: source, reporter, slack_user_id).
    `who` is an identity_gate.Identity already resolved for the would-be recipient.
    """
    # D41 (idempotency, closing a MAJOR from the Frame 1/2 audit wave): stamp_ticket()
    # is the ONLY writer of slack_channel_id on a ticket that started from a non-Slack
    # source, and it only runs after a successful open+post. A ticket that already has
    # one refuses outright -- a retry, a re-fired caller, or a re-queued job can never
    # re-open the DM or re-post the first message a second time.
    if str((ticket or {}).get("slack_channel_id") or "").strip():
        return False, "already_outreached"

    source = str((ticket or {}).get("source") or "")
    if source not in NON_SLACK_SOURCES:
        return False, "not_a_non_slack_source"

    if who is None:
        return False, "identity_unresolved"

    # Refusal (1): never an unresolved identity. UNKNOWN covers both "no match" and the
    # ambiguous multi-gym case (identity_gate.py already folds that into UNKNOWN -- there
    # is no separate AMBIGUOUS kind to check here, by design of that module).
    if who.kind in (_ig.UNKNOWN, _ig.BOT):
        return False, "identity_unresolved"

    # Refusal (2), first half: staff/coach can never be the outreach RECIPIENT. A ticket
    # reported by staff on a client's behalf must never turn into a DM to that staff
    # member framed as if they were the client, and a ticket that resolves its reporter
    # to staff is, by definition, not "the client".
    if who.kind in (_ig.STAFF, _ig.COACH):
        return False, "reporter_is_staff_not_client"

    if who.kind != _ig.CLIENT:
        return False, "identity_unresolved"  # defensive: any future Identity kind refuses

    if not who.slack_user_id:
        return False, "no_slack_user_id_resolved"

    # Refusal (2), second half: the ticket's own recorded reporter must be the SAME person
    # who will receive the DM. `reporter` on a non-Slack-sourced ticket is set by the
    # intake (portal form / engage / website intake) to the reporting client's email or
    # slack_user_id -- never trust a ticket whose reporter cannot be matched to `who`.
    reporter = str((ticket or {}).get("reporter") or "").strip().lower()
    ticket_slack_user_id = str((ticket or {}).get("slack_user_id") or "").strip()
    who_email = (who.email or "").strip().lower()
    matches_reporter = bool(reporter) and (
        reporter == who_email or reporter == who.slack_user_id.strip().lower()
    )
    matches_slack_id = bool(ticket_slack_user_id) and ticket_slack_user_id == who.slack_user_id
    if not (matches_reporter or matches_slack_id):
        return False, "reporter_mismatch"

    return True, "eligible"


def eligible(ticket, who):
    """Full gate for AUTONOMOUS outreach (no human in the loop): every _base_eligible
    check, plus provenance (D42/D45).

    D42 (CRITICAL, Frame 2 audit finding). `reporter` matching `who`'s email/slack_user_id
    only proves the TICKET is internally consistent; it proves nothing about whether the
    person who actually typed the intake form owns that email.

    D45 (Blake's ruling, 2026-09-04, resolving D42's open question): rather than build a
    real authentication mechanism for three intake producers that do not exist yet (a
    speculative, larger project), or block outreach on that work indefinitely, this
    system already has a proven, audited pattern for exactly this shape of problem --
    a human tap in #fixer gates anything that cannot verify itself (D20's hold-card +
    Release button, already used for fixer_request and held replies). `eligible()` stays
    the FAST path for a future strongly-authenticated producer that sets
    `ticket["reporter_verified"] = True` and genuinely does not need a human in the loop.
    Every OTHER ticket that clears `_base_eligible` but not this -- which today is every
    ticket, since nothing sets that flag -- is not a dead end: `request_approval()` below
    offers it to Blake as a tap instead of refusing it outright. The literal boolean
    `True` check (closing-audit fix, not a truthiness check) is unchanged."""
    ok, reason = _base_eligible(ticket, who)
    if not ok:
        return ok, reason
    if (ticket or {}).get("reporter_verified") is not True:
        return False, "reporter_not_verified"
    return True, "eligible"


def eligible_for_approval_request(ticket, who):
    """D45: the gate for OFFERING a human the choice, not for acting autonomously.
    Every _base_eligible check, EXCEPT provenance -- a human's own tap is what asserts
    provenance on this path, so `reporter_verified` is not required here. Still refuses
    everything `_base_eligible` refuses: an unresolved identity, a staff/coach
    "recipient", a reporter/who mismatch, an already-outreached ticket. A ticket this
    permits is a candidate for a #fixer card, never a reason to skip eligible()'s own
    checks anywhere else."""
    return _base_eligible(ticket, who)


def first_message_text(ticket, ident):
    """Plain language, no dashes (Blake's exact words -- 'plain language, no dashes').
    Restates what they asked and that the agent is on it. Bounded so a very long raw
    intake body cannot blow past Slack's message size or the bus's own truncation.

    D41 (CRITICAL, Frame 1/2 audit wave): `raw_text` on a ticket from one of
    NON_SLACK_SOURCES is submitted through an intake with no character-class validation
    (a public portal form, an engage tenant_event, a website intake) -- a LOWER-trust
    origin than a Slack workspace member, not a higher one, and the D27/D32 escaping this
    system already applies to every other client-facing body was missing here entirely.
    `ask` is Slack-escaped before it ever reaches the template, closing the same class of
    live-markup injection (`<!channel>`, `<@U...>`, a masked `<url|label>` link) those two
    decisions already closed on the Slack-sourced path."""
    ask = str((ticket or {}).get("raw_text") or "").strip().replace("\n", " ")
    ask = _slack_escape(ask[:400])
    name = (getattr(ident, "name", "") or "agent").capitalize()
    if ask:
        return (f"Hi, this is {name} from LASSO. I saw you asked about: {ask}. "
                f"I am on it and will follow up here.")
    return (f"Hi, this is {name} from LASSO. I saw your request come in. "
            f"I am on it and will follow up here.")


def initiate(ticket, who, ident, *, open_group_dm, post_first_message, record_outbound,
            stamp_ticket=None, message_text=None, mark_message=None, claim_message=None,
            log=print):
    """The one outbound-first call this whole adapter makes.

    `open_group_dm(user_ids: list[str]) -> {"ok": bool, "channel_id": str}` and
    `post_first_message(channel_id: str, text: str) -> {"ok": bool, "ts": str}` are
    injected Slack calls made with the OWNING agent's own bot token (never Blake's token,
    never a bare 1:1 client DM) -- live wiring resolves that client the same way
    listener_wiring.py resolves one per identity today. `record_outbound` is the same
    bus.record_outbound shape the rest of the adapter uses (ticket_id, author_type, body,
    delivery_status, kind, meta) so this row is indistinguishable in the record from any
    other outbound row, and the row is written BEFORE the post (row-first, the same
    invariant as everywhere else in this adapter, even on this one outbound-first path).

    `stamp_ticket(ticket_id, *, channel_id, thread_ts, slack_user_id, bot_identity,
    identity_kind)` is called AFTER a successful open+post so "the group DM thread
    becomes the ticket thread" (Blake's exact words) is true -- adapter.handle_event's
    existing MPIM matching (match_surface -> find_open_ticket_in_conversation) finds
    this ticket for the client's NEXT message purely by slack_channel_id, with no new
    matching logic needed. Optional so callers that only want the DM opened (e.g. a
    dry run) can omit it; live wiring always passes it. It is also what makes
    `eligible()`'s idempotency gate (D41) work: a ticket without a stamped
    slack_channel_id looks re-eligible, so a caller that wires this in for real must
    always pass it.

    `mark_message(message_id, delivery_status, slack_ts=None)` is the same
    bus.mark_message shape the outbox uses (D41, closing a CRITICAL from the Frame 1/2
    audit wave): this function posts directly rather than going through outbox.py's
    claim/dispatch loop, so it must ALSO close the row's lifecycle itself -- a row left
    sitting in 'ready' after this function already posted it is exactly the same row an
    identity's normal armed outbox loop would later claim and post AGAIN. On a
    successful post the row is marked 'posted' with the real Slack ts immediately; on a
    failed post it is marked 'failed' (the same convention outbox.py itself uses), never
    left in 'ready' for another consumer to find. Optional only so a caller doing a pure
    dry run (no real bus) can omit it; live wiring always passes it.

    Refuses (returns OutreachResult(opened=False, ...)) rather than raising for every
    business reason; only a bus write failure propagates (the caller decides how to log
    a genuine bus outage, same convention as adapter.handle_event)."""
    ok, reason = eligible(ticket, who)
    if not ok:
        log(f"[outreach] refused ticket={(ticket or {}).get('id')} reason={reason}")
        return OutreachResult(opened=False, reason=reason)
    return _send(ticket, who, ident, open_group_dm=open_group_dm,
                post_first_message=post_first_message, record_outbound=record_outbound,
                stamp_ticket=stamp_ticket, message_text=message_text,
                mark_message=mark_message, claim_message=claim_message, log=log)


def _send(ticket, who, ident, *, open_group_dm, post_first_message, record_outbound,
         stamp_ticket=None, message_text=None, message_text_already_escaped=False,
         mark_message=None, claim_message=None, log=print):
    """The actual Slack side of outreach, with NO eligibility check of its own -- every
    caller (`initiate()` after the autonomous `eligible()` gate, `release_approved_outreach()`
    after a human tap) has already decided this send is authorized, by a different route.
    Never call this directly from anywhere that has not itself gated the decision."""
    # D41 (CRITICAL): a caller-supplied `message_text` gets the SAME escaping treatment
    # as the default template, since either can carry untrusted content. Escaped here,
    # not inside first_message_text() twice -- that function already escapes its own
    # `ask` substring internally, so re-escaping its finished output here would
    # double-encode it (a closing-audit finding: the original version of this line
    # unconditionally re-escaped the already-escaped default text, turning "&lt;" into
    # "&amp;lt;" -- cosmetic, never a live-markup regression, but wrong).
    #
    # D45: `message_text_already_escaped` covers a THIRD case neither of the above two
    # anticipated -- release_approved_outreach() re-sends the EXACT body a held row
    # already stored, which request_approval() escaped once when it wrote that row. That
    # stored text must never be escaped again here, for the identical double-encoding
    # reason; it is also not the "default template" path, so first_message_text() must
    # not be called either.
    if message_text_already_escaped:
        text = message_text or ""
    else:
        text = (_slack_escape(message_text) if message_text is not None
               else first_message_text(ticket, ident))

    opened = open_group_dm([BLAKE_SLACK_USER_ID, who.slack_user_id])
    if not opened or not opened.get("ok") or not opened.get("channel_id"):
        log(f"[outreach] conversations.open failed ticket={(ticket or {}).get('id')}")
        return OutreachResult(opened=False, reason="open_failed")
    channel_id = opened["channel_id"]

    # Row-first even on this exceptional outbound-first path: the first message is
    # recorded before it is posted. `kind` matches KIND_ACK's shape (adapter.py) so the
    # outbox's per-kind verification gate treats it exactly like any other
    # non-substantive acknowledgement -- an outreach first message never claims a fact,
    # so it needs no grounding snapshot, same as every ack elsewhere in this system.
    row = record_outbound(
        ticket_id=(ticket or {}).get("id"), author_type=getattr(ident, "name", "system"),
        body=text, delivery_status="ready", kind="ack",
        meta={"identity": getattr(ident, "name", ""), "outreach": True,
              "recipient_kind": who.kind})
    row_id = (row or {}).get("id")

    # D44 (MINOR, Frame 2 closing-audit finding): the row sat in 'ready' for the whole
    # duration of the post call, the exact window an identity's own armed outbox loop
    # could also see it and race this function (its first-contact gate always passes
    # on a fresh outreach ticket, since inbound_count is 0). claim_message is the same
    # bus.claim_message CAS (ready -> posting) the outbox itself uses -- calling it here
    # closes that window to effectively zero. Optional, same pattern as mark_message,
    # for a dry-run caller with no real bus; a caller that DOES pass it and loses the
    # claim (return value falsy) means some other consumer already has this exact row,
    # so this call backs off rather than risk posting the DM a second time.
    if claim_message is not None and row_id is not None:
        try:
            claimed = claim_message(row_id)
        except Exception as e:  # noqa: BLE001 - a claim failure refuses, never guesses
            log(f"[outreach] claim_message failed row={row_id}: {type(e).__name__}")
            return OutreachResult(opened=True, channel_id=channel_id, reason="claim_failed")
        if not claimed:
            log(f"[outreach] row={row_id} already claimed by another consumer, backing off")
            return OutreachResult(opened=True, channel_id=channel_id, reason="lost_claim")

    posted = post_first_message(channel_id, text)
    if not posted or not posted.get("ok"):
        log(f"[outreach] first-message post failed ticket={(ticket or {}).get('id')} "
            f"channel={channel_id}")
        if mark_message is not None and row_id is not None:
            try:
                mark_message(row_id, "failed")
            except Exception as e:  # noqa: BLE001 - the failure is already logged above
                log(f"[outreach] mark_message(failed) itself failed row={row_id}: "
                    f"{type(e).__name__}")
        return OutreachResult(opened=True, channel_id=channel_id, reason="post_failed")

    if mark_message is not None and row_id is not None:
        try:
            mark_message(row_id, "posted", slack_ts=posted.get("ts") or None)
        except Exception as e:  # noqa: BLE001 - the message already sent; never undo it,
                                # but a stuck 'ready' row is exactly D41's finding, so this
                                # is logged loudly rather than swallowed quietly.
            log(f"[outreach] CRITICAL: mark_message(posted) failed row={row_id} "
                f"ticket={(ticket or {}).get('id')} -- row will still show 'ready' and "
                f"may be re-posted by the armed outbox: {type(e).__name__}")

    # "The group DM thread becomes the ticket thread": stamp the ticket with this
    # channel (and the client's own slack_user_id / this ticket's owning bot_identity)
    # so the client's NEXT message in this DM is recognised by adapter.handle_event's
    # existing MPIM path with no special-case code -- find_open_ticket_in_conversation
    # matches on slack_channel_id alone (D11), not thread_ts, so a DM never threads.
    # Best-effort: a stamp failure must not un-send an already-posted first message, so
    # it is logged, never raised.
    if stamp_ticket is not None:
        try:
            stamp_ticket((ticket or {}).get("id"), channel_id=channel_id,
                        thread_ts=posted.get("ts") or "", slack_user_id=who.slack_user_id,
                        bot_identity=getattr(ident, "name", ""), identity_kind=who.kind)
        except Exception as e:  # noqa: BLE001 - the DM already sent; never undo it
            log(f"[outreach] stamp_ticket failed ticket={(ticket or {}).get('id')}: "
                f"{type(e).__name__}")

    return OutreachResult(opened=True, channel_id=channel_id, reason="ok")


# ---- D45: the human-tap path (Blake's ruling, 2026-09-04, resolving D42) -----------------
#
# A ticket that clears every _base_eligible check but has no reporter_verified=True (which
# is every ticket today -- no producer sets that flag) is not refused with no path forward.
# It is offered to Blake as a #fixer card, reusing the SAME hold-notice + Release button
# pattern this system already built and audited for fixer_request/held replies (D20), not
# a new mechanism. His tap IS the provenance a real intake producer can't supply yet.
# KIND_OUTREACH_REQUEST lives in adapter.py alongside the other KIND_ constants
# (imported above) so write_hold_notice's label logic can recognize it too.


@dataclass
class ApprovalRequestResult:
    requested: bool
    held_message_id: str = ""
    reason: str = ""


def request_approval(ticket, who, ident, *, record_outbound, write_hold_notice,
                     message_text=None, log=print):
    """Write the held outreach content (the actual first-message text, kind
    KIND_OUTREACH_REQUEST, delivery_status='held' -- it is never postable by the normal
    outbox loop, since that loop does not know how to open_group_dm; only
    `release_approved_outreach()` below, itself only reachable from a validated Slack
    button tap, can act on it) and a hold-notice card describing it in #fixer.

    `record_outbound` is the same bus.record_outbound shape used everywhere else.
    `write_hold_notice` is adapter.write_hold_notice, called with kind=KIND_OUTREACH_REQUEST
    so the card's own release button (RELEASE_ACTION_ID) can route a tap here rather than
    to outbox.release_held (which does not know how to open a DM and would refuse this
    kind outright -- see outbox.py's own allowed-kinds check)."""
    ok, reason = eligible_for_approval_request(ticket, who)
    if not ok:
        log(f"[outreach] approval request refused ticket={(ticket or {}).get('id')} "
            f"reason={reason}")
        return ApprovalRequestResult(requested=False, reason=reason)

    text = (_slack_escape(message_text) if message_text is not None
           else first_message_text(ticket, ident))
    tid = (ticket or {}).get("id")

    held = record_outbound(
        ticket_id=tid, author_type=getattr(ident, "name", "system"), body=text,
        delivery_status="held", kind=KIND_OUTREACH_REQUEST,
        meta={"identity": getattr(ident, "name", ""), "outreach": True,
              "recipient_kind": who.kind, "slack_user_id": who.slack_user_id})
    held_id = (held or {}).get("id")
    if held_id is None:
        log(f"[outreach] approval request: held row write returned no id ticket={tid}")
        return ApprovalRequestResult(requested=False, reason="write_failed")

    write_hold_notice(
        ident_name=getattr(ident, "name", ""), tid=tid, recipient_kind=who.kind,
        user=who.slack_user_id or "", account_key=who.account_key or "",
        kind=KIND_OUTREACH_REQUEST, body=text, held_message_id=held_id,
        surface="outreach",
        why="proposed outreach to a client from a non-Slack ticket, no verified "
            "provenance yet -- tap to send")
    return ApprovalRequestResult(requested=True, held_message_id=held_id, reason="held")


def release_approved_outreach(message_id, ticket, who, ident, *, get_held_message,
                              open_group_dm, post_first_message, record_outbound,
                              stamp_ticket=None, mark_message=None, claim_message=None,
                              log=print):
    """The tap handler: validates a held KIND_OUTREACH_REQUEST row belongs to THIS ticket
    and THIS identity before doing anything Blake's tap did not actually authorize, then
    calls `_send()` -- the tap itself is the provenance _base_eligible's stricter sibling,
    `eligible()`, could not get from the ticket alone. Refuses (never raises) for every
    validation failure; a caller wires this to the SAME action-id dispatch RELEASE_ACTION_ID
    already uses for other held kinds, keyed on `attachments.held_kind`.

    `get_held_message(message_id) -> row | None` is bus.message's shape."""
    row = get_held_message(message_id)
    if not row or row.get("delivery_status") != "held":
        log(f"[outreach] release refused: row {message_id} not held")
        return OutreachResult(opened=False, reason="not_held")
    att = row.get("attachments") or {}
    if att.get("kind") != KIND_OUTREACH_REQUEST:
        log(f"[outreach] release refused: row {message_id} kind {att.get('kind')!r} "
            f"is not an outreach request")
        return OutreachResult(opened=False, reason="wrong_kind")
    if row.get("ticket_id") != (ticket or {}).get("id"):
        log(f"[outreach] release refused: row {message_id} belongs to a different ticket")
        return OutreachResult(opened=False, reason="ticket_mismatch")
    if (att.get("identity") or "") != getattr(ident, "name", ""):
        log(f"[outreach] release refused: row {message_id} belongs to identity "
            f"{att.get('identity')!r} not {getattr(ident, 'name', '')!r}")
        return OutreachResult(opened=False, reason="identity_mismatch")
    # Re-run the base gates (NOT provenance -- the tap replaces that) at release time too,
    # not just at request time: the ticket could have been outreached by a second path,
    # or the identity resolution could have changed, in the window between the card
    # posting and Blake's tap.
    ok, reason = eligible_for_approval_request(ticket, who)
    if not ok:
        log(f"[outreach] release refused at tap time: {reason}")
        return OutreachResult(opened=False, reason=reason)

    result = _send(ticket, who, ident, open_group_dm=open_group_dm,
                  post_first_message=post_first_message, record_outbound=record_outbound,
                  stamp_ticket=stamp_ticket, message_text=row.get("body"),
                  message_text_already_escaped=True,
                  mark_message=mark_message, claim_message=claim_message, log=log)

    # D45 closing-audit finding: _send() always writes a NEW row for the actual DM (the
    # held row is never itself postable, see request_approval's docstring), so without
    # this the held KIND_OUTREACH_REQUEST row sat at delivery_status='held' forever even
    # after a successful send -- not a duplicate-send risk (the ticket-level
    # already_outreached check still covers that), but an orphaned row nothing ever
    # closes. Best-effort, same as every other mark_message call in this module: the
    # real DM is already sent, a bookkeeping-close failure here must never look like the
    # send itself failed.
    if result.opened and mark_message is not None:
        try:
            mark_message(message_id, "posted")
        except Exception as e:  # noqa: BLE001 - the send already succeeded
            log(f"[outreach] held row {message_id} close-out failed (send itself "
                f"succeeded): {type(e).__name__}")

    return result
