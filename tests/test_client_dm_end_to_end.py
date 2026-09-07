"""END-TO-END PROOF for GAP 1 (audit of PR #68): a real client Slack DM, ingested
the SAME WAY Chad Edwards's and John Weeks's actual messages arrived -- a raw Slack
`message` event in a group DM, run through the REAL adapter.handle_event() -- must
now be visible to and processed by client_dm_support.run_once_all_identities().

This deliberately does NOT insert a support_tickets row by hand. It builds a Slack
event dict shaped like a real one, hands it to the same adapter.handle_event() a
live Bolt listener calls, and only then lets client_dm_support poll the row the
adapter itself produced -- so this proves the FULL path, not just the poll's own
query filters (which tests/test_client_dm_no_fakes.py already covers in isolation).

HybridBus below is deliberately a superset of the two existing bus fakes in this
repo (test_slack_convo.FakeBus for the adapter side, test_client_dm_no_fakes.
FaithfulBus for the poll side) so ONE bus object can carry a ticket through both.
"""
import uuid
from datetime import datetime, timezone

from agent.client_dm_support import lane as L
from agent.slack_convo import adapter as A
from agent.slack_convo import identities as IDS
from agent.slack_convo import identity_gate as IG


class HybridBus:
    """In-memory support_tickets / support_messages, speaking BOTH the adapter's
    method-call interface and client_dm_support's REST-shaped poll interface
    (bus._get / bus.recent_messages), backed by ONE shared store."""

    def __init__(self):
        self.tickets = {}
        self.msgs = []

    def _ts(self):
        return datetime.now(timezone.utc).isoformat()

    # ---- adapter-facing ----
    def find_ticket_by_thread(self, channel_id, thread_ts):
        for t in self.tickets.values():
            if t["slack_channel_id"] == channel_id and t["slack_thread_ts"] == thread_ts:
                return dict(t)
        return None

    def find_open_ticket_in_conversation(self, channel_id, within_days):
        opens = [t for t in self.tickets.values()
                 if t["slack_channel_id"] == channel_id
                 and t["status"] in ("new", "triage", "fixing", "verification",
                                     "hold", "approved")]
        return dict(opens[-1]) if opens else None

    def get_or_create_ticket(self, **kw):
        existing = self.find_ticket_by_thread(kw["channel_id"], kw["thread_ts"])
        if existing:
            return existing, False
        t = {"id": str(uuid.uuid4()), "product": kw["product"],
             "source": "slack_conversation", "client_id": kw.get("client_id"),
             "reporter": kw.get("reporter"), "raw_text": kw["raw_text"],
             "status": "new", "slack_channel_id": kw["channel_id"],
             "slack_thread_ts": kw["thread_ts"], "slack_user_id": kw["slack_user_id"],
             "identity_kind": kw["identity_kind"], "bot_identity": kw["bot_identity"],
             "classification": kw.get("classification"),
             "request_type": kw.get("request_type"), "verification_before": None,
             "verification_after": None, "escalated": False, "lane": None,
             "hold_tier": None, "is_test": False, "created_at": self._ts()}
        self.tickets[t["id"]] = t
        return dict(t), True

    def ticket(self, tid):
        return dict(self.tickets[tid]) if tid in self.tickets else None

    def set_ticket(self, tid, **fields):
        self.tickets[tid].update(fields)
        return dict(self.tickets[tid])

    def count_tickets_for_user_today(self, slack_user_id, bot_identity=None):
        return sum(1 for t in self.tickets.values()
                   if t["slack_user_id"] == slack_user_id
                   and (bot_identity is None or t["bot_identity"] == bot_identity))

    def find_recent_ticket_for_user_today(self, slack_user_id, bot_identity=None):
        mine = [t for t in self.tickets.values() if t["slack_user_id"] == slack_user_id
                and (bot_identity is None or t["bot_identity"] == bot_identity)]
        return dict(mine[-1]) if mine else None

    def record_inbound(self, **kw):
        if kw.get("slack_event_id") and any(
                m.get("slack_event_id") == kw["slack_event_id"] for m in self.msgs):
            return None, True
        m = {"id": str(uuid.uuid4()), "direction": "inbound", "delivery_status": None,
             "created_at": self._ts(), "attachments": kw.get("meta"), **kw}
        self.msgs.append(m)
        return dict(m), False

    def record_outbound(self, **kw):
        att = {"kind": kw["kind"]}
        att.update(kw.get("meta") or {})
        m = {"id": str(uuid.uuid4()), "direction": "outbound", "ticket_id": kw["ticket_id"],
             "author_type": kw["author_type"], "body": kw["body"],
             "delivery_status": kw["delivery_status"], "attachments": att,
             "created_at": self._ts()}
        self.msgs.append(m)
        return dict(m)

    def messages(self, tid, limit=40):
        return [dict(m) for m in self.msgs if m["ticket_id"] == tid]

    # ---- client_dm_support-facing (poll + _actionable + _newest_client_message) ----
    def available(self):
        return True

    @staticmethod
    def _match(row, key, expr):
        if expr.startswith("eq."):
            return str(row.get(key) or "") == expr[3:]
        if expr.startswith("in."):
            return str(row.get(key) or "") in expr[3:].strip("()").split(",")
        raise AssertionError(f"unsupported filter {expr!r}")

    def _get(self, table, params):
        assert table == "support_tickets"
        rows = [r for r in self.tickets.values()
                if all(self._match(r, k, v) for k, v in params.items()
                       if k not in ("select", "order", "limit", "offset"))]
        rows.sort(key=lambda r: r.get("created_at") or "")
        off = int(params.get("offset", 0))
        lim = int(params.get("limit", 50))
        return rows[off:off + lim]

    def recent_messages(self, ticket_id, limit=200):
        rows = [dict(m) for m in self.msgs if m["ticket_id"] == ticket_id]
        rows.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        return rows[:limit]


def _real_client_group_dm_event(text, *, channel="G0SCOUT", user="U_JOHN"):
    """Shaped exactly like the event Slack Socket Mode hands listener_wiring's
    @app.event('message') handler for a real multi-party group DM -- channel_type
    'mpim', no thread_ts (top-level, since people do not thread in a DM)."""
    return {"type": "message", "channel": channel, "channel_type": "mpim",
            "user": user, "text": text, "ts": "1700000000.000100",
            "_raw_event_id": "Ev_REAL_1"}


def _deps(bus, identity):
    return A.Deps(
        bus=bus, identity=identity,
        resolve_identity=lambda uid: IG.Identity(
            IG.CLIENT, uid, email="john@toughtemple.example", display="John Weeks",
            account_key="toughtemple52040e", gym_id="g-tough", reason="portal match"),
        identity_enabled=lambda: True,
        client_reply_armed=lambda: True, staff_reply_armed=lambda: True,
        daily_cap=lambda: 10, open_window_days=lambda: 7,
        answer=None, classify_llm=None, log=lambda *a, **k: None,
        describe_gym=None, auto_answer_armed=lambda: False,
        cross_product_armed=lambda: False)


def test_johns_real_message_via_the_real_adapter_now_reaches_client_dm_support(
        monkeypatch):
    """THE END-TO-END PROOF. John Weeks's real words, arriving the same way his real
    ones did: a raw Slack event in a group DM (this repo's established practice
    routes real client group DMs through the SCOUT bot -- see identities.py and
    slack-owner-dm-pattern), handled by the REAL adapter.handle_event(), with NO
    support_tickets row inserted by hand.
    """
    monkeypatch.setenv("AGENT_SLACK_BOT_USER_ID", "U_SCOUT_BOT")
    monkeypatch.setenv(L._arm.ENV_MASTER, "true")  # noqa: SLF001 - AGENT_CLIENT_DM_AUTOFIX

    scout = IDS.get("scout")
    bus = HybridBus()
    ev = _real_client_group_dm_event(
        "the posts need a real call to action", channel="G0SCOUT", user="U_JOHN")
    decision = A.handle_event(ev, "e1", _deps(bus, scout))

    assert decision.action == "ticketed"
    tid = decision.ticket_id
    ticket = bus.ticket(tid)
    # Ingestion happened for real: product/source/status are the adapter's OWN
    # writes, not anything this test asserted into existence.
    assert ticket["product"] == "scout"
    assert ticket["source"] == "slack_conversation"
    assert ticket["status"] == "hold"
    assert ticket["identity_kind"] == "client"

    # The existing hold lane already gave John an honest, non-invented acknowledgment
    # at intake time -- this is NOT something client_dm_support needs to add.
    ack_bodies = [m["body"] for m in bus.messages(tid)
                 if m["direction"] == "outbound"
                 and m["attachments"]["kind"] == A.KIND_TEMPLATE]
    assert any("recorded for the LASSO team" in b for b in ack_bodies), (
        "no honest hold-and-acknowledge reached the client at intake")

    # THE OLD SHAPE: every production caller used to invoke run_once() with no
    # arguments (identity='echo' default). It must NOT see this Scout ticket.
    old_shape = L.run_once(bus=bus)
    assert old_shape["handled"] == 0, (
        "GAP 1 not actually closed: the OLD call shape still sees this ticket")

    # THE FIX: run_once_all_identities must find it, diagnose it, and (since John's
    # voice doc is not stubbed here -- no fix Echo may perform for a missing CTA
    # doc it cannot read) escalate with a human card, never inventing an answer.
    monkeypatch.setattr(L, "_gym_key_for", lambda t: "toughtemple52040e")
    out = L.run_once_all_identities(bus=bus)
    assert out["identities"]["scout"]["handled"] == 1
    cards = [m for m in bus.messages(tid) if m["direction"] == "outbound"
            and m["attachments"].get(L.LANE_META) == L.LANE_NAME]
    assert cards, "client_dm_support never touched the ticket the real adapter created"
    assert any("they wrote:" in c["body"] for c in cards)
