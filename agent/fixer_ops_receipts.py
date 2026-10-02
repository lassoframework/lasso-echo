"""Durable keyed reservations for the FIXER's ops actions (agent/fixer_ops.py).

An ops action with a `reservation_key` runs AT MOST ONCE per key: begin() writes a
durable receipt (status "reserved") BEFORE any side effect; the runner then commit()s
the result, fail()s a refusal, or mark_unknown()s when the outcome after the
reservation cannot be determined. A replay (same key, same payload hash) returns the
stored receipt and the caller must NOT re-execute side effects. A key reused with a
different payload is a 409 reservation_conflict. A receipt whose status is "unknown"
is NEVER retried automatically: begin() raises reservation_outcome_unknown (409)
every time, and a person reconciles it by hand.

The receipt shape:
    {"schema_version": 1, "key", "action", "gym_key", "ticket_id", "payload_hash",
     "status": "reserved"|"done"|"failed"|"unknown",
     "request_key"?: current requester SHA for keyed swap_media,
     "created_at": ISO utc, "result": None|{...}, "error": None|str,
     "finished_at": None|ISO utc}

Store: any mapping-like object keyed by the reservation key with dict receipts as
values (a plain dict works in tests). default_store() is backed by the worker's
durable sqlite kv (agent.db kv_get/kv_set, key prefix "ops_receipt_", JSON values).
Any store failure is wrapped as ReceiptError("store_unavailable", 503) -- a reservation
that cannot be persisted cannot authorize a side effect.

commit()/fail()/mark_unknown() follow a successful begin(); calling one for a key with
no reservation raises ReceiptError("unknown_receipt", 404).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

STORE_PREFIX = "ops_receipt_"
KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
REQUEST_KEY_RE = re.compile(r"[0-9a-f]{64}")

_LOCK = threading.Lock()


class ReceiptError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _validate_key(key):
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ReceiptError("bad_reservation_key", 400)


def payload_hash(action, gym_key, ticket_id, args, request_key=None):
    """sha256 hex of the canonical reservation payload -- the identity a replay must match."""
    payload = {"action": action, "gym_key": gym_key, "ticket_id": ticket_id,
               "args": args}
    if request_key is not None:
        if not isinstance(request_key, str) or not REQUEST_KEY_RE.fullmatch(request_key):
            raise ReceiptError("bad_request_key", 400)
        payload["request_key"] = request_key
    canonical = json.dumps(
        payload,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load(store, key):
    try:
        return store[key]
    except KeyError:
        return None
    except ReceiptError:
        raise
    except Exception as e:  # noqa: BLE001 - an unreadable store cannot authorize a write
        raise ReceiptError("store_unavailable", 503) from e


def _save(store, key, receipt):
    try:
        store[key] = receipt
    except ReceiptError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ReceiptError("store_unavailable", 503) from e


def begin(store, key, action, gym_key, ticket_id, args, request_key=None):
    """Reserve `key` for this exact payload, or replay/conflict against the existing receipt."""
    _validate_key(key)
    digest = payload_hash(action, gym_key, ticket_id, args, request_key=request_key)
    with _LOCK:
        proposed = {"schema_version": 1, "key": key, "action": action,
                    "gym_key": gym_key, "ticket_id": ticket_id, "payload_hash": digest,
                    "status": "reserved", "created_at": _now_iso(),
                    "result": None, "error": None, "finished_at": None}
        if request_key is not None:
            proposed["request_key"] = request_key
        # The production store claims under SQLite BEGIN IMMEDIATE. A Python lock
        # alone does not protect another Echo process using the same volume.
        existing = store.reserve(key, proposed) if hasattr(store, "reserve") else _load(store, key)
        if existing is not None:
            state = existing.get("status")
            if state == "unknown":
                raise ReceiptError("reservation_outcome_unknown", 409)
            if existing.get("payload_hash") != digest:
                raise ReceiptError("reservation_conflict", 409)
            if state == "reserved":
                raise ReceiptError("reservation_in_progress", 409)
            if state == "failed":
                raise ReceiptError("reservation_previously_failed", 409)
            if state != "done":
                raise ReceiptError("reservation_outcome_unknown", 409)
            receipt = dict(existing)
            receipt["replay"] = True
            return receipt
        if not hasattr(store, "reserve"):
            _save(store, key, proposed)
        return dict(proposed)


def _finish(store, key, status, *, result=None, error=None, http_status=None):
    _validate_key(key)
    existing = _load(store, key)
    if existing is None:
        raise ReceiptError("unknown_receipt", 404)
    receipt = dict(existing)
    receipt.pop("replay", None)
    receipt["status"] = status
    if result is not None:
        receipt["result"] = result
    if error is not None:
        receipt["error"] = str(error)
    if http_status is not None:
        receipt["http_status"] = http_status
    receipt["finished_at"] = _now_iso()
    _save(store, key, receipt)
    return receipt


def commit(store, key, result, http_status=200):
    """The reserved action succeeded: record its result."""
    return _finish(store, key, "done", result=result, http_status=http_status)


def fail(store, key, error):
    """The reserved action was refused before/at its side effect: record why."""
    return _finish(store, key, "failed", error=error)


def mark_unknown(store, key, detail):
    """The outcome after the reservation cannot be determined. Terminal for automation:
    begin() refuses this key forever after; a person reconciles by hand."""
    return _finish(store, key, "unknown", error=detail)


def get_receipt(store, key, gym_key):
    """Tenant-bound read: never return another gym's receipt."""
    _validate_key(key)
    receipt = _load(store, key)
    if receipt is None:
        raise ReceiptError("unknown_receipt", 404)
    if receipt.get("gym_key") != gym_key:
        raise ReceiptError("receipt_tenant_mismatch", 403)
    return dict(receipt)


class KvReceiptStore:
    """Mapping-like store over the worker's durable sqlite kv (agent.db). Values are
    JSON; keys are prefixed so receipts never collide with other kv tenants."""

    def __init__(self, prefix=STORE_PREFIX):
        self._prefix = prefix

    @staticmethod
    def _durable_db():
        from . import db
        if not db.kv_is_durable():
            raise ReceiptError("receipt_store_not_durable", 503)
        return db

    def reserve(self, key, receipt):
        """Atomically insert a reservation across all processes on this volume."""
        db = self._durable_db()
        try:
            # Open the already-initialized volume database directly. db.connect()
            # runs schema migrations on each call, which needlessly contends with
            # other reservation processes before they can acquire this lock.
            conn = sqlite3.connect(db.db_path(), timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT value FROM kv WHERE key=?",
                                   (self._prefix + key,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)",
                                 (self._prefix + key, json.dumps(receipt, ensure_ascii=False)))
                conn.commit()
                return json.loads(row["value"]) if row is not None else None
            finally:
                conn.close()
        except ReceiptError:
            raise
        except Exception as e:  # noqa: BLE001 - never authorize on storage fault
            raise ReceiptError("store_unavailable", 503) from e

    def __getitem__(self, key):
        db = self._durable_db()
        raw = db.kv_get(self._prefix + key, default=None)
        if raw is None:
            raise KeyError(key)
        return json.loads(raw)

    def __setitem__(self, key, receipt):
        db = self._durable_db()
        db.kv_set(self._prefix + key, json.dumps(receipt, ensure_ascii=False))


def default_store():
    return KvReceiptStore()
