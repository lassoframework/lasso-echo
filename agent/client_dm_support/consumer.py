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

# THE FIFTH CONTRACT, and the one this file guessed wrong while claiming the other
# four had been "read out of the code rather than guessed".
#
# What the UNMODIFIED adapter actually writes for a client DM (agent/slack_convo/
# adapter.py), which is the only thing that matters here:
#     :835  QUESTION with an answer drafted        -> status "verification"
#     :847  QUESTION with no answer drafted        -> status "hold", escalated
#     :881  classifier undecided (the fallback)    -> status "hold", escalated
#     :862  CODE_FIX                               -> status "triage"
#     :873  ACTION_REQUEST                         -> status "new"
# Both documented cases -- "my posts have no photos" and "my posts have no call to
# action" -- land in HOLD. Polling only ("new","triage") made the lane inert on
# exactly the two shapes it was built for, and returned {ok:True, handled:0} doing
# it: byte-identical to a healthy idle pass. D68's "built but not wired" again.
#
# "verification" is DELIBERATELY EXCLUDED. That status means the adapter has already
# drafted an answer on the D67-locked auto-answer lane, sitting held for Blake's tap.
# If this lane also replied, releasing that tap later would send the client a second
# message about the same question. That lane is Blake's separate decision and this one
# does not reach into it. The cost is real and is stated rather than hidden: when the
# live classifier routes a client's photo question to QUESTION-with-a-draft, this
# capability does not fire and the existing hold card is what a human sees.
POLL_STATUSES = ("new", "triage", "hold")

_TICKETS = "support_tickets"


def _flag_on():
    from .. import config
    return config.client_dm_autofix_enabled()


# This lane never changes a ticket's status, and outbox._resolve_on_answer resolves
# only KIND_ANSWER (or a KIND_STATUS carrying resolve_notice) -- neither of which this
# lane writes. So an answered ticket stays in an open status forever. With a fixed
# `limit` and created_at.asc ordering, ten already-answered tickets at the front of the
# queue starve every new one behind them, permanently, at {ok:True, handled:0}. So the
# poll PAGES until it holds enough tickets this lane has not already answered.
POLL_PAGE = 50
POLL_MAX_PAGES = 10


def default_poll(bus, *, product=POLL_PRODUCT, source=POLL_SOURCE, limit=10,
                 already_handled=None):
    """This lane's own ticket poll, paged past tickets it has already answered.

    Deliberately NOT bus.find_new_tickets: that method also requires
    classification is.null, which is the portal worker's predicate and excludes every
    classified client DM. Same table, same transport, same test-row exclusion -- only
    the predicate differs.
    """
    from ..slack_convo import testdata as _td
    out, offset = [], 0
    for _page in range(POLL_MAX_PAGES):
        rows = bus._get(_TICKETS, {                              # noqa: SLF001
            "product": f"eq.{product}",
            "source": f"eq.{source}",
            "status": f"in.({','.join(POLL_STATUSES)})",
            "select": "*",
            "order": "created_at.asc",
            "limit": str(POLL_PAGE),
            "offset": str(offset),
        })
        rows = _td.exclude_test_strict(rows)
        if not rows:
            break
        out.extend(rows)
        offset += POLL_PAGE
        if already_handled is None:
            break                     # caller filters; one page is the old behaviour
        if sum(1 for r in out if not already_handled(r)) >= int(limit):
            break
        if len(rows) < POLL_PAGE:
            break
    return out


def resolve_gym_key(ticket, *, portal_key_for_gym=None, confirm_binding=None):
    """support_tickets.client_id -> the Echo account key, or "".

    Uses the repo's anti-divergence primitive rather than deriving a key here: Echo
    deriving its OWN key from a gym_id is what produced two live keys per gym in the
    first place (see agent/account_key_resolve.py). "" on any uncertainty, and ""
    escalates rather than guessing.

    TWO CONTROLS AT THIS SEAM, NOT ONE.
    This is the single binding between a support_tickets row and an Echo account key,
    and everything downstream trusts it: the diagnostic queries that key, the sync runs
    for that key, and the reply is posted into THIS ticket's thread. So one wrong
    answer here routes another gym's data into this client's DM -- reproduced by an
    audit, and not hypothetical in a repo with a documented account-key split-brain
    (7 of 19 gyms disagreed).

    The package's own written standard, from diagnostics.py, is "two independent
    controls must both fail for one gym to see another's data", and it was not applied
    at the seam where it matters most. So the forward answer is now CONFIRMED against
    the inverse map: the key must belong to this gym_id, and to NO OTHER gym. Any
    disagreement, or any uncertainty at all, returns "" and escalates.
    """
    if portal_key_for_gym is None:
        from .. import account_key_resolve as _akr
        portal_key_for_gym = _akr.portal_key_for_gym
    if confirm_binding is None:
        confirm_binding = confirm_gym_binding
    gym_uuid = str(ticket.get("client_id") or "").strip()
    if not gym_uuid:
        return ""
    try:
        key = str(portal_key_for_gym(gym_uuid) or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable plane is not a reason to guess
        return ""
    if not key:
        return ""
    # CONTROL 2: the inverse map must agree, and the key must be unique to this gym.
    try:
        if not confirm_binding(gym_uuid, key):
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return key


def confirm_gym_binding(gym_uuid, account_key, *, state=None, fresh_key=None):
    """Confirm a resolved account key three ways, or refuse.

    HONEST ABOUT WHAT THIS IS. The first version asserted `by_gym[gid] == key` against
    the SAME cached dict the forward call had just read it out of -- a tautology, and
    an audit said so. Only its uniqueness half could ever fire. These three checks are
    different reads and different assertions:

      1. FRESHNESS. Re-resolve with fresh=True, which bypasses the 300s success cache
         and re-reads the plane. A cached answer that a live read disagrees with is
         exactly the stale-fingerprint class this repo has been bitten by, and the
         cached value alone cannot detect it.
      2. LIVENESS. account_key_resolve.resolve(key) reads the `live` set and the
         stale->live `mapping` -- different structures, built from different rows than
         by_gym. It returns the key unchanged only when the key is genuinely live; a
         stale key comes back remapped, and a key that resolves to something else must
         never be used to decide whose data to read.
      3. UNIQUENESS. No OTHER gym may map to this key. Two gyms sharing one key IS the
         split-brain shape (7 of 19 gyms disagreed in this repo's own record), and a
         shared key cannot identify a tenant.

    False on ANY uncertainty: an unreadable plane, a half-read plane, a disagreement,
    a remap, a shared key.
    """
    from .. import account_key_resolve as _akr
    if state is None:
        state = _akr._state                                    # noqa: SLF001
    if fresh_key is None:
        def fresh_key(g):
            return _akr.portal_key_for_gym(g, fresh=True)
    gid = str(gym_uuid or "").strip().lower()
    key = str(account_key or "").strip().lower()
    if not gid or not key:
        return False

    # 1. FRESHNESS -- an independent read, not the cached one.
    try:
        if str(fresh_key(gym_uuid) or "").strip().lower() != key:
            return False
    except Exception:  # noqa: BLE001
        return False

    _live, _mapping, by_gym, ok = state()
    if not ok:
        return False                                  # a half-read plane proves nothing

    # 2. LIVENESS -- read out of `live`/`mapping`, not out of by_gym.
    try:
        if str(_akr.resolve(key) or "").strip().lower() != key:
            return False
    except Exception:  # noqa: BLE001
        return False

    # 3. UNIQUENESS.
    owners = [g for g, k in by_gym.items()
              if str(k or "").strip().lower() == key]
    return len(owners) == 1


def run_once(*, bus=None, deps=None, reply_sink=None, escalation_sink=None,
             notice_sink=None, limit=10, log=None, flag_on=None, poll=None,
             portal_key_for_gym=None, confirm_binding=None):
    """One pass. Returns a summary dict; never raises out of a normal degrade path."""
    log = log or (lambda m: print(f"[client-dm] {m}"))
    on = _flag_on() if flag_on is None else bool(flag_on)
    if not on:
        # LOUD off. Not an empty success.
        return {"ok": False, "reason": "AGENT_CLIENT_DM_AUTOFIX is off",
                "handled": 0, "replied": 0, "escalated": 0}

    # THE AD GUARANTEE, both controls, on the real path before any ticket is read.
    #   control 1 (load-bearing): this service has no ad-write rail at all, so there
    #     is nothing for any code path to reach and no credential to reach it with.
    #   the tripwire: no obvious ad import or ad-write call inside this package. It
    #     catches the careless case; it is NOT a proof, and ad_block's docstring says
    #     so plainly after two rounds of audit falsified the stronger claim.
    _ad.assert_no_ad_rail_in_repo()
    _ad.assert_no_ad_call_path()

    if bus is None:
        from ..slack_convo import bus as _bus_mod
        bus = _bus_mod.Bus()
    if not getattr(bus, "available", lambda: False)():
        return {"ok": False, "reason": "bus unavailable (no Supabase creds)",
                "handled": 0, "replied": 0, "escalated": 0}

    reply_sink = reply_sink or _wiring.bus_reply_sink(bus)
    escalation_sink = escalation_sink or _wiring.bus_escalation_sink(bus)
    notice_sink = notice_sink or _wiring.bus_answered_notice_sink(bus)
    poll = poll or default_poll

    def _handled_already(ticket):
        return _already_handled(_messages(bus, ticket))

    try:
        tickets = poll(bus, limit=int(limit), already_handled=_handled_already)
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
        t = dict(t)
        t["_surface"] = surface        # the REAL surface, for the rows the sinks write

        gym_key = resolve_gym_key(t, portal_key_for_gym=portal_key_for_gym,
                                  confirm_binding=confirm_binding)
        decision = _flow.handle_ticket(text=text, gym_key=gym_key, deps=deps)
        decisions.append(decision)
        handled += 1
        try:
            if decision.will_post:
                reply_sink(t, decision)
                replied += 1
                # EVERY AUTO-REPLY IS ALSO CARDED TO A HUMAN, unconditionally.
                #
                # This replaces a keyword list that could not work. is_ad_money_topic
                # missed 10 of 10 plainly ad-money phrasings an owner would type
                # ("raise the daily spend on facebook", "pause everything we are
                # paying for"), and a message pairing one of those with a routable
                # phrase auto-replied about the photos and dropped the rest in
                # silence. Same for a hard line riding along: "my posts have no
                # photos. did you take money out twice this month?"
                #
                # Widening the keyword list is the loop D68 forbids. The structural
                # answer is that this lane ANSWERS a narrow, verified thing and never
                # claims to have handled the whole message -- so a human reads every
                # message it replied to, sees the client's own words, and can follow
                # up on whatever Echo did not address. It costs one card per reply
                # and it does not care how anything was phrased.
                notice_sink(t, decision)
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
    if undelivered:
        # A ticket whose fix RAN and whose outcome reached nobody is not a healthy
        # pass, and one log line is not enough to notice it.
        try:
            from .. import ops_alerts
            ops_alerts.alert(
                f"client DM autofix: {undelivered} ticket(s) were diagnosed and acted "
                f"on but NEITHER the reply NOR the escalation could be written. Nobody "
                f"has been told. Check the bus.")
        except Exception:  # noqa: BLE001
            pass
    return {"ok": not undelivered, "handled": handled, "replied": replied,
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
    """True when this lane has already answered the client's LATEST message.

    The schema-free idempotency check: no new column, no migration, no foundation
    trigger. `msgs` is newest-first, so "has this lane written since the client last
    spoke" is answered by walking forward and seeing which comes first.

    WHY NOT "EVER ANSWERED THIS TICKET", which is what this used to mean: a client
    follow-up -- "no, they're still blank" -- was then dropped with no reply, no
    escalation, and a `skipped` counter that reads healthy. Silence on a client who
    wrote back twice is the failure mode this whole capability exists to avoid. Now a
    follow-up is handled like any other message, and if it is not groundable it
    escalates to a human instead of vanishing.
    """
    for m in msgs:
        direction = str(m.get("direction"))
        if direction == "outbound" and _att(m).get("lane") == _wiring.REPLY_META_LANE:
            return True                      # our own reply is the most recent event
        if direction == "inbound" and \
                str(m.get("author_type") or "").lower() not in ("staff", "bot"):
            return False                     # the client has spoken since; handle it
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
