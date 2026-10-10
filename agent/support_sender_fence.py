"""Process-local support_messages cutover fence; never a fleet drain assertion.

An optional atomic JSON control file permits draining without killing this process.
The env pause is also supported, but changing deployment env can restart replicas.
All support operations admitted before pause finish; new operations do not mutate rows.
"""
import functools
import hashlib
import json
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

_LOCK = threading.RLock()
_LOCAL = threading.local()
_ACTIVE = {}
_PROCESS = str(uuid.uuid4())

# Portal 0633 durable support-effect admission lanes. The fence receipt is not
# drained until BOTH durable lanes are paused with zero unresolved invocations and
# the complete paginated 0633 invocation inventory has been verified: every
# historical invocation is correlated to its immutable ticket/message binding, and
# every completed send is still held for independent external verification.
# support_messages.attachments, body, Slack timestamp and routing are mutable,
# so the message row cannot prove the exact admitted Slack effect by itself.
# support-ticket-close has no caller in this repo yet, so ANY
# historical close-lane invocation fails closed until a verified close path exists.
_LANE = "support-resolution-send"
_CLOSE_LANE = "support-ticket-close"
_LANES = (_LANE, _CLOSE_LANE)
_ADMISSION_KEY = "support_resolution_send_admission"
_READBACK_KEY = "support_resolution_send_readback"
_LANE_KEYS = ("lane", "generation", "unresolved", "paused", "drained", "operation_id")
_INVENTORY_PAGE = 200
_INVENTORY_MAX_PAGES = 1000


def _validated_lane_status(value, lane):
    """Fail-closed 0633 lane binding: exact lane, paused, drained, zero unresolved,
    valid generation and operation id. Anything else is not a drain basis."""
    if not isinstance(value, dict):
        return None
    generation, unresolved = value.get("generation"), value.get("unresolved")
    if (value.get("lane") != lane
            or type(generation) is not int or not 0 <= generation <= 2**53 - 1
            or type(unresolved) is not int or unresolved != 0
            or type(value.get("paused")) is not bool
            or type(value.get("drained")) is not bool
            or value["drained"] != (value["paused"] and unresolved == 0)
            or not isinstance(value.get("operation_id"), str)
            or not value["operation_id"]
            or value["paused"] is not True):
        return None
    return value


def _inventory_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _scan_lane_inventory(bus, lane, status):
    """Fail-closed paginated 0633 inventory scan for one paused lane.

    Validates every page: echoed lane/generation/operation/paused must equal the
    pre-scan status, limit/returned/has_more must be consistent, items must be
    strictly increasing over the (started_at, invocation_id) keyset with no
    duplicates, and the next cursor must equal the last returned item. Any defect
    stops the scan and blocks; a partial scan is never a drain basis.
    """
    inventory = getattr(bus, "support_admission_inventory", None)
    if not callable(inventory):
        return [], [f"admission_inventory_unavailable:{lane}"]
    malformed = f"admission_inventory_malformed:{lane}"
    items, seen, seen_ids = [], set(), set()
    after_started = after_invocation = None
    for _ in range(_INVENTORY_MAX_PAGES):
        try:
            page = inventory(lane, limit=_INVENTORY_PAGE,
                             after_started=after_started,
                             after_invocation=after_invocation)
        except Exception as exc:
            return [], [f"admission_inventory_read:{lane}:{type(exc).__name__}"]
        rows = page.get("invocations") if isinstance(page, dict) else None
        if (not isinstance(page, dict) or not isinstance(rows, list)
                or type(page.get("limit")) is not int
                or page["limit"] != _INVENTORY_PAGE
                or type(page.get("returned")) is not int
                or page["returned"] != len(rows) or len(rows) > _INVENTORY_PAGE
                or type(page.get("has_more")) is not bool
                or (page["has_more"] and len(rows) != _INVENTORY_PAGE)
                or page.get("lane") != lane
                or type(page.get("generation")) is not int
                or page["generation"] != status["generation"]
                or page.get("operation_id") != status["operation_id"]
                or page.get("paused") is not True):
            return [], [malformed]
        previous = ((after_started, after_invocation)
                    if after_started is not None else None)
        for row in rows:
            key = (row.get("started_at") if isinstance(row, dict) else None,
                   row.get("invocation_id") if isinstance(row, dict) else None)
            if (not isinstance(row, dict)
                    or not isinstance(key[0], str) or not key[0]
                    or not _inventory_uuid(key[1])
                    or row.get("lane") != lane
                    or key in seen or key[1] in seen_ids
                    or (previous is not None and key <= previous)):
                return [], [malformed]
            seen.add(key)
            seen_ids.add(key[1])
            previous = key
            items.append(row)
        next_started = page.get("next_after_started")
        next_invocation = page.get("next_after_invocation")
        if rows:
            if next_started != items[-1].get("started_at") or \
                    next_invocation != items[-1].get("invocation_id"):
                return [], [malformed]
        elif page["has_more"] or next_started is not None or next_invocation is not None:
            return [], [malformed]
        if not page["has_more"]:
            return items, []
        after_started, after_invocation = next_started, next_invocation
    return [], [f"admission_inventory_unbounded:{lane}"]


def _verify_completed_send(bus, row):
    """Inspect the bound row, but never clear a completed send from mutable data.

    The 0633 binding fixes ticket/version/message identity. It does not freeze
    destination, sender, body or Slack timestamp. A stored readback is also
    mutable. Even a fresh provider match against those mutable values would not
    establish which exact post this invocation made, so external reconciliation
    remains required until the durable contract carries that identity.
    """
    blocker = f"admission_completed_unverified:{row['invocation_id']}"
    ticket_id, version, message_id = (row.get("ticket_id"),
                                      row.get("request_version"),
                                      row.get("message_id"))
    if (not _inventory_uuid(ticket_id) or not _inventory_uuid(message_id)
            or type(version) is not int or version < 0):
        return blocker
    try:
        message = bus.message(message_id)
    except Exception:
        return blocker
    if not isinstance(message, dict):
        return blocker
    att = message.get("attachments")
    body = message.get("body")
    if (message.get("id") != message_id or message.get("ticket_id") != ticket_id
            or message.get("direction") != "outbound"
            or message.get("delivery_request_version") != version
            or message.get("delivery_status") != "posted"
            or not isinstance(body, str)
            or not isinstance(message.get("slack_ts"), str)
            or not message["slack_ts"] or not isinstance(att, dict)):
        return blocker
    proof = att.get(_READBACK_KEY)
    if (not isinstance(proof, dict)
            or proof.get("delivery_readback_verified") is not True
            or proof.get("delivery_readback_ts") != message["slack_ts"]
            or proof.get("delivery_readback_body_sha256")
            != hashlib.sha256(body.encode()).hexdigest()):
        return blocker
    return f"admission_external_verification_required:{message_id}"


def _verify_invocation(bus, lane, row):
    """Classify one durable invocation; unresolved effects and ambiguity block.

    Completed sends also block pending an immutable intent and external receipt.
    """
    outcome = row.get("outcome")
    unresolved = row.get("unresolved")
    ended = row.get("ended_at")
    generation = row.get("generation")
    invocation = row.get("invocation_id")
    if (outcome not in ("running", "completed", "unknown")
            or type(unresolved) is not bool or type(generation) is not int
            or generation < 0 or generation > 2**53 - 1
            or unresolved != (outcome != "completed")
            or (outcome == "running") != (ended is None)
            or (ended is not None and not isinstance(ended, str))):
        return f"admission_invocation_malformed:{lane}:{invocation}"
    bound = [row.get("ticket_id"), row.get("request_version"), row.get("message_id")]
    if outcome == "running":
        return f"admission_running:{lane}:{invocation}"
    if outcome == "unknown":
        return f"admission_unknown:{lane}:{invocation}"
    if any(value is None for value in bound):
        # A legacy pre-0633 invocation has no immutable ticket/message binding;
        # its durable effect can never be reconstructed from mutable rows.
        return f"admission_legacy_unbound:{lane}:{invocation}"
    if lane == _CLOSE_LANE:
        # No ticket-close caller or independent close verifier exists in this
        # repo yet. Any historical close-lane completion fails closed.
        return f"admission_close_unverified:{lane}:{invocation}"
    return _verify_completed_send(bus, row)


def control():
    if os.getenv("SUPPORT_MESSAGES_FENCE_ENABLED", "").lower() != "true":
        return {"enabled": False, "paused": False, "generation": None, "error": None}
    try:
        path = os.getenv("SUPPORT_MESSAGES_FENCE_CONTROL_FILE")
        if path:
            with open(path, encoding="utf-8") as stream:
                value = json.load(stream)
            paused = value["paused"]
            generation = value["generation"]
        else:
            raw_pause = os.getenv("SUPPORT_MESSAGES_FENCE_PAUSED", "true").lower()
            if raw_pause not in ("true", "false"):
                raise ValueError("invalid pause flag")
            paused = raw_pause == "true"
            generation = os.getenv("SUPPORT_MESSAGES_FENCE_GENERATION")
        if type(paused) is not bool or not isinstance(generation, str) or not generation.strip():
            raise ValueError("invalid fence control")
        return {"enabled": True, "paused": paused, "generation": generation, "error": None}
    except Exception as exc:
        return {"enabled": True, "paused": True, "generation": None,
                "error": type(exc).__name__}


@contextmanager
def admission(lane):
    # Nested sends belong to the already admitted operation, which drain waits for.
    nested = getattr(_LOCAL, "depth", 0) > 0
    with _LOCK:
        allowed = nested or not control()["paused"]
        if allowed:
            _LOCAL.depth = getattr(_LOCAL, "depth", 0) + 1
            if not nested:
                _ACTIVE[lane] = _ACTIVE.get(lane, 0) + 1
    try:
        yield allowed
    finally:
        if allowed:
            with _LOCK:
                _LOCAL.depth -= 1
                if not nested:
                    _ACTIVE[lane] -= 1
                    if not _ACTIVE[lane]:
                        del _ACTIVE[lane]


def guarded(lane, refused, *, receipt_on_pause=False):
    """Optionally log the paused receipt from the actual bus-owning process.

    Portal passes run even when Slack conversation listener health is disabled.
    Emit after admission exits so the completed pass does not count itself active.
    Receipt collection is read-only and never replaces the guarded return value.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                with admission(lane) as allowed:
                    if not allowed:
                        return refused()
                    return fn(*args, **kwargs)
            finally:
                if receipt_on_pause and control()["paused"]:
                    bus = args[0] if args else kwargs.get("bus")
                    log = kwargs.get("log", print)
                    try:
                        log("[support-sender-fence] " + json.dumps(
                            {"lane": lane, **receipt(bus)}, sort_keys=True))
                    except Exception:
                        # A failed health sink cannot replay or change a send outcome.
                        pass
        return wrapped
    return decorate


def receipt(bus=None):
    """Fresh local acknowledgment. DB posting/uncertainty is never auto-recovered here.

    No replica id is synthesized: process UUID and hostname identify this process only.
    The operator must inventory replicas and match each UUID, generation and deployed SHA.
    """
    with _LOCK:
        state = control()
        active = dict(_ACTIVE)
    blockers = []
    if state["enabled"] and state["paused"]:
        if bus is None:
            blockers.append("database_not_checked")
        else:
            try:
                lane_status = getattr(bus, "support_admission_status_lane", None)
                if not callable(lane_status):
                    raise RuntimeError("admission_lane_unavailable")
                uncertain = bus.support_uncertain_outbound(limit=1000)
                if not isinstance(uncertain, list) or len(uncertain) >= 1000:
                    blockers.append("uncertain_scan_incomplete")
                else:
                    for row in uncertain:
                        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                                or not row["id"].strip() or row.get("direction") != "outbound"
                                or not isinstance(row.get("attachments"), dict)):
                            blockers.append("uncertain_row_malformed")
                            continue
                        if row["attachments"].get(_ADMISSION_KEY) is not None:
                            # JSON on a mutable row cannot prove the durable
                            # invocation finished; external verification required.
                            blockers.append(
                                "admission_external_verification_required:"
                                f"{row['id']}")
                        if any(row["attachments"].get(key) for key in (
                                "fixer_slack_delivery_uncertain", "fixer_route_uncertain",
                                "fixer_route_pending", "outreach_delivery_uncertain",
                                "slack_replay_delivery_uncertain")):
                            blockers.append(f"uncertain:{row['id']}")
                for status in ("posting", "held"):
                    rows = bus.outbox(status, limit=1000)
                    if not isinstance(rows, list) or len(rows) >= 1000:
                        blockers.append(f"{status}_scan_incomplete")
                        continue
                    for row in rows:
                        if (not isinstance(row, dict)
                                or not isinstance(row.get("id"), str)
                                or not row["id"].strip()
                                or row.get("direction") != "outbound"
                                or row.get("delivery_status") != status
                                or ("attachments" in row
                                    and not isinstance(row["attachments"], dict))):
                            blockers.append(f"{status}_row_malformed")
                            continue
                        att = row.get("attachments", {})
                        if (status == "posting"
                                or any(key in att for key in (
                                    "fixer_slack_delivery_intent", "outreach_slack_delivery_intent"))
                                or any(att.get(key) for key in (
                                    "fixer_slack_delivery_uncertain", "fixer_route_uncertain",
                                    "fixer_route_pending", "outreach_delivery_uncertain",
                                    "slack_replay_delivery_uncertain"))):
                            blockers.append(f"{status}:{row.get('id', 'unknown')}")
                # Both support-effect lanes must be paused/drained with a fully
                # verified paginated 0633 invocation inventory. A lane
                # operation/generation change anywhere during the scans
                # invalidates every conclusion drawn from them.
                observed_lane_statuses = {}
                for lane in _LANES:
                    before = _validated_lane_status(lane_status(lane), lane)
                    if before is None:
                        blockers.append(f"admission_lane_invalid:{lane}")
                        continue
                    observed_lane_statuses[lane] = before
                    items, scan_blockers = _scan_lane_inventory(bus, lane, before)
                    blockers.extend(scan_blockers)
                    if not scan_blockers:
                        for item in items:
                            blocker = _verify_invocation(bus, lane, item)
                            if blocker:
                                blockers.append(blocker)
                        unresolved = sum(1 for item in items if item["unresolved"])
                        if unresolved != before["unresolved"]:
                            blockers.append(f"admission_inventory_inconsistent:{lane}")
                    after = _validated_lane_status(lane_status(lane), lane)
                    if after is None:
                        blockers.append(f"admission_lane_invalid:{lane}")
                    elif any(after.get(key) != before.get(key)
                             for key in _LANE_KEYS):
                        blockers.append(f"admission_lane_changed:{lane}")
                # Lane one may resume while lane two is being scanned. Check
                # both again after all inventory and message reads complete.
                for lane, before in observed_lane_statuses.items():
                    final = _validated_lane_status(lane_status(lane), lane)
                    if final is None or any(final.get(key) != before.get(key)
                                            for key in _LANE_KEYS):
                        blockers.append(f"admission_lane_changed:{lane}")
            except Exception as exc:
                blockers.append(f"database_read:{type(exc).__name__}")
    # A control replacement during DB read invalidates this acknowledgment.
    if control() != state:
        blockers.append("control_changed_during_read")
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("SUPPORT_MESSAGES_DEPLOYED_SHA")
    replica = os.getenv("RAILWAY_REPLICA_ID")
    local_drained = (state["enabled"] and state["paused"] and not state["error"]
                     and not active and not blockers and bool(sha))
    return {**state, "process_id": _PROCESS, "pid": os.getpid(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "hostname": socket.gethostname(), "replica_id": replica,
            "deployed_sha": sha, "active": active, "blockers": blockers,
            "local_drained": bool(local_drained), "fleet_drained": False}
