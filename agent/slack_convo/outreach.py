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
from datetime import datetime, timezone
import uuid

from .. import config
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
#
# D46: "website_tab" added deliberately -- this is the REAL `source` value the portal's
# /api/gyms/[gymId]/support route actually stamps (support/route.ts), not the "website
# intake" name D34's spec paraphrase used. Kept "website_intake" too rather than rename
# it, in case anything already relies on that name; a real producer stamps one or the
# other, never both.
NON_SLACK_SOURCES = frozenset({"portal_form", "engage_tenant_event", "website_intake",
                              "website_tab"})


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
    # C2 (2026-09-05 audit 2, CRITICAL): `opened` means conversations.open succeeded --
    # nothing more. It is True on claim_failed, lost_claim AND post_failed, and every caller
    # was reading it as "the client was told". A failed chat.postMessage therefore resolved
    # the ticket and wrote a receipt asserting "the client was told this, SENT AUTOMATICALLY
    # (no tap)" over a row whose own delivery_status was 'failed'. `delivered` is the fact
    # callers actually need, and it is True only after the post came back ok.
    delivered: bool = False
    # True only when the posted completion row was confirmed by the bus response.
    completion_posted: bool = False
    posted_ts: str = ""
    ticket_stamped: bool = False
    notice_id: str = ""
    attempt_token: str = ""


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
            completion=False, ticket_lookup=None, reconcile_uncertain=None,
            current_notice_bus=None, readback=None, member_check=None, log=print):
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

    `completion` (portal migration 0381 compatibility): True only when the caller will
    RESOLVE the ticket on a successful delivery -- today Echo's direct grounded-answer
    lane and the verified-fix notify lane in echo_ticket_worker. The completion stamp
    (attachments.resolve_notice=True, plus request_version when the ticket carries a
    trustworthy one) is written ONLY after the post comes back ok, merged into the row
    by the same mark_message('posted') call that closes the row's lifecycle, so a held,
    failed, claimed-away or never-posted row can never read as a completion to the
    portal's guard. Default False keeps every existing caller and the held/approval
    lanes untouched.

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
                mark_message=mark_message, claim_message=claim_message,
                completion=completion, ticket_lookup=ticket_lookup,
                reconcile_uncertain=reconcile_uncertain,
                current_notice_bus=current_notice_bus, readback=readback,
                member_check=member_check, log=log)


def _send(ticket, who, ident, *, open_group_dm, post_first_message, record_outbound,
         stamp_ticket=None, message_text=None, message_text_already_escaped=False,
         mark_message=None, claim_message=None, completion=False,
         ticket_lookup=None, reconcile_uncertain=None, current_notice_bus=None,
         readback=None, member_check=None, log=print):
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

    current_notice = bool(completion and current_notice_bus is not None
                          and ticket.get("product") == "echo"
                          and ticket.get("source") == "website_tab")
    if current_notice and not config.slack_convo_echo_current_notice_enabled():
        return OutreachResult(opened=False, reason="current_notice_disabled")

    opened = open_group_dm([BLAKE_SLACK_USER_ID, who.slack_user_id])
    if not opened or not opened.get("ok") or not opened.get("channel_id"):
        log(f"[outreach] conversations.open failed ticket={(ticket or {}).get('id')}")
        return OutreachResult(opened=False, reason="open_failed")
    channel_id = opened["channel_id"]

    notice_id = ""
    attempt_token = ""
    if current_notice:
        if (current_notice_bus is None or not callable(readback)
                or not callable(member_check) or not ident.bot_user_id()
                or not callable(claim_message)):
            return OutreachResult(opened=True, channel_id=channel_id,
                                  reason="current_notice_preflight_unavailable")
        if not member_check(channel_id, BLAKE_SLACK_USER_ID):
            return OutreachResult(opened=True, channel_id=channel_id,
                                  reason="blake_membership_unverified")
        notice_id = str(uuid.uuid4())
        attempt_token = current_notice_bus.begin_current_notice(
            ticket, notice_id, unrouted=True)
        if not attempt_token:
            return OutreachResult(opened=True, channel_id=channel_id,
                                  reason="current_notice_reservation_refused")

    # Row-first even on this exceptional outbound-first path. A completion uses
    # kind='status' because migration 0381 recognises only posted status notices;
    # all other first-contact messages retain the existing ack kind.
    outbound_args = {
        "ticket_id": (ticket or {}).get("id"),
        "author_type": getattr(ident, "name", "system"),
        "body": text, "delivery_status": "ready",
        "kind": "status" if completion else "ack",
        "meta": {"identity": getattr(ident, "name", ""), "outreach": True,
                 "recipient_kind": who.kind},
    }
    if current_notice:
        outbound_args["message_id"] = notice_id
        outbound_args["meta"].update({
            "fixer": True, "resolve_notice": True,
            "fixer_current_attempt_token": attempt_token,
            "fixer_route_pending": True,
            "surface": "portal_ticket_bridge",
            "request_version": ticket["request_version"],
            "delivery_expected_slack_channel_id": None,
            "delivery_expected_slack_thread_ts": None,
        })
    version = (ticket or {}).get("request_version")
    if type(version) is int and version >= 0:
        outbound_args["expected_request_version"] = version
        outbound_args["meta"].update({
            "delivery_identity_fence": True,
            "delivery_expected_product": ticket.get("product"),
            "delivery_expected_client_id": ticket.get("client_id"),
            "delivery_expected_status": ticket.get("status"),
            "delivery_expected_classification": ticket.get("classification"),
            "delivery_expected_bot_identity": getattr(ident, "name", ""),
            "delivery_expected_slack_user_id": who.slack_user_id,
        })
    row = record_outbound(**outbound_args)
    row_id = (row or {}).get("id")

    def cancel_current_notice(reason, *, claimed=False):
        if not current_notice or not row_id:
            return
        try:
            suppress = (current_notice_bus.suppress_unattempted_current_notice
                        if claimed else current_notice_bus.suppress_unclaimed_current_notice)
            canceled = suppress(row_id, reason)
            if not canceled or canceled.get("delivery_status") != "suppressed":
                return  # a competing claimant may own the posting row
            current_notice_bus.record_outbound(
                ticket_id=ticket["id"], author_type="system",
                body=(f"FIXER first-contact notice {row_id} on ticket "
                      f"{ticket['id']} was canceled before Slack delivery. "
                      "Review the ticket before opening another notice."),
                delivery_status="ready", kind="escalation",
                meta={"identity": getattr(ident, "name", ""),
                      "suppressed_message_id": row_id})
        except Exception as exc:  # noqa: BLE001 - no customer POST occurred
            log(f"[outreach] current notice cancellation/alert failed "
                f"row={row_id}: {type(exc).__name__}")

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
            cancel_current_notice("FIXER first-contact claim failed before Slack")
            return OutreachResult(opened=True, channel_id=channel_id, reason="claim_failed")
        if not claimed:
            log(f"[outreach] row={row_id} already claimed by another consumer, backing off")
            cancel_current_notice("FIXER first-contact claim was not acquired")
            return OutreachResult(opened=True, channel_id=channel_id, reason="lost_claim")

    if ticket_lookup is not None:
        try:
            fresh = ticket_lookup(ticket["id"])
        except Exception:  # noqa: BLE001 - no readable current identity, no post
            fresh = None
        fields = ("request_version", "product", "client_id", "status", "classification",
                  "bot_identity", "slack_user_id")
        if (not isinstance(fresh, dict)
                or any(fresh.get(field) != ticket.get(field) for field in fields)
                or fresh.get("bot_identity") != getattr(ident, "name", "")
                or fresh.get("slack_user_id") != who.slack_user_id
                or current_notice and (fresh.get("slack_channel_id") is not None
                                       or fresh.get("slack_thread_ts") is not None)
                or fresh.get("escalated") is True or fresh.get("hold_tier") is not None):
            if current_notice:
                cancel_current_notice("FIXER delivery identity changed before Slack",
                                      claimed=True)
            elif mark_message is not None and row_id is not None:
                try:
                    mark_message(row_id, "suppressed",
                                 meta_update={"suppressed_why": "delivery identity changed"})
                except Exception:  # noqa: BLE001 - no Slack post occurred
                    pass
            return OutreachResult(opened=True, channel_id=channel_id,
                                  reason="delivery_identity_changed")

    if current_notice:
        # The same designated row carries the first and only customer message.
        # Persist intent before Slack, read back the returned exact timestamp,
        # then let 0384 bind the route while the row is still posting.
        from . import outbox as _ob

        def uncertain(reason):
            log(f"[outreach] current notice uncertain row={row_id}: {reason}")
            try:
                current_notice_bus.hold_uncertain_fixer_delivery(row_id, reason)
            except Exception:  # noqa: BLE001 - never retry an uncertain Slack post
                pass
            return OutreachResult(opened=True, channel_id=channel_id,
                                  reason="current_notice_uncertain",
                                  notice_id=notice_id, attempt_token=attempt_token)

        intent = {
            "channel": channel_id, "thread_ts": None, "body": text,
            "sender": ident.bot_user_id(),
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "request_key": None, "request_version": ticket["request_version"],
        }
        try:
            claimed_row = current_notice_bus.message(row_id)
            prepared = current_notice_bus.prepare_fixer_delivery(
                row_id, intent,
                expected_attachments=dict((claimed_row or {}).get("attachments") or {}))
            if not prepared or prepared.get("delivery_status") != "posting":
                return uncertain("delivery intent was not persisted")
            intent["not_before"] = datetime.now(timezone.utc).isoformat()
            prepared = current_notice_bus.prepare_fixer_delivery(
                row_id, intent,
                expected_attachments=dict(prepared.get("attachments") or {}))
            if (not prepared or prepared.get("delivery_status") != "posting"
                    or (prepared.get("attachments") or {}).get(
                        "fixer_slack_delivery_intent") != intent):
                return uncertain("readback boundary was not persisted")
            fresh = ticket_lookup(ticket["id"])
            if (not isinstance(fresh, dict) or any(
                    fresh.get(field) != ticket.get(field) for field in
                    ("request_version", "status", "classification", "product",
                     "client_id", "bot_identity", "slack_user_id"))
                    or fresh.get("slack_channel_id") is not None
                    or fresh.get("slack_thread_ts") is not None
                    or fresh.get("escalated") or fresh.get("hold_tier") is not None
                    or not member_check(channel_id, BLAKE_SLACK_USER_ID)):
                return uncertain("request or destination changed before Slack")
            posted = post_first_message(channel_id, text)
            ts = posted.get("ts") if isinstance(posted, dict) and posted.get("ok") else None
            if not isinstance(ts, str) or not ts:
                return uncertain("Slack post returned no confirmed timestamp")
            stamped = current_notice_bus.record_fixer_delivery_timestamp(row_id, intent, ts)
            if not stamped:
                # A stale sweep may have moved posting to held while Slack was
                # answering. Keep the returned exact timestamp on that same
                # reserved row so readback can reconcile without another POST.
                held = current_notice_bus.hold_uncertain_fixer_delivery(
                    row_id, "Slack returned a timestamp after claim ownership changed")
                if held and held.get("delivery_status") == "held":
                    stamped = current_notice_bus.record_fixer_delivery_timestamp(
                        row_id, intent, ts)
            if not stamped or stamped.get("slack_ts") != ts:
                return uncertain("Slack timestamp was not persisted")
            proof, reason = _ob._readback_fixer_message(readback, intent, ts=ts)
            if not proof:
                return uncertain(reason)
            verified = current_notice_bus.transition_fixer_delivery(
                row_id, "posting", slack_ts=ts, meta_update=proof,
                expected_intent=intent, expected_ts=ts)
            if not verified:
                held = current_notice_bus.message(row_id)
                if (held or {}).get("delivery_status") == "held":
                    verified = current_notice_bus.record_held_current_notice_readback(
                        row_id, proof, expected_intent=intent, expected_ts=ts)
            if not verified or (verified.get("attachments") or {}).get(
                    "delivery_readback_verified") is not True:
                return uncertain("exact Slack readback could not be persisted")
            if not current_notice_bus.bind_current_notice_route(
                    ticket["id"], ticket["request_version"], notice_id,
                    attempt_token, channel_id, ts):
                return uncertain("posted route could not be bound")
            finished = current_notice_bus.transition_fixer_delivery(
                row_id, "posted", slack_ts=ts,
                expected_intent=intent, expected_ts=ts)
            if not finished or finished.get("delivery_status") != "posted":
                return uncertain("verified notice could not be marked posted")
            return OutreachResult(
                opened=True, channel_id=channel_id, reason="ok", delivered=True,
                completion_posted=True, posted_ts=ts, ticket_stamped=True,
                notice_id=notice_id, attempt_token=attempt_token)
        except Exception as exc:  # noqa: BLE001 - a timeout may follow Slack success
            return uncertain(f"{type(exc).__name__}: {exc}")

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
        return OutreachResult(opened=True, channel_id=channel_id, reason="post_failed",
                              delivered=False)

    completion_posted = False
    if mark_message is not None and row_id is not None:
        # Portal migration 0381 (not yet applied at time of writing) recognises a ticket
        # as complete only on a CURRENT-REQUEST posted answer or a status row with
        # resolve_notice. Echo's direct support deliveries used to land as a bare
        # kind='ack' followed by a status flip, which that guard cannot see. When the
        # caller asserts completion (it resolves the ticket on delivery), stamp the row
        # HERE -- after the post succeeded, in the same mark that closes the lifecycle --
        # never at row-write time, so a row that failed, lost its claim or was never
        # posted cannot claim completion. resolve_notice is explicit; request_version is
        # added only from a real non-negative int on the ticket (never fabricated), so
        # the portal's current-request check can bind the completion to this exact
        # request instead of a stale one.
        meta_update = None
        if completion:
            meta_update = {"resolve_notice": True}
            version = (ticket or {}).get("request_version")
            if isinstance(version, int) and not isinstance(version, bool) and version >= 0:
                meta_update["request_version"] = version
            else:
                log(f"[outreach] completion stamp row={row_id} "
                    f"ticket={(ticket or {}).get('id')}: no trustworthy request_version "
                    f"on the ticket (got {version!r}); resolve_notice only")
        try:
            if meta_update:
                marked = mark_message(row_id, "posted", slack_ts=posted.get("ts") or None,
                                      meta_update=meta_update)
            else:
                marked = mark_message(row_id, "posted", slack_ts=posted.get("ts") or None)
            attachments = (marked or {}).get("attachments") if isinstance(marked, dict) else None
            completion_posted = bool(
                completion and isinstance(marked, dict)
                and marked.get("id") == row_id
                and marked.get("delivery_status") == "posted"
                and isinstance(attachments, dict)
                and attachments.get("kind") == "status"
                and attachments.get("resolve_notice") is True
                and marked.get("delivery_request_version") == ticket.get("request_version"))
        except Exception as e:  # noqa: BLE001 - the message already sent; never undo it,
                                # so quarantine uncertain delivery for manual reconciliation.
            log(f"[outreach] CRITICAL: mark_message(posted) failed row={row_id} "
                f"ticket={(ticket or {}).get('id')}: {type(e).__name__}")
            if callable(reconcile_uncertain):
                try:
                    observed = reconcile_uncertain(row_id)
                    att = (observed or {}).get("attachments") or {}
                    completion_posted = bool(
                        completion and observed and observed.get("id") == row_id
                        and observed.get("delivery_status") == "posted"
                        and observed.get("delivery_request_version") == ticket.get("request_version")
                        and att.get("kind") == "status"
                        and att.get("resolve_notice") is True)
                except Exception:  # noqa: BLE001 - stale-claim recovery retries quarantine
                    pass

    # "The group DM thread becomes the ticket thread": stamp the ticket with this
    # channel (and the client's own slack_user_id / this ticket's owning bot_identity)
    # so the client's NEXT message in this DM is recognised by adapter.handle_event's
    # existing MPIM path with no special-case code -- find_open_ticket_in_conversation
    # matches on slack_channel_id alone (D11), not thread_ts, so a DM never threads.
    # Best-effort: a stamp failure must not un-send an already-posted first message, so
    # it is logged, never raised.
    ticket_stamped = False
    if stamp_ticket is not None:
        try:
            stamp_args = {
                "channel_id": channel_id, "thread_ts": posted.get("ts") or "",
                "slack_user_id": who.slack_user_id,
                "bot_identity": getattr(ident, "name", ""), "identity_kind": who.kind,
            }
            if ticket_lookup is not None:
                stamp_args["expected_ticket"] = ticket
            stamped = stamp_ticket((ticket or {}).get("id"), **stamp_args)
            ticket_stamped = (isinstance(stamped, dict)
                              and stamped.get("id") == ticket.get("id")
                              and stamped.get("request_version") == ticket.get("request_version")
                              and stamped.get("slack_channel_id") == channel_id
                              and stamped.get("slack_user_id") == who.slack_user_id
                              and stamped.get("bot_identity") == getattr(ident, "name", ""))
        except Exception as e:  # noqa: BLE001 - the DM already sent; never undo it
            log(f"[outreach] stamp_ticket failed ticket={(ticket or {}).get('id')}: "
                f"{type(e).__name__}")

    return OutreachResult(opened=True, channel_id=channel_id, reason="ok", delivered=True,
                          completion_posted=completion_posted,
                          posted_ts=posted.get("ts") or "",
                          ticket_stamped=ticket_stamped)


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
                              completion=False, log=print):
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
                  mark_message=mark_message, claim_message=claim_message,
                  completion=completion, log=log)

    # D45 closing-audit finding: _send() always writes a NEW row for the actual DM (the
    # held row is never itself postable, see request_approval's docstring), so without
    # this the held KIND_OUTREACH_REQUEST row sat at delivery_status='held' forever even
    # after a successful send -- not a duplicate-send risk (the ticket-level
    # already_outreached check still covers that), but an orphaned row nothing ever
    # closes. Best-effort, same as every other mark_message call in this module: the
    # real DM is already sent, a bookkeeping-close failure here must never look like the
    # send itself failed.
    # Finding 3 (2026-09-05 audit 3): this read `opened`, which is True even when the post
    # FAILED -- so a held outreach row was marked 'posted' with nothing sent. In the portal
    # that row's kind (outreach_request) is not on migration 0310's hidden list, so a client
    # could read a "message" they were never sent. `delivered` is the only correct predicate
    # for "the client has this".
    if getattr(result, "delivered", False) and mark_message is not None:
        try:
            mark_message(message_id, "posted")
        except Exception as e:  # noqa: BLE001 - the send already succeeded
            log(f"[outreach] held row {message_id} close-out failed (send itself "
                f"succeeded): {type(e).__name__}")
    elif not getattr(result, "delivered", False):
        # Nothing reached the client, so the row must never read as delivered -- but WHICH
        # non-delivery matters (audit 4, finding 6). Only a definitive send failure burns the
        # row; a transient one (the DM would not open, another consumer held the claim) LEAVES
        # IT HELD so Blake's Release card still works on the next tap. Burning it on a
        # transient fault re-created the exact silent no-op card that listener_wiring's own
        # dispatch fix exists to prevent.
        # Audit 5, finding 5: "leave it held so a retap works" is right for open_failed and
        # WRONG for claim_failed / lost_claim. On those two, _send has ALREADY written a
        # 'ready' outbound row that another consumer owns and will post -- so a retap writes
        # a second row and the client gets the same DM twice. Three outcomes, three states:
        #   open_failed  -> held      (nothing was written; a retap is the correct retry)
        #   post_failed  -> failed    (definitive: the send was attempted and refused)
        #   claim_*      -> suppressed(another consumer has it; a retap must NOT duplicate)
        state = {"post_failed": "failed",
                 "claim_failed": "suppressed",
                 "lost_claim": "suppressed"}.get(result.reason)
        if state and mark_message is not None:
            try:
                mark_message(message_id, state,
                             meta_update={"outreach_reason": result.reason})
            except Exception as e:  # noqa: BLE001
                log(f"[outreach] held row {message_id} state mark failed: "
                    f"{type(e).__name__}")
        log(f"[outreach] release did NOT deliver row={message_id} reason={result.reason} "
            f"(row left {state or 'held for a retap'})")

    return result
