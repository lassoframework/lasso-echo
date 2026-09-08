"""
lane.py — the trigger surface and the per-ticket decision, end to end.

    arm -> poll -> [per ticket] resolve key -> route -> probe -> match condition ->
    foundation gate -> act -> re-probe -> verify -> compose -> deliver + CARD A HUMAN

Every step can only ever move the outcome towards escalation. There is no branch that
turns a refusal back into a send, and the only producer of client-facing text anywhere
in this capability is conditions.compose().

A REPLY IS NOT A CLAIM THAT THE MESSAGE WAS HANDLED.
Every auto-reply ALSO writes an internal card carrying the client's own words and
Echo's reply, saying plainly that nothing else in the message was read. That is the
round-4 conclusion of the previous build and it is the right one: it means a widened
keyword belt is never the thing standing between a client and a wrong outcome, because
a human sees every message this lane touches either way.

ON THE ROUTER, WHICH IS NOT A SAFETY GATE.
route() reads the client's words to pick WHICH probe to run. That is a keyword map --
an enumeration over an open set, the shape D68 says must never be a safety control. It
is not one here, and the asymmetry is what makes that true:

    route() -> None                 -> ESCALATE
    route() -> more than one family -> ESCALATE (ambiguity is never resolved by order)
    route() -> the wrong family     -> that probe's readings will not match any
                                       condition, or will not verify, or will not
                                       ground a sentence -> ESCALATE
    route() -> the right family     -> safety still comes entirely from the readings

A phrasing the router misses costs a human a ticket; it never costs a client a wrong
autonomous action.

WHAT THE POLL ACTUALLY LOOKS FOR, measured rather than assumed.
Read-only against the production project on 2026-09-07:

  * every non-test client-originated ticket carries identity_kind='client' and a
    non-null slack_channel_id;
  * ALL THREE of them have source='website_tab' (the portal bridge), NOT
    'slack_conversation'. Every slack_conversation row in the table is a phase-4
    arming probe with is_test=true;
  * they sit in status='hold' with classification NULL, which is what the adapter
    writes for a client DM whose answer was not grounded (adapter.py:847) and for a
    ticket the classifier did not decide (adapter.py:881).

The previous build polled source='slack_conversation' and statuses ('new','triage')
only, which matched ZERO real rows on both axes and returned {ok: True, handled: 0}
doing it -- byte-identical to a healthy idle pass. Both axes are now pinned as
constants and asserted against the live CHECK constraint by
tests/test_client_dm_lane.py.

'verification' is DELIBERATELY EXCLUDED, and the reason is recorded rather than left
to be rediscovered: that status means the adapter already drafted an answer on the
D67-locked auto-answer lane, held for a human tap. Replying here too would give the
client two messages about one question. That lane is a separate, still-locked decision
and this one does not reach into it.

ONE PREDICATE, NOT TWO. The poll's stopping rule and the loop's iteration use the SAME
`_actionable` function over the SAME rows. The previous build had a stopping rule that
counted one thing and a loop that discarded rows for two other reasons, so fifty
undiscardable rows ended paging on page one and the pass reported a clean zero. There
is no longer a category of row that satisfies the stopping rule and is then dropped:
everything past `_actionable` is acted on and counted.
"""
from __future__ import annotations

from . import arming as _arm
from . import conditions as _c
from . import no_ad_rail as _tripwire
from . import probes as _p
from . import readings as _r

DEFAULT_IDENTITY = "echo"
DEFAULT_PRODUCT = "echo"

# Pinned against the live support_tickets CHECK constraints (project
# ooqcvmcjspeltuuhcvlh, read 2026-09-07). Two-way guarded in tests.
POLL_SOURCES = ("slack_conversation", "website_tab")
POLL_STATUSES = ("new", "triage", "hold")
POLL_IDENTITY_KIND = "client"

POLL_PAGE = 50
POLL_MAX_PAGES = 10

# Marks a support_messages row as this lane's, for idempotency. Renaming it makes every
# historical record invisible and the lane re-replies once to every ticket it has ever
# answered, so it is pinned by a test.
LANE_META = "client_dm_lane"
LANE_NAME = "client_dm_support"

# Contracts of the EXISTING outbox. Getting either wrong silently drops the row.
DELIVERY_READY = "ready"
# The status outbox.run_once stamps once a row has ACTUALLY been posted into the
# person's Slack thread (outbox.py:358). A row still 'ready' or 'held' reached nobody.
DELIVERY_POSTED = "posted"

_TICKETS = "support_tickets"


# ---------------------------------------------------------------------------
# The router. A belt, never a control. See the module docstring.
# ---------------------------------------------------------------------------
ROUTES = (
    (_p.PROBE_DRIVE, (
        "photo", "photos", "picture", "pictures", "image", "images",
        "google drive", "drive folder", "my folder", "no media", "blank post",
    )),
    (_p.PROBE_CTA, (
        "call to action", "cta", "link in bio", "booking link",
    )),
)

# UNCONDITIONAL ESCALATION, checked first, before anything reads a fact. A belt over
# Blake's hard limits: nothing rests on its completeness (see no_ad_rail.py), and a
# phrasing it misses still cannot reach a wrong action, because this capability has
# exactly one action and it is a per-gym media sync.
ESCALATE_ALWAYS = (
    "budget", "spend", "spending", "cpl", "cost per lead", "ad set", "adset",
    "campaign", "targeting", "audience", "boost", "facebook ad", "meta ad",
    "instagram ad", "google ad", "paying for", "pause the ads", "turn off the ads",
    "invoice", "billing", "bill me", "charge", "charged", "refund", "credit card",
    "subscription", "plan", "price", "pricing", "pixel", "capi", "conversions api",
    "password", "api key", "token", "login", "hours", "class schedule", "injur",
    "liabilit", "lawyer", "cancel my account", "another gym", "other client",
    # MINOR (audit of PR #68): the auditor tried four plain ad-money phrasings with
    # no line above and got an on-topic-but-wrong reply about photos instead of an
    # escalation. Added, not to enumerate every future phrasing (see BELT_MISSES in
    # tests/test_client_dm_lane.py for why that race is never run to completion),
    # but because these four are ordinary ways a gym owner actually asks for more ad
    # spend, more reach, or narrower targeting -- not edge cases.
    "more money", "scale up", "scale us up", "promote", "our ads", "show our ads",
    "showing our ads",
)

ESCALATE_ALWAYS_REASON = (
    "the message names something on Blake's always-escalate list (ads and budget, "
    "billing, pixel/CAPI, credentials, gym hours or schedule, injury or liability, or "
    "another client). This lane never acts on those and never answers them."
)


def route(text):
    """The probe family this message is about, or None. Exactly one, or None."""
    low = str(text or "").lower()
    hits = [pid for pid, terms in ROUTES if any(t in low for t in terms)]
    return hits[0] if len(hits) == 1 else None


def hard_line(text):
    low = str(text or "").lower()
    return any(t in low for t in ESCALATE_ALWAYS)


# MINOR (audit of PR #68, John Weeks's real case): "yes let's include a call to
# action to book a free intro class" was re-asked ASKS["cta_needed"] despite
# already answering it. SUPERSEDED here rather than fixed with a phrase check: D70
# (docs/slack_convo/DECISIONS.md, merged as PR #73 while this fix was in flight)
# independently found and closed the SAME bug via a fact about the CONVERSATION
# (`_echo_already_asked` below) rather than about the client's WORDS -- exactly the
# right axis, and its own test (test_the_identical_message_on_a_fresh_thread_still_
# gets_the_ask) explicitly proves a phrase-matching version of this fix is wrong: an
# ask must still fire for John's identical words on a FRESH thread Echo has never
# spoken on. An earlier draft of this fix here was a phrase-matching version and
# broke exactly that test; removed in favor of D70's conversation-level fix.
#
# ---------------------------------------------------------------------------
# The per-ticket decision. Posts nothing; the caller delivers.
# ---------------------------------------------------------------------------
class Outcome:
    REPLY = "reply"
    ESCALATE = "escalate"


class Decision:
    __slots__ = ("outcome", "reason", "gym_key", "condition_id", "reply_text",
                 "client_text", "audit")

    def __init__(self, outcome, reason, *, gym_key="", condition_id="",
                 reply_text="", client_text="", audit=None):
        self.outcome = outcome
        self.reason = reason
        self.gym_key = gym_key
        self.condition_id = condition_id
        self.reply_text = reply_text
        self.client_text = client_text
        self.audit = dict(audit or {})

    @property
    def will_reply(self):
        return self.outcome == Outcome.REPLY and bool(self.reply_text)


def _escalate(reason, **kw):
    return Decision(Outcome.ESCALATE, reason, **kw)


def decide(*, text, gym_key, may_reply, deps=None, echo_already_asked=False):
    """Decide what to do about ONE client message. Never raises.

    `echo_already_asked` is a fact about the CONVERSATION, not about the words: True
    when Echo has already spoken on this thread and the client has written since. See
    the comment at its use below.
    """
    deps = dict(deps or {})
    try:
        return _decide(text=text, gym_key=gym_key, may_reply=may_reply, deps=deps,
                       echo_already_asked=echo_already_asked)
    except _c.Refused as e:
        return _escalate(str(e), client_text=str(text or ""))
    except Exception as e:  # noqa: BLE001 - one ticket never sinks the pass
        return _escalate(f"{type(e).__name__}: {e}", client_text=str(text or ""))


def _decide(*, text, gym_key, may_reply, deps, echo_already_asked=False):
    client_text = str(text or "")

    # 1. The hard-line belt. Unconditional, first, before anything reads a fact.
    if hard_line(client_text):
        return _escalate(ESCALATE_ALWAYS_REASON, client_text=client_text)

    # 2. The join key. A portal uuid raises rather than querying with it.
    try:
        key = _p.require_account_key(gym_key)
    except _p.ProbeError as e:
        return _escalate(f"could not use the gym's account key: {e}",
                         client_text=client_text)

    # 3. Route.
    probe_id = deps.get("probe_id") or route(client_text)
    if probe_id not in _p.ALL_PROBES:
        return _escalate(
            "no enumerated condition family covers this message, so there is no "
            "measurement to ground a reply in",
            gym_key=key, client_text=client_text)

    # 4. Probe (read-only).
    probe_kw = {k: deps[k] for k in ("store", "voice_dir", "read_text") if k in deps}
    before = _p.run(probe_id, key, stage="diagnosis", **probe_kw)

    # 5. Match a condition from the READINGS. None -> escalate.
    condition = _c.match(before)
    if condition is None:
        return _escalate(
            "the measured state does not match any enumerated condition for this "
            "family",
            gym_key=key, client_text=client_text,
            audit={"probe": probe_id, "readings": dict(before.values)})

    # 5b. AN ASK IS SPENT ONCE THE CLIENT HAS ANSWERED IT.
    #
    # Measured, not reasoned: this file's own `decide()` was run against John Weeks'
    # REAL brand bible (read from the production volume, 2026-09-07) and his REAL
    # latest message -- "Yes let's include a call to action to book a free intro class,
    # similar to what our paid ads are doing". It returned Outcome.REPLY and composed
    # "What would you like your posts to ask people to do?", which is the question he
    # had just answered. Every control passed, correctly: the reading is right, the
    # condition genuinely applies, and the reply is faithfully grounded.
    #
    # The gap is that `_already_handled` asks "has THIS LANE written here", and Echo's
    # ORDINARY reply path is what asked him. So on a thread where Echo has spoken and
    # the client has answered, a lane that has never itself spoken sees a fresh ticket.
    #
    # The tempting fix is to read his words and notice they are an answer. That is
    # classifying the QUESTION over an open input space -- D67's failure, exactly, and
    # this package exists because that does not work. The closed version is a fact
    # about the CONVERSATION, which this codebase owns and can count.
    #
    # Scoped to ACTION-LESS conditions on purpose. A condition with an action CHANGES
    # something; running the Drive sync for a gym Echo has spoken to before is still
    # the right thing to do. It is only the ask/tell-only outcomes -- cta_needed,
    # drive_reshare -- that have nothing left to say once the client has responded.
    if echo_already_asked and not condition.action:
        return _escalate(
            "Echo has already spoken on this thread and the client has written since, "
            "so an ask-only outcome has nothing left to ask; a human should read what "
            "they said",
            gym_key=key, condition_id=condition.id, client_text=client_text,
            audit={"probe": probe_id, "readings": dict(before.values)})

    # 6. Act, or do not. Either way the reply is grounded in what was measured after.
    if condition.action:
        action = _c.foundation_gate(condition.action)     # Blake's hard limits
        run_kw = {k: deps[k] for k in ("store", "log", "sync_source") if k in deps}
        run_values = action.run(key, **run_kw)
        after = _p.run(probe_id, key, stage="verification", **probe_kw)
        note = condition.expect.check(before, after)      # refuses if it did not move
        snapshot = after.with_run_values(run_values)
    else:
        # A condition with no action has nothing to verify. It does NOT get a free
        # pass: the registry refuses an action without an expectation, and compose()
        # refuses an action-bearing condition composed off a diagnosis snapshot.
        snapshot = before
        note = "no write was performed; nothing to verify"

    if not may_reply:
        return _escalate(
            "this lane is not armed to reply to clients, so the diagnosis goes to a "
            "human instead",
            gym_key=key, condition_id=condition.id, client_text=client_text,
            audit={"probe": probe_id, "readings": dict(snapshot.values),
                   "verification": note})

    # 7. THE GROUNDED-REPLY GATE.
    reply_text, audit = _c.compose(condition, snapshot)
    audit["verification"] = note
    return Decision(
        Outcome.REPLY,
        f"grounded in {len(condition.report)} measured reading(s)",
        gym_key=key, condition_id=condition.id, reply_text=reply_text,
        client_text=client_text, audit=audit)


# ---------------------------------------------------------------------------
# The poll, and the pass.
# ---------------------------------------------------------------------------
def default_bus():
    from ..slack_convo.bus import Bus
    return Bus()


def default_identity(name=DEFAULT_IDENTITY):
    from ..slack_convo import identities as _ids
    return _ids.get(name)


def _kinds():
    from ..slack_convo import adapter as _a
    return _a.KIND_STATUS, _a.KIND_ESCALATION


def _already_handled(bus, ticket_id):
    """Has this lane already written a row against this ticket? One extra read per
    candidate, and the cost is stated rather than hidden: the alternative is a new
    column, and a schema change is a foundation trigger."""
    for m in (bus.recent_messages(ticket_id, limit=200) or []):
        att = m.get("attachments") or {}
        if isinstance(att, dict) and att.get(LANE_META):
            return True
    return False


def _echo_already_asked(bus, ticket_id):
    """Has ANY Echo-side message already gone out on this thread, BEFORE the client's
    latest words?

    ANY -- deliberately broader than `_already_handled`, which asks only about rows
    carrying this lane's own LANE_META stamp. That difference is the whole finding:
    John Weeks was asked for his CTAs by Echo's ORDINARY reply path, answered, and this
    lane -- never having written on that thread -- saw a fresh ticket and planned the
    same question again.

    WHAT COUNTS AS "ECHO SPOKE", AND WHY IT IS NOT author_type.
    The first version of this predicate counted any outbound row whose author_type was
    not "staff". That was guessed, and live production data (2026-09-07) falsifies it:
    support_messages holds `author_type='system'` rows for `escalation`, `hold_notice`
    and `fixer_request` -- INTERNAL cards delivered to the fixer channel, which the gym
    owner never sees -- and several carry `attachments.recipient_kind='client'`, so
    recipient_kind is no discriminator either. Counting one as "Echo spoke" would
    suppress a legitimate FIRST ask on any ticket that had merely been escalated
    internally, which is most of them: over-refusal, silently.

    The repo already maintains the closed set this needs, so it is READ rather than
    re-derived. adapter.INTERNAL_KINDS ({escalation, fixer_request, hold_notice}) go to
    the fixer channel and never into the person's thread; adapter.CONVERSATIONAL_KINDS
    ({ack, answer, template, status}) are the client-facing ones, and `kind` rides in
    attachments (bus.record_outbound:253). Membership is tested against
    CONVERSATIONAL_KINDS -- an ALLOWLIST -- and not against "not internal", because
    adapter.py's own comment records that the portal decides client visibility by a
    DENYLIST, so a new internal kind is client-visible by default over there. This side
    refuses by default instead.

    DELIVERY IS REQUIRED. A `held`, `ready` or `failed` row never reached the gym owner,
    so it cannot have consumed an ask.

    `bus.recent_messages` orders created_at DESC (bus.py:273), so this walks newest
    first and looks for a client-visible, delivered outbound sitting BEHIND at least one
    gym-side message. Returns False on a read failure: absence of evidence is not
    evidence of a prior ask, and failing closed here would mute the lane on every
    Supabase blip.
    """
    from ..slack_convo import adapter as _a

    try:
        msgs = bus.recent_messages(ticket_id, limit=200) or []
    except Exception:  # noqa: BLE001 - a read failure is not evidence of a prior ask
        return False
    seen_gym_side = False
    for m in msgs:
        direction = str(m.get("direction") or "")
        if direction == "inbound":
            # THE GYM SIDE IS {client, coach}; `staff` is LASSO. Read from
            # adapter.author_type_for, whose whole range is {staff, coach, client}
            # (adapter.py:550), over identity_gate's kinds: a gym OWNER resolves to
            # CLIENT and a gym's COACH to COACH, while STAFF is Blake and the team.
            # A coach answering Echo's question IS the gym answering it, so excluding
            # them would leave the very bug this predicate exists to fix.
            #
            # Written as "not staff" rather than an allowlist, deliberately and in the
            # direction that is safe HERE: an author type nobody anticipated counts as
            # the gym having spoken, which suppresses the ask and sends a human. The
            # opposite default would re-ask a paying client, which is the harm.
            if str(m.get("author_type") or "") != _a._ig.STAFF:   # noqa: SLF001
                seen_gym_side = True
            continue
        if direction != "outbound" or not seen_gym_side:
            continue
        att = m.get("attachments")
        att = att if isinstance(att, dict) else {}
        if str(att.get("kind") or "") not in _a.CONVERSATIONAL_KINDS:
            continue                      # an internal card the client never saw
        if str(m.get("delivery_status") or "") != DELIVERY_POSTED:
            continue                      # queued, held or failed: it never reached them
        return True
    return False


def _newest_client_message(bus, ticket):
    """The client's LATEST words, and the surface they arrived on.

    bus.recent_messages orders created_at DESC (bus.py:273). The previous build read
    this list forwards and therefore always saw the client's OLDEST message, so a
    follow-up -- including "forget the photos, can you double my ad budget?" -- was
    never seen by the hard-line belt at all.

    THE AUTHOR SET IS THE ADAPTER'S, NOT A LOCAL GUESS. This read
    ("client", "user", "human", "") and contained two values production never writes
    while omitting one it does. adapter.author_type_for's whole range is
    {staff, coach, client} (adapter.py:550): a gym OWNER resolves to CLIENT, a gym's
    COACH to COACH, and STAFF is Blake and the LASSO team. So a thread whose newest
    message came from a COACH skipped it entirely and fell through to
    `ticket.raw_text` -- the ORIGINAL message -- which is the same "decided on the
    client's oldest words" defect this docstring says was fixed, arriving through the
    author filter instead of through the ordering.
    """
    from ..slack_convo import adapter as _a

    for m in (bus.recent_messages(ticket["id"], limit=200) or []):
        if str(m.get("direction") or "") != "inbound":
            continue
        if str(m.get("author_type") or "") == _a._ig.STAFF:    # noqa: SLF001
            continue                    # LASSO talking in the thread, not the gym
        att = m.get("attachments") or {}
        surface = att.get("surface") if isinstance(att, dict) else ""
        return str(m.get("body") or ""), str(surface or "")
    return str(ticket.get("raw_text") or ""), ""


def _actionable(bus, ticket):
    """THE ONE PREDICATE. Used by the poll's stopping rule AND by the loop."""
    if str(ticket.get("identity_kind") or "") != POLL_IDENTITY_KIND:
        return False
    if not ticket.get("slack_channel_id"):
        return False
    return not _already_handled(bus, ticket["id"])


def poll(bus, *, product=DEFAULT_PRODUCT, limit=5):
    """This lane's own ticket poll, paged past rows it has already handled.

    Deliberately NOT bus.find_new_tickets: that also requires classification is.null,
    which is the portal worker's "nobody has picked this up" predicate and excludes
    every classified client DM. Same table, same transport, same test-row exclusion --
    only the predicate differs.

    Returns (tickets, capped) where `capped` is True when the paging ceiling was hit
    with the quota unmet, which is an ops condition, not an empty queue.
    """
    from ..slack_convo import testdata as _td
    out, offset = [], 0
    for _page in range(POLL_MAX_PAGES):
        rows = bus._get(_TICKETS, {                                   # noqa: SLF001
            "product": f"eq.{product}",
            "source": f"in.({','.join(POLL_SOURCES)})",
            "status": f"in.({','.join(POLL_STATUSES)})",
            "select": "*",
            "order": "created_at.asc",
            "limit": str(POLL_PAGE),
            "offset": str(offset),
        }) or []
        for t in _td.exclude_test_strict(rows):
            if _actionable(bus, t):
                out.append(t)
                if len(out) >= limit:
                    return out, False
        if len(rows) < POLL_PAGE:
            return out, False
        offset += POLL_PAGE
    return out, True


def _deliver(bus, ticket, identity, decision, arm, surface):
    """Write the client reply (when armed) and ALWAYS write the human card.

    Returns a dict of what was written. A delivery failure is never silence: the card
    is attempted independently, and an undelivered reply is reported explicitly.
    """
    kind_status, kind_escalation = _kinds()
    wrote = {"reply": False, "card": False, "undelivered": 0}

    if decision.will_reply and arm.may_reply_to_clients:
        try:
            bus.record_outbound(
                ticket_id=ticket["id"], author_type=identity.name,
                body=decision.reply_text, delivery_status=DELIVERY_READY,
                kind=kind_status,
                # `identity` is REQUIRED: outbox._dispatch_one suppresses any row
                # carrying no identity stamp (outbox.py:325).
                meta={"identity": identity.name, "recipient_kind": "client",
                      "surface": surface, LANE_META: LANE_NAME,
                      "condition_id": decision.condition_id})
            wrote["reply"] = True
        except Exception as e:  # noqa: BLE001
            wrote["undelivered"] = 1
            decision.audit["delivery_error"] = f"{type(e).__name__}: {e}"

    try:
        bus.record_outbound(
            ticket_id=ticket["id"], author_type=identity.name,
            body=_card_text(ticket, decision, arm, wrote),
            delivery_status=DELIVERY_READY, kind=kind_escalation,
            meta={"identity": identity.name, LANE_META: LANE_NAME,
                  "condition_id": decision.condition_id})
        wrote["card"] = True
    except Exception as e:  # noqa: BLE001
        print(f"[client-dm] card for ticket {ticket.get('id')} failed: "
              f"{type(e).__name__}: {e}")
    return wrote


def _fenced(text, cap=600):
    """The client's own words on an INTERNAL card. Bounded and escaped so a folder
    name or a pasted message cannot forge Slack structure in a card a human reads."""
    s = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("```", "'''")
    if len(s) > cap:
        s = s[:cap] + " ..."
    return s


def _card_text(ticket, decision, arm, wrote):
    gym_key = decision.gym_key or ticket.get("client_id") or "?"
    # OWNER + GYM NAME (Blake, 2026-09-07): resolved server-side from gym_key (OUR
    # OWN gyms/clients rows), never anything the client typed or set themselves.
    # FENCED (independent audit, 2026-09-08): gyms.name/clients.name can originate
    # from a self-serve client's own onboarding input -- the two sibling call sites
    # in slack_convo/adapter.py already wrap this same lookup in _slack_escape; this
    # one didn't. _fenced is this file's own escaping helper, same guarantee.
    gym_line = f"gym: {gym_key}"
    try:
        from .. import gym_identity
        label = gym_identity.submitter_label_for(gym_key)
        if label:
            gym_line = f"{gym_line} ({_fenced(label, cap=200)})"
    except Exception:  # noqa: BLE001 - a lookup failure never blocks the card
        pass
    lines = [
        f"CLIENT DM SUPPORT ({LANE_NAME}) on ticket {ticket.get('id')}",
        gym_line,
        f"they wrote: {_fenced(decision.client_text)}",
    ]
    if decision.will_reply and wrote.get("reply"):
        lines.append(
            f"Echo QUEUED this reply (condition {decision.condition_id}): "
            f"{_fenced(decision.reply_text)}"
        )
        lines.append(
            "QUEUED, not sent: the outbox re-checks the flags at post time, and the "
            "receipt is the record of what was actually delivered."
        )
        lines.append(
            "Echo answered ONLY the part above. Nothing else in their message was read "
            "by this lane. Please read the whole message."
        )
    elif wrote.get("undelivered"):
        lines.append("A reply was composed and could NOT be written. Nobody has told "
                     "this client anything. Please reply by hand.")
    else:
        lines.append(f"Echo did NOT reply. Reason: {decision.reason}")
    if arm.mode != _arm.MODE_LIVE:
        lines.append(f"lane mode: {arm.mode} ({arm.reason})")
    if decision.audit.get("readings"):
        lines.append(f"measured: {decision.audit['readings']}")
    return "\n".join(lines)


def run_once(*, bus=None, identity=None, limit=5, product=None, deps=None):
    """One pass, for ONE bot identity. Never raises. Always returns a summary that
    distinguishes OFF from BROKEN from IDLE by name and by count.

    `product` defaults to the IDENTITY'S OWN product (identities.py), not a fixed
    constant. GAP 1 (audit of PR #68): this used to default to DEFAULT_PRODUCT
    ("echo") regardless of which identity was passed in, so a caller that passed
    identity=scout (or ranger, or wrangler) still polled support_tickets for
    product='echo' -- silently reading nothing for that identity, forever. See
    run_once_all_identities() below for why one product was never enough on its
    own: every production caller invoked this with NO arguments at all."""
    ident = identity if identity is not None else default_identity()
    if product is None:
        product = getattr(ident, "product", None) or DEFAULT_PRODUCT
    arm = _arm.preflight(getattr(ident, "name", DEFAULT_IDENTITY))
    if arm.banner:
        print(arm.banner)
    if arm.mode == _arm.MODE_OFF:
        print(f"[client-dm] {arm.reason}")
        return {"ok": True, "mode": arm.mode, "reason": arm.reason, "handled": 0,
                "replies": 0, "cards": 0}

    summary = {"ok": True, "mode": arm.mode, "reason": arm.reason, "handled": 0,
               "replies": 0, "cards": 0, "escalated": 0, "undelivered": 0,
               "capped": False}
    try:
        _tripwire.assert_no_ad_rail()
        _r.assert_producers_exist()
        _c.assert_registry_wellformed()
    except Exception as e:  # noqa: BLE001
        summary.update(ok=False, reason=f"boot check failed: {type(e).__name__}: {e}")
        print(f"[client-dm] REFUSING to run: {summary['reason']}")
        return summary

    bus = bus if bus is not None else default_bus()
    if not bus.available():
        summary.update(ok=False, reason="the support bus has no Supabase credentials")
        print(f"[client-dm] {summary['reason']}")
        return summary

    try:
        tickets, capped = poll(bus, product=product, limit=limit)
    except Exception as e:  # noqa: BLE001
        summary.update(ok=False, reason=f"poll failed: {type(e).__name__}: {e}")
        print(f"[client-dm] {summary['reason']}")
        return summary
    summary["capped"] = capped
    if capped:
        summary["ok"] = False
        summary["reason"] = (
            f"the poll hit its {POLL_PAGE * POLL_MAX_PAGES}-row ceiling before finding "
            f"{limit} unhandled ticket(s); this is a backlog, not an empty queue")
        print(f"[client-dm] {summary['reason']}")

    for ticket in tickets:
        text, surface = _newest_client_message(bus, ticket)
        gym_key = _gym_key_for(ticket)
        decision = decide(text=text, gym_key=gym_key,
                          may_reply=arm.may_reply_to_clients, deps=deps,
                          echo_already_asked=_echo_already_asked(bus, ticket["id"]))
        wrote = _deliver(bus, ticket, ident, decision, arm, surface)
        summary["handled"] += 1
        summary["replies"] += 1 if wrote["reply"] else 0
        summary["cards"] += 1 if wrote["card"] else 0
        summary["escalated"] += 0 if wrote["reply"] else 1
        summary["undelivered"] += wrote["undelivered"]

    print(f"[client-dm] mode={arm.mode} handled={summary['handled']} "
          f"replies={summary['replies']} escalated={summary['escalated']} "
          f"cards={summary['cards']} undelivered={summary['undelivered']}")
    return summary


# GAP 1 (audit of PR #68, Blake confirmed directly): every bot identity below is
# armed in production (SLACK_CONVO_<ID>_ENABLED=true, live per Railway, 2026-09-07)
# and every one of them can resolve identity_kind='client' and write a
# support_tickets row via bus.get_or_create_ticket(product=ident.product) -- Scout
# in particular is the identity real clients' Slack group DMs go through (per
# established practice: client sends land in a group DM with Scout, Blake and the
# owner). But run_once() above defaulted to identity='echo' and every production
# caller (agent/runner.py) invoked it with NO ARGUMENTS AT ALL, so this lane's poll
# has only ever asked support_tickets for product='echo'. A real client ticket
# filed through Scout, Ranger or Wrangler's own Bolt App was invisible to this
# lane's diagnosis path from day one -- not because ingestion into support_tickets
# was broken (get_or_create_ticket stamps source/reporter/client_id/identity_kind
# correctly for every identity), but because nothing ever asked for those rows.
# Read-only against the production project confirms this is not hypothetical:
# zero support_tickets rows of ANY product other than 'echo' carry a real (is_test
# is not true) client_id + slack_channel_id today.
#
# Lainey is excluded: no Slack surface (repo rule, identities.py).
POLL_IDENTITIES = ("echo", "ranger", "scout", "wrangler")


def run_once_all_identities(*, limit=5, deps=None, bus=None):
    """Run the lane once per identity that can carry a client's Slack conversation,
    against ONE shared bus connection. This is the function production should call
    instead of run_once() directly -- see agent/runner.py's caller and
    tests/test_client_dm_lane.py::test_the_runner_calls_all_client_carrying_identities.

    Each identity's own arming (arming.preflight(identity.name)), own outbox loop
    and own bot token are already correctly wired per-identity (outbox._dispatch_one
    checks att['identity'] == ticket['bot_identity'] == the posting identity's own
    name), so looping here is safe by construction: it does not change what any one
    identity may do, only how many identities' own tickets get asked for.
    """
    bus = bus if bus is not None else default_bus()
    from ..slack_convo import identities as _ids
    out = {"ok": True, "mode": "", "identities": {}, "handled": 0, "replies": 0,
           "cards": 0, "escalated": 0, "undelivered": 0}
    for name in POLL_IDENTITIES:
        try:
            ident = _ids.get(name)
        except KeyError:
            continue
        summary = run_once(bus=bus, identity=ident, limit=limit, deps=deps)
        out["identities"][name] = summary
        if not summary.get("ok", True):
            out["ok"] = False
        if not out["mode"]:
            out["mode"] = summary.get("mode", "")
        for k in ("handled", "replies", "cards", "escalated", "undelivered"):
            out[k] += summary.get(k, 0) or 0
    return out


def _gym_key_for(ticket):
    """support_tickets.client_id is the PORTAL GYM UUID, never an Echo account key.
    Resolve it through the repo's anti-divergence primitive, which returns "" on ANY
    uncertainty -- and "" escalates rather than querying media tables with a uuid."""
    from .. import account_key_resolve as _akr
    return _akr.portal_key_for_gym(ticket.get("client_id") or "")
