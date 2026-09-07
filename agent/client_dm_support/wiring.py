"""
wiring.py — the REAL producers this capability depends on, named.

D68's cheapest general check: "for every capability, name the PRODUCER of the value it
gates on, and assert that producer exists — statically, in a test, with no test double
and no live service." Five separate "built but not wired" bugs in this system were all
the same shape: the capability existed, config said it was on, and the inert state was
byte-for-byte identical to a healthy one.

BOTH SINKS WERE THAT BUG FIRST TIME, and both are fixed here against the outbox's real
contract rather than an assumed one:

  * THE IDENTITY STAMP. outbox._dispatch_one suppresses any row whose
    attachments carry no `identity` (agent/slack_convo/outbox.py:314,325-327 —
    "row carries no identity stamp"). The first version of these sinks wrote
    meta={lane, template_id, ...} with no identity, so every reply was silently
    suppressed and nothing was ever posted. The identity now comes from the ticket's
    own `bot_identity`, and a ticket without one is refused rather than written.
  * THE ESCALATION'S DELIVERY STATUS. outbox.run_once reads only bus.outbox("ready")
    (outbox.py:271). The first version wrote escalations as "held", so the SAFETY path
    — the one that carries every refusal to a human — landed in the database and
    surfaced to nobody. Escalations are now "ready", which is what
    adapter.delivery_for returns for every INTERNAL kind (adapter.py:569-570) and what
    every other escalation writer in the system uses. INTERNAL_KINDS go to the fixer
    channel, never the client's thread, so "ready" here does not mean "post to the
    client".

THIS CAPABILITY INVENTS NO CHANNEL AND NO TABLE. It writes support_messages rows and
the EXISTING outbox (the Wrangler outbound role) delivers them. This module never
calls chat.postMessage: the adapter's architectural rule ("the adapter never posts; it
writes the row, Wrangler posts") holds here too.

WHY KIND_STATUS AND NOT KIND_ANSWER. KIND_ANSWER is the #fixer bus's own auto-answer
lane, whose gate is locked by D67 and is explicitly Blake's separate decision. This
capability must not ride it, re-enable it, or depend on `verification_after` semantics
that belong to it. KIND_STATUS is plain-language status on the ticket, is not gated on
`verification_after`, and is already a receipt kind.

ARMING TAKES TWO FLAGS, NOT ONE. An earlier version of this docstring claimed the
opposite -- that writing the row 'ready' bypassed the identity's client-reply flag, and
that arming AGENT_CLIENT_DM_AUTOFIX alone put client-visible messages on the wire. Both
halves were FALSE, and the tests that "closed" the delivery bug asserted it by
string-matching outbox.py's source instead of executing it, which is exactly why a third
contract on that same function went unnoticed (D68: a test shaped like the code, not
like the rule).

The truth, from outbox._dispatch_one: KIND_STATUS is a CONVERSATIONAL kind, so the row
is checked AGAIN at POST time by _recipient_armed(identity, recipient_kind) ->
slack_convo_client_reply_armed(identity) (outbox.py:429). delivery_status='ready' is a
claim about the row, not a bypass of a post-time gate. With that flag off the reply is
HELD with held_why='flag off at post time' and a hold card goes to a human -- it fails
safe, but it does not reach the client.

So a full arming is: AGENT_CLIENT_DM_AUTOFIX (this lane runs at all) AND the identity's
SLACK_CONVO_<IDENTITY>_CLIENT_REPLY (client-visible messages may leave). This lane reads
neither flag itself and does not touch the D67-locked AUTO_ANSWER gate, which governs
KIND_ANSWER only. tests/test_client_dm_flow.py drives a written row through the REAL
_dispatch_one in both flag states rather than grepping its source.

Nothing under agent/slack_convo/ is modified by this file; it is called, not changed.
"""
from __future__ import annotations

REPLY_META_LANE = "client_dm_autofix"

# The delivery status each kind must be written with, as named constants rather than
# inline literals, because both of them are contracts of the EXISTING outbox and
# getting either wrong silently drops the row:
#   * outbox.run_once reads ONLY bus.outbox("ready") (outbox.py:271). An escalation
#     written "held" lands in the database and surfaces to nobody -- and that is the
#     safety path, the one carrying every refusal this lane makes to a human.
#   * "ready" for an INTERNAL kind does NOT mean "post to the client": escalations go
#     to the fixer channel. It is what adapter.delivery_for returns for every internal
#     kind (adapter.py:569-570).
ESCALATION_DELIVERY_STATUS = "ready"
REPLY_DELIVERY_STATUS = "ready"


class WiringError(RuntimeError):
    """A row could not be written in a shape the existing outbox will actually
    deliver. Raised rather than writing a row that would be silently suppressed."""


def _kinds():
    from ..slack_convo import adapter as _a
    return _a.KIND_STATUS, _a.KIND_ESCALATION


def _identity_of(ticket):
    """The identity stamp the outbox requires, from the ticket's own bot_identity."""
    ident = str((ticket or {}).get("bot_identity") or "").strip()
    if not ident:
        raise WiringError(
            "ticket carries no bot_identity, so any row written for it would be "
            "suppressed by the outbox as unattributed; refusing to write it"
        )
    return ident


def _base_meta(ticket, surface=None, recipient_kind="client"):
    # The REAL surface, not a hardcoded "mpim". The consumer reads it off the inbound
    # message and used to discard it, so every row and every hold card recorded a
    # surface that might be false (a 1:1 DM is "im").
    surface = surface or str(ticket.get("_surface") or "mpim")
    return {
        "identity": _identity_of(ticket),      # REQUIRED by outbox._dispatch_one
        "surface": surface,
        "recipient_kind": recipient_kind,
        "lane": REPLY_META_LANE,
    }


def bus_reply_sink(bus):
    """callable(ticket, decision) -> writes the grounded reply as a 'ready' row that
    the existing outbox posts into the client's own thread."""
    kind_status, _ = _kinds()

    def _post(ticket, decision):
        meta = _base_meta(ticket)
        meta.update({
            "template_id": decision.template_id,
            "fact_keys": list(decision.audit.get("fact_keys", [])),
            "verification": decision.audit.get("verification", ""),
        })
        return bus.record_outbound(
            ticket_id=ticket.get("id"),
            author_type=meta["identity"],
            body=decision.reply_text,
            delivery_status=REPLY_DELIVERY_STATUS,
            kind=kind_status,
            meta=meta,
        )

    return _post


# The client's own words are UNTRUSTED TEXT on a card a human reads and acts on, so
# they are Slack-escaped and bounded before they are fenced -- the adapter's RT-M1/RA-M2
# rule, applied here for the same reason. Escaping & < > disarms every piece of Slack
# markup in one pass (all of it needs < and >), including <!channel> and <@U...>.
CARD_TEXT_MAX = 1200


def _fenced_client_text(text):
    body = str(text or "").strip()
    if not body:
        return "(no client text on this ticket)"
    if len(body) > CARD_TEXT_MAX:
        body = body[:CARD_TEXT_MAX] + " ...[truncated]"
    return body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bus_escalation_sink(bus):
    """callable(ticket, decision) -> writes a 'ready' ESCALATION row. Internal kinds go
    to the fixer channel, not the client's thread, so a human actually sees every
    refusal this lane makes.

    The card carries the CLIENT'S OWN MESSAGE. Without it the reader was asked to close
    a loop on a request they could not read: outbox.escalation_blocks renders only
    row["body"] plus a "Resolved, tell them" button, so whatever this sink omits is
    simply absent from the human's screen."""
    _, kind_escalation = _kinds()

    def _hold(ticket, decision):
        trigger = decision.foundation_trigger or "not_grounded"
        meta = _base_meta(ticket, recipient_kind="staff")
        meta["foundation_trigger"] = trigger
        body = (
            f"[{REPLY_META_LANE}] held for a human.\n"
            f"gym: {decision.gym_key or 'unresolved'}\n"
            f"slack user: {ticket.get('slack_user_id') or '?'}\n"
            f"diagnostic: {decision.diagnostic_id or 'none matched'}\n"
            f"trigger: {trigger}\n"
            f"reason: {decision.reason}\n"
            f"--- what they wrote (untrusted, escaped) ---\n"
            f"{_fenced_client_text(getattr(decision, 'client_text', ''))}"
        )
        return bus.record_outbound(
            ticket_id=ticket.get("id"),
            author_type=meta["identity"],
            body=body,
            delivery_status=ESCALATION_DELIVERY_STATUS,
            kind=kind_escalation,
            meta=meta,
        )

    return _hold


def bus_answered_notice_sink(bus):
    """callable(ticket, decision) -> an INTERNAL card, written for EVERY auto-reply.

    IT SAYS "QUEUED", NOT "REPLIED", because that is what is true when it is written.
    Arming takes two flags: with AGENT_CLIENT_DM_AUTOFIX on and the identity's
    SLACK_CONVO_<IDENTITY>_CLIENT_REPLY off -- the first and safest arming state -- the
    reply row is HELD at post time (outbox.py:429) and the client receives nothing. A
    card claiming "I auto-replied to this client" would then be false on 100% of
    replies, and this system's D55 rule exists precisely so nobody has to wonder
    whether a message actually landed: "receipts show what the client was actually
    told, written only after delivery." This card is written BEFORE delivery, so it
    describes the queue, and the existing receipt/hold-notice path remains the record
    of what was actually sent.

    Why this exists, and why it is not a bigger keyword list. This lane answers one
    narrow, verified thing. A client's message can contain that thing AND something
    else entirely -- an ad-budget request, a billing question, a class-schedule change,
    an injury. An audit put ten plainly ad-money phrasings past the keyword belt, and a
    message pairing one with a routable phrase got an auto-reply about the photos while
    the rest was dropped in silence and the ticket marked handled.

    Widening the belt is the loop D68 forbids: the phrasings are an open set. So the
    lane stops pretending a reply means the message is handled. A human reads every
    message this lane answered, in the client's own words, and picks up whatever Echo
    did not address. It is indifferent to phrasing, which is the whole point.
    """
    _, kind_escalation = _kinds()

    def _notice(ticket, decision):
        meta = _base_meta(ticket, recipient_kind="staff",
                          surface=str(ticket.get("_surface") or "mpim"))
        meta["answered_notice"] = True
        meta["template_id"] = decision.template_id
        body = (
            f"[{REPLY_META_LANE}] I QUEUED a reply to this client, about ONE thing.\n"
            f"gym: {decision.gym_key or 'unresolved'}\n"
            f"slack user: {ticket.get('slack_user_id') or '?'}\n"
            f"what I answered: {decision.template_id} "
            f"(grounded in {', '.join(decision.audit.get('fact_keys', [])) or 'n/a'})\n"
            f"verification: {decision.audit.get('verification', '')}\n"
            f"I did NOT read their message for anything else. If it asked for anything "
            f"beyond this, it has NOT been handled.\n"
            f"delivery: queued as '{REPLY_DELIVERY_STATUS}'. Whether it reached them "
            f"depends on the identity's client-reply flag at post time; the receipt "
            f"and any hold notice on this ticket are the record of what was sent.\n"
            f"--- what they wrote (untrusted, escaped) ---\n"
            f"{_fenced_client_text(getattr(decision, 'client_text', ''))}\n"
            f"--- what I said ---\n"
            f"{_fenced_client_text(decision.reply_text)}"
        )
        return bus.record_outbound(
            ticket_id=ticket.get("id"), author_type=meta["identity"], body=body,
            delivery_status=ESCALATION_DELIVERY_STATUS, kind=kind_escalation, meta=meta)

    return _notice


# The single registry of processes allowed to deliver this capability's output. It is
# a constant so that tests/test_client_dm_flow.py can hold a TWO-WAY guard on it:
# writers must stay inside it, AND it must still contain what it must, so a future
# edit cannot quietly narrow it back to an inert state and stay green (D68).
DELIVERY_PRODUCERS = frozenset({"bus_reply_sink", "bus_escalation_sink",
                                "bus_answered_notice_sink"})
