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


def _base_meta(ticket, surface="mpim", recipient_kind="client"):
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


def bus_escalation_sink(bus):
    """callable(ticket, decision) -> writes a 'ready' ESCALATION row. Internal kinds go
    to the fixer channel, not the client's thread, so a human actually sees every
    refusal this lane makes."""
    _, kind_escalation = _kinds()

    def _hold(ticket, decision):
        trigger = decision.foundation_trigger or "not_grounded"
        meta = _base_meta(ticket, recipient_kind="staff")
        meta["foundation_trigger"] = trigger
        body = (
            f"[{REPLY_META_LANE}] held for a human.\n"
            f"gym: {decision.gym_key or 'unresolved'}\n"
            f"diagnostic: {decision.diagnostic_id or 'none matched'}\n"
            f"trigger: {trigger}\n"
            f"reason: {decision.reason}"
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


# The single registry of processes allowed to deliver this capability's output. It is
# a constant so that tests/test_client_dm_flow.py can hold a TWO-WAY guard on it:
# writers must stay inside it, AND it must still contain what it must, so a future
# edit cannot quietly narrow it back to an inert state and stay green (D68).
DELIVERY_PRODUCERS = frozenset({"bus_reply_sink", "bus_escalation_sink"})
