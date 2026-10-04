#!/usr/bin/env python3
"""Exact-CAS media hold for active future infographic placeholders.

Dry run by default. Apply is OFF unless ECHO_FUTURE_IGFILL_HOLD_ENABLED=true,
the observed target digest is supplied, and a new private receipt path exists.

The armed PR268 visual-group trigger rejects an approved/publishing row with a
non-null media_not_ready_reason, so the hold cannot leave an approved row
approved: the atomic PATCH sets the hold reason and demotes approved rows to
pending in the same write. Pending rows change only media_not_ready_reason.
A held (demoted) row requires later reapproval; release never restores one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import media_swap
from agent.portal_calendar_store import SupabaseCalendarStore

REASON = "Photo-first hold: unverified infographic placeholder; approved gym photo required"
_FILL = re.compile(r"(?:^|/)igfill_\d{4}-\d{2}-\d{2}(?:[_-]|\.)", re.I)
_FIELDS = ("id", "gym_id", "post_date", "status", "variant_status", "account",
           "format", "caption", "image_url", "source_media_url",
           "source_media_asset_id", "media_not_ready_reason", "created_at",
           "published_at", "late_post_id")


def _image(row):
    return {field: row.get(field) for field in _FIELDS}


def _is_fill(row):
    return any(_FILL.search(urlsplit(str(row.get(field) or "")).path)
               for field in ("image_url", "source_media_url"))


def targets(rows, *, today):
    """All active waiting/approved fill rows, including same-media siblings."""
    day = date.fromisoformat(today)
    groups = {}
    for row in rows:
        groups.setdefault((row.get("gym_id"), row.get("post_date")), []).append(row)
    selected = {}
    for row in rows:
        try:
            row_day = date.fromisoformat(str(row.get("post_date") or "")[:10])
        except ValueError:
            continue
        if (row_day < day or row.get("variant_status") != "active"
                or row.get("status") not in ("pending", "approved")
                or not _is_fill(row)):
            continue
        for sibling in (row, *media_swap.sibling_rows(
                row, groups[(row.get("gym_id"), row.get("post_date"))])):
            if (all(field in sibling for field in _FIELDS)
                    and sibling.get("status") in ("pending", "approved")
                    and sibling.get("variant_status") == "active"
                    and sibling.get("media_not_ready_reason") is None
                    and sibling.get("published_at") is None
                    and sibling.get("late_post_id") is None):
                selected[str(sibling.get("id"))] = _image(sibling)
    return sorted(selected.values(), key=lambda row: (
        str(row["post_date"]), str(row["gym_id"]), str(row["id"])))


def _digest(rows):
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _receipt(path, value, *, create=False):
    target = Path(path)
    if not target.parent.is_dir():
        raise ValueError("receipt parent directory does not exist")
    payload = (json.dumps(value, indent=2, sort_keys=True, default=str) + "\n").encode()
    if create:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def run(*, store=None, today=None, horizon_days=365, apply=False,
        expected_digest=None, receipt_path=None):
    today = today or datetime.now(timezone.utc).date().isoformat()
    end = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
    store = store or SupabaseCalendarStore()
    rows = store.list_future_media_maintenance_rows(today, end)
    before = targets(rows, today=today)
    digest = _digest(before)
    summary = {"operation": "hold_future_igfill_photos", "today": today,
               "target_digest": digest, "target_count": len(before),
               "pending_count": sum(row["status"] == "pending" for row in before),
               "approved_count": sum(row["status"] == "approved" for row in before),
               "before_image": before, "reason": REASON,
               "status_transition": "approved rows atomically demote to pending; "
                                    "pending rows stay pending",
               "preserved": ["caption", "image_url", "source_media_url",
                             "source_media_asset_id", "publication"],
               "rollback": "Re-read each ID and use an exact row CAS matching this receipt and hold reason before clearing any hold."}
    if not apply:
        return {"ok": True, "dry_run": True, "preflight": summary}
    if os.environ.get("ECHO_FUTURE_IGFILL_HOLD_ENABLED", "").lower() != "true":
        return {"ok": False, "reason": "hold apply flag OFF"}
    if not before or expected_digest != digest or not receipt_path:
        return {"ok": False, "reason": "nonempty exact digest and new receipt path required",
                "observed_digest": digest}
    progress = {**summary, "state": "before_write", "changed_ids": [],
                "inflight_id": None, "readback": [],
                "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        _receipt(receipt_path, progress, create=True)
    except Exception as exc:
        return {"ok": False, "reason": f"before-image receipt failed:{type(exc).__name__}"}
    for row in before:
        progress["state"] = "write_intent"
        progress["inflight_id"] = str(row["id"])
        try:
            _receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": f"write-intent receipt failed:{type(exc).__name__}",
                    "receipt": receipt_path}
        try:
            updated = store.hold_future_infographic_media(row["gym_id"], row, REASON)
        except Exception as exc:
            return {"ok": False, "reason": f"hold CAS uncertain:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": row["id"]}
        if updated is None:
            progress["state"] = "cas_conflict"
            progress["inflight_id"] = None
            progress.setdefault("conflict_ids", []).append(str(row["id"]))
            _receipt(receipt_path, progress)
            continue
        try:
            current = store.get_row(row["gym_id"], str(row["id"]))
        except Exception as exc:
            return {"ok": False, "reason": f"readback failed:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": row["id"]}
        expected = {**row, "media_not_ready_reason": REASON}
        if row["status"] == "approved":
            # The hold persists only as an atomic hold+demote; an approved row
            # still approved after the write is the trigger-rejected state and
            # must surface here as a readback mismatch, never as success.
            expected["status"] = "pending"
        if current is None or _image(current) != expected:
            return {"ok": False, "reason": "readback mismatch; reconcile before retry",
                    "receipt": receipt_path, "inflight_id": row["id"]}
        progress["changed_ids"].append(str(row["id"]))
        progress["readback"].append(_image(current))
        progress["inflight_id"] = None
        progress["state"] = "partial_hold"
        _receipt(receipt_path, progress)
    progress["state"] = ("readback_verified" if not progress.get("conflict_ids")
                         else "partial_hold_conflicts")
    _receipt(receipt_path, progress)
    return {"ok": not progress.get("conflict_ids"), "receipt": receipt_path,
            "target_digest": digest, "changed_ids": progress["changed_ids"],
            "conflict_ids": progress.get("conflict_ids", [])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", help="UTC date YYYY-MM-DD; defaults to today")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt", help="new private JSON path required for apply")
    args = parser.parse_args(argv)
    result = run(today=args.today, apply=args.apply,
                 expected_digest=args.expected_digest, receipt_path=args.receipt)
    displayed = dict(result)
    if isinstance(displayed.get("preflight"), dict):
        displayed["preflight"] = {key: value for key, value in displayed["preflight"].items()
                                  if key != "before_image"}
    print(json.dumps(displayed, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
