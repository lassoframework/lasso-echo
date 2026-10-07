"""Resumable read-only batches over explicit historical row IDs.

This orchestration is intentionally limited to evidence collection. It
requires a caller-supplied allowlist of at most 517 published row IDs, splits
it into 5–10-row manifests, validates every selected row/source/URL using the
existing collector before any configured reader runs, and writes hash-only
0600 local manifests. Existing complete batches are resumed by exact input
hash and row-list identity. No database writes, claims, clearance decisions,
approvals, or media files are produced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from agent.historical_media_evidence_collect import (
    EvidenceCollectionError,
    collect_pilot,
    write_manifest,
)

MAX_ROWS = 517
MIN_BATCH_SIZE = 5
MAX_BATCH_SIZE = 10
FORMAT = "historical-media-evidence-batch-v1"
_ROW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class BatchError(ValueError):
    """Safe batch orchestration failure with a static reason code."""


def _rows(payload, label):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload["rows"]
    raise BatchError(f"{label}_snapshot_invalid")


def _ids(payload):
    values = payload.get("row_ids") if isinstance(payload, dict) else payload
    if (not isinstance(values, list) or not MIN_BATCH_SIZE <= len(values) <= MAX_ROWS
            or any(not isinstance(value, str) or not _ROW_ID_RE.fullmatch(value)
                   for value in values)
            or len(set(values)) != len(values)):
        raise BatchError("allowlist_must_name_5_to_517_unique_row_ids")
    return list(values)


def _partition(row_ids, max_size):
    if (not isinstance(max_size, int) or isinstance(max_size, bool)
            or not MIN_BATCH_SIZE <= max_size <= MAX_BATCH_SIZE):
        raise BatchError("batch_size_must_be_5_to_10")
    count = (len(row_ids) + max_size - 1) // max_size
    base, remainder = divmod(len(row_ids), count)
    if base < MIN_BATCH_SIZE:
        raise BatchError("allowlist_cannot_form_bounded_batches")
    batches, cursor = [], 0
    for i in range(count):
        size = base + (1 if i < remainder else 0)
        batches.append(row_ids[cursor:cursor + size])
        cursor += size
    return batches


def _canonical_hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _input_refs(calendar_snapshot, asset_snapshot, source_snapshot, supplied=None):
    if supplied is not None:
        required = {"calendar_snapshot_sha256", "asset_snapshot_sha256",
                    "source_snapshot_sha256"}
        if (not isinstance(supplied, dict) or set(supplied) != required or
                any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                    for v in supplied.values())):
            raise BatchError("input_refs_invalid")
        return dict(supplied)
    return {
        "calendar_snapshot_sha256": _canonical_hash(calendar_snapshot),
        "asset_snapshot_sha256": _canonical_hash(asset_snapshot),
        "source_snapshot_sha256": _canonical_hash(source_snapshot),
    }


def _private_output_dir(path):
    target = Path(path)
    existed = target.exists()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode & 0o077:
        # Do not silently chmod a pre-existing directory whose ownership or
        # contents may belong to another task/user.
        raise BatchError("output_dir_not_private")
    return target


def _batch_key(index, row_ids, input_refs):
    payload = {"batch_index": index, "row_ids": row_ids,
               "input_refs": input_refs}
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _read_existing(path, expected):
    if not path.exists():
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise BatchError("existing_manifest_unreadable") from None
    if mode != 0o600:
        raise BatchError("existing_manifest_permissions_invalid")
    # Full manifest identity is checked by canonical comparison, ensuring a
    # partial or edited artifact is never mistaken for a completed batch.
    if not isinstance(value, dict) or value.get("batch_key") != expected:
        raise BatchError("existing_manifest_identity_mismatch")
    if value.get("format") != FORMAT or value.get("cleared") != 0:
        raise BatchError("existing_manifest_invalid")
    evidence = value.get("evidence")
    summary = evidence.get("summary") if isinstance(evidence, dict) else None
    evidence_rows = evidence.get("rows") if isinstance(evidence, dict) else None
    if (not isinstance(evidence, dict) or not isinstance(summary, dict) or
            evidence.get("format") != "historical-media-evidence-pilot-v1" or
            summary.get("cleared") != 0 or
            not isinstance(evidence_rows, list) or
            evidence.get("pilot_size") != len(evidence_rows)):
        raise BatchError("existing_manifest_invalid")
    for row in evidence_rows:
        if (not isinstance(row, dict) or row.get("decision") != "unresolved" or
                not isinstance(row.get("row_id"), str) or
                row.get("classification") not in
                ("candidate_exact_byte_match", "unknown")):
            raise BatchError("existing_manifest_invalid")
    return value


def run_batches(calendar_snapshot, asset_snapshot, source_snapshot, allowlist,
                output_dir, *, delivered_reader, drive_reader, batch_size=10,
                input_refs=None):
    """Validate the full allowlist locally, then collect/resume bounded batches."""
    row_ids = _ids(allowlist)
    batches = _partition(row_ids, batch_size)
    refs = _input_refs(calendar_snapshot, asset_snapshot, source_snapshot, input_refs)

    # Whole-run schema, publication, tenant, source and URL validation. These
    # callbacks are local synthetic values; this loop intentionally precedes
    # every production reader call and manifest write.
    for batch in batches:
        collect_pilot(calendar_snapshot, asset_snapshot, source_snapshot, batch,
                      delivered_reader=lambda _url: b"preflight-only",
                      drive_reader=lambda _file_id: b"preflight-only")

    destination = _private_output_dir(output_dir)
    planned = []
    for index, row_batch in enumerate(batches, 1):
        key = _batch_key(index, row_batch, refs)
        path = destination / f"batch-{index:03d}-{key[:16]}.json"
        existing = _read_existing(path, key)
        if existing is not None:
            if (existing.get("selected_row_ids") != row_batch or
                    existing.get("input_refs") != refs or
                    existing.get("batch_index") != index or
                    existing.get("batch_count") != len(batches) or
                    existing.get("evidence", {}).get("pilot_size") != len(row_batch) or
                    {row["row_id"] for row in existing["evidence"]["rows"]} != set(row_batch)):
                raise BatchError("existing_manifest_batch_mismatch")
        planned.append((index, row_batch, key, path, existing))

    results = []
    for index, row_batch, key, path, existing in planned:
        if existing is not None:
            results.append({"path": str(path), "resumed": True,
                            "row_count": len(row_batch), "batch_key": key})
            continue

        evidence = collect_pilot(calendar_snapshot, asset_snapshot, source_snapshot,
                                 row_batch, delivered_reader=delivered_reader,
                                 drive_reader=drive_reader)
        manifest = {
            "format": FORMAT,
            "batch_key": key,
            "batch_index": index,
            "batch_count": len(batches),
            "selected_row_ids": row_batch,
            "input_refs": refs,
            "candidate_matches": evidence["summary"]["candidate_exact_byte_match"],
            "unknown": evidence["summary"]["unknown"],
            "cleared": 0,
            "evidence": evidence,
        }
        write_manifest(path, manifest)
        results.append({"path": str(path), "resumed": False,
                        "row_count": len(row_batch), "batch_key": key})
    return {"format": FORMAT, "selected_rows": len(row_ids),
            "batch_count": len(batches), "completed_batches": len(results),
            "resumed_batches": sum(r["resumed"] for r in results),
            "cleared": 0, "batches": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calendar_snapshot", type=Path)
    parser.add_argument("asset_snapshot", type=Path)
    parser.add_argument("source_snapshot", type=Path)
    parser.add_argument("allowlist", type=Path,
                        help="JSON array or {row_ids:[...]} of explicit published row IDs")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        paths = (args.calendar_snapshot, args.asset_snapshot, args.source_snapshot)
        calendar, assets, sources, allowlist = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (*paths, args.allowlist)]
        refs = {
            "calendar_snapshot_sha256": hashlib.sha256(paths[0].read_bytes()).hexdigest(),
            "asset_snapshot_sha256": hashlib.sha256(paths[1].read_bytes()).hexdigest(),
            "source_snapshot_sha256": hashlib.sha256(paths[2].read_bytes()).hexdigest(),
        }
        result = run_batches(calendar, assets, sources, allowlist, args.output_dir,
                             delivered_reader=_hosted_reader,
                             drive_reader=_drive_reader,
                             batch_size=args.batch_size, input_refs=refs)
    except (OSError, json.JSONDecodeError, BatchError, EvidenceCollectionError) as exc:
        reason = str(exc) if isinstance(exc, (BatchError, EvidenceCollectionError)) else "input_or_output_error"
        print(f"historical media batch failed: {reason}", file=sys.stderr)
        return 2
    print(f"selected={result['selected_rows']} batches={result['batch_count']} "
          f"resumed={result['resumed_batches']} cleared=0")
    return 0


def _hosted_reader(url):
    from agent.historical_media_evidence_collect import _hosted_reader as read
    return read(url)


def _drive_reader(file_id):
    from agent.historical_media_evidence_collect import _drive_reader as read
    return read(file_id)


if __name__ == "__main__":
    raise SystemExit(main())
