"""Export a read-only, reconciled observed calendar scan for byte inventory.

This captures rows observed in two matching reads of ``content_calendar``.
The reads are not transactional. Deleted/orphan ledger history and image byte
proof are absent.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIELDS = ("id", "gym_id", "status", "variant_status", "post_date",
          "image_url", "source_media_url", "source_media_asset_id")
DEFAULT_PAGE_SIZE = 500
DEFAULT_MAX_PAGES = 10000
WARNING = ("This snapshot excludes deleted/orphan ledger history and does not "
           "provide image byte proof. It is a non-atomic observed scan; scan "
           "start/end times and a second-pass reconciliation do not establish "
           "a point-in-time database snapshot.")


def _read_pass(store, page_size, max_pages):
    rows = []
    expected_total = None
    for page in range(max_pages):
        offset = page * page_size
        response = store._client().get(
            store._rest("content_calendar"),
            params={"select": ",".join(FIELDS), "order": "id.asc",
                    "limit": str(page_size), "offset": str(offset)},
            headers=store._headers({"Prefer": "count=exact"}), timeout=30)
        if response.status_code >= 400:
            raise ValueError(f"calendar query failed with HTTP {response.status_code}")
        page_rows = response.json()
        if not isinstance(page_rows, list):
            raise ValueError("calendar query returned a non-list page")
        content_range = (getattr(response, "headers", {}) or {}).get("Content-Range", "")
        try:
            range_part, total_text = content_range.rsplit("/", 1)
            total = int(total_text)
            if range_part == "*" and total == 0 and not page_rows and offset == 0:
                start, end = 0, -1
            else:
                start_text, end_text = range_part.split("-", 1)
                start, end = int(start_text), int(end_text)
        except (ValueError, AttributeError):
            raise ValueError("calendar page is missing a valid exact Content-Range") from None
        if expected_total is None:
            expected_total = total
        if total != expected_total:
            raise ValueError("calendar total changed during pagination")
        if (start != offset or
                (end - start + 1 if page_rows else 0) != len(page_rows)):
            raise ValueError("calendar pagination range does not match returned rows")
        expected_count = min(page_size, max(0, total - offset))
        if len(page_rows) != expected_count:
            raise ValueError("calendar page is truncated or inconsistent with exact count")
        for row in page_rows:
            if not isinstance(row, dict) or any(field not in row for field in FIELDS):
                raise ValueError("calendar row is missing a required selected field")
            # `status` may be NULL for legitimate drafts. Preserve it exactly;
            # identity and variant/date fields still must be present and usable.
            if any(row[field] is None or row[field] == "" for field in
                   ("id", "gym_id", "variant_status", "post_date")):
                raise ValueError("calendar row has an empty required identity field")
            rows.append({field: row[field] for field in FIELDS})
        if len(rows) == total:
            break
        if not page_rows:
            raise ValueError("calendar pagination stopped before exact total")
    else:
        raise ValueError("calendar pagination exceeded max_pages")

    ids = [str(row["id"]) for row in rows]
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError("calendar rows are not in unique stable id order")
    return rows


def _iso_utc(timestamp):
    if timestamp.tzinfo is None:
        raise ValueError("scan timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_snapshot(store, *, page_size=DEFAULT_PAGE_SIZE,
                   max_pages=DEFAULT_MAX_PAGES, now=None):
    """Read and reconcile two bounded passes; this is not a point-in-time read."""
    if not isinstance(page_size, int) or page_size < 1 or page_size > 1000:
        raise ValueError("page_size must be between 1 and 1000")
    if not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages must be positive")
    clock = (lambda: now) if now is not None else lambda: datetime.now(timezone.utc)
    scan_started = clock()
    if scan_started.tzinfo is None:
        raise ValueError("scan timestamp must include a timezone")
    rows = _read_pass(store, page_size, max_pages)
    reconciled_rows = _read_pass(store, page_size, max_pages)
    if rows != reconciled_rows:
        raise ValueError("calendar rows changed between reconciliation passes")
    scan_completed = clock()
    if scan_completed.tzinfo is None:
        raise ValueError("scan timestamp must include a timezone")
    return {
        "format": "visual-calendar-snapshot-v1",
        "snapshot_at": _iso_utc(scan_completed),
        "scan_started_at": _iso_utc(scan_started),
        "scan_completed_at": _iso_utc(scan_completed),
        "consistency": "non_atomic_observed_scan_reconciled",
        "reconciliation_passes": 2,
        "row_count": len(rows),
        "warning": WARNING,
        "rows": rows,
    }


def write_snapshot(path, snapshot):
    """Atomically replace ``path`` with JSON whose permissions are owner-only."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                     dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="local JSON output path")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args(argv)
    try:
        from agent.portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
        if not store._url or not store._key:
            raise RuntimeError
    except Exception:
        print("visual calendar snapshot failed: configuration_error", file=sys.stderr)
        return 2
    try:
        snapshot = fetch_snapshot(store, page_size=args.page_size,
                                  max_pages=args.max_pages)
    except Exception:
        print("visual calendar snapshot failed: query_or_reconciliation_error",
              file=sys.stderr)
        return 2
    try:
        write_snapshot(args.snapshot, snapshot)
    except Exception:
        print("visual calendar snapshot failed: output_error", file=sys.stderr)
        return 2
    print(f"wrote {snapshot['row_count']} rows to {args.snapshot}; {WARNING}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
