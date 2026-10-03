#!/usr/bin/env python3
"""Fail-closed hold for Swift River's historic pending ``igfill_`` cards.

Dry run is the default.  ``--apply`` requires the reviewed target digest and a
new receipt path.  It only sets ``media_not_ready_reason`` through a complete
row compare-and-swap; it never changes status, source, creative, or creates a
ticket.  Apply the hold before choosing archive versus photo restaging; use the
receipt's before image and CAS preconditions for reconciliation or rollback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "swiftrivercrossfite5c9db"
_IG_KEY = f"{_BASE}_ig"
_START, _END = date(2026, 9, 2), date(2026, 10, 2)
_ROWS, _DATES = 25, 17
_REASON = "Historical igfill card held: archive versus real-photo restage decision pending"
_DIGEST_FIELDS = ("id", "gym_id", "post_date", "account", "format", "status",
                  "image_url", "source_media_url", "source_media_asset_id",
                  "media_not_ready_reason", "variant_status", "created_at")


def _is_target(row):
    if (str(row.get("gym_id") or "") != _BASE
            or str(row.get("status") or "").lower() != "pending"):
        return False
    try:
        day = date.fromisoformat(str(row.get("post_date") or "")[:10])
    except ValueError:
        return False
    if not _START <= day <= _END:
        return False
    return any("igfill_" in urlparse(str(value or "")).path.lower()
               for value in (row.get("image_url"), row.get("source_media_url")))


def _target(rows):
    result = sorted((dict(row) for row in rows if _is_target(row)),
                    key=lambda row: (str(row.get("post_date")), str(row.get("id"))))
    ids = [str(row.get("id") or "") for row in result]
    days = {str(row.get("post_date") or "")[:10] for row in result}
    if len(result) != _ROWS or len(days) != _DATES or not all(ids) or len(set(ids)) != len(ids):
        return None
    return result


def _digest(rows):
    canonical = [{field: row.get(field) for field in _DIGEST_FIELDS} for row in rows]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _read(store):
    return store.list_pending_media_between(_BASE, _START.isoformat(), _END.isoformat())


def _resolve():
    from agent import accounts, config
    from agent.portal_calendar_store import SupabaseCalendarStore
    return {"account": accounts.get_account(_IG_KEY),
            "calendar": SupabaseCalendarStore() if config.portal_calendar_supabase_enabled() else None}


def _receipt(before, digest):
    return {"operation": "swift_river_historical_igfill_hold", "client": _BASE,
            "target_rows": _ROWS, "target_dates": _DATES, "target_digest": digest,
            "reason": _REASON, "before_image": before,
            "rollback": "Do not bulk-clear holds. Re-read changed_ids and any inflight_id from the receipt, then use a row-level CAS that matches this hold reason and the recorded before image before restoring media_not_ready_reason.",
            "status_or_source_changed": False, "synthetic_ticket_created": False}


def _write_receipt(path, value, *, create=False):
    destination = Path(path)
    if destination.parent.exists() is False or (create and destination.exists()):
        raise ValueError("receipt path must be new and its parent must exist")
    payload = (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode()
    def sync_directory():
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    if create:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory()
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        sync_directory()
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(*, apply=False, expected_digest=None, receipt_path=None, ctx=None):
    ctx = ctx or _resolve()
    calendar = ctx.get("calendar")
    if ctx.get("account") is None or calendar is None:
        return {"ok": False, "reason": "missing Swift River account or production calendar store"}
    try:
        before = _target(_read(calendar))
    except Exception as exc:
        return {"ok": False, "reason": f"calendar read failed:{type(exc).__name__}"}
    if before is None:
        return {"ok": False, "reason": "exact 25-row, 17-date pending igfill target absent"}
    digest = _digest(before)
    preflight = _receipt(before, digest)
    if not apply:
        return {"ok": True, "dry_run": True, "preflight": preflight}
    if expected_digest != digest:
        return {"ok": False, "reason": "target digest missing or changed", "observed_digest": digest}
    if not receipt_path:
        return {"ok": False, "reason": "--receipt is required before any write"}
    try:
        progress = {**preflight, "applied_at": datetime.now(timezone.utc).isoformat(),
                    "state": "before_write", "changed_ids": [], "inflight_id": None}
        _write_receipt(receipt_path, progress, create=True)
    except Exception as exc:
        return {"ok": False, "reason": f"before-image receipt failed:{type(exc).__name__}"}
    changed = []
    for row in before:
        # Write ahead of the external CAS. If the database accepts a row but
        # the subsequent receipt update fails, inflight_id is still durable
        # and names the exact row that needs reconciliation.
        progress = {**progress, "state": "write_intent", "inflight_id": str(row["id"])}
        try:
            _write_receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": f"write-intent receipt failed:{type(exc).__name__}",
                    "changed_ids": changed, "receipt": receipt_path}
        try:
            updated = calendar.hold_pending_media(_BASE, row, _REASON)
        except Exception as exc:
            return {"ok": False, "reason": f"hold write failed:{type(exc).__name__}",
                    "changed_ids": changed, "receipt": receipt_path}
        if updated is None:
            return {"ok": False, "reason": "CAS conflict; reconciliation required",
                    "changed_ids": changed, "receipt": receipt_path}
        changed.append(str(row["id"]))
        progress = {**progress, "state": "partial_hold", "changed_ids": list(changed),
                    "inflight_id": None}
        try:
            _write_receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": f"partial receipt update failed:{type(exc).__name__}",
                    "changed_ids": changed, "receipt": receipt_path}
    readback = []
    for row in before:
        try:
            current = calendar.get_row(_BASE, str(row["id"]))
        except Exception as exc:
            return {"ok": False, "reason": f"readback failed:{type(exc).__name__}",
                    "changed_ids": changed, "receipt": receipt_path}
        expected = {**row, "media_not_ready_reason": _REASON}
        if (current is None or any(current.get(field) != expected.get(field)
                                   for field in _DIGEST_FIELDS)):
            return {"ok": False, "reason": "readback mismatch; reconciliation required",
                    "changed_ids": changed, "receipt": receipt_path}
        readback.append(current)
    final = {**preflight, "state": "readback_verified", "changed_ids": changed,
             "readback": readback}
    # The before-image file exists before the first mutation.  Replace its contents
    # only after every guarded write has been read back, retaining that before image.
    try:
        _write_receipt(receipt_path, final)
    except Exception as exc:
        return {"ok": False, "reason": f"final receipt update failed:{type(exc).__name__}",
                "changed_ids": changed, "receipt": receipt_path}
    return {"ok": True, "receipt": receipt_path, "changed_ids": changed,
            "target_digest": digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt", help="new JSON receipt file; mandatory with --apply")
    args = parser.parse_args(argv)
    result = run(apply=args.apply, expected_digest=args.expected_digest, receipt_path=args.receipt)
    displayed = dict(result)
    if isinstance(displayed.get("preflight"), dict):
        displayed["preflight"] = {key: value for key, value in displayed["preflight"].items()
                                  if key != "before_image"}
    print(json.dumps(displayed, default=str, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
