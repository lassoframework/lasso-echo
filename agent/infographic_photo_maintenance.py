"""Read-only inventory of future infographic-fill posts needing real photos.

The separate hold path protects pending rows. This inventory makes no media,
approval, status, or publication changes and never hosts a replacement.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import media_swap

_ACTIVE = {"pending", "approved", "coach_review"}
_WAITING = {"pending"}
_IGFILL = re.compile(r"(?:^|/)igfill_\d{4}-\d{2}-\d{2}(?:[_-]|\.)", re.I)
_BEFORE = ("id", "gym_id", "post_date", "status", "variant_status", "caption",
           "image_url", "source_media_url", "source_media_asset_id")


def _is_fill(row):
    url = urlsplit(str(row.get("image_url") or "")).path
    return (_IGFILL.search(url) is not None
            and str(row.get("format") or "").lower() == "feed"
            and row.get("status") in _ACTIVE
            and row.get("variant_status") == "active")


def plan(rows):
    """Enumerate complete same-post groups; never pick, host, or write."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row.get("gym_id"), row.get("post_date"))].append(row)
    plans = []
    seen_ids = set()
    for row in rows:
        if not _is_fill(row) or row.get("id") in seen_ids:
            continue
        siblings = media_swap.sibling_rows(
            row, groups[(row.get("gym_id"), row.get("post_date"))])
        affected = [row, *siblings]
        seen_ids.update(r.get("id") for r in affected)
        locked = [r for r in affected if r.get("status") not in _WAITING]
        plans.append({"gym_id": row.get("gym_id"), "post_date": row.get("post_date"),
                      "target_id": row.get("id"), "rows": affected,
                      "blocked": bool(locked),
                      "locked_ids": [str(r.get("id")) for r in locked]})
    return plans


def _receipt_write(path, receipts):
    """Private atomic inventory; preserve the prior receipt if a write fails."""
    import os
    import tempfile
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".infographic-photo-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(receipts, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(rows, *, receipt_path=None):
    """Inventory every matching post; no mutation path exists in this module."""
    receipts = []
    for item in plan(rows):
        affected = item["rows"]
        before = [{key: row.get(key) for key in _BEFORE} for row in affected]
        receipt = {"gym_id": item["gym_id"], "post_date": item["post_date"],
                   "target_id": item["target_id"], "before": before,
                   "status": ("approved_sibling_requires_scoped_hold" if item["blocked"]
                              else "requires_hold_until_photo_verified"),
                   "pending_hold_ids": [str(row.get("id")) for row in affected
                                        if row.get("status") in _WAITING],
                   "locked_ids": item["locked_ids"]}
        receipts.append(receipt)
        if receipt_path:
            _receipt_write(receipt_path, receipts)
    return receipts


def load_future(store, *, today, horizon_days=365):
    start = date.fromisoformat(today)
    end = (start + timedelta(days=horizon_days)).isoformat()
    return store.list_future_media_maintenance_rows(
        (start + timedelta(days=1)).isoformat(), end)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only future infographic inventory")
    parser.add_argument("--today", required=True, help="UTC YYYY-MM-DD scan start")
    parser.add_argument("--receipt", required=True, help="private JSON receipt path")
    args = parser.parse_args(argv)
    from .portal_calendar_store import SupabaseCalendarStore
    store = SupabaseCalendarStore()
    rows = load_future(store, today=args.today)
    receipts = run(rows, receipt_path=args.receipt)
    print(json.dumps({"dry_run": True, "rows_read": len(rows),
                      "posts": len(receipts), "receipt": args.receipt}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
