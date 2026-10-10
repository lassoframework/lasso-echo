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

# Portal 0633 durable support-resolution-send admission lane. The fence receipt is
# not drained until this durable lane is paused with zero unresolved invocations;
# a row stamped with an admission lease but no trusted completion has an unknown
# durable effect and blocks drain no matter what delivery_status it now carries.
_LANE = "support-resolution-send"
_ADMISSION_KEY = "support_resolution_send_admission"
_COMPLETION_KEY = "support_resolution_send_completion"
_LANE_KEYS = ("lane", "generation", "unresolved", "paused", "drained", "operation_id")


def _validated_lane_status(value):
    """Fail-closed 0633 lane binding: exact lane, paused, drained, zero unresolved,
    valid generation and operation id. Anything else is not a drain basis."""
    if not isinstance(value, dict):
        return None
    generation, unresolved = value.get("generation"), value.get("unresolved")
    if (value.get("lane") != _LANE
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


def _trusted_admission_completion(row):
    """A completed 0633 row needs its finish receipt bound to this exact scanned row.

    Fail closed unless the admission lease binding matches the row id, ticket id,
    request version and message body sha256, the row is terminal posted with a
    Slack timestamp, and a verified readback proof confirms the exact body,
    timestamp, authenticated sender and channel recorded in the lease binding.
    """
    if not isinstance(row, dict):
        return False
    att = row.get("attachments")
    if not isinstance(att, dict):
        return False
    lease, done = att.get(_ADMISSION_KEY), att.get(_COMPLETION_KEY)
    # The ordinary outbox stores the proof under a dedicated key. The current
    # notice sender stores the same verified fields directly in attachments.
    proof = att.get("support_resolution_send_readback")
    if proof is None and att.get("delivery_readback_verified") is True:
        proof = att
    if not isinstance(lease, dict) or not isinstance(done, dict) or not isinstance(proof, dict):
        return False
    binding = lease.get("binding")
    if not isinstance(binding, dict) or not isinstance(binding.get("ticket"), dict):
        return False
    body = row.get("body")
    body_sha = hashlib.sha256(body.encode()).hexdigest() if isinstance(body, str) else None
    slack_ts = row.get("slack_ts")
    return (
        # Lease binding pinned to the exact scanned message and ticket row.
        binding.get("message_id") == row.get("id")
        and binding["ticket"].get("id") == row.get("ticket_id")
        and type(binding["ticket"].get("request_version")) is int
        and binding["ticket"]["request_version"] == row.get("delivery_request_version")
        and body_sha is not None and binding.get("body_sha256") == body_sha
        # Terminal posted with a Slack timestamp.
        and row.get("delivery_status") == "posted"
        and isinstance(slack_ts, str) and bool(slack_ts)
        # Verified readback proof of the exact posted message.
        and proof.get("delivery_readback_verified") is True
        and proof.get("delivery_readback_ts") == slack_ts
        and proof.get("delivery_readback_body_sha256") == binding["body_sha256"]
        and proof.get("delivery_readback_request_version")
            == binding["ticket"]["request_version"]
        # Authenticated sender/channel where row data supports them.
        and isinstance(binding.get("sender"), str) and bool(binding["sender"])
        and proof.get("delivery_readback_sender") == binding["sender"]
        and isinstance(binding.get("channel"), str) and bool(binding["channel"])
        and proof.get("delivery_readback_channel") == binding["channel"]
        and proof.get("delivery_readback_thread_ts") == binding.get("thread_ts")
        and proof.get("delivery_readback_request_key") == binding.get("request_key")
        and (att.get("identity") is None
             or att.get("identity") == binding.get("sender_identity"))
        # Completion receipt recorded against the exact lease invocation/generation.
        and lease.get("lane") == _LANE and done.get("lane") == _LANE
        and done.get("recorded") is True
        and done.get("outcome") == "completed"
        and isinstance(lease.get("invocation_id"), str) and bool(lease["invocation_id"])
        and done.get("invocation_id") == lease["invocation_id"]
        and type(lease.get("generation")) is int
        and type(done.get("generation")) is int
        and done.get("generation") == lease["generation"])


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
                lane_before = lane_status()
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
                        if (row["attachments"].get(_ADMISSION_KEY) is not None
                                and not _trusted_admission_completion(row)):
                            blockers.append(f"admission_unresolved:{row['id']}")
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
                lane_after = lane_status()
                # A lane operation/generation change during the message scans
                # invalidates every conclusion drawn from them.
                if (_validated_lane_status(lane_before) is None
                        or _validated_lane_status(lane_after) is None):
                    blockers.append("admission_lane_invalid")
                elif any(lane_before.get(key) != lane_after.get(key)
                         for key in _LANE_KEYS):
                    blockers.append("admission_lane_changed")
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
