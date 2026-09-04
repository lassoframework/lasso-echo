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
from .slack_convo import outreach as _out

# D47 (Blake, 2026-09-04): generalized from Echo-only to any (product, identity) pair
# routed through the identity map -- portal tickets (product='portal', the generic
# Website tab form's default) route to Scout, not Ranger's ad-engine-specific
# fixer-lane.ts, which never had a reason to see a non-ranger ticket in the first
# place. Every call site defaults to Echo so existing behavior and tests are
# unchanged; a caller wiring a second (product, identity) pair passes them explicitly.
PRODUCT = "echo"
SOURCE = "website_tab"


def resolve_client_identity(ticket, *, slack_lookup_email, account_key_for_gym, log=print):
    """ticket['reporter'] is a real, server-authenticated email (D42's provenance).
    ticket['client_id'] is the gym's portal uuid. Resolve both into a CLIENT identity,
    or UNKNOWN on any lookup failure -- identity_gate.py's own rule (a lookup failure is
    never promoted to a client) applies here too, even though this path never calls
    identity_gate.resolve() directly (that function is keyed the OPPOSITE direction,
    Slack-user-id -> account; here we start from an authenticated email instead)."""
    email = (ticket.get("reporter") or "").strip()
    gym_id = ticket.get("client_id") or ""
    if not email or not gym_id:
        return _ig.Identity(_ig.UNKNOWN, "", reason="ticket missing reporter or client_id")
    try:
        slack_user_id = slack_lookup_email(email)
    except Exception as e:  # noqa: BLE001 - a lookup fault is UNKNOWN, never a guess
        log(f"[echo-ticket-worker] slack lookup failed: {type(e).__name__}")
        slack_user_id = None
    if not slack_user_id:
        return _ig.Identity(_ig.UNKNOWN, "", email=email,
                            reason="no Slack account for this authenticated email")
    try:
        account_key = account_key_for_gym(gym_id) or ""
    except Exception as e:  # noqa: BLE001
        log(f"[echo-ticket-worker] account_key lookup failed: {type(e).__name__}")
        account_key = ""
    return _ig.Identity(_ig.CLIENT, slack_user_id, email=email, account_key=account_key,
                        gym_id=gym_id, reason="portal echo ticket, authenticated session")


def _verified_ticket_dict(ticket):
    """A copy of the ticket row with reporter_verified stamped True -- in-memory only,
    D42's provenance signal for THIS caller, who knows (because the row came from
    /api/gyms/[gymId]/support) that the reporter is real. Never persisted; there is no
    such column on support_tickets, by design (see outreach.py's D42/D45 docstrings)."""
    d = dict(ticket)
    d["reporter_verified"] = True
    return d


def _escalate_unresolved(bus, ticket, *, reason, identity_name="echo", log=print):
    tid = ticket.get("id")
    bus.set_ticket(tid, status="escalated", escalated=True)
    bus.record_outbound(
        ticket_id=tid, author_type="system",
        body=f"Portal ticket {tid} ({identity_name}) could not be routed "
             f"automatically: {reason}. Raw message: "
             f"{(ticket.get('raw_text') or '')[:300]}",
        delivery_status="ready", kind=_a.KIND_ESCALATION,
        meta={"identity": identity_name, "surface": "portal_ticket_bridge"})
    log(f"[ticket-worker/{identity_name}] escalated ticket={tid} reason={reason}")


def intake_pass(bus, *, slack_lookup_email, account_key_for_gym, open_group_dm,
               post_first_message, write_hold_notice, product=PRODUCT, source=SOURCE,
               identity_name="echo", fetch_state=None, llm=None, mark_message=None,
               claim_message=None, stamp_ticket=None, log=print):
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
        processed += 1
        # Row-first: the client's original words are recorded as an inbound message
        # before anything else touches this ticket -- the portal insert only wrote the
        # TICKET, not a support_messages row, unlike a Slack-sourced ticket.
        bus.record_inbound(ticket_id=tid, slack_event_id=None, slack_ts=None,
                           author_type="client", author_id=ticket.get("reporter") or "",
                           body=ticket.get("raw_text") or "",
                           meta={"surface": "portal_ticket_bridge"})

        who = resolve_client_identity(ticket, slack_lookup_email=slack_lookup_email,
                                      account_key_for_gym=account_key_for_gym, log=log)
        if who.kind != _ig.CLIENT:
            _escalate_unresolved(bus, ticket, reason=f"identity_{who.kind}",
                                identity_name=identity_name, log=log)
            continue

        # Persisted so fixed_pass (a later poll, possibly after a redeploy) can
        # reconstruct who to notify without re-resolving.
        bus.set_ticket(tid, slack_user_id=who.slack_user_id)

        classification = _cls.classify(ticket.get("raw_text") or "", has_open_ticket=False,
                                       identity_product=ident.product, llm=llm)

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
                result = _out.initiate(
                    _verified_ticket_dict(ticket), who, ident,
                    open_group_dm=open_group_dm, post_first_message=post_first_message,
                    record_outbound=bus.record_outbound, stamp_ticket=stamp_ticket,
                    message_text=answer["body"], mark_message=mark_message,
                    claim_message=claim_message, log=log)
                if result.opened:
                    bus.set_ticket(tid, status="resolved")
                else:
                    log(f"[echo-ticket-worker] outreach refused ticket={tid} "
                        f"reason={result.reason}")
            else:
                _escalate_unresolved(bus, ticket, reason="question_not_groundable",
                                    identity_name=identity_name, log=log)
            continue

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
            continue

        _escalate_unresolved(bus, ticket, identity_name=identity_name,
                            reason=f"classification_{classification or 'none'}", log=log)
    return {"processed": processed}


def _fix_summary_text(verification):
    """Plain language, no dashes (same voice rule outreach.py's templates follow)."""
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
    again next cycle. Never runs if the config flag is off."""
    if not config.portal_echo_tickets_enabled():
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
                                identity_name=identity_name, log=log)
            continue
        result = _out.initiate(
            _verified_ticket_dict(ticket), who, ident,
            open_group_dm=open_group_dm, post_first_message=post_first_message,
            record_outbound=bus.record_outbound, stamp_ticket=stamp_ticket,
            message_text=_fix_summary_text(verification), mark_message=mark_message,
            claim_message=claim_message, log=log)
        if result.opened:
            bus.set_ticket(tid, status="resolved")
            notified += 1
        else:
            log(f"[ticket-worker/{identity_name}] fixed-pass outreach refused "
                f"ticket={tid} reason={result.reason}")
    return {"notified": notified}
