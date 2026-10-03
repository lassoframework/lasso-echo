#!/usr/bin/env python3
"""Archive Swift River's held historical ``igfill_`` cards, fail closed.

This is an audit-only operation. Dry-run is the default. ``--apply`` requires
the digest shown by dry-run and a new private receipt path; each row is written
through an exact before-image CAS, then read back. It never touches production
unless an operator deliberately runs it in an environment configured for that
database, and it never changes a status, caption, source, media URL, or ledger.
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
_HOLD_REASON = "Historical igfill card held: archive versus real-photo restage decision pending"
_FIELDS = ("id", "gym_id", "post_date", "account", "format", "status", "caption", "image_url",
           "source_media_url", "source_media_asset_id", "media_not_ready_reason",
           "variant_status", "variant_of", "created_at", "published_at", "late_post_id",
           "scheduled_at", "slot_index")


def _is_target(row):
    """The exact PR246-held active card, not merely a similarly named sibling."""
    if (str(row.get("gym_id") or "") != _BASE or row.get("status") != "pending"
            or row.get("variant_status", "active") != "active"
            or row.get("media_not_ready_reason") != _HOLD_REASON
            or row.get("published_at") is not None or row.get("late_post_id") is not None):
        return False
    try:
        day = date.fromisoformat(str(row.get("post_date") or "")[:10])
    except ValueError:
        return False
    return (_START <= day <= _END and any(
        "igfill_" in urlparse(str(value or "")).path.lower()
        for value in (row.get("image_url"), row.get("source_media_url"))))


def _target(rows):
    matches = sorted((dict(row) for row in rows if _is_target(row)),
                     key=lambda row: (str(row.get("post_date")), str(row.get("id"))))
    ids = [str(row.get("id") or "") for row in matches]
    days = {str(row.get("post_date") or "")[:10] for row in matches}
    # Any matching-but-not-target-state row is drift, not an invitation to archive it.
    historical_igfill = [row for row in rows if str(row.get("gym_id") or "") == _BASE
                         and _START.isoformat() <= str(row.get("post_date") or "")[:10] <= _END.isoformat()
                         and any("igfill_" in urlparse(str(v or "")).path.lower()
                                 for v in (row.get("image_url"), row.get("source_media_url")))]
    if (len(matches) != _ROWS or len(days) != _DATES or not all(ids)
            or len(set(ids)) != len(ids) or len(historical_igfill) != _ROWS):
        return None
    return matches


def _digest(rows):
    canonical = [{field: row.get(field) for field in _FIELDS} for row in rows]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _resolve():
    from agent import accounts, config
    from agent.portal_calendar_store import SupabaseCalendarStore
    return {"account": accounts.get_account(_IG_KEY),
            "calendar": SupabaseCalendarStore() if config.portal_calendar_supabase_enabled() else None}


def _read(calendar):
    return calendar.list_pending_media_between(_BASE, _START.isoformat(), _END.isoformat())


def _receipt(before, digest):
    return {"operation": "swift_river_historical_igfill_archive", "client": _BASE,
            "target_rows": _ROWS, "target_dates": _DATES, "target_digest": digest,
            "required_hold_reason": _HOLD_REASON, "before_image": before,
            "mutation": {"variant_status": "active_to_archived"},
            "preserved": [field for field in _FIELDS if field != "variant_status"],
            "rollback": "Do not bulk unarchive. Re-read each archived id and use a row CAS that matches this receipt before restoring variant_status=active."}


def _write_receipt(path, value, *, create=False):
    destination = Path(path)
    if not destination.parent.is_dir() or (create and destination.exists()):
        raise ValueError("receipt path must be new and its parent must exist")
    payload = (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode()
    def sync_directory():
        fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if create:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as receipt:
            receipt.write(payload); receipt.flush(); os.fsync(receipt.fileno())
        sync_directory()
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as receipt:
            receipt.write(payload); receipt.flush(); os.fsync(receipt.fileno())
        os.replace(temporary, destination); sync_directory()
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(*, apply=False, expected_digest=None, receipt_path=None, ctx=None):
    ctx = ctx or _resolve()
    calendar = ctx.get("calendar")
    if ctx.get("account") is None or calendar is None:
        return {"ok": False, "reason": "missing Swift River account or calendar store"}
    try:
        before = _target(_read(calendar))
    except Exception as exc:  # unreadable/partial state must not mutate
        return {"ok": False, "reason": f"calendar read failed:{type(exc).__name__}"}
    if before is None:
        return {"ok": False, "reason": "exact 25-row, 17-date held active igfill target absent"}
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
        progress = {**progress, "state": "write_intent", "inflight_id": str(row["id"])}
        try:
            _write_receipt(receipt_path, progress)
            updated = calendar.archive_pending_media(_BASE, row)
        except Exception as exc:
            return {"ok": False, "reason": f"archive write failed:{type(exc).__name__}",
                    "changed_ids": changed, "receipt": str(receipt_path)}
        if updated is None:
            return {"ok": False, "reason": "CAS conflict; reconciliation required",
                    "changed_ids": changed, "receipt": str(receipt_path)}
        changed.append(str(row["id"]))
        progress = {**progress, "state": "partial_archive", "changed_ids": list(changed),
                    "inflight_id": None}
        try:
            _write_receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": f"partial receipt update failed:{type(exc).__name__}",
                    "changed_ids": changed, "receipt": str(receipt_path)}
    readback = []
    for row in before:
        try:
            current = calendar.get_row(_BASE, str(row["id"]))
        except Exception as exc:
            return {"ok": False, "reason": f"readback failed:{type(exc).__name__}",
                    "changed_ids": changed, "receipt": str(receipt_path)}
        expected = {**row, "variant_status": "archived"}
        if current is None or any(current.get(field) != expected.get(field) for field in _FIELDS):
            return {"ok": False, "reason": "readback mismatch; reconciliation required",
                    "changed_ids": changed, "receipt": str(receipt_path)}
        readback.append(current)
    final = {**preflight, "state": "readback_verified", "changed_ids": changed,
             "readback": readback}
    try:
        _write_receipt(receipt_path, final)
    except Exception as exc:
        return {"ok": False, "reason": f"final receipt update failed:{type(exc).__name__}",
                "changed_ids": changed, "receipt": str(receipt_path)}
    return {"ok": True, "receipt": str(receipt_path), "changed_ids": changed,
            "target_digest": digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt", help="new private JSON receipt; required with --apply")
    args = parser.parse_args(argv)
    result = run(apply=args.apply, expected_digest=args.expected_digest, receipt_path=args.receipt)
    # Never print private before/readback images to an operator terminal or CI log.
    shown = dict(result)
    if isinstance(shown.get("preflight"), dict):
        shown["preflight"] = {k: v for k, v in shown["preflight"].items() if k != "before_image"}
    print(json.dumps(shown, default=str, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
