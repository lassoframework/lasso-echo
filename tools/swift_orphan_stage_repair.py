"""Attended, fenced maintenance for three frozen Swift orphan stage records.

Default CLI mode reads only supplied offline evidence and SQLite (mode=ro).
Apply requires an independently verified fence, fresh evidence, explicit opt-in,
and a durable journal. Never call the legacy ID-only update_asset API.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3

GYM = "swiftrivercrossfite5c9db"
TARGETS = {
    "1PhOFVHpEm8_ye2a546WT2VGwpRtCXcFr": ("2026-10-16", "2026-10-07T01:05:48.303394+00:00"),
    "1esVxnCv-EU-6N5R_-QYUNxCg9oypjk4s": ("2026-10-21", "2026-10-07T02:02:42.327208+00:00"),
    "1Vqo3q7OdbQcO5txCh_VrRIuFJryrTOlP": ("2026-11-01", "2026-10-07T01:21:54.057128+00:00"),
}
PINS = ("id", "gym_id", "source_id", "content_hash", "drive_modified", "used_count",
        "last_used_at", "review_status", "eligible", "excluded_by_coach")
WRITERS = {"echo_builders", "echo_publishers", "echo_retries", "echo_sweeps",
           "portal_calendar_writers", "portal_media_writers", "drive_sync",
           "manual_scripts", "external_provider_queues"}
CLEAR = ("active_owners", "leases", "queued_writes", "unknown_sends", "calendar_rows",
         "posts", "audit_receipts", "served_receipts", "ops_receipts")


class Blocked(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ts(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise Blocked("invalid timezone-aware timestamp") from exc


def equal(left, right):
    for key in PINS:
        if key not in left or key not in right:
            return False
        if key in ("drive_modified", "last_used_at") and left[key] and right[key]:
            if ts(left[key]) != ts(right[key]):
                return False
        elif left[key] != right[key]:
            return False
    return True


def validate(plan):
    if plan.get("gym_id") != GYM or plan.get("schema") != 1:
        raise Blocked("wrong tenant or schema")
    entries = plan.get("entries", [])
    if len(entries) != 3 or {e.get("asset", {}).get("id") for e in entries} != set(TARGETS):
        raise Blocked("requires exactly the three authorized Swift assets")
    for entry in entries:
        asset = entry["asset"]
        date, staged = TARGETS[asset["id"]]
        if entry.get("key") != f"gym_media_use:{GYM}:{date}":
            raise Blocked("wrong stage date")
        if not all(k in asset for k in PINS):
            raise Blocked("incomplete asset before-image")
        if (asset["gym_id"] != GYM or not asset["source_id"] or not asset["content_hash"]
                or not asset["drive_modified"] or type(asset["used_count"]) is not int
                or asset["used_count"] != 1 or ts(asset["last_used_at"]) != ts(staged)
                or asset["review_status"] != "approved" or asset["eligible"] is not True
                or asset["excluded_by_coach"] is not False):
            raise Blocked("asset is not the approved exact orphan before-image")
        ts(asset["drive_modified"])
        source = entry.get("source_before")
        if (not isinstance(source, dict) or source.get("id") != asset["source_id"]
                or source.get("gym_id") != GYM or source.get("active") is not True):
            raise Blocked("frozen active own-tenant source before-image required")
        # Validate every predicate before the first mutation, including unsupported encodings.
        for key in PINS:
            filter_value(asset[key])
        raw = entry.get("ledger_before")
        try:
            records = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise Blocked("invalid whole ledger before-image") from exc
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise Blocked("ledger must be an object or list of objects")
        matches = [r for r in records if r.get("asset_id") == asset["id"]]
        if len(matches) != 1:
            raise Blocked("ambiguous stage ledger identity")
        rec = matches[0]
        if (rec.get("gym_id") != GYM or rec.get("rolled_back") is not False
                or type(rec.get("prev_used_count")) is not int or rec["prev_used_count"] != 0
                or rec.get("prev_last_used_at") is not None
                or ts(rec.get("staged_at")) != ts(staged)):
            raise Blocked("stage before-image contradicts exact orphan")
    return plan


def filter_value(value):
    if value is None:
        return "is.null"
    if isinstance(value, bool):
        return "eq." + str(value).lower()
    if not isinstance(value, (str, int)) or (isinstance(value, str) and
            ("\\" in value or any(ord(c) < 32 for c in value))):
        raise Blocked("unsupported atomic CAS predicate; no ID-only fallback")
    return "eq." + str(value)


def after_asset(entry):
    return dict(entry["asset"], used_count=0, last_used_at=None)


def after_ledger(entry):
    records = json.loads(entry["ledger_before"])
    items = records if isinstance(records, list) else [records]
    for rec in items:
        if rec.get("asset_id") == entry["asset"]["id"]:
            rec["rolled_back"] = True
    return json.dumps(records, separators=(",", ":"), ensure_ascii=False)


def verify_fence(receipt, plan_hash, now):
    if (receipt.get("plan_sha256") != plan_hash or receipt.get("gym_id") != GYM
            or not receipt.get("fence_id") or not receipt.get("verified_by")
            or receipt.get("independently_verified") is not True
            or receipt.get("fence_held") is not True
            or receipt.get("retain_until_journal_complete") is not True
            or not WRITERS.issubset(set(receipt.get("writers", [])))
            or receipt.get("other_relevant_writers") != []
            or not all(receipt.get(k) == [] for k in CLEAR)):
        raise Blocked("verified whole-writer fence / absence evidence required")
    observed = ts(receipt.get("verified_at"))
    expires = ts(receipt.get("expires_at"))
    if not observed <= now < expires or (expires - observed).total_seconds() > 900:
        raise Blocked("fence verification expired or exceeds 15 minute attended window")


class SupabaseCAS:
    """Atomic same-row PostgREST PATCH; rejects zero/multiple matches and API errors."""
    def __init__(self, store):
        self.store = store
        if not store.available():
            raise Blocked("Supabase unavailable")

    def read(self, entry):
        rows = self.store._get_all("media_asset", {
            "select": ",".join(PINS), "gym_id": filter_value(GYM),
            "id": filter_value(entry["asset"]["id"])})
        if len(rows) != 1:
            raise Blocked("tenant-scoped asset read did not return exactly one row")
        return rows[0]

    def read_source(self, entry):
        rows = self.store._get_all("media_source", {
            "select": "*", "id": filter_value(entry["asset"]["source_id"]),
            "gym_id": filter_value(GYM)})
        if len(rows) != 1:
            raise Blocked("own-tenant source read did not return exactly one row")
        return rows[0]

    def cas(self, entry):
        response = self.store._client().patch(
            self.store._rest("media_asset"),
            params={k: filter_value(entry["asset"][k]) for k in PINS},
            json={"used_count": 0, "last_used_at": None},
            headers=self.store._headers({"Prefer": "return=representation",
                                         "Content-Type": "application/json"}), timeout=30)
        if response.status_code >= 400:
            raise Blocked("atomic Supabase CAS unavailable or refused; retain fence")
        rows = response.json()
        if not isinstance(rows, list) or len(rows) != 1 or not equal(rows[0], after_asset(entry)):
            raise Blocked("atomic Supabase CAS did not match exact before-image; retain fence")


def connect(path, write=False):
    # Existing DB only; never initialize or accidentally create an empty ledger.
    return sqlite3.connect(Path(path).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)


def ledger_read(db, key):
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def save_journal(path, journal):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(journal, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    dirfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def run(plan, db_path, *, apply=False, receipt=None, api=None, journal_path=None, now=None):
    validate(plan)
    plan_hash = digest(plan)
    if not apply:
        with connect(db_path) as db:
            for entry in plan["entries"]:
                if ledger_read(db, entry["key"]) != entry["ledger_before"]:
                    raise Blocked("changed whole ledger before-image")
        return {"mode": "dry_run", "plan_sha256": plan_hash, "assets": 3,
                "apply_requires": "fresh independently verified fence and live atomic CAS readback"}
    if api is None or journal_path is None or receipt is None:
        raise Blocked("apply requires API, fence receipt and durable journal")
    now_fn = now if callable(now) else (lambda: now or datetime.now(timezone.utc))
    verify_fence(receipt, plan_hash, now_fn())
    path = Path(journal_path).resolve()
    if not path.parent.is_dir():
        raise Blocked("journal parent must already exist on durable storage")
    lockfd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked("another maintenance operator owns this journal") from exc
        journal = json.loads(path.read_text()) if path.exists() else {
            "schema": 1, "plan_sha256": plan_hash, "plan": plan, "fence_id": receipt["fence_id"],
            "states": {}, "complete": False}
        if (journal.get("plan_sha256") != plan_hash or journal.get("plan") != plan
                or journal.get("fence_id") != receipt["fence_id"]):
            raise Blocked("journal plan/fence mismatch; retain original fence")
        with connect(db_path, write=True) as db:
            # Before any intent, check ALL entries. Existing intents may be before/after.
            for entry in plan["entries"]:
                ident = entry["asset"]["id"]
                state = journal["states"].get(ident)
                if api.read_source(entry) != entry["source_before"]:
                    raise Blocked("source ownership/version before-image changed; retain fence")
                current = api.read(entry)
                ledger = ledger_read(db, entry["key"])
                allowed_asset = equal(current, entry["asset"]) or (state and equal(current, after_asset(entry)))
                allowed_ledger = ledger == entry["ledger_before"] or (state and ledger == after_ledger(entry))
                if not allowed_asset or not allowed_ledger:
                    raise Blocked("changed asset or whole ledger; retain fence")
                if ledger == after_ledger(entry) and not equal(current, after_asset(entry)):
                    raise Blocked("ledger settled before asset; retain fence")
            for entry in plan["entries"]:
                verify_fence(receipt, plan_hash, now_fn())
                ident = entry["asset"]["id"]
                if api.read_source(entry) != entry["source_before"]:
                    raise Blocked("source ownership/version changed after preflight; retain fence")
                if ident not in journal["states"]:
                    journal["states"][ident] = "intent"
                    save_journal(path, journal)  # durable BEFORE external write
                current = api.read(entry)
                if equal(current, entry["asset"]):
                    # Reads and journal fsync can outlast the verified window.
                    verify_fence(receipt, plan_hash, now_fn())
                    api.cas(entry)
                elif not equal(current, after_asset(entry)):
                    raise Blocked("asset changed after intent; retain fence")
                if not equal(api.read(entry), after_asset(entry)):
                    raise Blocked("asset readback failed; retain fence")
                journal["states"][ident] = "asset_verified"
                save_journal(path, journal)
                verify_fence(receipt, plan_hash, now_fn())
                db.execute("BEGIN IMMEDIATE")
                try:
                    # BEGIN IMMEDIATE may wait for an existing writer lock.
                    verify_fence(receipt, plan_hash, now_fn())
                    current = ledger_read(db, entry["key"])
                    expected_after = after_ledger(entry)
                    if current == entry["ledger_before"]:
                        verify_fence(receipt, plan_hash, now_fn())
                        changed = db.execute("UPDATE kv SET value=? WHERE key=? AND value=?",
                                             (expected_after, entry["key"], entry["ledger_before"]))
                        if changed.rowcount != 1:
                            raise Blocked("whole ledger CAS lost; retain fence")
                    elif current != expected_after:
                        raise Blocked("whole ledger changed; retain fence")
                    if ledger_read(db, entry["key"]) != expected_after:
                        raise Blocked("ledger transactional readback failed; retain fence")
                    verify_fence(receipt, plan_hash, now_fn())
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                if ledger_read(db, entry["key"]) != expected_after:
                    raise Blocked("ledger durable readback failed; retain fence")
                journal["states"][ident] = "complete"
                save_journal(path, journal)
            # Joint final readback while fence still held; repeat apply performs no decrements.
            for entry in plan["entries"]:
                if api.read_source(entry) != entry["source_before"] or not equal(api.read(entry), after_asset(entry)) or ledger_read(db, entry["key"]) != after_ledger(entry):
                    raise Blocked("joint final readback failed; retain fence")
            verify_fence(receipt, plan_hash, now_fn())
            journal["complete"] = True
            save_journal(path, journal)
        return {"mode": "applied", "complete": True, "plan_sha256": plan_hash,
                "assets": 3, "fence_release": "external operator only after independent journal readback"}
    finally:
        os.close(lockfd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fence-receipt", type=Path)
    parser.add_argument("--journal", type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    try:
        if args.apply:
            if args.fence_receipt is None or args.journal is None:
                raise Blocked("--apply requires --fence-receipt and --journal")
            # Validate before loading credentials or constructing any external API.
            validate(plan)
            receipt = json.loads(args.fence_receipt.read_text())
            verify_fence(receipt, digest(plan), datetime.now(timezone.utc))
            from agent.media_source_store import SupabaseMediaStore
            result = run(plan, args.db, apply=True, receipt=receipt,
                         api=SupabaseCAS(SupabaseMediaStore()), journal_path=args.journal)
        else:
            result = run(plan, args.db)
        print(json.dumps(result, sort_keys=True))
    except Blocked as exc:
        parser.exit(2, f"BLOCKED: {exc}. Retain maintenance fence if any intent exists.\n")


if __name__ == "__main__":
    main()
