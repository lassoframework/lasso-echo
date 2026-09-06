"""
wiring.py — the REAL producers this capability depends on, named.

D68's cheapest general check: "for every capability, name the PRODUCER of the value it
gates on, and assert that producer exists — statically, in a test, with no test double
and no live service." Five separate "built but not wired" bugs in this system were all
the same shape: the capability existed, config said it was on, and the inert state was
byte-for-byte identical to a healthy one.

So the two sinks below are real, and they are the defaults consumer.run_once() uses.
There is no `None` default that a test double would quietly fill in.

BOTH SINKS REUSE THE EXISTING RAILS. This capability invents no channel and no table:

  * the reply goes out as a support_messages row of kind STATUS in 'ready' state, and
    the EXISTING outbox (the Wrangler outbound role) posts it into the same thread.
    This module never calls chat.postMessage — the adapter's architectural rule
    ("the adapter never posts; it writes the row, Wrangler posts") holds here too.
  * the escalation goes out as a row of kind ESCALATION in 'held' state — the existing
    #fixer hold-card mechanism, unchanged.

WHY KIND_STATUS AND NOT KIND_ANSWER. KIND_ANSWER is the #fixer bus's own auto-answer
lane, whose gate is locked by D67 and is explicitly Blake's separate decision. This
capability must not ride it, re-enable it, or depend on `verification_after` semantics
that belong to it. KIND_STATUS is plain-language status on the ticket, is not gated on
`verification_after`, and is already a receipt kind — so the reply appears in the
receipt trail like every other client-visible message. Nothing under agent/slack_convo/
is modified by this file; it is called, not changed.
"""
from __future__ import annotations

REPLY_META_LANE = "client_dm_autofix"


def _kinds():
    from ..slack_convo import adapter as _a
    return _a.KIND_STATUS, _a.KIND_ESCALATION


def bus_reply_sink(bus):
    """Return a callable(ticket_id, decision) that writes the grounded reply as a
    'ready' row for the existing outbox to post into the client's own thread."""
    kind_status, _ = _kinds()

    def _post(ticket_id, decision):
        return bus.record_outbound(
            ticket_id=ticket_id,
            author_type="bot",
            body=decision.reply_text,
            delivery_status="ready",
            kind=kind_status,
            meta={
                "lane": REPLY_META_LANE,
                "template_id": decision.template_id,
                "fact_keys": list(decision.audit.get("fact_keys", [])),
                "verification": decision.audit.get("verification", ""),
            },
        )

    return _post


def bus_escalation_sink(bus):
    """Return a callable(ticket_id, decision) that writes a HELD escalation row — the
    existing #fixer hold card — naming the foundation trigger, if any."""
    _, kind_escalation = _kinds()

    def _hold(ticket_id, decision):
        trigger = decision.foundation_trigger or "not_grounded"
        body = (
            f"[{REPLY_META_LANE}] held for a human.\n"
            f"gym: {decision.gym_key or 'unresolved'}\n"
            f"diagnostic: {decision.diagnostic_id or 'none matched'}\n"
            f"trigger: {trigger}\n"
            f"reason: {decision.reason}"
        )
        return bus.record_outbound(
            ticket_id=ticket_id,
            author_type="bot",
            body=body,
            delivery_status="held",
            kind=kind_escalation,
            meta={"lane": REPLY_META_LANE, "foundation_trigger": trigger},
        )

    return _hold


# The single registry of processes allowed to deliver this capability's output. It is
# a constant so that tests/test_client_dm_flow.py can hold a TWO-WAY guard on it:
# writers must stay inside it, AND it must still contain what it must, so a future
# edit cannot quietly narrow it back to an inert state and stay green (D68).
DELIVERY_PRODUCERS = frozenset({"bus_reply_sink", "bus_escalation_sink"})
