#!/usr/bin/env python3
"""Audit future gym captions, or correct unheld pending feed copy with exact CAS.

Dry run by default. Apply requires explicit dates, a matching dry-run target
digest, and a new private receipt path. No caption text is logged or receipted.
Story, held, approved, publishing, and published rows are never changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.copy_gate import format_caption
from agent.portal_calendar_store import SupabaseCalendarStore

_REQUIRED = ("id", "gym_id", "post_date", "status", "variant_status", "format",
             "caption", "media_not_ready_reason", "published_at")
_READBACK = ("id", "gym_id", "post_date", "status", "variant_status", "account",
             "format", "caption", "media_not_ready_reason", "published_at",
             "image_url", "source_media_url", "source_media_asset_id",
             "thumbnail_url", "logical_post_id", "created_at", "scheduled_at",
             "slot_index", "time_slot", "late_post_id", "publish_claim_token")
# Columns the format_pending_feed_caption_cas WHERE clause actually guards.
# The post-write readback must compare only these; concurrent changes to
# unguarded columns (scheduled_at, slot_index, time_slot, thumbnail_url,
# logical_post_id) are unrelated to the caption fix and must not abort a run.
_CAS_GUARDED = ("id", "gym_id", "status", "variant_status", "format",
                "post_date", "caption", "media_not_ready_reason",
                "published_at", "late_post_id", "publish_claim_token",
                "account", "image_url", "source_media_url",
                "source_media_asset_id", "created_at")


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(row):
    return {field: row.get(field) for field in _READBACK}


def _cas_snapshot(row):
    return {field: row.get(field) for field in _CAS_GUARDED}


def _eligible(row):
    return (row["status"] == "pending" and row["variant_status"] == "active"
            and row["format"] == "feed"
            and row["media_not_ready_reason"] is None
            and row["published_at"] is None
            and row.get("late_post_id") is None
            and row.get("publish_claim_token") is None)


def inspect(rows, *, start, end, gym=None):
    """Validate the complete read and return privacy-safe audit plus in-memory targets."""
    summary = Counter()
    by_gym = defaultdict(Counter)
    targets = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or any(field not in row for field in _REQUIRED):
            raise ValueError("incomplete calendar row in audit")
        row_id = str(row["id"] or "")
        gym_id = str(row["gym_id"] or "")
        day = str(row["post_date"] or "")[:10]
        if (not row_id or not gym_id or row_id in seen
                or not start <= day <= end or row["variant_status"] != "active"):
            raise ValueError("out-of-scope or repeated calendar row in audit")
        seen.add(row_id)
        if gym and gym_id != gym:
            continue
        summary["rows_seen"] += 1
        by_gym[gym_id]["rows_seen"] += 1
        caption = row["caption"]
        if not isinstance(caption, str) or not caption.strip():
            summary["empty_caption"] += 1
            by_gym[gym_id]["empty_caption"] += 1
            continue
        try:
            after = format_caption(caption)
        except ValueError:
            summary["blocked_url_semicolon"] += 1
            by_gym[gym_id]["blocked_url_semicolon"] += 1
            continue
        if after == caption:
            continue
        summary["needs_format"] += 1
        by_gym[gym_id]["needs_format"] += 1
        if ";" in caption:
            summary["has_semicolon"] += 1
            by_gym[gym_id]["has_semicolon"] += 1
        if _eligible(row):
            summary["eligible_pending_feed"] += 1
            by_gym[gym_id]["eligible_pending_feed"] += 1
            before = _snapshot(row)
            targets.append((before, after))
        else:
            summary["preserved_other"] += 1
            by_gym[gym_id]["preserved_other"] += 1
    targets.sort(key=lambda pair: (str(pair[0]["gym_id"]),
                                   str(pair[0]["post_date"]), str(pair[0]["id"])))
    manifest = [{"id": before["id"], "gym_id": before["gym_id"],
                 "post_date": before["post_date"],
                 "before_caption_sha256": _hash(before["caption"]),
                 "after_caption_sha256": _hash(after),
                 "before_row_sha256": _hash(before),
                 "after_row_sha256": _hash({**before, "caption": after})}
                for before, after in targets]
    audit = {"start": start, "end": end, "gym_filter": gym,
             "counts": dict(summary),
             "by_gym": {key: dict(value) for key, value in sorted(by_gym.items())},
             "target_digest": _hash(manifest), "targets": manifest}
    return audit, targets


def _receipt(path, value, *, create=False):
    target = Path(path)
    if not target.parent.is_dir():
        raise ValueError("receipt parent directory does not exist")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if create:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def run(*, start, end, gym=None, apply=False, expected_digest=None,
        receipt_path=None, store=None, today=None):
    if date.fromisoformat(end) < date.fromisoformat(start):
        raise ValueError("end date precedes start date")
    today = today or datetime.now(timezone.utc).date().isoformat()
    if apply and date.fromisoformat(start) < date.fromisoformat(today):
        return {"ok": False, "reason": "apply start must be today or later"}
    store = store or SupabaseCalendarStore()
    rows = store.list_future_media_maintenance_rows(start, end)
    audit, targets = inspect(rows, start=start, end=end, gym=gym)
    if not apply:
        return {"ok": True, "dry_run": True, "audit": audit}
    if not targets or expected_digest != audit["target_digest"] or not receipt_path:
        return {"ok": False, "reason": "nonempty exact digest and new receipt required",
                "audit": audit}
    progress = {"operation": "format_pending_feed_captions", "audit": audit,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "state": "before_write", "completed": [], "conflicts": [],
                "inflight_id": None}
    receipt_errors = []
    try:
        _receipt(receipt_path, progress, create=True)
    except Exception as exc:
        return {"ok": False, "reason": f"receipt create failed:{type(exc).__name__}"}
    for before, after in targets:
        row_id = str(before["id"])
        progress["state"] = "write_intent"
        progress["inflight_id"] = row_id
        try:
            _receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": f"receipt update failed:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": row_id}
        try:
            changed = store.format_pending_feed_caption_cas(before["gym_id"], before)
        except Exception as exc:
            return {"ok": False, "reason": f"caption CAS uncertain:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": row_id}
        if changed is None:
            progress["conflicts"].append(row_id)
            progress["inflight_id"] = None
            progress["state"] = "cas_conflict"
            try:
                _receipt(receipt_path, progress)
            except Exception as exc:
                return {"ok": False, "reason": "receipt update failed after CAS conflict; reconcile",
                        "receipt": receipt_path, "inflight_id": row_id,
                        "changed": len(progress["completed"]),
                        "conflicts": progress["conflicts"],
                        "receipt_errors": [{"state": "cas_conflict", "id": row_id,
                                            "error": type(exc).__name__}]}
            continue
        try:
            current = store.get_row(before["gym_id"], row_id)
        except Exception as exc:
            return {"ok": False, "reason": f"readback uncertain:{type(exc).__name__}",
                    "receipt": receipt_path, "inflight_id": row_id}
        expected = _cas_snapshot({**before, "caption": after})
        if current is None or _cas_snapshot(current) != expected:
            return {"ok": False, "reason": "readback mismatch; reconcile receipt",
                    "receipt": receipt_path, "inflight_id": row_id}
        progress["completed"].append({"id": row_id, "gym_id": before["gym_id"],
                                      "before_caption_sha256": _hash(before["caption"]),
                                      "after_caption_sha256": _hash(current["caption"]),
                                      "before_row_sha256": _hash(before),
                                      "after_row_sha256": _hash(_snapshot(current))})
        progress["inflight_id"] = None
        progress["state"] = "partial_verified"
        try:
            _receipt(receipt_path, progress)
        except Exception as exc:
            return {"ok": False, "reason": "receipt update failed after verified write; reconcile",
                    "receipt": receipt_path, "inflight_id": row_id,
                    "changed": len(progress["completed"]),
                    "conflicts": progress["conflicts"],
                    "receipt_errors": [{"state": "partial_verified", "id": row_id,
                                        "error": type(exc).__name__}]}
    progress["state"] = ("verified" if not progress["conflicts"] else "partial_conflicts")
    try:
        _receipt(receipt_path, progress)
    except Exception as exc:
        receipt_errors.append({"state": progress["state"],
                               "error": type(exc).__name__})
    result = {"ok": not progress["conflicts"] and not receipt_errors,
              "receipt": receipt_path,
              "target_digest": audit["target_digest"],
              "changed": len(progress["completed"]), "conflicts": progress["conflicts"]}
    if receipt_errors:
        result["receipt_errors"] = receipt_errors
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="inclusive YYYY-MM-DD; defaults to UTC today")
    parser.add_argument("--end", help="inclusive YYYY-MM-DD; defaults to start + 61 days")
    parser.add_argument("--gym", help="optional gym_id filter")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt", help="new private JSON path required for apply")
    args = parser.parse_args(argv)
    if args.apply and (not args.start or not args.end):
        parser.error("--apply requires explicit --start and --end")
    start = args.start or datetime.now(timezone.utc).date().isoformat()
    end = args.end or (date.fromisoformat(start) + timedelta(days=61)).isoformat()
    try:
        result = run(start=start, end=end, gym=args.gym, apply=args.apply,
                     expected_digest=args.expected_digest, receipt_path=args.receipt)
    except Exception as exc:
        result = {"ok": False, "reason": f"audit failed:{type(exc).__name__}"}
    # The exact target manifest stays in the private apply receipt. Routine
    # audits need counts and the digest, not hundreds of row hashes on stdout.
    if isinstance(result.get("audit"), dict):
        result["audit"] = {key: value for key, value in result["audit"].items()
                           if key != "targets"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
