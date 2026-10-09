"""Offline replay RPC double for older adapter routing tests.

Production replay planning still runs. This double models capture, snapshot CAS,
claim ownership and atomic plan commit; PostgreSQL acceptance tests exercise the
full SQL contract, including concurrency, leases and composed pending requests.
"""
import copy
import threading
import uuid

from agent.slack_convo import replay
from agent.slack_convo.bus import BusError


class ReplayRPC:
    def __init__(self):
        self.items = {}
        self.lock = threading.RLock()

    def __call__(self, bus, name, args):
        with self.lock:
            return self.call(bus, name, args)

    def call(self, bus, name, args):
        if name == "support_slack_replay_capture":
            context = copy.deepcopy(args["p_context"])
            event = context["event"]
            ticket = bus.tickets[args["p_ticket_id"]]
            if (ticket.get("source", "slack_conversation") != "slack_conversation"
                    or ticket["bot_identity"] != args["p_identity"]
                    or context["product"] != ticket["product"]
                    or (event["channel"] != ticket["slack_channel_id"]
                        and not (context["rate_limited"]
                                 and context["who"]["kind"] in ("client", "unknown")))
                    or context["who"]["kind"] == "client"
                    and context["who"]["gym_id"] != ticket.get("client_id")):
                raise BusError(400, "Slack replay capture identity/source mismatch")
            assert args["p_event_key"] == f'{event["channel"]}:{event["ts"]}'
            for item in self.items.values():
                if item["event_key"] == args["p_event_key"] and item["bus"] is bus:
                    assert item["ticket_id"] == args["p_ticket_id"]
                    assert item["context"]["event"] == event
                    return {"item": copy.deepcopy({k: v for k, v in item.items() if k != "bus"}), "duplicate": True}
            tid = args["p_ticket_id"]
            bus.tickets[tid].setdefault("request_version", 0)
            bus.tickets[tid].setdefault("source", "slack_conversation")
            who = context["who"]
            assert event["user"] == who["slack_user_id"]
            context["dispatch"] = {key: copy.deepcopy(context[key]) for key in (
                "classification", "request_type", "text", "created", "who", "event",
                "surface", "unknown_in_channel", "rate_limited")}
            context["dispatch"]["noop"] = context.get("chatter", False)
            if context["rate_limited"] and event["channel"] != ticket["slack_channel_id"]:
                for previous in reversed(list(self.items.values())):
                    prior = previous["context"]["dispatch"]
                    if (previous["bus"] is bus and previous["ticket_id"] == tid
                            and previous["state"] != "committed"
                            and prior["event"]["channel"] == ticket["slack_channel_id"]
                            and prior["who"]["slack_user_id"] == ticket["slack_user_id"]):
                        context["dispatch"] = copy.deepcopy(prior)
                        break
            message, duplicate = bus.record_inbound(
                ticket_id=tid, author_type=who["kind"] if who["kind"] in ("staff", "coach") else "client",
                author_id=event["user"], body=context["text"],
                slack_event_id=args["p_event_key"], slack_ts=event["ts"],
                meta={"surface": context["surface"], "identity_reason": who["reason"],
                      "raw_event_id": event.get("_raw_event_id", ""),
                      "chatter": context.get("chatter", False),
                      "slack_replay_contract": replay.CONTRACT})
            assert not duplicate, "historical inbound requires reconciliation"
            item = {"id": str(uuid.uuid4()), "ticket_id": tid, "inbound_id": message["id"],
                    "bot_identity": args["p_identity"], "event_key": args["p_event_key"],
                    "context": context, "state": "pending",
                    "ticket_snapshot": copy.deepcopy(bus.tickets[tid]),
                    "messages_snapshot": copy.deepcopy(bus.messages(tid))}
            self.items[item["id"]] = item
            item["bus"] = bus
            return {"item": copy.deepcopy({k: v for k, v in item.items() if k != "bus"}), "duplicate": False}
        if "p_message_id" in args:
            return self.delivery(bus, name, args)
        item = self.items[args["p_id"]]
        assert args["p_identity"] == item["bot_identity"]
        if name == "support_slack_replay_claim":
            if item["state"] in ("held", "committed"):
                return {"state": item["state"], "reason": item.get("reason"),
                        "decision": item.get("plan", {}).get("decision")}
            if item["state"] == "planning" and item["token"] != args["p_token"]:
                return {"state": "busy"}
            if not self.current(bus, item):
                item.update(state="held", reason="snapshot_changed")
                return {"state": "held", "reason": item["reason"]}
            item.update(state="planning", token=args["p_token"])
            return {"state": "planning", "item": copy.deepcopy({k: v for k, v in item.items() if k != "bus"})}
        assert item["token"] == args["p_token"]
        if name == "support_slack_replay_hold":
            item.update(state="held", reason=args["p_reason"])
            return {"state": "held", "reason": item["reason"]}
        assert name == "support_slack_replay_commit"
        plan = args["p_plan"]
        if item["state"] == "committed":
            assert item["plan"] == plan
            return {"state": "committed", "decision": copy.deepcopy(plan["decision"])}
        if not self.current(bus, item):
            item.update(state="held", reason="snapshot_changed")
            return {"state": "held", "reason": item["reason"]}
        tid = item["ticket_id"]
        assert plan["decision"]["ticket_id"] == tid
        bus.set_ticket(tid, **copy.deepcopy(plan["ticket_fields"]))
        version = bus.tickets[tid].get("request_version", 0)
        for row in copy.deepcopy(plan["rows"]):
            assert row["ticket_id"] == tid and row["direction"] == "outbound"
            assert row["delivery_status"] in ("ready", "held")
            row["attachments"].update(slack_replay_id=item["id"],
                                      slack_replay_contract=replay.CONTRACT,
                                      request_version=version)
            row.update(delivery_request_version=version, slack_ts=None,
                       created_at=bus._ts() if hasattr(bus, "_ts") else "2026-10-09T00:00:00+00:00")
            bus.msgs.append(row)
        item.update(state="committed", plan=copy.deepcopy(plan),
                    committed_ticket=copy.deepcopy(bus.tickets[tid]))
        return {"state": "committed", "decision": copy.deepcopy(plan["decision"])}

    @staticmethod
    def current(bus, item):
        return (bus.tickets[item["ticket_id"]] == item["ticket_snapshot"]
                and bus.messages(item["ticket_id"]) == item["messages_snapshot"])

    def delivery(self, bus, name, args):
        row = next(row for row in bus.msgs if row["id"] == args["p_message_id"])
        att = row["attachments"]
        item = self.items[att["slack_replay_id"]]
        ticket = bus.tickets[row["ticket_id"]]
        snapshot = item["committed_ticket"]
        source_row = next(r for r in item["plan"]["rows"] if r["id"] == row["id"])
        fields = ("id", "client_id", "product", "source", "bot_identity", "slack_user_id",
                  "slack_channel_id", "slack_thread_ts", "request_version", "classification",
                  "status", "hold_tier", "escalated", "verification_before", "verification_after")
        released = bool(att.get("released_by")) and source_row["delivery_status"] == "held"
        allowed = (item["state"] == "committed" and item["bot_identity"] == args["p_identity"]
                   and row["body"] == source_row["body"]
                   and all(att.get(k) == v for k, v in source_row["attachments"].items())
                   and row["delivery_request_version"] == snapshot.get("request_version", 0)
                   and not att.get("slack_replay_delivery_uncertain")
                   and all(ticket.get(k) == snapshot.get(k) for k in fields
                           if not (released and k in ("status", "hold_tier", "escalated")))
                   and [m for m in bus.messages(row["ticket_id"]) if m["direction"] == "inbound"]
                   == [m for m in item["messages_snapshot"] if m["direction"] == "inbound"])
        if name == "support_slack_replay_dispatch_allowed":
            return {"allowed": allowed and row["delivery_status"] in ("ready", "posting")}
        if name == "support_slack_replay_delivery_claim":
            if not allowed or row["delivery_status"] != "ready":
                return {"row": None}
            row["delivery_status"] = "posting"
            att["slack_replay_delivery_token"] = args["p_token"]
            att["claimed_at"] = bus._ts()
        elif name == "support_slack_replay_delivery_finish":
            assert att["slack_replay_delivery_token"] == args["p_token"]
            if allowed and row["delivery_status"] == "posting":
                row.update(delivery_status="posted", slack_ts=args["p_ts"])
            else:
                row["delivery_status"] = "held"
                att.update(slack_replay_delivery_uncertain=True,
                           slack_replay_provider_ts=args["p_ts"])
        elif name == "support_slack_replay_delivery_hold":
            row["delivery_status"] = "held"
            att["held_why"] = args["p_reason"]
        elif name == "support_slack_replay_delivery_resolve":
            assert att["slack_replay_delivery_token"] == args["p_token"]
            resolved = allowed and row["delivery_status"] == "posted" and bool(ticket.get("verification_after"))
            if resolved:
                ticket.update(status="resolved", resolved_at=bus._ts())
            return {"resolved": resolved, "ticket": copy.deepcopy(ticket)}
        else:
            raise AssertionError(name)
        return {"row": copy.deepcopy(row)}


def handle_event(event, raw_event_id, deps):
    """Use the canonical key supplied by the real listener for a Slack message."""
    from agent.slack_convo import adapter
    event = copy.deepcopy(event)
    event.setdefault("_raw_event_id", raw_event_id)
    return adapter.handle_event(event, f'{event["channel"]}:{event["ts"]}', deps)
