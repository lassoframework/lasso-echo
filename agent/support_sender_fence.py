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
import stat
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

# Frozen Portal durable support cutover reservation contract (DRAFT_0639 head
# f7ca4a04175be10bf6d401e22e91660c4e63b120; Echo validates strictly and fails
# closed on ANY deviation):
#
#   public.support_cutover_reservation_status(p_reservation_id uuid, p_epoch bigint) -> {
#     "reservation_id": <echoed uuid string>, "epoch": <echoed positive int>,
#     "owner_epoch": <echoed positive int>, "owner": <str>,
#     "pinned": {"support-resolution-send": {"generation": int, "operation_id": uuid str},
#                "support-ticket-close":   { ... same shape ... }},
#     "send_generation": int, "send_operation_id": uuid str,
#     "close_generation": int, "close_operation_id": uuid str,
#     "support-resolution-send": {"lane", "paused": true, "drained": true,
#        "generation", "operation_id", "reservation_id": <echoed>,
#        "unresolved": 0, "pinned_generation", "pinned_operation_id"},
#     "support-ticket-close": { ... same shape ... }}
#   There is NO "state"/"is_current"; the Portal guard itself locks both
#   controls and requires both active pointers, the epoch, paused state and
#   both pinned tuples.
#
#   public.support_cutover_reservation_inventory(p_reservation_id uuid,
#       p_epoch bigint, p_lane text, p_limit integer,
#       p_after_started timestamptz, p_after_invocation uuid) -> {
#     "reservation_id": <echoed>, "epoch": <echoed>, "owner_epoch": <echoed>,
#     "pinned": { BOTH pinned lane tuples, exactly as the status receipt },
#     "send_generation"/"send_operation_id"/"close_generation"/"close_operation_id",
#     "lane": <echoed>, "limit": int, "returned": int, "has_more": bool,
#     "next_after_started": str|null, "next_after_invocation": uuid|null,
#     "invocations": [ ... invocation rows ... ]}
#   There is NO top-level "generation"/"operation_id"/"paused" and no "lanes"
#   map; both pinned tuples are echoed in "pinned" and in the flat duplicates.
#
# One serialized Portal receipt pins BOTH durable lanes under the held
# reservation; Echo never infers a cross-lane snapshot from a status_pair or
# from two independent per-lane status reads. The reservation is re-read after
# all scans and must match the pre-scan receipt exactly.
_RESERVATION_ID_ENV = "SUPPORT_CUTOVER_RESERVATION_ID"
_OWNER_EPOCH_ENV = "SUPPORT_CUTOVER_OWNER_EPOCH"

# The observation counter catches transitions seen by another local operation.
# A cutover receipt additionally requires an atomic control file and compares
# its identity/change metadata across the database scan. That catches a file
# replacement away and back even when no Python thread observes the middle.
_CONTROL_OBSERVATION = {"control": None, "sequence": 0}


def _control_file_fingerprint():
    path = os.getenv("SUPPORT_MESSAGES_FENCE_CONTROL_FILE")
    if not path:
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (path, info.st_dev, info.st_ino, info.st_ctime_ns,
            info.st_mtime_ns, info.st_size)


def _control_sequence():
    """Observe control with a stable file fingerprint when one is configured."""
    before = _control_file_fingerprint()
    current = control()
    after = _control_file_fingerprint()
    fingerprint = before if before is not None and before == after else None
    with _LOCK:
        if (_CONTROL_OBSERVATION["control"] is not None
                and _CONTROL_OBSERVATION["control"] != current):
            _CONTROL_OBSERVATION["sequence"] += 1
        _CONTROL_OBSERVATION["control"] = current
        return current, _CONTROL_OBSERVATION["sequence"], fingerprint


def _valid_reservation_id(value):
    if not _inventory_uuid(value):
        return False
    return value == str(uuid.UUID(value))


def _valid_owner_epoch(value):
    return type(value) is int and 0 < value <= 2**53 - 1


def _reservation_credentials(reservation_id, owner_epoch):
    """Explicit receipt arguments win; operator env is the fallback. Fail closed."""
    if reservation_id is None:
        reservation_id = os.getenv(_RESERVATION_ID_ENV) or None
    if owner_epoch is None:
        raw = os.getenv(_OWNER_EPOCH_ENV)
        if raw:
            try:
                owner_epoch = int(raw, 10)
            except ValueError:
                owner_epoch = raw  # fails validation below
    if reservation_id is None and owner_epoch is None:
        return None, None, None
    if not _valid_reservation_id(reservation_id) or not _valid_owner_epoch(owner_epoch):
        return None, None, "reservation_credentials_malformed"
    return reservation_id, owner_epoch, None


def _valid_pinned_generation(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _valid_pinned_operation_id(value):
    return isinstance(value, str) and _inventory_uuid(value)


def _validated_pinned_lane(value):
    """One pinned durable lane tuple: exactly {generation, operation_id}."""
    if not isinstance(value, dict):
        return None
    generation, operation_id = value.get("generation"), value.get("operation_id")
    if (not _valid_pinned_generation(generation)
            or not _valid_pinned_operation_id(operation_id)):
        return None
    return {"generation": generation, "operation_id": operation_id}


def _pinned_lanes(value):
    """The 'pinned' map, keyed by BOTH durable lane names. Fail closed."""
    if not isinstance(value, dict):
        return None
    pinned = {}
    for lane in _LANES:
        pin = _validated_pinned_lane(value.get(lane))
        if pin is None:
            return None
        pinned[lane] = pin
    return pinned


def _validated_cutover_status(value, reservation_id, owner_epoch):
    """Strict fail-closed validation of the frozen Portal status receipt.

    Requires the exact echoed reservation id, a positive integer epoch under
    BOTH aliases (epoch and owner_epoch), the pinned map keyed by both lane
    names, the flat send_/close_ duplicate tuples matching the pinned map, and
    BOTH nested live lane objects with exact lane name, paused=true,
    drained=true, unresolved=0, the exact reservation pointer and generation/
    operation_id equal to the pinned tuple. Any omission, wrong type or
    mismatch is never a drain basis.
    """
    if not isinstance(value, dict):
        return None
    if value.get("reservation_id") != reservation_id:
        return None
    epoch, alias = value.get("epoch"), value.get("owner_epoch")
    if (not _valid_owner_epoch(epoch) or not _valid_owner_epoch(alias)
            or epoch != owner_epoch or alias != owner_epoch):
        return None
    owner = value.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None
    pinned = _pinned_lanes(value.get("pinned"))
    if pinned is None:
        return None
    for lane, prefix in ((_LANE, "send"), (_CLOSE_LANE, "close")):
        if (value.get(f"{prefix}_generation") != pinned[lane]["generation"]
                or value.get(f"{prefix}_operation_id") != pinned[lane]["operation_id"]):
            return None
    lanes = {}
    for lane in _LANES:
        live = value.get(lane)
        if not isinstance(live, dict):
            return None
        if (live.get("lane") != lane
                or live.get("paused") is not True
                or live.get("drained") is not True
                or type(live.get("unresolved")) is not int
                or live["unresolved"] != 0
                or live.get("reservation_id") != reservation_id
                or live.get("generation") != pinned[lane]["generation"]
                or live.get("operation_id") != pinned[lane]["operation_id"]
                or live.get("pinned_generation") != pinned[lane]["generation"]
                or live.get("pinned_operation_id") != pinned[lane]["operation_id"]):
            return None
        lanes[lane] = dict(pinned[lane])
    return {"reservation_id": reservation_id, "owner_epoch": owner_epoch,
            "lanes": lanes}


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


def _parse_inventory_ts(value):
    """Parse an RFC3339/timestamptz JSON string; None when not canonical-comparable.

    Cursor ordering must never rely on lexical comparison of noncanonical
    timestamp strings (offsets, spaces vs 'T', fractional seconds), so order
    keys use parsed aware datetimes.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _inventory_row_key(row):
    """(order_key, dup_key) for one inventory row, or (None, None) if malformed."""
    if not isinstance(row, dict):
        return None, None
    started, invocation = row.get("started_at"), row.get("invocation_id")
    parsed = _parse_inventory_ts(started)
    if parsed is None or not _inventory_uuid(invocation):
        return None, None
    return (parsed, invocation), (started, invocation)


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
        previous = None
        if after_started is not None:
            previous, _ = _inventory_row_key({
                "started_at": after_started, "invocation_id": after_invocation})
            if previous is None:
                return [], [malformed]
        for row in rows:
            order_key, dup_key = _inventory_row_key(row)
            if (order_key is None
                    or row.get("lane") != lane
                    or dup_key in seen or dup_key[1] in seen_ids
                    or (previous is not None and order_key <= previous)):
                return [], [malformed]
            seen.add(dup_key)
            seen_ids.add(dup_key[1])
            previous = order_key
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


def _scan_lane_inventory_guarded(bus, lane, reservation):
    """Fail-closed paginated inventory scan pinned to the held reservation.

    Every page must echo the reservation id and the epoch under BOTH aliases,
    the 'pinned' map keyed by BOTH durable lane names exactly as the pre-scan
    reservation receipt, and the flat send_/close_ duplicate tuples matching
    it. Any change, omission or mismatch stops the scan and blocks. A partial
    scan is never a drain basis.
    """
    inventory = getattr(bus, "support_cutover_reservation_inventory", None)
    if not callable(inventory):
        return [], [f"admission_inventory_unavailable:{lane}"]
    malformed = f"admission_inventory_malformed:{lane}"
    items, seen, seen_ids = [], set(), set()
    after_started = after_invocation = None
    for _ in range(_INVENTORY_MAX_PAGES):
        try:
            page = inventory(reservation["reservation_id"],
                             reservation["owner_epoch"], lane,
                             limit=_INVENTORY_PAGE,
                             after_started=after_started,
                             after_invocation=after_invocation)
        except Exception as exc:
            return [], [f"admission_inventory_read:{lane}:{type(exc).__name__}"]
        rows = page.get("invocations") if isinstance(page, dict) else None
        epoch, alias = ((page.get("epoch"), page.get("owner_epoch"))
                        if isinstance(page, dict) else (None, None))
        pinned = _pinned_lanes(page.get("pinned")) if isinstance(page, dict) else None
        if (not isinstance(page, dict) or not isinstance(rows, list)
                or page.get("reservation_id") != reservation["reservation_id"]
                or not _valid_owner_epoch(epoch) or not _valid_owner_epoch(alias)
                or epoch != reservation["owner_epoch"]
                or alias != reservation["owner_epoch"]
                or pinned != reservation["lanes"]
                or type(page.get("limit")) is not int
                or page["limit"] != _INVENTORY_PAGE
                or type(page.get("returned")) is not int
                or page["returned"] != len(rows) or len(rows) > _INVENTORY_PAGE
                or type(page.get("has_more")) is not bool
                or (page["has_more"] and len(rows) != _INVENTORY_PAGE)
                or page.get("lane") != lane
                or page.get("send_generation")
                != reservation["lanes"][_LANE]["generation"]
                or page.get("send_operation_id")
                != reservation["lanes"][_LANE]["operation_id"]
                or page.get("close_generation")
                != reservation["lanes"][_CLOSE_LANE]["generation"]
                or page.get("close_operation_id")
                != reservation["lanes"][_CLOSE_LANE]["operation_id"]):
            return [], [malformed]
        previous = None
        if after_started is not None:
            previous, _ = _inventory_row_key({
                "started_at": after_started, "invocation_id": after_invocation})
            if previous is None:
                return [], [malformed]
        for row in rows:
            order_key, dup_key = _inventory_row_key(row)
            if (order_key is None
                    or row.get("lane") != lane
                    or dup_key in seen or dup_key[1] in seen_ids
                    or (previous is not None and order_key <= previous)):
                return [], [malformed]
            seen.add(dup_key)
            seen_ids.add(dup_key[1])
            previous = order_key
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


def receipt(bus=None, *, reservation_id=None, owner_epoch=None):
    """Fresh local acknowledgment. DB posting/uncertainty is never auto-recovered here.

    No replica id is synthesized: process UUID and hostname identify this process only.
    The operator must inventory replicas and match each UUID, generation and deployed SHA.

    With a durable Portal cutover reservation (explicit arguments or the
    SUPPORT_CUTOVER_RESERVATION_ID / SUPPORT_CUTOVER_OWNER_EPOCH env), drain
    requires one serialized, held reservation receipt pinning BOTH durable lanes
    plus guarded paginated inventory scans that re-echo it on every page. Without
    a reservation the legacy per-lane path runs and always stays blocked on the
    missing atomic cross-lane snapshot. fleet_drained is always False: no
    process-local receipt can assert fleet-wide drain.
    """
    reservation_id, owner_epoch, credential_error = _reservation_credentials(
        reservation_id, owner_epoch)
    with _LOCK:
        state, sequence, file_fingerprint = _control_sequence()
        active = dict(_ACTIVE)
    blockers = []
    reservation_held = False
    admission_drained = False
    if credential_error:
        blockers.append(credential_error)
    if state["enabled"] and state["paused"]:
        if bus is None:
            blockers.append("database_not_checked")
        else:
            try:
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
                if credential_error is None and reservation_id is not None:
                    # Durable cutover reservation path: one serialized Portal
                    # receipt pins BOTH lanes under the held reservation, and
                    # every guarded inventory page must re-echo the reservation
                    # id, owner epoch and both pinned lane tuples.
                    status_fn = getattr(bus, "support_cutover_reservation_status", None)
                    if not callable(status_fn):
                        blockers.append("reservation_cutover_status_unavailable")
                    else:
                        reservation = _validated_cutover_status(
                            status_fn(reservation_id, owner_epoch),
                            reservation_id, owner_epoch)
                        if reservation is None:
                            blockers.append("reservation_cutover_invalid")
                        else:
                            lanes_drained = True
                            for lane in _LANES:
                                items, scan_blockers = _scan_lane_inventory_guarded(
                                    bus, lane, reservation)
                                blockers.extend(scan_blockers)
                                if scan_blockers:
                                    lanes_drained = False
                                    continue
                                for item in items:
                                    blocker = _verify_invocation(bus, lane, item)
                                    if blocker:
                                        blockers.append(blocker)
                                unresolved = sum(
                                    1 for item in items if item["unresolved"])
                                if unresolved != 0:
                                    blockers.append(
                                        f"admission_inventory_inconsistent:{lane}")
                                    lanes_drained = False
                            # Re-read the reservation after all scans: any
                            # release, resume, re-pin or pointer change during
                            # the reads invalidates every conclusion from them.
                            after = _validated_cutover_status(
                                status_fn(reservation_id, owner_epoch),
                                reservation_id, owner_epoch)
                            if after is None:
                                blockers.append("reservation_cutover_invalid")
                            elif after != reservation:
                                blockers.append("reservation_cutover_changed")
                            else:
                                reservation_held = True
                                admission_drained = lanes_drained
                elif credential_error is None:
                    lane_status = getattr(bus, "support_admission_status_lane", None)
                    if not callable(lane_status):
                        raise RuntimeError("admission_lane_unavailable")
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
                    # Separate RPCs cannot observe both controls atomically. The
                    # first lane may resume after its final read while the second
                    # lane is being read, with no generation change on resume.
                    # Keep local drain blocked until Portal exposes a paired,
                    # serialized status receipt and release holds it through cutover.
                    blockers.append("admission_cross_lane_snapshot_unverified")
            except Exception as exc:
                blockers.append(f"database_read:{type(exc).__name__}")
    # Recheck local control AND active operations under _LOCK after DB reads.
    # The transition sequence also catches an ABA flip (pause/generation changed
    # away and back during the reads) observed by any in-process control read.
    with _LOCK:
        after_state, after_sequence, after_file_fingerprint = _control_sequence()
        after_active = dict(_ACTIVE)
    if after_sequence != sequence or after_state != state:
        blockers.append("control_changed_during_read")
    if (reservation_id is not None and
            (file_fingerprint is None or after_file_fingerprint != file_fingerprint)):
        blockers.append("control_file_changed_or_unavailable")
    if after_active != active:
        blockers.append("active_operations_changed_during_read")
        for lane, count in after_active.items():
            if count and not active.get(lane):
                active[lane] = count
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("SUPPORT_MESSAGES_DEPLOYED_SHA")
    replica = os.getenv("RAILWAY_REPLICA_ID")
    local_operations_drained = bool(state["enabled"] and state["paused"]
                                    and not state["error"] and not active
                                    and not after_active
                                    and after_sequence == sequence
                                    and (reservation_id is None or
                                         (file_fingerprint is not None and
                                          after_file_fingerprint == file_fingerprint)))
    # Every historical completed send stays blocked until an immutable Portal
    # binding plus a direct Slack provider readback is wired and verified here,
    # so effects_reconciled can never be true while any invocation blocker or
    # uncertain/posting/held row remains.
    effects_reconciled = bool(reservation_held and not blockers)
    local_drained = (local_operations_drained and not blockers and bool(sha))
    return {**state, "process_id": _PROCESS, "pid": os.getpid(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "hostname": socket.gethostname(), "replica_id": replica,
            "deployed_sha": sha, "active": active, "blockers": blockers,
            "local_operations_drained": local_operations_drained,
            "admission_drained": bool(admission_drained),
            "effects_reconciled": effects_reconciled,
            "reservation_held": bool(reservation_held),
            "local_drained": bool(local_drained), "fleet_drained": False}
