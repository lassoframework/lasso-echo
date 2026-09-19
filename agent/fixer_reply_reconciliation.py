"""Immutable reply-alert snapshots and read-only reconciliation for FIXER.

This module deliberately stops short of replying, hiding, deleting, or otherwise
changing provider state.  A reconciliation result is only a description of what
the provider's read APIs prove now compared with the exact evidence stored when an
inbox alert was created.

The safety model is intentionally strict:

* snapshots are tenant bound, immutable, and durable on the Echo worker volume;
* an item is identified by source + provider + account + container + item id;
* duplicate identities make the snapshot ambiguous and are rejected;
* pagination must explicitly say ``complete: true``.  Absence is not completeness;
* source errors, partial pages, missing identities, missing rows, and old snapshots
  never become success evidence;
* mentions always remain ``needs_human`` because the current provider surface has
  no verified reply-thread read for them;
* comments resolve only from an explicit owner reply (or an explicit hidden flag
  for an item originally classified as spam); reviews resolve only from the
  provider's explicit ``hasReply`` flag.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
STORE_PREFIX = "fixer_reply_snapshot_"
MAX_SNAPSHOT_AGE_SECONDS = 48 * 3600
SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{32}")
GYM_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}")
SOURCES = frozenset({"comment", "review", "mention"})


class ReconciliationError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pagination_complete(payload):
    """True only for the one explicit completeness assertion this adapter knows.

    Zernio's local contract says responses contain ``pagination`` but does not
    document cursor or last-page semantics.  Inferring completeness from a short
    page, a missing cursor, or an unknown pagination field would be unsafe, so the
    reconciliation lane accepts only an explicit adapter assertion.
    """
    pagination = payload.get("pagination") if isinstance(payload, dict) else None
    return isinstance(pagination, dict) and pagination.get("complete") is True


def provider_identity(item):
    """Return the exact five-part provider identity, or None when incomplete."""
    raw = item.get("provider_identity") if isinstance(item, dict) else None
    if not isinstance(raw, dict):
        return None
    identity = {
        "source": raw.get("source"),
        "provider": raw.get("provider"),
        "account_id": raw.get("account_id"),
        "container_id": raw.get("container_id"),
        "item_id": raw.get("item_id"),
    }
    if identity["source"] not in SOURCES:
        return None
    if not all(isinstance(identity[k], str) and identity[k]
               for k in ("provider", "account_id", "container_id", "item_id")):
        return None
    return identity


def identity_key(identity):
    return json.dumps(identity, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _item_snapshot(item):
    identity = provider_identity(item)
    return {
        "identity": identity,
        "kind": item.get("kind"),
        "source": item.get("source"),
        "text_sha256": hashlib.sha256(
            str(item.get("text") or "").encode("utf-8")).hexdigest(),
        "created_at": item.get("created_at"),
        "provider_evidence": dict(item.get("provider_evidence") or {}),
    }


def build_snapshot(gym_key, profile_id, items, source_status, *, now=None,
                   snapshot_id=None):
    """Build an immutable minimal snapshot.  No store or network is touched."""
    if not isinstance(gym_key, str) or not GYM_KEY_RE.fullmatch(gym_key):
        raise ReconciliationError("bad_gym_key", 400)
    if not isinstance(profile_id, str) or not profile_id:
        raise ReconciliationError("missing_profile_id", 409)
    if snapshot_id is None:
        snapshot_id = uuid.uuid4().hex
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise ReconciliationError("bad_snapshot_id", 400)
    if not isinstance(source_status, dict):
        raise ReconciliationError("bad_source_status", 400)

    frozen = [_item_snapshot(i) for i in (items or []) if isinstance(i, dict)]
    identities = [identity_key(i["identity"]) for i in frozen if i["identity"]]
    if len(identities) != len(set(identities)):
        raise ReconciliationError("duplicate_provider_identity", 409)

    statuses = {}
    for source in sorted(SOURCES):
        raw = source_status.get(source) or {}
        statuses[source] = {
            "ok": raw.get("ok") is True,
            "complete": raw.get("complete") is True,
            "error": str(raw.get("error") or "")[:120] or None,
        }
    complete = (bool(frozen)
                and all(i["identity"] is not None for i in frozen)
                and all(i["identity"]["source"] == i["source"] for i in frozen
                        if i["identity"] is not None)
                and all(s["ok"] and s["complete"] for s in statuses.values()))
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "gym_key": gym_key,
        "profile_id": profile_id,
        "captured_at": _utc(now).isoformat(),
        "complete": complete,
        "source_status": statuses,
        "items": frozen,
    }


class KvSnapshotStore:
    """Immutable JSON snapshots in the worker's durable sqlite key/value plane."""
    def __init__(self, prefix=STORE_PREFIX):
        self._prefix = prefix

    @staticmethod
    def _db():
        from . import db
        if not db.kv_is_durable():
            raise ReconciliationError("snapshot_store_not_durable", 503)
        return db

    def save(self, snapshot):
        db = self._db()
        key = self._prefix + snapshot["snapshot_id"]
        try:
            conn = sqlite3.connect(db.db_path(), timeout=30)
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT 1 FROM kv WHERE key=?", (key,)).fetchone()
                if existing is not None:
                    raise ReconciliationError("snapshot_conflict", 409)
                conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)",
                             (key, json.dumps(snapshot, sort_keys=True,
                                              ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
        except ReconciliationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ReconciliationError("snapshot_store_unavailable", 503) from exc

    def load(self, snapshot_id):
        db = self._db()
        try:
            raw = db.kv_get(self._prefix + snapshot_id, default=None)
            if raw is None:
                raise ReconciliationError("snapshot_not_found", 404)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except ReconciliationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ReconciliationError("snapshot_store_unavailable", 503) from exc


def default_store():
    return KvSnapshotStore()


def save_snapshot(snapshot, store=None):
    if store is None:
        store = default_store()
    if hasattr(store, "save"):
        store.save(snapshot)
    else:
        key = snapshot["snapshot_id"]
        if key in store:
            raise ReconciliationError("snapshot_conflict", 409)
        store[key] = json.loads(json.dumps(snapshot))
    return snapshot


def load_snapshot(snapshot_id, gym_key, *, store=None, now=None,
                  max_age_seconds=MAX_SNAPSHOT_AGE_SECONDS):
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        raise ReconciliationError("bad_snapshot_id", 400)
    if not isinstance(gym_key, str) or not GYM_KEY_RE.fullmatch(gym_key):
        raise ReconciliationError("bad_gym_key", 400)
    if store is None:
        store = default_store()
    try:
        snapshot = store.load(snapshot_id) if hasattr(store, "load") else store[snapshot_id]
    except KeyError as exc:
        raise ReconciliationError("snapshot_not_found", 404) from exc
    except ReconciliationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ReconciliationError("snapshot_store_unavailable", 503) from exc
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ReconciliationError("snapshot_invalid", 409)
    if snapshot.get("snapshot_id") != snapshot_id:
        raise ReconciliationError("snapshot_invalid", 409)
    if snapshot.get("gym_key") != gym_key:
        raise ReconciliationError("snapshot_tenant_mismatch", 403)
    captured = _parse_iso(snapshot.get("captured_at"))
    age = (_utc(now) - captured).total_seconds() if captured else None
    if age is None or age < 0 or age > max_age_seconds:
        raise ReconciliationError("snapshot_expired", 409)
    if snapshot.get("complete") is not True:
        raise ReconciliationError("snapshot_incomplete", 409)
    items = snapshot.get("items")
    statuses = snapshot.get("source_status")
    if (not isinstance(snapshot.get("profile_id"), str) or not snapshot["profile_id"]
            or not isinstance(items, list) or not items
            or not isinstance(statuses, dict)
            or any(not isinstance(statuses.get(source), dict)
                   or statuses[source].get("ok") is not True
                   or statuses[source].get("complete") is not True
                   for source in SOURCES)):
        raise ReconciliationError("snapshot_invalid", 409)
    keys = []
    for item in items:
        if not isinstance(item, dict) or item.get("source") not in SOURCES:
            raise ReconciliationError("snapshot_invalid", 409)
        identity = item.get("identity")
        if not isinstance(identity, dict):
            raise ReconciliationError("snapshot_invalid", 409)
        normalized = provider_identity({"provider_identity": identity})
        if normalized != identity or identity["source"] != item["source"]:
            raise ReconciliationError("snapshot_invalid", 409)
        keys.append(identity_key(identity))
    if len(keys) != len(set(keys)):
        raise ReconciliationError("duplicate_provider_identity", 409)
    return json.loads(json.dumps(snapshot))


def _current_comment(item, zernio):
    ident = item["identity"]
    # A one-page inbox read cannot prove a comment disappeared or was answered.
    # Production uses ZernioClient's bounded complete reader, which returns
    # ``pagination.complete`` only after it has traversed coherent page metadata.
    payload = zernio.inbox_post_comments_complete(
        ident["container_id"], ident["account_id"])
    if not pagination_complete(payload):
        return None, "comment_page_incomplete"
    matches = []
    for row in payload.get("comments") or []:
        if not isinstance(row, dict):
            continue
        current = {
            "source": "comment", "provider": row.get("platform"),
            "account_id": ident["account_id"], "container_id": ident["container_id"],
            "item_id": row.get("id"),
        }
        if current == ident:
            matches.append(row)
    if len(matches) != 1:
        return None, "comment_identity_missing_or_ambiguous"
    return matches[0], None


def _current_reviews(snapshot, zernio):
    payload = zernio.list_inbox_reviews_complete(snapshot["profile_id"])
    if not pagination_complete(payload):
        return None, "review_page_incomplete"
    by_identity = {}
    duplicate = set()
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        ident = {
            "source": "review", "provider": row.get("platform"),
            "account_id": row.get("accountId"), "container_id": row.get("id"),
            "item_id": row.get("id"),
        }
        if provider_identity({"provider_identity": ident}) is None:
            continue
        key = identity_key(ident)
        if key in by_identity:
            duplicate.add(key)
        by_identity[key] = row
    if duplicate:
        return None, "duplicate_current_review_identity"
    return by_identity, None


def reconcile_snapshot(snapshot_id, gym_key, *, zernio, store=None, now=None,
                       max_age_seconds=MAX_SNAPSHOT_AGE_SECONDS):
    """Compare an immutable alert snapshot with explicit current provider evidence."""
    snapshot = load_snapshot(snapshot_id, gym_key, store=store, now=now,
                             max_age_seconds=max_age_seconds)
    try:
        current_profile = zernio.find_profile_id(gym_key)
    except Exception:  # noqa: BLE001
        current_profile = None
    if current_profile != snapshot["profile_id"]:
        return {"ok": False, "complete": False, "resolved": False,
                "snapshot_id": snapshot_id, "gym_key": gym_key,
                "reason": "profile_identity_unconfirmed", "items": []}

    review_map = None
    review_error = None
    if any(i.get("source") == "review" for i in snapshot["items"]):
        try:
            review_map, review_error = _current_reviews(snapshot, zernio)
        except Exception:  # noqa: BLE001
            review_error = "review_read_failed"

    results = []
    complete = review_error is None
    for item in snapshot["items"]:
        ident = item.get("identity")
        source = item.get("source")
        result = {"identity": ident, "source": source, "status": "needs_human",
                  "provider_evidence": None}
        if source == "mention":
            result["reason"] = "mention_reply_state_not_supported"
        elif source == "comment":
            try:
                row, error = _current_comment(item, zernio)
            except Exception:  # noqa: BLE001
                row, error = None, "comment_read_failed"
            if error:
                complete = False
                result["reason"] = error
            else:
                owner_replies = [r for r in (row.get("replies") or [])
                                 if isinstance(r, dict)
                                 and (r.get("from") or {}).get("isOwner") is True
                                 and isinstance(r.get("id"), str) and r.get("id")]
                reply_ids = [r["id"] for r in owner_replies]
                if len(reply_ids) != len(set(reply_ids)):
                    complete = False
                    result["reason"] = "duplicate_owner_reply_identity"
                elif owner_replies:
                    result["status"] = "resolved"
                    result["provider_evidence"] = {
                        "owner_reply_ids": reply_ids}
                elif item.get("kind") == "spam" and row.get("isHidden") is True:
                    result["status"] = "resolved"
                    result["provider_evidence"] = {"is_hidden": True}
                else:
                    result["status"] = "pending"
                    result["provider_evidence"] = {
                        "is_hidden": row.get("isHidden") is True,
                        "owner_reply_ids": [],
                    }
        elif source == "review":
            if review_error:
                result["reason"] = review_error
            else:
                row = (review_map or {}).get(identity_key(ident))
                if row is None:
                    complete = False
                    result["reason"] = "review_identity_missing"
                elif row.get("hasReply") is True:
                    result["status"] = "resolved"
                    result["provider_evidence"] = {"has_reply": True}
                else:
                    result["status"] = "pending"
                    result["provider_evidence"] = {"has_reply": False}
        results.append(result)

    resolved = bool(results) and complete and all(r["status"] == "resolved"
                                                  for r in results)
    return {"ok": True, "complete": complete, "resolved": resolved,
            "snapshot_id": snapshot_id, "gym_key": gym_key,
            "captured_at": snapshot["captured_at"], "items": results}
