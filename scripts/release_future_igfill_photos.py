#!/usr/bin/env python3
"""Receipt-bound rollback of future infographic holds; dry run by default.

Release restores only media_not_ready_reason to NULL. It requires the private
receipt from the original hold, exact current row images, a matching digest,
an OFF-by-default release flag, and a new private write-ahead receipt.

The hold that produced the receipt now atomically demotes approved rows to
pending (the armed PR268 trigger rejects approved+hold_reason), so a held
approved-origin row is persisted as pending. This script accepts either the
demoted readback (new receipts) or the legacy approved readback (receipts
written before the demotion patch), derives the demoted release image, and
verifies after release that the row is pending with the reason cleared --
never reapproved. Rows protected by scene_review_hold or archived variants
are refused by the store CAS and treated as conflicts here, never released.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.hold_future_igfill_photos import REASON, _FIELDS, _digest, _image, _receipt
from agent.portal_calendar_store import SupabaseCalendarStore


def _original_receipt(path):
    if not path:
        raise ValueError("original hold receipt required")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()):
            raise ValueError("original hold receipt must be an owned private regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            source = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    if (not isinstance(source, dict)
            or source.get("operation") != "hold_future_igfill_photos"
            or source.get("reason") != REASON
            or source.get("state") not in ("readback_verified", "partial_hold_conflicts")):
        raise ValueError("original hold receipt is not a completed operation")
    before = source.get("before_image")
    readback = source.get("readback")
    changed = source.get("changed_ids")
    if (not isinstance(before, list) or not isinstance(readback, list)
            or not isinstance(changed, list) or not changed
            or source.get("target_digest") != _digest(before)
            or source.get("target_count") != len(before)):
        raise ValueError("original hold receipt image or digest invalid")
    before_by_id = {str(row.get("id")): row for row in before if isinstance(row, dict)}
    held_by_id = {str(row.get("id")): row for row in readback if isinstance(row, dict)}
    if (len(before_by_id) != len(before) or len(held_by_id) != len(readback)
            or len(set(changed)) != len(changed)
            or set(changed) != set(held_by_id)
            or not set(changed).issubset(before_by_id)
            or set(changed) & set(source.get("conflict_ids", []))):
        raise ValueError("original hold receipt row linkage invalid")
    for row_id in changed:
        original, held = before_by_id[row_id], held_by_id[row_id]
        if (any(field not in original or field not in held for field in _FIELDS)
                or original["media_not_ready_reason"] is not None):
            raise ValueError("original hold receipt readback mismatch")
        # New receipts carry the atomic demotion (approved-origin rows held as
        # pending); legacy receipts may still show the row approved with the
        # reason set. Both are structurally valid hold outcomes; the actual
        # persisted state is re-verified against the store before any write.
        expected_held = {**original, "media_not_ready_reason": REASON}
        if original.get("status") == "approved":
            expected_held["status"] = "pending"
        if held != expected_held and held != {**original, "media_not_ready_reason": REASON}:
            raise ValueError("original hold receipt readback mismatch")
    return source, [held_by_id[row_id] for row_id in changed]


def current_image_status_is_demoted(held, current):
    """True when the persisted row matched the receipt only in demoted form."""
    by_id = {str(row["id"]): row for row in current}
    observed = by_id.get(str(held["id"]))
    return observed is not None and observed == {**held, "status": "pending"}


def run(*, store=None, hold_receipt_path=None, apply=False, expected_digest=None,
        receipt_path=None):
    source, held_rows = _original_receipt(hold_receipt_path)
    store = store or SupabaseCalendarStore()
    current = []
    conflicts = []
    for held in held_rows:
        row = store.get_row(held["gym_id"], str(held["id"]))
        # The only legitimate persisted state for a held row is the receipt
        # image itself, or -- for approved-origin rows from legacy receipts --
        # the same image demoted to pending with the hold reason. A persisted
        # row that is STILL approved is refused at preflight (fail closed
        # before any mutation): a legacy approved-with-hold row must not be
        # released into a publishable state. Anything else (drift, republish,
        # cleared reason, scene_review_hold takeover, archived variant) is a
        # conflict and is never released.
        demoted = {**held, "status": "pending"}
        allowed = (demoted,) if held.get("status") == "approved" else (held, demoted)
        if (row is None
                or row.get("status") == "approved"
                or held.get("media_not_ready_reason") != REASON
                or held.get("variant_status") != "active"
                or _image(row) not in allowed):
            conflicts.append(str(held["id"]))
        else:
            current.append(_image(row))
    digest = _digest(current)
    preflight = {"operation": "release_future_igfill_photos",
                 "original_hold_receipt": str(Path(hold_receipt_path).resolve()),
                 "original_target_digest": source["target_digest"],
                 "target_digest": digest, "target_count": len(current),
                 "conflict_ids": conflicts, "before_image": current,
                 "reason": REASON,
                 "status_transition": "held rows stay or return to pending; "
                                      "release never reapproves",
                 "preserved": ["caption", "image_url", "source_media_url",
                               "source_media_asset_id", "publication"]}
    if not apply:
        return {"ok": not conflicts, "dry_run": True, "preflight": preflight}
    if os.environ.get("ECHO_FUTURE_IGFILL_RELEASE_ENABLED", "").lower() != "true":
        return {"ok": False, "reason": "release apply flag OFF"}
    if conflicts or not current or expected_digest != digest or not receipt_path:
        return {"ok": False, "reason": "exact complete digest and new receipt required",
                "preflight": preflight}
    if Path(receipt_path).resolve() == Path(hold_receipt_path).resolve():
        return {"ok": False, "reason": "release receipt must differ from original"}
    progress = {**preflight, "state": "before_write", "changed_ids": [],
                "conflict_ids": [],
                "inflight_id": None, "readback": [],
                "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        _receipt(receipt_path, progress, create=True)
    except Exception as exc:
        return {"ok": False, "reason": f"before-image receipt failed:{type(exc).__name__}"}
    for held in current:
        progress["state"] = "write_intent"
        progress["inflight_id"] = str(held["id"])
        try:
            _receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": f"write-intent receipt failed:{type(exc).__name__}",
                    "receipt": receipt_path}
        # The store CAS matches the persisted row exactly and refuses any
        # non-pending row before a PATCH. Preflight already rejected persisted
        # approved rows; defensively still release only the demoted image for
        # approved-origin receipt rows. The payload clears only
        # media_not_ready_reason, so a demoted row returns to pending and can
        # never be reapproved by this path.
        release_image = dict(held)
        if release_image.get("status") == "approved":
            release_image["status"] = "pending"
        try:
            updated = store.release_future_infographic_media(
                held["gym_id"], release_image, REASON)
        except Exception as exc:
            return {"ok": False, "reason": f"release CAS uncertain:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": held["id"]}
        if updated is None:
            progress["state"] = "cas_conflict"
            progress["inflight_id"] = None
            progress.setdefault("conflict_ids", []).append(str(held["id"]))
            _receipt(receipt_path, progress)
            continue
        try:
            observed = store.get_row(held["gym_id"], str(held["id"]))
        except Exception as exc:
            return {"ok": False, "reason": f"readback failed:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": held["id"]}
        expected_released = {**release_image, "media_not_ready_reason": None}
        if (observed is None or _image(observed) != expected_released
                or (held["status"] == "approved"
                    and observed.get("status") == "approved")):
            return {"ok": False, "reason": "readback mismatch; reconcile before retry",
                    "receipt": receipt_path, "inflight_id": held["id"]}
        progress["changed_ids"].append(str(held["id"]))
        progress["readback"].append(_image(observed))
        progress["inflight_id"] = None
        progress["state"] = "partial_release"
        _receipt(receipt_path, progress)
    progress["state"] = ("readback_verified" if not progress["conflict_ids"]
                         else "partial_release_conflicts")
    _receipt(receipt_path, progress)
    return {"ok": not progress["conflict_ids"], "receipt": receipt_path,
            "target_digest": digest, "changed_ids": progress["changed_ids"],
            "conflict_ids": progress["conflict_ids"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold-receipt", required=True, help="original private hold receipt")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt", help="new private release receipt path")
    args = parser.parse_args(argv)
    result = run(hold_receipt_path=args.hold_receipt, apply=args.apply,
                 expected_digest=args.expected_digest, receipt_path=args.receipt)
    displayed = dict(result)
    if isinstance(displayed.get("preflight"), dict):
        displayed["preflight"] = {key: value for key, value in displayed["preflight"].items()
                                  if key != "before_image"}
    print(json.dumps(displayed, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
