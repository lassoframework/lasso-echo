"""
echo_ticket_worker.py -- D46 (Blake, 2026-09-04): the bridge from a portal-submitted
Echo support ticket to the real pipeline.

Ground truth (Blake's own words): "with echo if someone submits a support echo should
receive that then echo should fix it verify the fix and then send slack message with
them and me in the message." This module receives and classifies the portal
ticket; the actual code fix is owned by Scout's FIXER worker. The original
fixer_request remains held for an internal audit trail. Blake's later
2026-09-18 portal-intake direction permits Scout to verify that authenticated
bridge record and queue this narrow code-fix source autonomously. Scout owns
that fix and customer handoff; this module's legacy fixed_pass currently
refuses code-fix notification because it has no registered fix-verdict writer.
Customer contact still waits for a verified live fix with Blake present.

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
  2. fixed_pass(): legacy verification consumer; currently refuses code-fix
     notification because FIX_VERIFICATION_PRODUCERS is empty. Scout's newer
     portal handoff owns its post-deploy customer notice.

Both passes are pure given their injected dependencies -- no import of a live Slack
client or the live bus at module scope, so they are fully unit-testable offline.
"""
import json
import os
import time

from . import config
from .slack_convo import adapter as _a
from .slack_convo import classifier as _cls
from .slack_convo import identities as _ids
from .slack_convo import identity_gate as _ig
from .slack_convo import outbox as _ob
from .slack_convo import outreach as _out


# D47 (Blake, 2026-09-04): generalized from Echo-only to any (product, identity) pair
# routed through the identity map -- portal tickets (product='portal', the generic
# Website tab form's default) route to Scout, not Ranger's ad-engine-specific
# fixer-lane.ts, which never had a reason to see a non-ranger ticket in the first
# place. Every call site defaults to Echo so existing behavior and tests are
# unchanged; a caller wiring a second (product, identity) pair passes them explicitly.
PRODUCT = "echo"
SOURCE = "website_tab"
_INTAKE_PAGE_LIMIT = 20
_INTAKE_MAX_SWEEP_SECONDS = 24 * 60 * 60
_intake_now = time.time


# ---------------------------------------------------------------------------
# Intake keyset cursor (2026-09-23 starvation fix)
#
# The old poll re-read the same oldest-20 window every pass, so 20 permanently-failing
# (or probe/test) rows starved every fresh customer ticket behind them. The cursor below
# is what makes the window BOUNDED AND FAIR: bus.find_new_tickets(after=cursor) walks
# strictly FORWARD through the unclassified queue in (created_at, id) order -- keyset,
# never OFFSET, so a concurrent insert can never shift the page and silently skip a row --
# and the cursor is advanced past EVERY raw row of the page, processed or not. A ticket
# that throws stays 'new' and is retried on the next sweep, after the tickets behind it
# have had their turn; it no longer monopolises the window.
#
# Restart behaviour: the cursor is persisted atomically (tmp + os.replace) under
# config.data_dir() -- the same durable volume db.db_path uses -- keyed per
# (product, source, identity) leg, so the portal->scout and echo->echo legs paginate
# independently and a redeploy resumes where the last pass left off instead of restarting
# at the poison block. A crash mid-pass replays at most the current page: rows that
# SUCCEEDED changed status/classification and are never re-fetched, and the inbound-row
# guard in _intake_one prevents a duplicated message for a row that failed later, so a
# replay can never double-process a successful ticket. A missing or corrupt cursor file
# is a cold start from the top of the queue -- safe for the same reason, and fail-closed:
# it can re-attempt work, never fabricate a delivery.
# ---------------------------------------------------------------------------

# Last known cursor state per configured path. This keeps pagination fair while the
# durable volume is temporarily unwritable; it is deliberately process-local and never
# changes any ticket/send safety decision.
_INTAKE_CURSOR_CACHE = {}


def _intake_cursor_path():
    return os.path.join(config.data_dir(), "echo_intake_cursor.json")


def _intake_leg_key(product, source, identity_name):
    return f"{product}|{source}|{identity_name}"


def _load_intake_cursors(path):
    cache_key = os.path.abspath(path)
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001 - missing/corrupt state is a safe cold start
        data = {}
    # A malformed leg is discarded independently so it cannot reset another leg.
    data = {key: value for key, value in data.items()
            if isinstance(value, dict)
            and isinstance(value.get("created_at"), str) and value["created_at"]
            and isinstance(value.get("id"), str) and value["id"]}
    # If persistence failed earlier in this process, its newer state wins over the
    # older on-disk snapshot. Valid untouched legs from disk remain available.
    data.update(_INTAKE_CURSOR_CACHE.get(cache_key, {}))
    _INTAKE_CURSOR_CACHE[cache_key] = dict(data)
    return data


def _save_intake_cursors(path, cursors, *, log=print):
    cache_key = os.path.abspath(path)
    safe_cursors = {key: value for key, value in cursors.items()
                    if isinstance(value, dict)
                    and isinstance(value.get("created_at"), str) and value["created_at"]
                    and isinstance(value.get("id"), str) and value["id"]}
    # Record first: even a write or atomic replace failure must not restart the
    # in-process queue at the oldest page on the next poll.
    _INTAKE_CURSOR_CACHE[cache_key] = dict(safe_cursors)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(safe_cursors, f)
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - memory fallback is safe; operator must see loss of durability
        log(f"[echo-ticket-worker] cursor persistence failed path={path}; "
            f"using process memory: {type(exc).__name__}: {exc}")


def _fetch_intake_page(bus, *, product, source, cursor):
    """One bounded page of new tickets, keyset-forward of `cursor` when one exists.
    `after` is only passed when set: older bus fakes with the pre-pagination signature
    keep working unchanged."""
    kw = {"product": product, "source": source}
    if cursor:
        kw["after"] = {"created_at": cursor["created_at"], "id": cursor["id"]}
    return bus.find_new_tickets(**kw)


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
    # Round 3 (audit of PR #107): classification is cleared explicitly. The QUESTION branch
    # stamps answerable_question before an undelivered answer lands here, and the FIXER's
    # poll (hold + escalated + classification NULL) would skip that ticket forever.
    bus.set_ticket(tid, status="hold", escalated=True, classification=None)
    bus.record_outbound(
        ticket_id=tid, author_type="system",
        body=f"Portal ticket {tid} ({identity_name}) could not be routed "
             f"automatically: {reason}. Raw message: "
             f"{(ticket.get('raw_text') or '')[:300]}",
        delivery_status="ready", kind=_a.KIND_ESCALATION,
        meta={"identity": identity_name, "surface": "portal_ticket_bridge"})
    # Portal incidents stay internal until the fix is merged, deployed and verified,
    # and Blake is included in the eventual customer conversation. An ACK here can
    # immediately open a group DM or queue a portal-thread message.
    log(f"[ticket-worker/{identity_name}] escalated ticket={tid} reason={reason}")


def intake_pass(bus, *, slack_lookup_email, slack_user_info, portal_lookup, open_group_dm,
               post_first_message, write_hold_notice, product=PRODUCT, source=SOURCE,
               identity_name="echo", operator_ids=(), fetch_state=None, llm=None,
               classify_llm=None, mark_message=None, claim_message=None, stamp_ticket=None,
               cursor_path=None, log=print):
    """First pass: NEW, unclassified tickets for (product, source), dispatched under
    identity_name. Never runs if the config flag is off. Defaults preserve the
    original Echo-only behavior; D47 generalized this for a second (product,
    identity) pair (portal -> scout) without touching Echo's call site.

    2026-09-23 starvation fix: the page is one keyset-forward WINDOW (see the cursor
    note above), not the permanent oldest-20. The persisted cursor advances past every
    raw row of the page -- failed tickets included -- so a poison block is walked past
    and a fresh ticket behind it is reached within ceil(queue_depth / page_size) polls;
    when a poll past the tail returns nothing, the cursor wraps to the top and the
    still-'new' failures get their next attempt."""
    if not config.portal_echo_tickets_enabled():
        return {"processed": 0}
    ident = _ids.IDENTITIES[identity_name]
    path = cursor_path or _intake_cursor_path()
    leg = _intake_leg_key(product, source, identity_name)
    cursors = _load_intake_cursors(path)
    cursor = cursors.get(leg) or None
    now = _intake_now()
    sweep_started_at = (cursor.get("_sweep_started_at")
                        if cursor and isinstance(cursor.get("_sweep_started_at"), (int, float))
                        else now)
    if cursor and now - sweep_started_at >= _INTAKE_MAX_SWEEP_SECONDS:
        # Under a sustained full queue the tail may never arrive. Periodically begin a
        # new sweep so old failed rows get another chance without pinning the worker.
        cursor = None
        sweep_started_at = now
    tickets = _fetch_intake_page(bus, product=product, source=source, cursor=cursor)
    raw_last = getattr(tickets, "raw_last", None)
    raw_count = getattr(tickets, "raw_count", len(tickets))
    if raw_count == 0 and cursor:
        # The tail: nothing past the cursor. Wrap to the top so the tickets that failed
        # earlier in the sweep (still status='new') are retried.
        cursor = None
        sweep_started_at = now
        tickets = _fetch_intake_page(bus, product=product, source=source, cursor=None)
        raw_last = getattr(tickets, "raw_last", None)
        raw_count = getattr(tickets, "raw_count", len(tickets))
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
    # Advance the leg's cursor past the last RAW row of this page -- succeeded, failed
    # and locally test-filtered rows alike (raw_last comes from the unfiltered page, so
    # probe rows the strict filter removed still move the window forward). A plain-list
    # result from an older fake carries no page facts: the cursor simply stays put, which
    # is the pre-fix behaviour for those callers. Persisted once per pass; a crash before
    # this point replays at most the current page, which is safe (see the cursor note).
    new_cursor = None
    if isinstance(raw_last, dict) and raw_last.get("created_at") and raw_last.get("id"):
        new_cursor = {"created_at": str(raw_last["created_at"]), "id": str(raw_last["id"]),
                      "_sweep_started_at": sweep_started_at}
    # A short page reached the current tail even if a new row arrives before the next
    # poll. Clear the cursor now so failures earlier in the sweep are retried next time.
    if raw_count < _INTAKE_PAGE_LIMIT:
        new_cursor = None
    if new_cursor is not None:
        cursors[leg] = new_cursor
    else:
        cursors.pop(leg, None)
    if new_cursor is not None or leg in _load_intake_cursors(path):
        _save_intake_cursors(path, cursors, log=log)
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
            # Finding 2 (audit 3, CRITICAL): this used to call auto_answer_forbidden twice
            # and auto_answer_allowed NEVER -- so the allowlist, documented as THE primary
            # gate for unattended sending, was absent from the one path C2 was filed against.
            # "a member tweaked her back, what do we tell her?" posted to a client's group DM
            # with no tap. Both paths now call the SAME shared decision so they cannot drift
            # apart again.
            # D72 (2026-09-11): the verdict carries a tier, and a held answer is never
            # silent to the team -- hold_answer_for_team writes the team card and
            # escalates the ticket with the tier visible (needs_review clears the
            # classification so the FIXER's poll picks it up). Portal tickets suppress
            # its customer template until verification and Blake's handoff.
            verdict = _a.auto_answer_verdict(ticket.get("raw_text") or "", answer["body"])
            armed = (config.slack_convo_auto_answer_armed(identity_name)
                     and config.slack_convo_client_reply_armed(identity_name))
            if verdict.held or not armed:
                held_verdict = (verdict if verdict.held else
                                _a.AnswerVerdict(False, _a.HOLD_TIER_UNARMED,
                                                 "auto_answer_not_armed"))
                row = bus.record_outbound(
                    ticket_id=tid, author_type=identity_name, body=answer["body"],
                    delivery_status="held", kind=_a.KIND_ANSWER,
                    meta={"identity": identity_name, "recipient_kind": who.kind,
                          "surface": "portal_ticket_bridge",
                          "auto_answer_forbidden": bool(
                              verdict.tier == _a.HOLD_TIER_ORG_FLOOR),
                          "hold_tier": held_verdict.tier, "hold_rule": held_verdict.rule})
                fresh = bus.ticket(tid) or {"id": tid}
                _a.hold_answer_for_team(
                    bus, ticket=fresh, ident_name=identity_name, recipient_kind=who.kind,
                    user=who.slack_user_id or "", account_key=who.account_key,
                    surface="portal_ticket_bridge", body=answer["body"],
                    held_message_id=(row or {}).get("id"), verdict=held_verdict,
                    write_hold_notice_fn=write_hold_notice or None,
                    unarmed_flag=f"SLACK_CONVO_{identity_name.upper()}_AUTO_ANSWER",
                    client_notice=False, log=log)
                return
            result = _out.initiate(
                _verified_ticket_dict(ticket), who, ident,
                open_group_dm=open_group_dm, post_first_message=post_first_message,
                record_outbound=bus.record_outbound, stamp_ticket=stamp_ticket,
                message_text=answer["body"], mark_message=mark_message,
                claim_message=claim_message, log=log)
            if getattr(result, "delivered", False):
                # Round 2 (MAJOR 6): an answer that promised a PERSON will follow up must
                # not close the ticket -- it goes to the FIXER with the follow-up marker
                # instead (same disposition the Slack adapter and the outbox use).
                if _a.promises_human_follow_up(answer["body"]):
                    _a.route_follow_up_promise(bus, bus.ticket(tid) or {"id": tid},
                                               ident_name=identity_name, body=answer["body"],
                                               recipient_kind=who.kind,
                                               surface="portal_ticket_bridge", log=log)
                else:
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
        # Preserve the HELD fixer_request as durable internal evidence. Scout's
        # narrow authenticated portal bridge may independently verify it and
        # queue the original ticket without releasing this internal row.
        bus.set_ticket(tid, classification=_cls.CODE_FIX, status="fixing")
        # Customer contact for a code fix waits for merge, verified deployment,
        # and a conversation that includes Blake. The held internal request is
        # the durable intake signal; an early acknowledgement would violate that
        # customer-contact gate before the FIXER has changed anything.
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
_VERDICT_KEYS = ("verified", "ok", "success", "passed", "status", "result")
# An ALLOWLIST of affirmative verdicts, not a denylist of negative ones (2026-09-05 audit 3,
# CRITICAL). The first version asked "is this verdict one of five known false values?", so
# "not verified", "pending", "unverified", "could not verify", "0 of 3", "timeout" and
# "partial" all read as SUCCESS and the client was told "Fixed it and confirmed the change is
# live". That is the identical whack-a-mole shape M3 rejected for auto-answer, reused one
# file away for the single sentence that most directly lies to a client.
#
# This column is written by ops-fix-triage.js, a process in another repo. We do not get to
# assume its vocabulary. So: an explicit true value, or an explicit affirmative word, or we
# make no claim -- and a bare PR link is NOT a verification, because a PR can be open,
# closed, reverted or unmerged.
_AFFIRMATIVE = frozenset({"true", "yes", "y", "verified", "passed", "pass", "success",
                          "successful", "ok", "okay", "fixed", "confirmed", "done",
                          "complete", "completed", "green"})
_NEGATIVE_HINT = ("not ", "un", "fail", "error", "pending", "partial", "timeout", "could not",
                  "cannot", "skip", "0 of", "no ")


def verification_succeeded(verification):
    """True ONLY when the snapshot affirmatively says the fix was verified.

    THE PRODUCER IS IN ANOTHER REPO, AND THIS WAS WRITTEN WITHOUT READING IT (2026-09-05
    audit 3 wrote an allowlist of affirmative words; audit 4 went and looked). The real
    writer is ~/scout-listener src/index.js's runVerify, which resolves:

        {phase, exit_code, tail, at}

    -- a pytest exit code, no verdict word anywhere. So the allowlist matched NOTHING the
    producer actually writes, and fixed_pass could never fire for any real ticket while
    writing a #fixer card asserting the verification was not a success over one that PASSED.
    A guess about another process's vocabulary is not a contract; the shape below is read
    from that code.

    AND THEN AUDIT 5 READ THE SAME CODE ONE LINE FURTHER. That command is:

        bash -lc 'python3 -m pytest -q 2>&1 | tail -5 || true; echo "__EXIT__:$?"'

    `|| true` (and the fact that $? is the exit status of `tail`, not of pytest) means
    `exit_code` is **always 0**, for a passing suite and a failing one alike. So reading it as
    a verdict made verification_succeeded a constant True for the only producer there is --
    and "Fixed it and confirmed the change is live" would have gone to a paying client over a
    failing test suite. That is a worse failure than the inert one it replaced.

    The honest conclusion, and the one this function now implements: **that snapshot carries
    no verdict at all.** Not a pass, not a fail -- unreadable. We do not infer one from a
    field that is structurally incapable of expressing failure, and we do not parse the
    `tail` text either (that is the same guess wearing a different hat). A fix is announced
    to a client only when the snapshot says so EXPLICITLY, and until the ops-fix worker
    writes such a field, this path stays honest by staying quiet and telling a human why.
    See D63; wiring that field is a cross-repo change and Blake's call.

    Recognised:
      * an explicit True, or an affirmative word we recognise.
      * ALL present verdict keys must agree (audit 4, finding 10): first-present-key-wins let
        {"status": "completed", "result": "failed"} read as a success.
    Anything else is not a success, and an unrecognised snapshot is reported as exactly that
    rather than as a failure -- see fixed_pass, which no longer flips such a ticket out of
    the poll it needs to stay in."""
    if not isinstance(verification, dict) or not verification:
        return False
    # A non-zero exit code IS a definite failure (nothing else writes one, and if something
    # ever does, it means what it says). A zero one means nothing, for the reason above.
    if "exit_code" in verification:
        try:
            if int(verification["exit_code"]) != 0:
                return False
        except (TypeError, ValueError):
            return False
    verdicts = [verification[k] for k in _VERDICT_KEYS if k in verification]
    if not verdicts:
        return False
    return all(_is_affirmative(v) for v in verdicts)


def _is_affirmative(val):
    if val is True:
        return True
    if isinstance(val, str):
        v = val.strip().lower()
        if any(h in v for h in _NEGATIVE_HINT):
            return False
        return v in _AFFIRMATIVE
    return False


def verification_is_unreadable(verification):
    """True when we cannot tell what this snapshot means, as opposed to it saying failure.

    The difference matters: an unreadable snapshot must leave the ticket exactly where it is,
    still polled, and must not tell a human the verification failed.

    The ops-fix worker's {phase, exit_code, tail, at} with exit_code 0 lands here, because
    that zero cannot distinguish a pass from a failure (see verification_succeeded)."""
    if not isinstance(verification, dict) or not verification:
        return True
    if [k for k in _VERDICT_KEYS if k in verification]:
        return False
    try:
        if int(verification.get("exit_code", 0)) != 0:
            return False        # a definite failure, not an unreadable snapshot
    except (TypeError, ValueError):
        return True
    return True


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


STUCK_FIXING_HOURS = 24


class InertVerificationLane(RuntimeError):
    """Raised when the fix lane is asked to gate on a field nothing can populate.

    See D68. This exists so that "no producer is wired" is a LOUD, catchable condition
    rather than a `continue` that looks exactly like "nothing to verify yet"."""


# ---------------------------------------------------------------------------------
# D68 (2026-09-06): the fix lane's verification field refuses to be read as a pass.
#
# THE REGISTRY IS EMPTY ON PURPOSE. It is the single source of truth for "is there a
# process that can write a FIX verdict onto support_tickets.verification_after for the
# ticket this pass is watching". Today there is none: ~/scout-listener's ops-fix worker
# polls status='new', mints a BRAND NEW support_tickets row (src/fixer/intake.js), and
# writes its verification onto THAT row (src/fixer/store.js setVerificationAfter). The
# originating portal ticket's verification_after is NULL forever.
#
# Blake's ruling (2026-09-06): "Either wire the producer or make the field refuse to be
# read as a pass. An inert verification field is worse than no field."
#
# Two things made "refuse" the right half of that choice, both recorded in D68:
#
#   1. THE COLUMN IS OVERLOADED. The ANSWER lane writes its grounding snapshot to this
#      same column (answer_pass below, and slack_convo/adapter.py). That lane is wired and
#      correct and is NOT touched by any of this. But it means a non-NULL
#      verification_after has never, on this column, meant "a fix was verified" -- so
#      "populated" was never a safe proxy for "verified", even before the producer gap.
#
#   2. A verdict this pass cannot attribute to a known producer is a guess about another
#      repo's vocabulary, which is the exact failure mode D62/D63 are written about.
#
# So: a fix verification is only readable as a pass when it carries a `producer` this
# registry recognises. Filling this set is a deliberate act that says "I wired a producer
# and I checked what it writes" -- and test_echo_ticket_worker.py holds it to that in both
# directions (empty -> fixed_pass refuses; non-empty -> an unattributed snapshot is still
# refused). It is not a config flag and it is not settable from the environment.
FIX_VERIFICATION_PRODUCERS = frozenset()


def fix_verification_lane_is_wired():
    """True only when some process can actually write a fix verdict onto the ticket this
    pass polls. False today -- see FIX_VERIFICATION_PRODUCERS."""
    return bool(FIX_VERIFICATION_PRODUCERS)


def read_fix_verification(ticket):
    """The ONLY sanctioned way to read verification_after AS A FIX VERDICT.

    Refuses loudly instead of returning a falsy "not yet":

      * lane unwired  -> raises InertVerificationLane. There is no such thing as "not
        verified yet" when nothing can ever write the verdict, and returning None here is
        precisely the inert-but-plausible state Blake's ruling names as worse than no
        field at all.
      * wired, but the snapshot carries no `producer` this module recognises -> returns
        None. That is a genuine "not from a fix producer" (the answer lane's grounding
        snapshot lands on this same column), not a refusal.

    Does NOT decide whether the fix SUCCEEDED -- verification_succeeded() still owns that,
    unchanged. This function only decides whether the value is a fix verdict at all."""
    if not fix_verification_lane_is_wired():
        raise InertVerificationLane(
            "support_tickets.verification_after has no registered fix producer, so this "
            "field can never become true for a ticket in 'fixing' and must not be read as "
            "a pass. FIX_VERIFICATION_PRODUCERS is empty; see D68 in "
            "docs/slack_convo/DECISIONS.md. Wiring the producer (ops-fix writing back to "
            "the ORIGINATING ticket rather than the row it mints) is a cross-repo change "
            "and Blake's call.")
    verification = ticket.get("verification_after")
    if not isinstance(verification, dict) or not verification:
        return None
    if verification.get("producer") not in FIX_VERIFICATION_PRODUCERS:
        return None
    return verification


def _report_stuck_fixing(bus, tickets, *, identity_name, log=print):
    """One honest card per stuck ticket per day. Never claims anything to the client."""
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    for ticket in tickets or []:
        if ticket.get("verification_after"):
            continue
        started = _parse_iso(ticket.get("created_at"))
        if started is None or (now - started).total_seconds() < STUCK_FIXING_HOURS * 3600:
            continue
        tid = ticket["id"]
        if _outbound_escalations_today(bus, tid) >= 1:
            continue
        # Audit 7, MAJOR 3: the first version asserted ONE cause as fact -- the cross-repo
        # wiring gap -- and told Blake to close the ticket by hand. In the ordinary case that
        # is simply wrong: _intake_one sets status='fixing' BEFORE writing the fixer_request
        # card, so a ticket whose card is still HELD awaiting a tap looks identical from the
        # ticket row alone. The right action there is to tap Release, and the card said the
        # opposite. It looks at the ticket's own rows now instead of guessing, and it does
        # not claim the client has heard nothing when an ack is sitting right there.
        # F2 (audit 8, MAJOR): three ways this still stated things it did not know.
        #   (a) an unreadable bus read left rows=[] and the card then asserted "was
        #       dispatched" and "close this by hand" -- the wrong remedy, stated as fact;
        #   (b) only delivery_status=='held' counted as not-dispatched, so a request sitting
        #       in ready/failed/suppressed (failed is directly reachable when the ops-fix
        #       channel is unset) was asserted dispatched;
        #   (c) `acked` ignored delivery_status, so a HELD ack produced "the client has an
        #       acknowledgement" when the client had nothing.
        # When we cannot tell, the card now says we cannot tell.
        # Audit 9, MINOR: reading only the NEWEST 200 rows (MINOR-1's own fix) put the
        # fixer_request -- written at ticket creation, so the OLDEST row -- out of range on a
        # long ticket, and the card then asserted "No fix request was ever written for it,
        # which should be impossible on this path". Untrue at the moment it is written. Both
        # ends are read, because the two rows this card reasons about live at opposite ends
        # of the ticket: the request at the start, the acknowledgement anywhere after.
        def _read(fn_name):
            fn = getattr(bus, fn_name, None)
            if fn is None:
                return None
            try:
                return fn(tid, limit=200) or []
            except Exception:  # noqa: BLE001
                return None

        newest = _read("recent_messages")
        oldest = _read("messages")
        if newest is None and oldest is None:
            rows = None
        else:
            seen = set()
            rows = []
            for m in list(newest or []) + list(oldest or []):
                key = m.get("id") or id(m)
                if key not in seen:
                    seen.add(key)
                    rows.append(m)

        if rows is None:
            cause = ("Its rows could not be read just now, so this card cannot say whether "
                     "the fix request was ever dispatched. Open the ticket before acting on "
                     "it.")
            client_state = "Whether the client has heard anything is unknown from here."
        else:
            def _kind(k):
                return [m for m in rows
                        if (m.get("attachments") or {}).get("kind") == k]

            requests = _kind(_a.KIND_FIXER_REQUEST)
            dispatched = [m for m in requests if m.get("delivery_status") == "posted"]
            undispatched = [m for m in requests if m.get("delivery_status") != "posted"]
            acked = [m for m in (_kind(_a.KIND_ACK) + _kind(_a.KIND_TEMPLATE))
                     if m.get("delivery_status") == "posted"]
            if not requests:
                cause = ("No fix request was ever written for it, which should be "
                         "impossible on this path. This one needs eyes on the ticket "
                         "itself.")
            elif undispatched and not dispatched:
                states = ", ".join(sorted({str(m.get("delivery_status") or "?")
                                           for m in undispatched}))
                cause = (f"Its fix request never reached the worker: the row is "
                         f"'{states}'. If it is held, the Release card in #fixer is what "
                         f"moves this; if it failed, check that the ops-fix channel is set.")
            else:
                cause = ("Its fix request was dispatched, and no verification has been "
                         "written back to THIS row. Known cause: the ops-fix worker mints "
                         "its own source='ops_fix' ticket and writes the verification "
                         "there, so this pass cannot see it. Check the fix and close this "
                         "by hand, or wire the worker to write verification_after back to "
                         "the originating ticket.")
            client_state = ("The client has an acknowledgement but has heard nothing since."
                            if acked else "The client has been told nothing at all.")
        try:
            bus.record_outbound(
                ticket_id=tid, author_type="system",
                body=(f"Ticket {tid} ({identity_name}) has been in 'fixing' for over "
                      f"{STUCK_FIXING_HOURS}h with no verification on it. {cause} "
                      f"{client_state}"),
                delivery_status="ready", kind=_a.KIND_ESCALATION,
                meta={"identity": identity_name, "surface": "portal_ticket_bridge",
                      "stuck_in_fixing": True, "rows_readable": rows is not None})
        except Exception as e:  # noqa: BLE001 - a report failure never breaks the pass
            log(f"[ticket-worker/{identity_name}] stuck-report failed ticket={tid}: "
                f"{type(e).__name__}")


def _parse_iso(value):
    from datetime import datetime as _dt, timezone as _tz
    try:
        d = _dt.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_tz.utc)
    except Exception:  # noqa: BLE001
        return None


def _outbound_escalations_today(bus, tid):
    """Escalation cards written today on this ticket, NOT counting receipts.

    Audit 5, finding 7: receipts ride on kind='escalation' (the C3 workaround for the
    portal's denylist), so a plain count of that kind meant one receipt permanently
    suppressed the unreadable-snapshot card and the ticket looped in 'fixing' with nobody
    ever told. The rows are distinguishable by attachments.receipt; this counts what a human
    would actually call an escalation."""
    from datetime import datetime as _dt, timezone as _tz
    start = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    # Audit 6, finding 3: the first version of this scanned bus.messages(tid, limit=200),
    # which is ordered created_at.asc -- the OLDEST 200 rows. On a long ticket today's
    # escalation is outside that window, the count returns 0, and the one-card-per-day bound
    # silently disappears. That is verbatim the bug count_outbound_kind_since exists to
    # prevent, reintroduced by hand to filter out receipts. Filtered server-side now.
    try:
        return bus.count_escalation_cards_since(tid, start)
    except AttributeError:
        pass
    except Exception:  # noqa: BLE001 - fail closed: assume we already said it
        return 10 ** 9
    try:
        rows = bus.messages(tid, limit=200)
    except Exception:  # noqa: BLE001
        return 10 ** 9
    return sum(1 for m in rows or []
               if m.get("direction") == "outbound"
               and (m.get("attachments") or {}).get("kind") == _a.KIND_ESCALATION
               and not (m.get("attachments") or {}).get("receipt")
               and str(m.get("created_at") or "") >= start)


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
    # AUDIT 6, FINDING 2 -- THE THING THIS PASS CANNOT DO, SAID OUT LOUD.
    #
    # This pass polls status='fixing' and waits for verification_after to appear on the
    # ticket. Reading ~/scout-listener one step further than D63 did: the ops-fix worker
    # polls status='new' and, via runTriage -> opsFixIntake.fromOpsFix -> store.insertNew,
    # mints a BRAND NEW support_tickets row (src/index.js:334, src/fixer/intake.js:100). Its
    # verification lands on THAT row. The ticket this pass is watching keeps
    # verification_after NULL forever, so the loop below short-circuits on every cycle and no
    # client is ever told their fix was verified.
    #
    # That is a cross-repo wiring gap, not something to invent a workaround for here (writing
    # our own verdict would be exactly the guessing D62 and D63 were written about). What
    # this code CAN do is refuse to be silently inert -- D56's whole lesson -- so a ticket
    # that has waited too long says so, once per ticket per day, in plain words.
    _report_stuck_fixing(bus, tickets, identity_name=identity_name, log=log)

    # D68 (2026-09-06), Blake's ruling: "Either wire the producer or make the field refuse
    # to be read as a pass. An inert verification field is worse than no field."
    #
    # The loop below used to open with `if not ticket.get("verification_after"): continue`.
    # That line is indistinguishable, at every call site and in every log, from the healthy
    # state "this fix just is not verified YET" -- while in fact NOTHING can ever populate
    # it (see FIX_VERIFICATION_PRODUCERS). A reader of this function, of its metrics, or of
    # its return value could not tell "waiting" from "impossible". That is the exact failure
    # this ruling names.
    #
    # So the pass no longer pretends to poll a gate that cannot open. It refuses, by name,
    # every cycle, and says so in the return value so a caller or a metric can see it too.
    # _report_stuck_fixing above still runs FIRST, so the humans who have tickets sitting in
    # 'fixing' keep getting told about them -- refusing to claim a fix is not refusing to
    # report one.
    if not fix_verification_lane_is_wired():
        if tickets:
            log(f"[ticket-worker/{identity_name}] REFUSING the fix lane: "
                f"{len(tickets)} ticket(s) in 'fixing' and "
                f"support_tickets.verification_after has no registered fix producer, so no "
                f"fix can ever be confirmed to a client from here. This is not 'not yet'. "
                f"See D68 in docs/slack_convo/DECISIONS.md.")
        return {"notified": 0, "refused": "fix_verification_lane_unwired",
                "fixing": len(tickets or [])}

    for ticket in tickets:
        tid = ticket["id"]
        # Not `.get("verification_after")`: a value that did not come from a registered fix
        # producer is not a fix verdict, whatever else it is. The ANSWER lane writes its
        # grounding snapshot to this same column, and that lane is wired, correct, and
        # untouched by any of this -- but it means "populated" has never meant "verified".
        verification = read_fix_verification(ticket)
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
        if summary is None and verification_is_unreadable(verification):
            # Audit 4, finding 1: an unreadable snapshot used to be escalated as
            # "verification_not_a_success" AND flipped to status='hold', which removes the
            # ticket from find_fixing_tickets forever -- so a fix that later verified could
            # never notify anyone. Left in 'fixing' (still polled), reported honestly, and
            # bounded to one card per ticket per day so it cannot flood.
            if _outbound_escalations_today(bus, tid) < 1:
                bus.record_outbound(
                    ticket_id=tid, author_type="system",
                    body=(f"Ticket {tid} ({identity_name}) has a verification snapshot this "
                          f"worker cannot read, so nothing was claimed to the client and the "
                          f"ticket is LEFT IN 'fixing' and still polled. Snapshot keys: "
                          f"{sorted(verification.keys()) if isinstance(verification, dict) else type(verification).__name__}. "
                          f"Expected either exit_code (the ops-fix worker's shape) or a "
                          f"verdict field."),
                    delivery_status="ready", kind=_a.KIND_ESCALATION,
                    meta={"identity": identity_name, "surface": "portal_ticket_bridge"})
            continue
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
