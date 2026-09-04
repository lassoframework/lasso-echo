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
from .adapter import _slack_escape

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


def eligible(ticket, who):
    """Pure gate check, no Slack call, no bus write. Returns (ok: bool, reason: str).

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

    # D42 (CRITICAL, Frame 2 audit finding, ruling still open -- see DECISIONS.md).
    # `reporter` matching `who`'s email/slack_user_id only proves the TICKET is
    # internally consistent; it proves nothing about whether the person who actually
    # typed the intake form owns that email. None of NON_SLACK_SOURCES has a producer in
    # this repo yet, so none of them can set this today -- which is exactly the point:
    # this gate fails closed until a real producer positively asserts provenance
    # (a confirmed magic-link click, an authenticated portal session, etc.) by setting
    # `ticket["reporter_verified"] = True`. A ticket without that flag refuses here,
    # even if every other check above passed.
    if not (ticket or {}).get("reporter_verified"):
        return False, "reporter_not_verified"

    return True, "eligible"


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
            stamp_ticket=None, message_text=None, mark_message=None, log=print):
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

    # D41 (CRITICAL): escaped unconditionally here too, not only inside
    # first_message_text() -- a caller-supplied `message_text` must get the same
    # treatment as the default template, since either can carry untrusted content.
    text = _slack_escape(message_text if message_text is not None
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
