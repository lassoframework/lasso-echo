"""Read-only historical delivered-byte inventory for a local JSON snapshot.

The input is a saved row snapshot. This command never queries a database or
registers/claims media. Exact URLs are read once per invocation and results are
written as a deterministic local JSON manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Direct script execution puts scripts/, rather than the repository root, on
# sys.path. Keep both `python scripts/...` and `python -m scripts...` working.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

MAX_BYTES = 128 * 1024 * 1024


def _valid_url(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme in ("http", "https") and bool(parts.netloc) and
                not parts.username and not parts.password and not parts.fragment)
    except ValueError:
        return False


def _load_default_reader():
    """Load the repository's guarded reader once, failing on setup errors."""
    from agent.visual_writer_prepare import _bytes_for_url

    def read_exact_url(url):
        data = _bytes_for_url(url)
        if data is None:
            raise ValueError("exact URL could not be read without redirect or size violation")
        return data

    return read_exact_url


def _default_reader(url):
    """Use the same exact-URL HTTP/R2 reader as the writer preparation path."""
    return _load_default_reader()(url)


def _observe(url, reader):
    if not _valid_url(url):
        return {"status": "error", "error": "invalid_or_missing_exact_url"}
    try:
        data = reader(url)
    except Exception:
        return {"status": "error", "error": "exact_url_read_failed"}
    if not isinstance(data, bytes):
        return {"status": "error", "error": "reader_returned_non_bytes"}
    if not data:
        return {"status": "error", "error": "empty_object"}
    if len(data) > MAX_BYTES:
        return {"status": "error", "error": "object_exceeds_byte_limit"}
    return {
        "status": "observed",
        "md5": hashlib.md5(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_length": len(data),
    }


def _row_ref(row, index):
    value = row.get("row_ref", row.get("id"))
    return str(value) if value is not None and str(value) else f"snapshot-row-{index:06d}"


def _delivered_url(row):
    values = []
    for key in ("image_url", "delivered_url"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    unique = list(dict.fromkeys(values))
    if not unique:
        return None, "missing_delivered_url"
    if len(unique) > 1:
        return None, "ambiguous_delivered_url"
    return unique[0], None


def build_manifest(snapshot, reader=None):
    """Build a stable manifest; reader(url) must return exact delivered bytes."""
    # Resolve this before processing rows: a broken repository import is a
    # command setup failure, not a per-URL observation failure.
    reader = reader if reader is not None else _load_default_reader()
    if isinstance(snapshot, dict):
        rows = snapshot.get("rows")
    else:
        rows = snapshot
    if not isinstance(rows, list):
        raise ValueError("snapshot must be a JSON array or an object containing a rows array")

    cache = {}

    def observation(url):
        # Cache failures too: one exact URL represents one attempted object read.
        if url not in cache:
            cache[url] = _observe(url, reader)
        return dict(cache[url])

    manifest_rows = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            manifest_rows.append({
                "row_ref": f"snapshot-row-{index:06d}", "gym": None,
                "status": None, "date": None, "delivered": {
                    "exact_url": None, "status": "error", "error": "row_is_not_an_object"},
                "source_observation": {"exact_url": None, "status": "not_provided"},
            })
            continue
        delivered_url, error = _delivered_url(row)
        if error:
            delivered = {"exact_url": None, "status": "error", "error": error}
        else:
            delivered = {"exact_url": delivered_url, **observation(delivered_url)}

        source_url = row.get("source_media_url")
        if isinstance(source_url, str) and source_url:
            # This is a separate observation. It never substitutes for delivery.
            source = {"exact_url": source_url, **observation(source_url)}
        else:
            source = {"exact_url": None, "status": "not_provided"}
        manifest_rows.append({
            "row_ref": _row_ref(row, index),
            "gym": row.get("gym_id", row.get("gym")),
            "status": row.get("status", row.get("variant_status")),
            "date": row.get("date", row.get("post_date")),
            "delivered": delivered,
            "source_observation": source,
        })

    return {
        "format": "visual-delivered-byte-inventory-v1",
        "input_row_count": len(rows),
        "unique_exact_urls_read": len(cache),
        "rows": manifest_rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="local JSON snapshot of historical rows")
    parser.add_argument("manifest", type=Path, help="local output JSON manifest")
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        result = build_manifest(snapshot)
        args.manifest.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n",
                                 encoding="utf-8")
    except ImportError as exc:
        print(f"visual byte inventory reader setup failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"visual byte inventory failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
