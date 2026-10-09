"""Durable Slack intake replay, enabled with the support sender cutover fence.

The DRAFT migration is required before enabling that fence. No local fallback:
an unavailable capture/claim/commit RPC is an intake error, never permission to
produce source-less rows. Planning buffers ALL bus writes; the database commits
the whole plan under an expiring token and exact ticket/message snapshots.
Only planning leases expire. A committed plan is never regenerated or resent.
"""
import copy
import json
import re
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone

from .. import support_sender_fence as fence
from . import identity_gate as ig
from .bus import BusError

TABLE = "support_slack_replay"
CONTRACT = "support-slack-replay-v1"
_NAMESPACE = uuid.UUID("482631e9-3d86-4d83-b394-1e0f48396062")
_STOP = re.compile(r"^\s*(?:stop|unsubscribe|dnd|do not disturb|do not (?:contact|message|reply to) me|"
                   r"don't (?:contact|message|reply to) me)[.!\s]*$", re.I)
_HUMAN = re.compile(r"\b(?:speak|talk) (?:to|with) (?:a |an )?(?:human|person)|"
                    r"\bhuman takeover\b|\blet (?:me|us|a human|the team) handle\b", re.I)
_BINDING = ("kind", "slack_user_id", "email", "account_key", "gym_id")
_BENIGN_CHATTER = re.compile(
    r"^\s*(?:hey|hi|hello|yo|thanks|thank you|thx|ty|ok|okay|k|got it|sounds good|great|"
    r"perfect|awesome|cool|nice|yep|yes|sure|np|no problem|lol|haha|👍|🙏|✅)[\s!.,]*$", re.I)


def benign_chatter(text):
    return len(text or "") <= 60 and bool(_BENIGN_CHATTER.fullmatch(text or ""))


def safety_hold(text):
    if _STOP.search(text or ""):
        return "STOP"
    if _HUMAN.search(text or ""):
        return "human_takeover"
    return None


def rpc(bus, name, body):
    """One attempt. Callers retry with the SAME identity/token/plan after uncertainty."""
    response = bus._client().post(
        f"{bus._url}/rest/v1/rpc/{name}", data=json.dumps(body),
        headers=bus._headers(), timeout=30)
    if response.status_code >= 400:
        raise BusError(response.status_code, (response.text or "")[:200])
    value = response.json()
    if not isinstance(value, dict):
        raise BusError(502, "malformed Slack replay RPC receipt")
    return value


def capture(deps, *, event, event_id, ticket, who, surface, text, created,
            classification, unknown_in_channel, rate_limited, request_type, chatter=False):
    context = {
        "contract": CONTRACT, "event": copy.deepcopy(event), "event_key": event_id,
        "identity": deps.identity.name, "product": deps.identity.product,
        "who": asdict(who), "surface": surface, "text": text,
        "created": created, "classification": classification,
        "unknown_in_channel": unknown_in_channel, "rate_limited": rate_limited,
        "request_type": request_type, "safety_hold": safety_hold(text),
        "chatter": chatter,
    }
    body = {
        "p_ticket_id": ticket["id"], "p_event_key": event_id,
        "p_identity": deps.identity.name, "p_context": context,
    }
    # A capture response can be lost after COMMIT. Reusing the exact key/context
    # makes the retry return the one persisted inbound and original replay plan.
    try:
        return rpc(deps.bus, "support_slack_replay_capture", body)
    except Exception:
        return rpc(deps.bus, "support_slack_replay_capture", body)


class PlanningBus:
    """Explicit bus surface: unknown reads/writes raise instead of escaping buffering."""
    _FIELDS = frozenset({"status", "classification", "lane", "hold_tier", "request_type",
                         "verification_before", "verification_after", "escalated"})

    def __init__(self, item):
        self.item = item
        self.current = copy.deepcopy(item["ticket_snapshot"])
        self.history = copy.deepcopy(item["messages_snapshot"])
        self.fields = {}
        self.rows = []

    def ticket(self, tid):
        if tid != self.current["id"]:
            raise ValueError("replay ticket mismatch")
        return copy.deepcopy(self.current)

    def set_ticket(self, tid, **fields):
        if tid != self.current["id"] or not fields.keys() <= self._FIELDS:
            raise ValueError("unsupported replay ticket mutation")
        self.fields.update(copy.deepcopy(fields))
        self.current.update(copy.deepcopy(fields))
        return self.ticket(tid)

    def record_outbound(self, *, ticket_id, author_type, body, delivery_status, kind, meta=None):
        if ticket_id != self.current["id"]:
            raise ValueError("replay outbound ticket mismatch")
        row = {
            "id": str(uuid.uuid5(_NAMESPACE, f"{self.item['id']}:{len(self.rows)}")),
            "ticket_id": ticket_id, "direction": "outbound", "author_type": author_type,
            "author_id": None, "body": (body or "")[:8000],
            "delivery_status": delivery_status, "attachments": {"kind": kind, **(meta or {})},
        }
        self.rows.append(row)
        return copy.deepcopy(row)

    def messages(self, tid, limit=40):
        self.ticket(tid)
        return copy.deepcopy((self.history + self.rows)[-limit:])

    def count_outbound_kind_since(self, tid, kind, since_iso):
        self.ticket(tid)
        return sum(1 for row in self.history + self.rows
                   if row.get("direction") == "outbound"
                   and (row.get("attachments") or {}).get("kind") == kind
                   and (not row.get("created_at") or row["created_at"] >= since_iso))


def _held(bus, item, token, reason):
    return rpc(bus, "support_slack_replay_hold", {
        "p_id": item["id"], "p_identity": item["bot_identity"],
        "p_token": token, "p_reason": reason,
    })


def process(deps, replay_id):
    """Claim, plan, CAS commit. Pure planning can retry; external actions cannot."""
    from . import adapter
    with fence.admission("slack_adapter_replay") as allowed:
        if not allowed or not deps.identity_enabled():
            return {"state": "paused"}
        token = str(uuid.uuid4())
        args = {"p_id": replay_id, "p_identity": deps.identity.name, "p_token": token}
        try:
            claimed = rpc(deps.bus, "support_slack_replay_claim", args)
        except Exception:
            # Same claim token handles a response lost after the selection COMMIT.
            claimed = rpc(deps.bus, "support_slack_replay_claim", args)
        item = claimed.get("item")
        if claimed.get("state") != "planning" or not isinstance(item, dict):
            return claimed
        context = item["context"]
        dispatch = context["dispatch"]
        # The newest event supplies current-request authority; an undispatched
        # initial request retains its own actor, surface and stricter client
        # gates even when the newest note is from an authorized staff member.
        who = ig.Identity(**dispatch["who"])
        for actor in (context["who"], dispatch["who"]):
            bound_who = ig.Identity(**actor)
            fresh_who = deps.resolve_identity(bound_who.slack_user_id)
            if any(getattr(fresh_who, key) != getattr(bound_who, key) for key in _BINDING):
                return _held(deps.bus, item, token, "identity_binding_changed")
        if dispatch["classification"] == adapter._cls.CANCEL_POST:
            # cancel_lane mutates a calendar in a separate store. It cannot be
            # rolled back with this replay plan and is not safe to auto-retry.
            return _held(deps.bus, item, token, "external_cancel_requires_reconciliation")
        planner = PlanningBus(item)
        planned_deps = replace(deps, bus=planner)
        try:
            if dispatch.get("noop"):
                decision = adapter.Decision("ticketed", "chatter_noted", dispatch["surface"],
                                            who.kind, item["ticket_id"])
            else:
                decision = adapter._dispatch_recorded_event(
                    deps=planned_deps, ident=deps.identity, who=who,
                    user=who.slack_user_id, text=dispatch["text"], surface=dispatch["surface"],
                    channel=dispatch["event"]["channel"], tid=item["ticket_id"],
                    ticket=planner.ticket(item["ticket_id"]), created=dispatch["created"],
                    classification=dispatch["classification"],
                    unknown_in_channel=dispatch["unknown_in_channel"],
                    rate_limited=dispatch["rate_limited"], request_type=dispatch["request_type"])
        except Exception as exc:
            # No plan escaped the buffering bus. Hold rather than repeat an
            # unsupported effect or guess what a failed custom dependency did.
            return _held(deps.bus, item, token, f"planning_failed:{type(exc).__name__}")
        plan = {"ticket_fields": planner.fields, "rows": planner.rows,
                "decision": asdict(decision)}
        args = {"p_id": item["id"], "p_identity": deps.identity.name,
                "p_token": token, "p_plan": plan}
        try:
            receipt = rpc(deps.bus, "support_slack_replay_commit", args)
        except Exception:
            # The very same plan is retried, never regenerated. SQL returns its
            # committed receipt unchanged, or refuses an expired/replaced claim.
            receipt = rpc(deps.bus, "support_slack_replay_commit", args)
        return receipt


def run_once(deps, *, limit=20):
    """Called by the listener's durable outbox loop, including after restart."""
    if not fence.control()["enabled"] or fence.control()["paused"]:
        return {}
    now = datetime.now(timezone.utc).isoformat()
    rows = deps.bus._get(TABLE, {
        "bot_identity": f"eq.{deps.identity.name}",
        "or": f"(state.eq.pending,and(state.eq.planning,claim_until.lt.{now}))",
        "select": "id", "order": "created_at.asc,id.asc", "limit": str(limit),
    })
    counts = {}
    for row in rows:
        result = process(deps, row["id"])
        state = result.get("state", "invalid")
        counts[state] = counts.get(state, 0) + 1
        if state == "held":
            deps.log(f"[slack-convo/{deps.identity.name}] replay HELD "
                     f"id={row['id']} reason={result.get('reason')}")
    return counts


def dispatch_allowed(bus, row, identity):
    """Fresh DB authority, immediately before posting a replay-produced row."""
    att = row.get("attachments") or {}
    if "slack_replay_id" not in att:
        return True
    try:
        value = rpc(bus, "support_slack_replay_dispatch_allowed", {
            "p_message_id": row["id"], "p_identity": identity,
        })
        return value.get("allowed") is True
    except Exception:
        return False


def claim_delivery(bus, row):
    """The exact token survives a lost claim response; no second send owner."""
    args = {"p_message_id": row["id"], "p_identity": row["attachments"]["identity"],
            "p_token": str(uuid.uuid4())}
    try:
        value = rpc(bus, "support_slack_replay_delivery_claim", args)
    except Exception:
        value = rpc(bus, "support_slack_replay_delivery_claim", args)
    claimed = value.get("row")
    if not isinstance(claimed, dict) or claimed.get("delivery_status") != "posting":
        return None
    row.update(claimed)
    return claimed


def finish_delivery(bus, row, ts):
    args = {"p_message_id": row["id"], "p_identity": row["attachments"]["identity"],
            "p_token": row["attachments"]["slack_replay_delivery_token"], "p_ts": ts}
    try:
        value = rpc(bus, "support_slack_replay_delivery_finish", args)
    except Exception:
        value = rpc(bus, "support_slack_replay_delivery_finish", args)
    return value.get("row")


def hold_delivery(bus, row, why):
    return rpc(bus, "support_slack_replay_delivery_hold", {
        "p_message_id": row["id"], "p_identity": row["attachments"]["identity"],
        "p_token": row["attachments"].get("slack_replay_delivery_token"), "p_reason": why,
    }).get("row")


def resolve_delivery(bus, row):
    args = {"p_message_id": row["id"], "p_identity": row["attachments"]["identity"],
            "p_token": row["attachments"]["slack_replay_delivery_token"]}
    try:
        return rpc(bus, "support_slack_replay_delivery_resolve", args)
    except Exception:
        return rpc(bus, "support_slack_replay_delivery_resolve", args)
