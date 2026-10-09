"""Process-local support_messages cutover fence; never a fleet drain assertion.

An optional atomic JSON control file permits draining without killing this process.
The env pause is also supported, but changing deployment env can restart replicas.
All support operations admitted before pause finish; new operations do not mutate rows.
"""
import functools
import json
import os
import socket
import threading
import uuid
from contextlib import contextmanager

_LOCK = threading.RLock()
_LOCAL = threading.local()
_ACTIVE = {}
_PROCESS = str(uuid.uuid4())


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


def guarded(lane, refused):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with admission(lane) as allowed:
                if not allowed:
                    return refused()
                return fn(*args, **kwargs)
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
                                    "fixer_route_pending", "outreach_delivery_uncertain"))):
                            blockers.append(f"{status}:{row.get('id', 'unknown')}")
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
            "hostname": socket.gethostname(), "replica_id": replica,
            "deployed_sha": sha, "active": active, "blockers": blockers,
            "local_drained": bool(local_drained), "fleet_drained": False}
