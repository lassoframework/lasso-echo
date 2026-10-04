"""Export a read-only, reconciled observed scan of public.media_asset.

This captures rows observed in two matching reads of ``media_asset`` (the
live table has: id, source_id, gym_id, kind, title, mime_type, content_hash,
rendition_key, rendition_url, eligible, excluded_by_coach, used_count,
last_used_at, indexed_at, review_status, moderation_status, consent_status,
review_content_hash). The reads are not transactional.

``content_hash`` is a Drive MD5 hint only: it is NOT an authenticated digest
of original upload bytes and is never byte-identity proof. ``rendition_url``
is a processed-rendition URL, NOT a source_media_url. This snapshot selects
only the safe columns above; it never maps ``content_hash`` to a digest field
and never maps ``rendition_url`` to a source URL, and it never writes SQL.
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

FIELDS = ("id", "source_id", "gym_id", "kind", "title", "mime_type",
          "content_hash", "rendition_key", "rendition_url", "eligible",
          "excluded_by_coach", "used_count", "last_used_at", "indexed_at",
          "review_status", "moderation_status", "consent_status",
          "review_content_hash")
# These are the actual NOT NULL columns in public.media_asset. The remaining
# selected fields are preserved verbatim because they are genuinely nullable.
REQUIRED_NON_NULL_FIELDS = ("id", "source_id", "gym_id", "kind", "title",
                            "excluded_by_coach", "used_count", "indexed_at",
                            "review_status", "moderation_status",
                            "consent_status")
TABLE = "media_asset"
FORMAT = "visual-asset-snapshot-v1"
DEFAULT_PAGE_SIZE = 500
DEFAULT_MAX_PAGES = 10000
WARNING = ("This snapshot is a non-atomic observed scan reconciled across "
           "two passes; scan start/end times do not establish a "
           "point-in-time database snapshot. content_hash is a Drive MD5 "
           "hint only and is not authenticated original-byte proof; "
           "rendition_url is a processed-rendition URL, not a source URL; "
           "no sha256/md5/source_media_url columns are exported.")


def _read_pass(store, page_size, max_pages):
    rows = []
    expected_total = None
    for page in range(max_pages):
        offset = page * page_size
        response = store._client().get(
            store._rest(TABLE),
            params={"select": ",".join(FIELDS), "order": "id.asc",
                    "limit": str(page_size), "offset": str(offset)},
            headers=store._headers({"Prefer": "count=exact"}), timeout=30)
        if response.status_code >= 400:
            raise ValueError(f"media_asset query failed with HTTP {response.status_code}")
        page_rows = response.json()
        if not isinstance(page_rows, list):
            raise ValueError("media_asset query returned a non-list page")
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
            raise ValueError("media_asset page is missing a valid exact Content-Range") from None
        if expected_total is None:
            expected_total = total
        if total != expected_total:
            raise ValueError("media_asset total changed during pagination")
        if (start != offset or
                (end - start + 1 if page_rows else 0) != len(page_rows)):
            raise ValueError("media_asset pagination range does not match returned rows")
        expected_count = min(page_size, max(0, total - offset))
        if len(page_rows) != expected_count:
            raise ValueError("media_asset page is truncated or inconsistent with exact count")
        for row in page_rows:
            if not isinstance(row, dict) or any(field not in row for field in FIELDS):
                raise ValueError("media_asset row is missing a required selected field")
            if any(row[field] is None or
                   (isinstance(row[field], str) and not row[field].strip())
                   for field in REQUIRED_NON_NULL_FIELDS):
                raise ValueError("media_asset row has an empty required NOT NULL field")
            rows.append({field: row[field] for field in FIELDS})
        if len(rows) == total:
            break
        if not page_rows:
            raise ValueError("media_asset pagination stopped before exact total")
    else:
        raise ValueError("media_asset pagination exceeded max_pages")

    ids = [str(row["id"]) for row in rows]
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError("media_asset rows are not in unique stable id order")
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
        raise ValueError("media_asset rows changed between reconciliation passes")
    scan_completed = clock()
    if scan_completed.tzinfo is None:
        raise ValueError("scan timestamp must include a timezone")
    return {
        "format": FORMAT,
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
        print("visual asset snapshot failed: configuration_error", file=sys.stderr)
        return 2
    try:
        snapshot = fetch_snapshot(store, page_size=args.page_size,
                                  max_pages=args.max_pages)
    except Exception:
        print("visual asset snapshot failed: query_or_reconciliation_error",
              file=sys.stderr)
        return 2
    try:
        write_snapshot(args.snapshot, snapshot)
    except Exception:
        print("visual asset snapshot failed: output_error", file=sys.stderr)
        return 2
    print(f"wrote {snapshot['row_count']} rows to {args.snapshot}; {WARNING}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
