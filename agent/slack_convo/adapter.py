"""
adapter.py — the intake. A Slack message comes in; rows go out. Nothing is posted here.

  match the surface -> ignore self/bots -> identity gate -> thread equals ticket ->
  dedupe on event id -> rate limit -> classify -> write the inbound row -> write the
  outbound rows (acknowledgement, answer, escalation, fixer request) in the right
  delivery state -> return a Decision the caller can log and tests can assert on.

Blake (spec item 8): "The adapter never calls chat.postMessage itself. It writes the row,
Wrangler posts." That is structural here: this module has no Slack client. The outbox
(the Wrangler outbound role) reads 'ready' rows back and is the only thing that posts.

Blake (spec item 3): "The first-contact rule stands: the bot never opens a conversation
with anyone. It only responds where a person spoke first." Also structural: the only entry
point is handle_event(), which is only ever called with an inbound human message. There is
no code path here that writes an outbound row for a ticket without first writing the human's
inbound row on it.

Surfaces (spec item 2): DMs to the bot, group DMs the bot is in, @mentions anywhere, and
replies in a thread where the bot ALREADY has a ticket. Not every channel message. Silent
otherwise -- silent means we return a Decision saying why and write nothing.
"""
from dataclasses import dataclass, field

from . import classifier as _cls
from . import identity_gate as _ig

SURFACE_IM = "im"
SURFACE_MPIM = "mpim"
SURFACE_MENTION = "mention"
SURFACE_THREAD = "thread_reply"

# outbound `kind` values (ride in support_messages.attachments.kind)
KIND_ACK = "ack"                 # "got it, here is what I understood" -- not substantive
KIND_ANSWER = "answer"           # substantive: needs verification_after on the ticket
KIND_TEMPLATE = "template"       # fixed text (unknown user redirect, queued notice)
KIND_STATUS = "status"           # plain-language status as the ticket moves
KIND_ESCALATION = "escalation"   # to the fixer channel: a human must look
KIND_FIXER_REQUEST = "fixer_request"  # to the fixer channel: a code fix for the worker
KIND_HOLD_NOTICE = "hold_notice"      # to the fixer channel: a client reply awaits a tap

# Delivered to the fixer channel, never into the client's thread.
INTERNAL_KINDS = frozenset({KIND_ESCALATION, KIND_FIXER_REQUEST, KIND_HOLD_NOTICE})

TEMPLATE_UNKNOWN = (
    "Thanks for reaching out. I can only help from inside your LASSO portal, so this message "
    "did not reach a person yet. Please use the Support page in your portal and the team "
    "will pick it up there.")
TEMPLATE_QUEUED = (
    "Got it. You have sent a lot today, so I have queued this for the team rather than "
    "acting on it right away. Someone will pick it up.")
TEMPLATE_ESCALATED = (
    "Got it. This one needs a person, so I have flagged it for the LASSO team and they "
    "will follow up here.")


@dataclass
class Decision:
    action: str                          # ignored | ticketed
    reason: str = ""
    surface: str = ""
    identity_kind: str = ""
    ticket_id: str = ""
    created: bool = False
    classification: str = ""
    outbound_kinds: list = field(default_factory=list)
    duplicate: bool = False
    rate_limited: bool = False

    @property
    def ignored(self):
        return self.action == "ignored"


@dataclass
class Deps:
    """Everything the adapter touches, injected. See listener_wiring.live_deps()."""
    bus: object
    identity: object                       # identities.BotIdentity
    resolve_identity: object               # (slack_user_id) -> identity_gate.Identity
    identity_enabled: object               # () -> bool
    client_reply_armed: object             # () -> bool
    staff_reply_armed: object              # () -> bool
    daily_cap: object                      # () -> int
    open_window_days: object               # () -> int
    answer: object = None                  # (ticket, identity, messages) -> dict|None
    classify_llm: object = None            # (text) -> label | None
    log: object = print


def _ignore(reason, surface="", identity_kind=""):
    return Decision(action="ignored", reason=reason, surface=surface,
                    identity_kind=identity_kind)


def match_surface(event, deps):
    """Which of our four surfaces is this, or '' for not ours (silent)."""
    etype = event.get("type") or "message"
    if etype == "app_mention":
        return SURFACE_MENTION
    ch_type = event.get("channel_type") or ""
    if ch_type == "im":
        return SURFACE_IM
    if ch_type == "mpim":
        return SURFACE_MPIM
    if ch_type in ("channel", "group"):
        thread_ts = event.get("thread_ts")
        if thread_ts and deps.bus.find_ticket_by_thread(event.get("channel"), thread_ts):
            return SURFACE_THREAD
    return ""


def author_type_for(identity_obj):
    return {_ig.STAFF: "staff", _ig.COACH: "coach", _ig.CLIENT: "client"}.get(
        identity_obj.kind, "client")


def delivery_for(deps, identity_obj, kind):
    """Where a row starts its life. Internal kinds are always ready (they go to the fixer
    channel, never to the person). Conversational kinds hold behind the per-identity flag
    for that KIND of person: staff first, clients when Blake arms them. An unknown person
    is treated as a client for this purpose (the strictest gate)."""
    if kind in INTERNAL_KINDS:
        return "ready"
    if identity_obj.kind in (_ig.STAFF, _ig.COACH):
        return "ready" if deps.staff_reply_armed() else "held"
    return "ready" if deps.client_reply_armed() else "held"


def handle_event(event, event_id, deps):
    """The whole intake for one inbound Slack event. Never raises to the caller for a
    business reason; a bus outage raises so the listener can log it loudly."""
    ident = deps.identity
    # 0) FLAGS OFF EQUALS TODAY: return before reading or writing anything.
    if not deps.identity_enabled():
        return _ignore("flag_off")
    # 1) never talk to ourselves, other bots, or edits/joins -- the loop guard.
    if event.get("bot_id") or event.get("subtype"):
        return _ignore("bot_or_subtype")
    user = str(event.get("user") or "").strip()
    if not user or user == ident.bot_user_id():
        return _ignore("self_or_no_user")
    text = str(event.get("text") or "").strip()
    if not text:
        return _ignore("empty_text")
    # 2) surface
    surface = match_surface(event, deps)
    if not surface:
        return _ignore("not_our_surface")
    channel = str(event.get("channel") or "")
    # 3) identity gate
    who = deps.resolve_identity(user)
    if who.kind == _ig.BOT:
        return _ignore("bot_user", surface)
    # 4) thread equals ticket: which conversation is this?
    thread_root = event.get("thread_ts") or ""
    existing = None
    if thread_root:
        existing = deps.bus.find_ticket_by_thread(channel, thread_root)
    elif surface in (SURFACE_IM, SURFACE_MPIM):
        existing = deps.bus.find_open_ticket_in_conversation(channel, deps.open_window_days())
        if existing:
            thread_root = existing["slack_thread_ts"]
    if not thread_root:
        thread_root = str(event.get("ts") or "")
    has_open = bool(existing and existing.get("status") in
                    ("new", "triage", "fixing", "verification", "hold", "approved"))

    # 5) rate limit -- gates DISPATCH for a new ticket, never recording.
    rate_limited = False
    if existing is None and who.kind != _ig.STAFF:
        try:
            if deps.bus.count_tickets_for_user_today(user) >= deps.daily_cap():
                rate_limited = True
        except Exception as e:  # noqa: BLE001 - a counting failure fails CLOSED
            deps.log(f"[slack-convo] rate-limit read failed ({type(e).__name__}); "
                     "treating as limited")
            rate_limited = True

    # 6) classify (unknown identities are never classified: no worker, no answer)
    classification = None
    request_type = None
    if who.is_human_known and not rate_limited:
        classification = _cls.classify(text, has_open_ticket=has_open,
                                       identity_product=ident.product,
                                       llm=deps.classify_llm)
        if classification == _cls.ACTION_REQUEST:
            request_type = _cls.request_type_for(text)

    # 7) the ticket row
    if existing is None:
        ticket, created = deps.bus.get_or_create_ticket(
            channel_id=channel, thread_ts=thread_root, product=ident.product,
            bot_identity=ident.name, slack_user_id=user,
            identity_kind=who.kind if who.kind != _ig.BOT else _ig.UNKNOWN,
            client_id=who.gym_id or None,
            reporter=(who.email or who.display or user),
            raw_text=text,
            classification=(classification if classification != _cls.FOLLOW_UP else None),
            request_type=request_type)
    else:
        ticket, created = existing, False
    tid = ticket["id"]

    # 8) THE INBOUND ROW FIRST. Duplicate event id -> we already did all of this; stop.
    _, dup = deps.bus.record_inbound(
        ticket_id=tid, slack_event_id=event_id, slack_ts=event.get("ts"),
        author_type=author_type_for(who) if who.is_human_known else "client",
        author_id=user, body=text,
        meta={"surface": surface, "raw_event_id": event.get("_raw_event_id") or "",
              "identity_reason": who.reason})
    if dup:
        return Decision(action="ignored", reason="duplicate_event", surface=surface,
                        identity_kind=who.kind, ticket_id=tid, duplicate=True)

    out = []

    def emit(kind, body, author_type=None, meta=None):
        status = delivery_for(deps, who, kind)
        m = {"surface": surface, "recipient_kind": who.kind}
        if meta:
            m.update(meta)
        row = deps.bus.record_outbound(ticket_id=tid, author_type=author_type or ident.name,
                                       body=body, delivery_status=status, kind=kind, meta=m)
        out.append(kind)
        if status == "held":
            # ONE tap notice per held row, to the fixer channel: the draft, who it is for,
            # and the ticket. The outbox posts the notice; a tap flips the row to ready.
            deps.bus.record_outbound(
                ticket_id=tid, author_type="system",
                body=(f"HELD REPLY awaiting your tap ({who.kind} {who.display or user}, "
                      f"{ident.name}, ticket {tid}):\n\n{body}"),
                delivery_status="ready", kind=KIND_HOLD_NOTICE,
                meta={"surface": surface, "held_message_id": (row or {}).get("id"),
                      "recipient_kind": who.kind})
            out.append(KIND_HOLD_NOTICE)
        return status

    # 9) the gates that end early
    if not who.is_human_known:
        deps.bus.set_ticket(tid, status="hold", escalated=True, identity_kind=_ig.UNKNOWN)
        emit(KIND_ESCALATION,
             f"Unknown Slack user {user} ({who.reason}) wrote to {ident.name} in {channel}. "
             f"No fix, no answer. Ticket {tid}.", author_type="system")
        emit(KIND_TEMPLATE, TEMPLATE_UNKNOWN)
        return Decision("ticketed", "unknown_identity", surface, who.kind, tid, created,
                        "", out)
    if rate_limited:
        deps.bus.set_ticket(tid, status="hold", escalated=True)
        emit(KIND_ESCALATION,
             f"{who.kind} {who.display or user} hit the daily ticket cap "
             f"({deps.daily_cap()}) on {ident.name}. Queued, no worker. Ticket {tid}.",
             author_type="system")
        emit(KIND_TEMPLATE, TEMPLATE_QUEUED)
        return Decision("ticketed", "rate_limited", surface, who.kind, tid, created, "",
                        out, rate_limited=True)

    # 10) route by classification
    if classification == _cls.FOLLOW_UP:
        # attach + re-trigger: the new message is the instruction ("fix it differently: X")
        deps.bus.set_ticket(tid, status="triage")
        if (ticket.get("classification") or "") == _cls.CODE_FIX:
            emit(KIND_FIXER_REQUEST, _fixer_request_text(ident, ticket, text, who,
                                                          follow_up=True),
                 author_type="system")
        emit(KIND_ACK, f"Got it, I have added that to the open request and it is being "
                       f"looked at again.")
        return Decision("ticketed", "follow_up", surface, who.kind, tid, created,
                        _cls.FOLLOW_UP, out)

    if classification == _cls.QUESTION:
        emit(KIND_ACK, "Got it, checking that for you now.")
        answer = None
        if deps.answer is not None:
            try:
                answer = deps.answer(ticket, who, deps.bus.messages(tid))
            except Exception as e:  # noqa: BLE001 - a model fault escalates, never invents
                deps.log(f"[slack-convo] answer lane failed: {type(e).__name__}")
                answer = None
        if answer and answer.get("body"):
            # The grounding snapshot IS the verification for an answer: what was true when
            # we said it. Both fields populated before the outbox may post the answer.
            deps.bus.set_ticket(tid, classification=_cls.QUESTION, status="resolved",
                                verification_before=answer.get("grounding") or {},
                                verification_after=answer.get("grounding") or {})
            emit(KIND_ANSWER, answer["body"])
        else:
            deps.bus.set_ticket(tid, classification=_cls.QUESTION, status="hold",
                                escalated=True)
            emit(KIND_ESCALATION, f"Question from {who.kind} {who.display or user} on "
                                  f"{ident.name} could not be answered from live state. "
                                  f"Ticket {tid}.", author_type="system")
            emit(KIND_TEMPLATE, TEMPLATE_ESCALATED)
        return Decision("ticketed", "question", surface, who.kind, tid, created,
                        _cls.QUESTION, out)

    if classification == _cls.CODE_FIX:
        lane = ident.default_lane if ident.default_lane in ident.allowed_lanes else "hold"
        deps.bus.set_ticket(tid, classification=_cls.CODE_FIX, status="triage", lane=lane,
                            hold_tier="routine" if lane == "hold" else None)
        emit(KIND_FIXER_REQUEST, _fixer_request_text(ident, ticket, text, who),
             author_type="system")
        emit(KIND_ACK, "Got it. I read that as something not working on our side, so I "
                       "have opened a fix request and the team's fixer is on it. I will "
                       "post here once it is verified, not before.")
        return Decision("ticketed", "code_fix", surface, who.kind, tid, created,
                        _cls.CODE_FIX, out)

    if classification == _cls.ACTION_REQUEST:
        # Ranger lane: its cron polls status='new', product='ranger' with a request_type.
        deps.bus.set_ticket(tid, classification=_cls.ACTION_REQUEST, status="new",
                            request_type=request_type or "other")
        emit(KIND_ACK, "Got it. I read that as a request to change something on your "
                       "ads, so it is in the Ranger lane and will be reviewed before "
                       "anything changes.")
        return Decision("ticketed", "action_request", surface, who.kind, tid, created,
                        _cls.ACTION_REQUEST, out)

    # ESCALATE: nothing decided -> a human looks. No worker, no answer.
    deps.bus.set_ticket(tid, status="hold", escalated=True)
    emit(KIND_ESCALATION, f"{who.kind} {who.display or user} wrote to {ident.name} and the "
                          f"classifier did not decide. Ticket {tid}.", author_type="system")
    emit(KIND_TEMPLATE, TEMPLATE_ESCALATED)
    return Decision("ticketed", "escalated", surface, who.kind, tid, created, "", out)


def _fixer_request_text(ident, ticket, text, who, follow_up=False):
    head = "OPS-FIX REQUEST" if not follow_up else "OPS-FIX FOLLOW-UP"
    return (f"{head}: ECHO ALERT: slack conversation ticket {ticket['id']} "
            f"(product {ident.product}, {who.kind} {who.display or who.slack_user_id}"
            f"{', account ' + who.account_key if who.account_key else ''}): {text}")
