"""
consumer.py — THE TRIGGER SURFACE.

An inbound message in a client's own group DM (their bot + Blake + the owner) is
already turned into a support_tickets row by agent/slack_convo/adapter.py. That module
is UNMODIFIED by this capability and stays that way: this consumer reads the tickets it
wrote, decides, and hands the result to the existing delivery rails in wiring.py.

THE FOUR CONTRACTS THIS FILE GOT WRONG FIRST TIME, now read out of the code rather
than guessed. Each one alone made the lane return {'ok': True, 'handled': 0} — the
D68 signature where the inert state is byte-for-byte identical to a healthy one:

  1. SOURCE is 'slack_conversation', not 'slack'. bus.get_or_create_ticket hardcodes
     it (agent/slack_convo/bus.py:133). Polling 'slack' matched zero rows, forever.
  2. SURFACE is not a ticket column. It rides in the INBOUND MESSAGE's attachments
     (adapter.py:729, meta={"surface": surface, ...}). So the surface is read off the
     message, not the ticket.
  3. THE GYM KEY is not on the ticket either. support_tickets.client_id is the PORTAL
     GYM UUID (adapter.py:715, client_id=who.gym_id) — precisely the key
     diagnostics.require_account_key REFUSES. It is resolved to the Echo account key
     through account_key_resolve.portal_key_for_gym, the repo's anti-divergence
     primitive, which returns "" on ANY uncertainty; "" escalates.
  4. bus.find_new_tickets ADDITIONALLY filters classification is.null (bus.py:167).
     That is the portal-ticket worker's "nobody has picked this up yet" predicate, and
     the adapter classifies a DM ticket at creation (adapter.py:718/835/862/873), so
     every client DM is invisible to it. This lane needs its own poll, below.

IDEMPOTENCY WITHOUT A SCHEMA CHANGE. A new column would be a foundation trigger, so
"have I already handled this ticket" is answered from rows that already exist: a
ticket carrying an outbound row stamped with this lane is skipped. That is honest
about the cost — one extra read per candidate ticket — and it needs no migration.

Order of operations, and every one of them can only refuse:

  1. the flag. AGENT_CLIENT_DM_AUTOFIX, default OFF. Off returns a REASON, loudly, so
     OFF is distinguishable from BROKEN.
  2. ad_block.assert_no_ad_call_path(), before a single ticket is read.
  3. surface + already-handled + gym-key resolution.
  4. flow.handle_ticket() per ticket.
  5. deliver: a grounded reply, or an escalation card. Never both, never neither.

The consumer posts nothing itself and holds no Slack client, exactly like the adapter.
"""
from __future__ import annotations

from . import ad_block as _ad
from . import flow as _flow
from . import wiring as _wiring

# The message surfaces this lane will act on: a 1:1 DM or a client group DM.
CLIENT_DM_SURFACES = frozenset({"im", "mpim"})

# The bus `source` the Slack adapter actually writes. Verified against
# agent/slack_convo/bus.py:133 — not inferred.
POLL_SOURCE = "slack_conversation"
POLL_PRODUCT = "echo"

# Ticket statuses worth looking at. A resolved or approved ticket is finished.
POLL_STATUSES = ("new", "triage")

_TICKETS = "support_tickets"


def _flag_on():
    from .. import config
    return config.client_dm_autofix_enabled()


def default_poll(bus, *, product=POLL_PRODUCT, source=POLL_SOURCE, limit=10):
    """This lane's own ticket poll.

    Deliberately NOT bus.find_new_tickets: that method also requires
    classification is.null, which is the portal worker's predicate and excludes every
    classified client DM. Same table, same transport, same test-row exclusion — only
    the predicate differs.
    """
    from ..slack_convo import testdata as _td
    rows = bus._get(_TICKETS, {                                  # noqa: SLF001
        "product": f"eq.{product}",
        "source": f"eq.{source}",
        "status": f"in.({','.join(POLL_STATUSES)})",
        "select": "*",
        "order": "created_at.asc",
        "limit": str(int(limit)),
    })
    return _td.exclude_test_strict(rows)


def resolve_gym_key(ticket, *, portal_key_for_gym=None):
    """support_tickets.client_id -> the Echo account key, or "".

    Uses the repo's anti-divergence primitive rather than deriving a key here: Echo
    deriving its OWN key from a gym_id is what produced two live keys per gym in the
    first place (see agent/account_key_resolve.py). "" on any uncertainty, and ""
    escalates rather than guessing.
    """
    if portal_key_for_gym is None:
        from .. import account_key_resolve as _akr
        portal_key_for_gym = _akr.portal_key_for_gym
    gym_uuid = str(ticket.get("client_id") or "").strip()
    if not gym_uuid:
        return ""
    try:
        return str(portal_key_for_gym(gym_uuid) or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable plane is not a reason to guess
        return ""


def run_once(*, bus=None, deps=None, reply_sink=None, escalation_sink=None,
             limit=10, log=None, flag_on=None, poll=None, portal_key_for_gym=None):
    """One pass. Returns a summary dict; never raises out of a normal degrade path."""
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
    poll = poll or default_poll

    try:
        tickets = poll(bus, limit=int(limit))
    except Exception as e:  # noqa: BLE001
        log(f"poll failed: {type(e).__name__}: {e}")
        return {"ok": False, "reason": f"poll failed: {type(e).__name__}",
                "handled": 0, "replied": 0, "escalated": 0}

    replied = escalated = handled = skipped = undelivered = 0
    decisions = []
    for t in tickets:
        msgs = _messages(bus, t)
        surface = _surface_of(msgs)
        if surface not in CLIENT_DM_SURFACES:
            continue
        if _already_handled(msgs):
            skipped += 1
            continue
        text = _latest_client_text(msgs)
        if not text:
            continue

        gym_key = resolve_gym_key(t, portal_key_for_gym=portal_key_for_gym)
        decision = _flow.handle_ticket(text=text, gym_key=gym_key, deps=deps)
        decisions.append(decision)
        handled += 1
        try:
            if decision.will_post:
                reply_sink(t, decision)
                replied += 1
            else:
                escalation_sink(t, decision)
                escalated += 1
        except Exception as e:  # noqa: BLE001 - one ticket never sinks the pass
            # A swallowed delivery failure was silence: the fix had already run, the
            # client was told nothing, no escalation row existed, and the counters read
            # {replied:0, escalated:0} -- indistinguishable from "nothing to do". Try
            # the escalation sink so a human sees it; if that fails too, count it as an
            # explicit failure rather than letting the pass look clean.
            log(f"delivery failed for ticket {t.get('id')}: {type(e).__name__}: {e}")
            try:
                escalation_sink(t, decision)
                escalated += 1
            except Exception as e2:  # noqa: BLE001
                log(f"escalation ALSO failed for ticket {t.get('id')}: "
                    f"{type(e2).__name__}: {e2}")
                undelivered += 1

    log(f"client-dm autofix: {handled} handled, {replied} grounded reply(ies), "
        f"{escalated} escalated, {skipped} already handled, {undelivered} UNDELIVERED")
    return {"ok": True, "handled": handled, "replied": replied,
            "escalated": escalated, "skipped": skipped,
            "undelivered": undelivered, "decisions": decisions}


def _messages(bus, ticket):
    try:
        return list(bus.recent_messages(ticket.get("id"), limit=50) or [])
    except Exception:  # noqa: BLE001
        return []


def _att(m):
    a = m.get("attachments")
    return a if isinstance(a, dict) else {}


def _surface_of(msgs):
    """The surface, read off the INBOUND message's attachments where the adapter
    actually writes it (adapter.py:729) — support_tickets has no surface column.

    ORDERING: bus.recent_messages returns created_at.desc, i.e. NEWEST FIRST
    (bus.py:273-277). Iterating forward therefore reads the most recent inbound
    message, which is what we want."""
    for m in msgs:
        if str(m.get("direction")) != "inbound":
            continue
        s = str(_att(m).get("surface") or "").lower()
        if s:
            return s
    return ""


def _already_handled(msgs):
    """True when this lane has already written an outbound row on this ticket. The
    schema-free idempotency check: no new column, no migration, no foundation trigger."""
    for m in msgs:
        if str(m.get("direction")) != "outbound":
            continue
        if _att(m).get("lane") == _wiring.REPLY_META_LANE:
            return True
    return False


def _latest_client_text(msgs):
    """The most recent CLIENT-authored inbound message. Staff text is ignored on
    purpose: this lane answers the gym owner, and a staff instruction in the same
    thread is not a support request.

    ORDERING BUG, FIXED: bus.recent_messages orders created_at.DESC — newest first
    (bus.py:273-277, and its own docstring says so). The first version reversed that
    list and returned the OLDEST client message, so the lane diagnosed and replied to
    the client's first sentence and never saw a follow-up -- including a follow-up the
    ad belt would have caught ("forget the photos, can you double my ad budget?").
    Newest-first is the order we are given, so iterate it forward."""
    for m in msgs:
        if str(m.get("direction")) != "inbound":
            continue
        if str(m.get("author_type") or "").lower() in ("staff", "bot"):
            continue
        return str(m.get("body") or "")
    return ""
