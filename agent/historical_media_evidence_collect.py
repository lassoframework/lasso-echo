"""Read-only, bounded collection of historical delivered/original byte evidence.

The collector requires a local two-pass calendar snapshot and media-asset
snapshot plus an explicit 5–10 row allowlist. It downloads exact published
objects from the configured hosted-media reader and candidate Drive originals
for the row's same-tenant source asset. It stores only hashes, lengths, opaque
URL references and provenance IDs in an owner-only local JSON manifest. It
never persists raw media, updates production, clears history, approves assets,
or claims usage. Exact byte equality is only a candidate-use finding; all
other cases remain unknown.

Production reads are opt-in at invocation. Tests inject readers and use only
synthetic bytes. Before a live invocation, an operator must verify the
configured hosted-media origin and Drive source mapping for the selected rows.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

PILOT_MIN = 5
PILOT_MAX = 10
MAX_OBJECT_BYTES = 128 * 1024 * 1024
FORMAT = "historical-media-evidence-pilot-v1"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class EvidenceCollectionError(ValueError):
    """Fail-closed input or evidence error with no secret-bearing detail."""


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _rows(payload, label):
    rows = payload if isinstance(payload, list) else payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise EvidenceCollectionError(f"{label}_snapshot_invalid")
    return rows


def _url_ref(url):
    """Return a non-reversible reference; never persist URL query credentials."""
    if not _text(url):
        raise EvidenceCollectionError("exact_url_missing")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise EvidenceCollectionError("exact_url_invalid") from None
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        raise EvidenceCollectionError("exact_url_invalid")
    return "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest()


def _read_bytes(reader, ref):
    try:
        data = reader(ref)
    except Exception:
        raise EvidenceCollectionError("object_read_failed") from None
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise EvidenceCollectionError("object_bytes_missing")
    data = bytes(data)
    if len(data) > MAX_OBJECT_BYTES:
        raise EvidenceCollectionError("object_too_large")
    return data


def _hashes(data):
    return {"md5": hashlib.md5(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_length": len(data)}


def _is_published_at(value):
    if not _text(value):
        return False
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    return stamp.tzinfo is not None and stamp.utcoffset() is not None


def collect_pilot(calendar_snapshot, asset_snapshot, source_snapshot, row_ids, *,
                  delivered_reader, drive_reader):
    """Collect an explicit 5–10 row pilot. Readers are read-only callbacks.

    ``delivered_reader(exact_url)`` and ``drive_reader(drive_file_id)`` return
    exact bytes. A missing asset binding, tenant mismatch, or read failure
    aborts the whole packet so no partial manifest can be mistaken for coverage.
    """
    if (not isinstance(row_ids, (list, tuple)) or
            not PILOT_MIN <= len(row_ids) <= PILOT_MAX or
            any(not isinstance(v, str) or not _ID_RE.fullmatch(v) for v in row_ids) or
            len(set(row_ids)) != len(row_ids)):
        raise EvidenceCollectionError("pilot_must_name_5_to_10_unique_row_ids")
    calendars = _rows(calendar_snapshot, "calendar")
    assets = _rows(asset_snapshot, "asset")
    sources = _rows(source_snapshot, "source")
    by_row = {}
    for row in calendars:
        if isinstance(row, dict) and _text(row.get("id")):
            if row["id"] in by_row:
                raise EvidenceCollectionError("duplicate_calendar_row_id")
            by_row[row["id"]] = row
    by_asset = {}
    for asset in assets:
        if isinstance(asset, dict) and _text(asset.get("id")):
            if asset["id"] in by_asset:
                raise EvidenceCollectionError("duplicate_asset_id")
            by_asset[asset["id"]] = asset

    by_source = {}
    for source in sources:
        if isinstance(source, dict) and _text(source.get("id")):
            if source["id"] in by_source:
                raise EvidenceCollectionError("duplicate_source_id")
            by_source[source["id"]] = source

    # Validate the complete allowlist and all identity/URL bindings before the
    # first reader callback can perform network I/O.
    selected = []
    result = []
    for row_id in sorted(row_ids):
        row = by_row.get(row_id)
        if row is None:
            raise EvidenceCollectionError("selected_row_missing")
        if not _text(row.get("gym_id")):
            raise EvidenceCollectionError("tenant_missing")
        if row.get("status") != "published" or not _is_published_at(row.get("published_at")):
            raise EvidenceCollectionError("selected_row_not_published")
        asset_id = row.get("source_media_asset_id")
        if not _text(asset_id):
            raise EvidenceCollectionError("source_asset_id_missing")
        asset = by_asset.get(asset_id)
        if asset is None:
            raise EvidenceCollectionError("source_asset_missing")
        if not _text(asset.get("gym_id")) or asset["gym_id"] != row["gym_id"]:
            raise EvidenceCollectionError("tenant_mismatch")
        if not _text(asset.get("id")):
            raise EvidenceCollectionError("drive_file_id_missing")
        if not _text(asset.get("source_id")):
            raise EvidenceCollectionError("drive_source_id_missing")
        source = by_source.get(asset["source_id"])
        if source is None:
            raise EvidenceCollectionError("media_source_missing")
        if not _text(source.get("gym_id")) or source["gym_id"] != row["gym_id"]:
            raise EvidenceCollectionError("media_source_tenant_mismatch")
        if source.get("kind") != "gym_drive" or not _text(source.get("folder_id")):
            raise EvidenceCollectionError("media_source_mapping_invalid")
        delivered_url = row.get("image_url")
        source_url = row.get("source_media_url")
        if not _text(delivered_url):
            raise EvidenceCollectionError("delivered_url_missing")
        # Source endpoint must be an exact HTTPS URL when present. It is
        # recorded only as a one-way ref and never used as a substitute for the
        # Drive original identified by this tenant-bound asset row.
        delivered_ref = _url_ref(delivered_url)
        source_ref = _url_ref(source_url) if _text(source_url) else None
        selected.append((row, asset, delivered_url, delivered_ref, source_ref))

    for row, asset, delivered_url, delivered_ref, source_ref in selected:
        row_id = row["id"]
        asset_id = row["source_media_asset_id"]
        delivered = _read_bytes(delivered_reader, delivered_url)
        original = _read_bytes(drive_reader, asset["id"])
        delivered_hashes, original_hashes = _hashes(delivered), _hashes(original)
        equal = delivered_hashes["sha256"] == original_hashes["sha256"]
        result.append({
            "row_id": row_id,
            "gym_id": row["gym_id"],
            "post_date": row.get("post_date"),
            "published_at": row.get("published_at"),
            "provider_post_id_present": bool(_text(row.get("late_post_id"))),
            "source_asset_id": asset_id,
            "source_id": asset.get("source_id"),
            "delivered_url_ref": delivered_ref,
            "source_url_ref": source_ref,
            "drive_file_ref": "sha256:" + hashlib.sha256(asset["id"].encode()).hexdigest(),
            "delivered": delivered_hashes,
            "drive_original": original_hashes,
            "classification": "candidate_exact_byte_match" if equal else "unknown",
            "reason": "same_tenant_exact_bytes_candidate" if equal else "byte_identity_differs_or_rendition",
            "decision": "unresolved",
        })
    return {"format": FORMAT, "pilot_size": len(result),
            "summary": {"candidate_exact_byte_match": sum(
                r["classification"] == "candidate_exact_byte_match" for r in result),
                "unknown": sum(r["classification"] == "unknown" for r in result),
                "cleared": 0},
            "rows": result}


def _hosted_reader(url):
    from agent.forward_media_owner import HostedObjectReader
    return HostedObjectReader().read(url)


def _drive_reader(file_id, *, client_factory=None):
    if client_factory is None:
        from agent.integrations.drive_client import DriveClient
        client_factory = DriveClient
    client = client_factory()
    if not client.available():
        raise EvidenceCollectionError("drive_unavailable")
    # Read authorized metadata first, then enforce the same limit while bytes
    # stream. The sink stops the transfer if the object changes or metadata lies.
    transport = client._t()
    try:
        meta = transport._service().files().get(
            fileId=file_id, fields="id,size,trashed", supportsAllDrives=True).execute()
    except Exception:
        raise EvidenceCollectionError("drive_metadata_read_failed") from None
    if (not isinstance(meta, dict) or meta.get("id") != file_id or
            meta.get("trashed") is not False):
        raise EvidenceCollectionError("drive_file_identity_invalid")
    try:
        size = int(meta.get("size"))
    except (TypeError, ValueError):
        raise EvidenceCollectionError("drive_size_unavailable") from None
    if size < 1 or size > MAX_OBJECT_BYTES:
        raise EvidenceCollectionError("object_too_large_or_empty")

    class BoundedSink:
        def __init__(self, limit):
            self.buffer = io.BytesIO()
            self.limit = limit

        def write(self, chunk):
            if len(self.buffer.getbuffer()) + len(chunk) > self.limit:
                raise EvidenceCollectionError("object_too_large")
            return self.buffer.write(chunk)

        def bytes(self):
            return self.buffer.getvalue()

    sink = BoundedSink(MAX_OBJECT_BYTES)
    try:
        transport.download_to(file_id, sink)
    except EvidenceCollectionError:
        raise
    except Exception:
        raise EvidenceCollectionError("drive_download_failed") from None
    data = sink.bytes()
    if not data or len(data) != size:
        raise EvidenceCollectionError("drive_download_size_mismatch")
    return data


def write_manifest(path, manifest):
    """Atomically write a local owner-only JSON manifest (never object bytes)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, indent=2)
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
    parser.add_argument("calendar_snapshot", type=Path)
    parser.add_argument("asset_snapshot", type=Path)
    parser.add_argument("source_snapshot", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--row-id", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        calendar = json.loads(args.calendar_snapshot.read_text(encoding="utf-8"))
        assets = json.loads(args.asset_snapshot.read_text(encoding="utf-8"))
        sources = json.loads(args.source_snapshot.read_text(encoding="utf-8"))
        result = collect_pilot(calendar, assets, sources, args.row_id,
                               delivered_reader=_hosted_reader, drive_reader=_drive_reader)
        result["input_refs"] = {
            "calendar_snapshot_sha256": hashlib.sha256(args.calendar_snapshot.read_bytes()).hexdigest(),
            "asset_snapshot_sha256": hashlib.sha256(args.asset_snapshot.read_bytes()).hexdigest(),
            "source_snapshot_sha256": hashlib.sha256(args.source_snapshot.read_bytes()).hexdigest(),
        }
        write_manifest(args.manifest, result)
    except (OSError, json.JSONDecodeError, EvidenceCollectionError) as exc:
        reason = str(exc) if isinstance(exc, EvidenceCollectionError) else "input_or_output_error"
        print(f"historical media evidence pilot failed: {reason}", file=sys.stderr)
        return 2
    print(f"wrote {result['pilot_size']} rows; candidate_matches="
          f"{result['summary']['candidate_exact_byte_match']}; unknown="
          f"{result['summary']['unknown']}; cleared=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
