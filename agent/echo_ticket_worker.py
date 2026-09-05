"""
echo_ticket_worker.py -- D46 (Blake, 2026-09-04): the bridge from a portal-submitted
Echo support ticket to the real pipeline.

Ground truth (Blake's own words): "with echo if someone submits a support echo should
receive that then echo should fix it verify the fix and then send slack message with
them and me in the message." This module is the "receive" + "dispatch" + "notify once
verified" half; the actual fix + verify for a code_fix is the SAME ops-fix-triage.js
worker every Slack-sourced code_fix already uses (D3/D14) -- this does not bypass that,
or its hold gate. A code_fix from a client is ALWAYS held behind Blake's #fixer tap,
exactly like every other code_fix in this system; D14's invariant is untouched. What
changes is that once that worker has verified a real fix, THIS module is what notices
and sends the client (and Blake) the verified result, automatically.

PROVENANCE (D42/D45): a ticket from source='website_tab' is trustworthy because
lasso-ops-portal's /api/gyms/[gymId]/support route stamps `reporter` from the
AUTHENTICATED Clerk session server-side -- the client cannot spoof it via the POST
body. This satisfies D42's "an authenticated portal session" escape hatch, so THIS
worker uses outreach's FAST autonomous path (reporter_verified=True, in-memory only --
there is no such column on support_tickets, by design), not the human-tap hold path.
Blake asked for a fully automatic notify-once-verified pipeline for this specific,
trusted source; the tap gate D42/D45 built stays available for any OTHER, less-trusted
non-Slack source added later.

Two poll passes, run every config.portal_echo_tickets_poll_minutes() minutes, both
no-ops while config.portal_echo_tickets_enabled() is off:

  1. intake_pass(): NEW, unclassified website_tab/echo tickets -> resolve identity,
     classify, dispatch (a grounded question gets an immediate answer + outreach; a
     code_fix gets a HELD fixer_request card, same as any other code_fix).
  2. fixed_pass(): tickets already dispatched to the fixer worker (status='fixing')
     whose verification has since landed -> outreach with the VERIFIED result as the
     first message. A not-yet-verified ticket is left exactly as-is for next cycle.

Both passes are pure given their injected dependencies -- no import of a live Slack
client or the live bus at module scope, so they are fully unit-testable offline.
"""
from . import config
from .slack_convo import adapter as _a
from .slack_convo import classifier as _cls
from .slack_convo import identities as _ids
from .slack_convo import identity_gate as _ig
from .slack_convo import outbox as _ob
from .slack_convo import outreach as _out

_EPOCH_ISO = "1970-01-01T00:00:00+00:00"

# D47 (Blake, 2026-09-04): generalized from Echo-only to any (product, identity) pair
# routed through the identity map -- portal tickets (product='portal', the generic
# Website tab form's default) route to Scout, not Ranger's ad-engine-specific
# fixer-lane.ts, which never had a reason to see a non-ranger ticket in the first
# place. Every call site defaults to Echo so existing behavior and tests are
# unchanged; a caller wiring a second (product, identity) pair passes them explicitly.
PRODUCT = "echo"
SOURCE = "website_tab"


def resolve_client_identity(ticket, *, slack_lookup_email, slack_user_info, portal_lookup,
                            operator_ids=(), log=print):
    """ticket['reporter'] is a real, server-authenticated email (D42's provenance).
    ticket['client_id'] is the gym id the PORTAL ROUTE'S URL PARAM claimed, which is
    NOT itself proof of any relationship -- the portal's own access check
    (canReadGym) grants coach/executive/owner roles read on ANY gym, not just their
    own, so an authenticated staff account hitting a different gym's ticket endpoint
    would previously be treated as THAT gym's verified client (Frame 2 audit finding,
    2026-09-04: this let staff impersonate any client and trigger the no-tap
    autonomous outreach path meant only for a real gym owner).

    Fixed by routing through identity_gate.resolve() -- the SAME staff/coach/client
    classification the Slack-initiated path already trusts -- rather than a separate,
    looser check. STAFF and COACH are never promoted to CLIENT here (identity_gate's
    own rule), and a CLIENT's gym_id comes from THEIR OWN gym_assignments row, never
    from the ticket's client_id. The two are then compared explicitly: a real
    client_owner whose OWN gym does not match the ticket's claimed client_id is
    UNKNOWN here too, not a guess in either gym's favor."""
    email = (ticket.get("reporter") or "").strip()
    claimed_gym_id = ticket.get("client_id") or ""
    if not email or not claimed_gym_id:
        return _ig.Identity(_ig.UNKNOWN, "", reason="ticket missing reporter or client_id")
    try:
        slack_user_id = slack_lookup_email(email)
    except Exception as e:  # noqa: BLE001 - a lookup fault is UNKNOWN, never a guess
        log(f"[echo-ticket-worker] slack lookup failed: {type(e).__name__}")
        slack_user_id = None
    if not slack_user_id:
        return _ig.Identity(_ig.UNKNOWN, "", email=email,
                            reason="no Slack account for this authenticated email")
    identity = _ig.resolve(slack_user_id, slack_user_info=slack_user_info,
                           portal_lookup=portal_lookup, operator_ids=operator_ids)
    if identity.kind != _ig.CLIENT:
        # STAFF/COACH/UNKNOWN, exactly as identity_gate already defines them --
        # never promoted to CLIENT just because they authenticated to SOME account.
        return identity
    if identity.gym_id != claimed_gym_id:
        return _ig.Identity(_ig.UNKNOWN, slack_user_id, email=email,
                            reason=f"authenticated client owns a different gym "
                                   f"than this ticket's client_id "
                                   f"({identity.gym_id!r} != {claimed_gym_id!r})")
    return identity


def _verified_ticket_dict(ticket):
    """A copy of the ticket row with reporter_verified stamped True -- in-memory only,
    D42's provenance signal for THIS caller, who knows (because the row came from
    /api/gyms/[gymId]/support) that the reporter is real. Never persisted; there is no
    such column on support_tickets, by design (see outreach.py's D42/D45 docstrings)."""
    d = dict(ticket)
    d["reporter_verified"] = True
    return d


def _escalate_unresolved(bus, ticket, *, reason, identity_name="echo", log=print,
                         who=None, outreach=None):
    """LIVE BUG FIX (2026-09-04, found running the real Echo regression test): the
    support_tickets.status CHECK constraint has never allowed the literal value
    'escalated' -- the rest of this codebase's own convention (tests/test_slack_convo.py)
    has always been status='hold' + the separate escalated=True boolean. This function
    used the wrong string since D46 shipped, so EVERY unresolved-identity or
    unclassifiable ticket through this bridge raised a BusError on the very first
    bus.set_ticket call, before record_outbound ever ran -- caught (after the Frame 1
    MINOR fix) by intake_pass's per-ticket try/except, which stopped it from starving
    other tickets, but meant the escalation notice was NEVER written and the ticket's
    status stayed 'new', so it silently retried and failed identically every poll,
    forever, with no card ever reaching #fixer. Found live: a real client's ticket
    ("Can we add our group sessions schedule to the website?") was stuck in exactly
    this loop from the moment AGENT_PORTAL_ECHO_TICKETS_ENABLED first armed."""
    tid = ticket.get("id")
    bus.set_ticket(tid, status="hold", escalated=True)
    bus.record_outbound(
        ticket_id=tid, author_type="system",
        body=f"Portal ticket {tid} ({identity_name}) could not be routed "
             f"automatically: {reason}. Raw message: "
             f"{(ticket.get('raw_text') or '')[:300]}",
        delivery_status="ready", kind=_a.KIND_ESCALATION,
        meta={"identity": identity_name, "surface": "portal_ticket_bridge"})
    acknowledge_submitter(bus, ticket, identity_name=identity_name, who=who,
                          outreach=outreach, log=log)
    log(f"[ticket-worker/{identity_name}] escalated ticket={tid} reason={reason}")


def acknowledge_submitter(bus, ticket, *, identity_name="echo", who=None, outreach=None,
                          log=print):
    """D48 (Blake, 2026-09-05): an escalation must never be silence for the person who
    wrote in.

    Found live on three real portal tickets: each one reached #fixer correctly and each one
    left its submitter with nothing at all -- no "we got it", and no word when it was dealt
    with. The Slack-initiated path has always sent this acknowledgement (adapter.py emits
    ACK/TEMPLATE_NO_ANSWER_YET inline); the portal bridge never did, because it escalates and
    returns before any client-facing row is written.

    Best channel available, in order:
      1. a Slack group DM (Blake + the client + this bot) when we resolved the person to a
         real client -- the same outreach.initiate the answered path uses, so the DM thread
         becomes the ticket thread and everything after this lands there too;
      2. the portal support thread they submitted from, which outbox.py now delivers to.

    Written exactly once per ticket: an ack already on the row (including the one
    outreach.initiate writes for itself) means this has been done. Returns True when an
    acknowledgement exists after this call."""
    tid = ticket.get("id")
    if _has_outbound_kind(bus, tid, _a.KIND_ACK, log=log):
        return True
    # M1 (audit 2): a group DM to a client is a client-facing send and obeys the same flags
    # as every other one. With them off this falls through to the portal-thread row below,
    # which the outbox gates in the usual way -- the client is still acknowledged, through a
    # surface that respects the trust ladder.
    dm_allowed = (config.slack_convo_identity_enabled(identity_name)
                  and config.slack_convo_client_reply_armed(identity_name))
    if (dm_allowed and outreach and who is not None and who.kind == _ig.CLIENT
            and who.slack_user_id):
        result = _out.initiate(
            _verified_ticket_dict(ticket), who, outreach["ident"],
            open_group_dm=outreach["open_group_dm"],
            post_first_message=outreach["post_first_message"],
            record_outbound=bus.record_outbound, stamp_ticket=outreach.get("stamp_ticket"),
            message_text=_a.TEMPLATE_NO_ANSWER_YET, mark_message=outreach.get("mark_message"),
            claim_message=outreach.get("claim_message"), log=log)
        if getattr(result, "delivered", False):
            return True
        # C2 (audit 2): this used to return True on `opened`, which is True even when the
        # post FAILED -- so the client got nothing AND the portal-thread fallback below was
        # skipped, on the one path whose entire job is making sure they hear something.
        log(f"[ticket-worker/{identity_name}] escalation ack not delivered "
            f"ticket={tid} reason={result.reason}; falling back to the portal thread")
    bus.record_outbound(
        ticket_id=tid, author_type=identity_name, body=_a.TEMPLATE_NO_ANSWER_YET,
        delivery_status="ready", kind=_a.KIND_ACK,
        meta={"identity": identity_name, "surface": "portal_ticket_bridge",
              "recipient_kind": "client"})
    return True


def _has_outbound_kind(bus, tid, kind, *, log=print):
    """Fails CLOSED (True, "already sent") on a bus fault -- the same convention
    adapter._outbound_kind_ever uses: a read failure must never license a second send."""
    try:
        return bus.count_outbound_kind_since(tid, kind, _EPOCH_ISO) > 0
    except AttributeError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"[ticket-worker] ack lookup failed ticket={tid}: {type(e).__name__}")
        return True
    try:
        return any(m.get("direction") == "outbound"
                   and (m.get("attachments") or {}).get("kind") == kind
                   for m in bus.messages(tid, limit=200))
    except Exception as e:  # noqa: BLE001
        log(f"[ticket-worker] ack lookup failed ticket={tid}: {type(e).__name__}")
        return True


def intake_pass(bus, *, slack_lookup_email, slack_user_info, portal_lookup, open_group_dm,
               post_first_message, write_hold_notice, product=PRODUCT, source=SOURCE,
               identity_name="echo", operator_ids=(), fetch_state=None, llm=None,
               classify_llm=None, mark_message=None, claim_message=None, stamp_ticket=None,
               log=print):
    """First pass: NEW, unclassified tickets for (product, source), dispatched under
    identity_name. Never runs if the config flag is off. Defaults preserve the
    original Echo-only behavior; D47 generalized this for a second (product,
    identity) pair (portal -> scout) without touching Echo's call site."""
    if not config.portal_echo_tickets_enabled():
        return {"processed": 0}
    ident = _ids.IDENTITIES[identity_name]
    tickets = bus.find_new_tickets(product=product, source=source)
    processed = 0
    for ticket in tickets:
        tid = ticket["id"]
        try:
            _intake_one(bus, ticket,
                        slack_lookup_email=slack_lookup_email,
                        slack_user_info=slack_user_info,
                        portal_lookup=portal_lookup,
                        operator_ids=operator_ids,
                        open_group_dm=open_group_dm,
                        post_first_message=post_first_message,
                        write_hold_notice=write_hold_notice, ident=ident,
                        identity_name=identity_name, fetch_state=fetch_state, llm=llm,
                        classify_llm=classify_llm,
                        mark_message=mark_message, claim_message=claim_message,
                        stamp_ticket=stamp_ticket, log=log)
            processed += 1
        except Exception as e:  # noqa: BLE001 -- one bad ticket must never starve the rest
            log(f"[echo-ticket-worker] intake failed ticket={tid}: "
                f"{type(e).__name__}: {e}")
    return {"processed": processed}


def _intake_one(bus, ticket, *, slack_lookup_email, slack_user_info, portal_lookup,
                operator_ids, open_group_dm, post_first_message, write_hold_notice,
                ident, identity_name, fetch_state, llm, classify_llm, mark_message,
                claim_message,
                stamp_ticket, log):
    tid = ticket["id"]
    # Row-first: the client's original words are recorded as an inbound message
    # before anything else touches this ticket -- the portal insert only wrote the
    # TICKET, not a support_messages row, unlike a Slack-sourced ticket. Guarded by
    # inbound_count so a ticket that fails a LATER step (and so stays 'new' for the
    # next poll to pick up again) does not duplicate this row every retry -- found
    # live: the same real client message was recorded 5 times over 5 failed polls
    # before the _escalate_unresolved status bug (fixed alongside this) was found.
    if bus.inbound_count(tid) < 1:
        bus.record_inbound(ticket_id=tid, slack_event_id=None, slack_ts=None,
                           author_type="client", author_id=ticket.get("reporter") or "",
                           body=ticket.get("raw_text") or "",
                           meta={"surface": "portal_ticket_bridge"})

    # D46/D47 audit fix (Frame 1, CRITICAL): outbox.py's dispatch gate refuses to post
    # ANY row whose parent ticket's bot_identity does not match the identity currently
    # running (outbox.py's own cross-identity-leak guard, D33). A portal-inserted ticket
    # never passes through get_or_create_ticket, the only other place that stamps this
    # column, so without this line every held fixer_request card and every escalation
    # row this worker writes was silently unreachable forever -- no error, no alert,
    # just a ticket that sits in "fixing"/"escalated" and never reaches a human. Stamped
    # unconditionally, before any dispatch decision, so both of those branches (and any
    # future one) are covered, not just the one path (QUESTION) that happens to bypass
    # outbox.py entirely.
    bus.set_ticket(tid, bot_identity=identity_name)

    outreach = {"ident": ident, "open_group_dm": open_group_dm,
                "post_first_message": post_first_message, "stamp_ticket": stamp_ticket,
                "mark_message": mark_message, "claim_message": claim_message}

    who = resolve_client_identity(ticket, slack_lookup_email=slack_lookup_email,
                                  slack_user_info=slack_user_info,
                                  portal_lookup=portal_lookup,
                                  operator_ids=operator_ids, log=log)
    if who.kind != _ig.CLIENT:
        _escalate_unresolved(bus, ticket, reason=f"identity_{who.kind}",
                            identity_name=identity_name, log=log, who=who,
                            outreach=outreach)
        return

    # Persisted so fixed_pass (a later poll, possibly after a redeploy) can
    # reconstruct who to notify without re-resolving.
    bus.set_ticket(tid, slack_user_id=who.slack_user_id)

    # RTF-2 (2026-09-05, found live): this used to pass `llm` -- the ANSWER LANE's model
    # callable, whose signature is (system, user, model=None) -- as the CLASSIFIER's llm,
    # whose contract is (text) -> label. Calling it with one argument raised TypeError on
    # every single message, classify() caught it (a model fault escalates, by design) and
    # returned ESCALATE. So the portal bridge's LLM fallback never once ran: every message
    # the deterministic rules did not recognise silently escalated, which is precisely what
    # happened to the one real client ticket of 2026-09-05 (35e066d0, the "nothing was
    # recreated" report). Same bug class as classify_llm=None in listener_wiring, one layer
    # subtler: here something WAS passed, it was just the wrong shape, and the fail-closed
    # path made it look identical to "the classifier had nothing to say".
    classification = _cls.classify(ticket.get("raw_text") or "", has_open_ticket=False,
                                   identity_product=ident.product, llm=classify_llm)

    if classification == _cls.QUESTION:
        answer = None
        try:
            from .slack_convo import answer_lane as _al
            answer = _al.answer(ticket, who, [], ticket.get("raw_text") or "",
                                identity=ident, fetch_state=fetch_state, llm=llm)
        except Exception as e:  # noqa: BLE001 - escalates, never invents
            log(f"[echo-ticket-worker] answer lane failed ticket={tid}: "
                f"{type(e).__name__}")
        if answer and answer.get("body") and answer.get("grounding"):
            bus.set_ticket(tid, classification=_cls.QUESTION, status="verification",
                           verification_before=answer["grounding"],
                           verification_after=answer["grounding"])
            # C2 (2026-09-05 audit, CRITICAL): this branch posts a model-written answer
            # straight to the client through outreach.initiate, bypassing outbox._dispatch_one
            # and therefore EVERY gate D54 added -- the trust ladder, the AUTO_ANSWER flag and
            # the hard lines. Reproduced by the auditor on this system's own real ticket text
            # ("Can we add our group sessions schedule to the website?" -- 'schedule' is a hard
            # line): with CLIENT_REPLY and AUTO_ANSWER both OFF it posted to the client's group
            # DM and resolved the ticket. The gates now live in front of the send, not only in
            # the outbox, because "checked at draft time AND at post time" has to mean every
            # path that can reach a client, not every path that happens to use one module.
            forbidden = (_a.auto_answer_forbidden(ticket.get("raw_text") or "")
                         or _a.auto_answer_forbidden(answer["body"]))
            armed = (config.slack_convo_auto_answer_armed(identity_name)
                     and config.slack_convo_client_reply_armed(identity_name))
            if forbidden or not armed:
                why = ("hard line (billing, hours or schedule, injury or liability): this "
                       "never auto answers, whatever the flags say" if forbidden else
                       f"SLACK_CONVO_{identity_name.upper()}_AUTO_ANSWER is off: a grounded "
                       f"answer needs your tap")
                row = bus.record_outbound(
                    ticket_id=tid, author_type=identity_name, body=answer["body"],
                    delivery_status="held", kind=_a.KIND_ANSWER,
                    meta={"identity": identity_name, "recipient_kind": who.kind,
                          "surface": "portal_ticket_bridge",
                          "auto_answer_forbidden": bool(forbidden)})
                bus.set_ticket(tid, status="hold", escalated=True)
                if write_hold_notice:
                    write_hold_notice(ident_name=identity_name, tid=tid,
                                      recipient_kind=who.kind,
                                      user=who.slack_user_id or "", account_key=who.account_key,
                                      kind=_a.KIND_ANSWER, body=answer["body"],
                                      held_message_id=(row or {}).get("id"),
                                      surface="portal_ticket_bridge", why=why)
                log(f"[echo-ticket-worker] answer HELD ticket={tid} why={why}")
                return
            result = _out.initiate(
                _verified_ticket_dict(ticket), who, ident,
                open_group_dm=open_group_dm, post_first_message=post_first_message,
                record_outbound=bus.record_outbound, stamp_ticket=stamp_ticket,
                message_text=answer["body"], mark_message=mark_message,
                claim_message=claim_message, log=log)
            if getattr(result, "delivered", False):
                bus.set_ticket(tid, status="resolved")
                # M1: the one path that sends a model answer with NO tap at all produced no
                # receipt, so the very thing Blake asked to see was the one thing invisible.
                try:
                    _ob.write_receipt(bus, bus.ticket(tid) or {"id": tid}, identity=ident,
                                      body=answer["body"], kind=_a.KIND_ANSWER,
                                      where="a group DM opened for this ticket", auto=True,
                                      extra={"surface": "portal_ticket_bridge"})
                except Exception as e:  # noqa: BLE001 - never undo a delivery over a receipt
                    log(f"[echo-ticket-worker] receipt failed ticket={tid}: "
                        f"{type(e).__name__}")
            else:
                # C2: `opened` is not delivery. A post_failed / claim_failed / lost_claim
                # result leaves the client with NOTHING, so the ticket must not resolve and
                # no receipt may claim it was told. It escalates to a person instead.
                log(f"[echo-ticket-worker] outreach did not deliver ticket={tid} "
                    f"reason={result.reason}")
                _escalate_unresolved(bus, ticket, reason=f"answer_undelivered_{result.reason}",
                                     identity_name=identity_name, log=log, who=who,
                                     outreach=None)
        else:
            _escalate_unresolved(bus, ticket, reason="question_not_groundable",
                                identity_name=identity_name, log=log, who=who,
                                outreach=outreach)
        return

    if classification == _cls.CODE_FIX:
        # Same HELD fixer_request path every Slack-sourced code_fix uses (D14) --
        # a client's code_fix is ALWAYS held behind Blake's #fixer tap, no exception
        # for this source. This worker only automates the NOTIFY step once the
        # existing worker (ops-fix-triage.js) has actually verified a fix. NOTE
        # (D10, unchanged): that desktop worker trusts only Echo's bot_id today, so
        # a non-Echo identity's fixer_request will queue correctly here but not yet
        # execute -- the same documented limitation every other non-Echo code_fix
        # path in this system already carries.
        bus.set_ticket(tid, classification=_cls.CODE_FIX, status="fixing")
        text = _a.fixer_request_text(ident, tid, ticket.get("raw_text") or "", who,
                                    who.slack_user_id)
        row = bus.record_outbound(ticket_id=tid, author_type="system", body=text,
                                  delivery_status="held", kind=_a.KIND_FIXER_REQUEST,
                                  meta={"identity": identity_name,
                                        "surface": "portal_ticket_bridge",
                                        "recipient_kind": who.kind})
        write_hold_notice(ident_name=identity_name, tid=tid, recipient_kind=who.kind,
                          user=who.slack_user_id, account_key=who.account_key or "",
                          kind=_a.KIND_FIXER_REQUEST, body=text,
                          held_message_id=(row or {}).get("id"),
                          surface="portal_ticket_bridge")
        return

    _escalate_unresolved(bus, ticket, identity_name=identity_name,
                        reason=f"classification_{classification or 'none'}", log=log,
                        who=who, outreach=outreach)


# M5 (2026-09-05 audit 2): what counts as a verification that actually SUCCEEDED. The old
# code claimed "Fixed it and confirmed the change is live" whenever verification_after was
# merely non-empty -- it never looked inside. A snapshot saying {"verified": false, "reason":
# "could not reproduce"} was announced to the client as a confirmed fix. This module does not
# do the verifying; the external fixer worker writes that column. So the rule is: say it only
# when the snapshot says it, and when the snapshot says anything else (or nothing legible),
# make no claim at all and put it in front of a person.
_VERIFIED_TRUE_KEYS = ("verified", "ok", "success", "passed")
_VERIFIED_FALSE_VALUES = (False, "false", "failed", "no", "error")


def verification_succeeded(verification):
    """True only when the snapshot affirmatively says the fix was verified."""
    if not isinstance(verification, dict) or not verification:
        return False
    for key in _VERIFIED_TRUE_KEYS:
        if key in verification:
            val = verification[key]
            if isinstance(val, str):
                return val.strip().lower() not in _VERIFIED_FALSE_VALUES
            return bool(val)
    status = str(verification.get("status") or verification.get("result") or "").lower()
    if status:
        return status in ("verified", "passed", "success", "ok", "fixed")
    # No verdict field at all: a snapshot with a PR url and nothing contradicting it is the
    # shape the existing fixer worker writes, so that stays a success; anything else is not.
    return bool(verification.get("fix_pr_url") or verification.get("pr_url"))


def _fix_summary_text(verification):
    """Plain language, no dashes (same voice rule outreach.py's templates follow), or None
    when the verification does not actually say the fix was verified."""
    if not verification_succeeded(verification):
        return None
    pr = ""
    if isinstance(verification, dict):
        pr = str(verification.get("fix_pr_url") or verification.get("pr_url") or "")
    if pr:
        return f"Fixed it. {pr} I confirmed the change is live before sending this."
    return "Fixed it and confirmed the change is live before sending this."


def fixed_pass(bus, *, open_group_dm, post_first_message, product=PRODUCT,
              identity_name="echo", mark_message=None, claim_message=None,
              stamp_ticket=None, log=print):
    """Second pass: code_fix tickets already dispatched, whose verification has landed.
    A ticket the fixer worker has not finished yet is left exactly as-is -- polled
    again next cycle. Never runs if the config flag is off.

    M1 (2026-09-05 audit 2): this path DMs a client and checked no slack_convo flag at all --
    not client_reply, not even the identity's master switch. "Flags off equals today" was
    simply not true for the portal bridge. It is now."""
    if not config.portal_echo_tickets_enabled():
        return {"notified": 0}
    if not (config.slack_convo_identity_enabled(identity_name)
            and config.slack_convo_client_reply_armed(identity_name)):
        return {"notified": 0}
    ident = _ids.IDENTITIES[identity_name]
    tickets = bus.find_fixing_tickets(product=product)
    notified = 0
    for ticket in tickets:
        tid = ticket["id"]
        verification = ticket.get("verification_after")
        if not verification:
            continue  # not verified yet -- next poll
        who = _ig.Identity(_ig.CLIENT, ticket.get("slack_user_id") or "",
                           email=ticket.get("reporter") or "", account_key="",
                           gym_id=ticket.get("client_id") or "",
                           reason="portal ticket, previously resolved")
        if not who.slack_user_id:
            _escalate_unresolved(bus, ticket, reason="fixed_but_no_slack_user_id",
                                identity_name=identity_name, log=log, who=who)
            continue
        summary = _fix_summary_text(verification)
        if summary is None:
            # M5 (audit 2): the old text asserted "Fixed it and confirmed the change is live"
            # on the mere PRESENCE of verification_after, without ever reading whether the
            # verification succeeded. A failed or inconclusive verification would have been
            # announced to the client as a confirmed fix. No claim is made now; a person is.
            _escalate_unresolved(bus, ticket, reason="verification_not_a_success",
                                identity_name=identity_name, log=log, who=who)
            continue
        result = _out.initiate(
            _verified_ticket_dict(ticket), who, ident,
            open_group_dm=open_group_dm, post_first_message=post_first_message,
            record_outbound=bus.record_outbound, stamp_ticket=stamp_ticket,
            message_text=summary, mark_message=mark_message,
            claim_message=claim_message, log=log)
        if getattr(result, "delivered", False):
            bus.set_ticket(tid, status="resolved")
            notified += 1
            try:
                _ob.write_receipt(bus, bus.ticket(tid) or {"id": tid}, identity=ident,
                                  body=summary, kind=_a.KIND_STATUS,
                                  where="a group DM opened for this ticket", auto=True,
                                  extra={"surface": "portal_ticket_bridge"})
            except Exception as e:  # noqa: BLE001
                log(f"[ticket-worker/{identity_name}] receipt failed ticket={tid}: "
                    f"{type(e).__name__}")
        else:
            # C2: `opened` is not delivery; a failed post must not resolve the ticket.
            log(f"[ticket-worker/{identity_name}] fixed-pass not delivered "
                f"ticket={tid} reason={result.reason}")
    return {"notified": notified}
