"""
consumer.py — THE TRIGGER SURFACE.

An inbound message in a client's own group DM (their bot + Blake + the owner) is
already turned into a support_tickets row by agent/slack_convo/adapter.py. That module
is UNMODIFIED by this capability and stays that way: this consumer reads the tickets it
wrote, decides, and hands the result to the existing delivery rails in wiring.py.

Order of operations, and every one of them can only refuse:

  1. the flag. AGENT_CLIENT_DM_AUTOFIX, default OFF. Off returns a REASON, loudly, so
     OFF is distinguishable from BROKEN (D68's not-wired guard, pointed the other way).
  2. ad_block.assert_no_ad_call_path(). Runs before a single ticket is read. If any
     ad-write import or call ever appears in this package, the lane refuses to run at
     all rather than running with the structural guarantee gone. D68: "an assertion
     nobody calls is itself an instance of the bug it exists to catch" — so it is
     called here, on the real path, not only from tests.
  3. surface. Only a client group DM / DM ticket. A channel or a staff-only surface is
     not this lane's business.
  4. flow.handle_ticket() per ticket.
  5. deliver: a grounded reply, or a held escalation card. Never both, never neither.

The consumer posts nothing itself and holds no Slack client, exactly like the adapter.
"""
from __future__ import annotations

from . import ad_block as _ad
from . import flow as _flow
from . import wiring as _wiring

# The ticket surfaces this lane will act on. A ticket from any other surface is left
# alone for the existing lanes to handle.
CLIENT_DM_SURFACES = frozenset({"im", "mpim"})

# The bus `source` this lane polls. Client DMs arrive on the Slack adapter's own
# source; a portal/website/ops-authored ticket is a different lane's work.
POLL_SOURCE = "slack"
POLL_PRODUCT = "echo"


def _flag_on():
    from .. import config
    return config.client_dm_autofix_enabled()


def run_once(*, bus=None, deps=None, reply_sink=None, escalation_sink=None,
             limit=10, log=None, flag_on=None):
    """One pass. Returns a summary dict; never raises out of a normal degrade path.

    Every dependency has a real production default (wiring.bus_reply_sink /
    wiring.bus_escalation_sink), so no seam here is filled only by a test double.
    """
    log = log or (lambda m: print(f"[client-dm] {m}"))
    on = _flag_on() if flag_on is None else bool(flag_on)
    if not on:
        # LOUD off. Not an empty success.
        return {"ok": False, "reason": "AGENT_CLIENT_DM_AUTOFIX is off",
                "handled": 0, "replied": 0, "escalated": 0}

    # The structural ad guarantee, checked on the real path before any work.
    _ad.assert_no_ad_call_path()

    if bus is None:
        from ..slack_convo import bus as _bus_mod
        bus = _bus_mod.Bus()
    if not getattr(bus, "available", lambda: False)():
        return {"ok": False, "reason": "bus unavailable (no Supabase creds)",
                "handled": 0, "replied": 0, "escalated": 0}

    reply_sink = reply_sink or _wiring.bus_reply_sink(bus)
    escalation_sink = escalation_sink or _wiring.bus_escalation_sink(bus)

    try:
        tickets = bus.find_new_tickets(product=POLL_PRODUCT, source=POLL_SOURCE,
                                       limit=int(limit))
    except Exception as e:  # noqa: BLE001
        log(f"poll failed: {type(e).__name__}: {e}")
        return {"ok": False, "reason": f"poll failed: {type(e).__name__}",
                "handled": 0, "replied": 0, "escalated": 0}

    replied = escalated = handled = 0
    decisions = []
    for t in tickets:
        surface = str(t.get("surface") or t.get("slack_surface") or "").lower()
        if surface not in CLIENT_DM_SURFACES:
            continue
        text = _latest_client_text(bus, t)
        if not text:
            continue
        gym_key = t.get("account_key") or t.get("gym_key") or t.get("gym_id")

        decision = _flow.handle_ticket(text=text, gym_key=gym_key, deps=deps)
        decisions.append(decision)
        handled += 1
        try:
            if decision.will_post:
                reply_sink(t.get("id"), decision)
                replied += 1
            else:
                escalation_sink(t.get("id"), decision)
                escalated += 1
        except Exception as e:  # noqa: BLE001 - one ticket never sinks the pass
            log(f"delivery failed for ticket {t.get('id')}: {type(e).__name__}: {e}")

    log(f"client-dm autofix: {handled} handled, {replied} grounded reply(ies), "
        f"{escalated} escalated")
    return {"ok": True, "handled": handled, "replied": replied,
            "escalated": escalated, "decisions": decisions}


def _latest_client_text(bus, ticket):
    """The most recent CLIENT-authored inbound message on this ticket. Staff text is
    ignored on purpose: this lane answers the gym owner, and a staff instruction in the
    same thread is not a support request."""
    try:
        msgs = bus.recent_messages(ticket.get("id"), limit=50) or []
    except Exception:  # noqa: BLE001
        return ""
    for m in reversed(list(msgs)):
        if str(m.get("direction")) != "inbound":
            continue
        if str(m.get("author_type") or "").lower() in ("staff", "bot"):
            continue
        return str(m.get("body") or "")
    return ""
