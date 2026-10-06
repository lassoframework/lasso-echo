"""Build a deterministic, offline JSONL manifest for historical media review.

This tool reads only supplied JSON snapshots and operator-saved delivered
bytes. Delivered-byte hash matches are candidate evidence only; they never
establish source lineage or original use. The tool makes no network, database,
or production-state requests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:  # direct script and package import
    from historical_media_evidence import _OversizedFile, _md5_file, _safe_bytes_path
except ImportError:  # pragma: no cover - package execution path
    from scripts.historical_media_evidence import _OversizedFile, _md5_file, _safe_bytes_path


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _reference(row, fields):
    present = [(field, row[field]) for field in fields
               if field in row and row[field] is not None and row[field] != ""]
    values = {json.dumps(value, sort_keys=True, default=str) for _, value in present}
    if len(values) > 1:
        return None, True
    if not present:
        return None, False
    field, value = present[0]
    return {"field": field, "value": value}, False


def _asset_id(asset):
    return _text(asset.get("asset_id") or asset.get("media_asset_id"))


def _row_ref(row):
    return _text(row.get("row_id") or row.get("id"))


def _candidate_index(assets):
    """Index explicit source references and asset MD5 hints separately."""
    index = {"asset_id": {}, "source_url": {}, "content_hash": {}}
    malformed = 0
    seen_ids = Counter()
    for asset in assets:
        if not isinstance(asset, dict):
            malformed += 1
            continue
        aid = _asset_id(asset)
        gym = _text(asset.get("gym_id") or asset.get("tenant_id"))
        if not aid or not gym:
            malformed += 1
            continue
        entry = {"asset_id": aid, "gym_id": gym}
        seen_ids[(gym, aid)] += 1
        index["asset_id"].setdefault((gym, aid), []).append(entry)
        # Only explicit source fields participate. Rendition/delivered URLs
        # are intentionally excluded from source lineage.
        source_url = _text(asset.get("source_media_url") or asset.get("source_url"))
        if source_url:
            index["source_url"].setdefault((gym, source_url), []).append(entry)
        digest = _text(asset.get("content_hash"))
        if digest:
            if re.fullmatch(r"[0-9a-fA-F]{32}", digest):
                index["content_hash"].setdefault((gym, digest.lower()), []).append(entry)
            else:
                malformed += 1
    duplicate_assets = sum(count - 1 for count in seen_ids.values() if count > 1)
    return index, malformed, duplicate_assets


def build_manifest(snapshot, media_dir: Path, snapshot_identity=None):
    """Return JSONL record objects from a local snapshot and local byte files.

    Accepted snapshot keys are ``published_rows`` (or ``rows``),
    ``media_assets`` (or ``assets``), and optional ``snapshot_identity``.
    Byte files are named by the exact row reference, using the existing safe
    path resolver and bounded streaming digest helper.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a JSON object")
    rows = snapshot.get("published_rows", snapshot.get("rows"))
    assets = snapshot.get("media_assets", snapshot.get("assets", []))
    if not isinstance(rows, list):
        raise ValueError("snapshot must contain a published_rows or rows array")
    if not isinstance(assets, list):
        assets = []
    identity = snapshot_identity if snapshot_identity is not None else snapshot.get("snapshot_identity")
    if identity is None:
        identity = {"provided": False}

    index, malformed_assets, duplicate_assets = _candidate_index(assets)
    refs = Counter(_row_ref(row) for row in rows if isinstance(row, dict) and _row_ref(row))
    output = []
    reason_counts = Counter()
    malformed_rows = duplicate_rows = delivered_read_count = 0

    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            malformed_rows += 1
            reason_counts["malformed_row"] += 1
            output.append({
                "record_type": "row", "snapshot_identity": identity,
                "input_index": number, "row_ref": None, "gym_id": None,
                "date": None, "provider": None, "delivered_reference": None,
                "source_reference": None, "delivered_bytes": {"status": "unavailable"},
                "candidate_matches": {"basis": "delivered_md5_vs_asset_content_hash",
                                      "cardinality": 0, "asset_ids": []},
                "source_lineage_evidence": {"basis": "explicit_source_reference_only",
                                            "asset_ids": [], "status": "not_assessed"},
                "unresolved_reasons": ["malformed_row"],
            })
            continue

        row_ref = _row_ref(row)
        gym = _text(row.get("gym_id") or row.get("tenant_id"))
        date = row.get("post_date", row.get("date"))
        provider = row.get("provider") or row.get("platform")
        delivered_ref, delivered_ambiguous = _reference(row, ("delivered_url", "image_url", "media_url"))
        source_ref, source_ambiguous = _reference(row, ("source_media_asset_id", "source_asset_id", "source_media_url", "source_url"))
        unresolved = []
        if not row_ref:
            unresolved.append("missing_row_ref")
        if not gym:
            unresolved.append("missing_gym_id")
        if not delivered_ref:
            unresolved.append("ambiguous_delivered_reference" if delivered_ambiguous else "missing_delivered_reference")
        if source_ambiguous:
            unresolved.append("ambiguous_source_reference")

        byte_result = {"status": "unavailable", "reason": "missing_row_ref"}
        digest = None
        if row_ref:
            path = _safe_bytes_path(Path(media_dir), row_ref)
            if path is None:
                byte_result = {"status": "unavailable", "reason": "missing_or_unsafe_local_bytes"}
            else:
                try:
                    digest = _md5_file(path)
                    byte_result = {"status": "read", "md5": digest, "byte_length": path.stat().st_size}
                    delivered_read_count += 1
                except _OversizedFile:
                    byte_result = {"status": "unavailable", "reason": "local_bytes_exceed_limit"}
                except OSError:
                    byte_result = {"status": "unavailable", "reason": "local_bytes_unreadable"}
        if byte_result["status"] != "read":
            unresolved.append(byte_result["reason"])

        candidates = []
        if gym and digest:
            candidates = sorted({entry["asset_id"] for entry in index["content_hash"].get((gym, digest), [])})
        if source_ref and gym:
            value = source_ref["value"]
            if source_ref["field"] in ("source_media_asset_id", "source_asset_id") and _text(value):
                lineage = sorted({e["asset_id"] for e in index["asset_id"].get((gym, str(value)), [])})
            elif source_ref["field"] in ("source_media_url", "source_url") and _text(value):
                lineage = sorted({e["asset_id"] for e in index["source_url"].get((gym, str(value)), [])})
            else:
                lineage = []
            lineage_status = "explicit_reference_match" if lineage else "explicit_reference_unmatched"
        else:
            lineage = []
            lineage_status = "source_reference_missing"
        if not source_ref and not source_ambiguous:
            unresolved.append("source_reference_missing")
        elif not lineage:
            unresolved.append("source_lineage_unmatched")
        if not candidates:
            unresolved.append("no_delivered_byte_candidate")
        if len(candidates) > 1:
            unresolved.append("ambiguous_delivered_byte_candidates")
        if row_ref and refs[row_ref] > 1:
            duplicate_rows += 1
            unresolved.append("duplicate_row_ref")
        for reason in unresolved:
            reason_counts[reason] += 1

        output.append({
            "record_type": "row", "snapshot_identity": identity,
            "input_index": number, "row_ref": row_ref, "gym_id": gym,
            "date": date, "provider": provider,
            "delivered_reference": delivered_ref, "source_reference": source_ref,
            "delivered_bytes": byte_result,
            "candidate_matches": {"basis": "delivered_md5_vs_asset_content_hash",
                                  "cardinality": len(candidates), "asset_ids": candidates},
            "source_lineage_evidence": {"basis": "explicit_source_reference_only",
                                        "asset_ids": lineage, "status": lineage_status},
            "unresolved_reasons": sorted(set(unresolved)),
        })

    summary = {
        "record_type": "summary", "format": "historical-media-recovery-manifest-v1",
        "snapshot_identity": identity, "input_row_count": len(rows),
        "manifest_row_count": len(output), "malformed_row_count": malformed_rows,
        "duplicate_row_ref_count": duplicate_rows, "input_asset_count": len(assets),
        "malformed_asset_count": malformed_assets, "duplicate_asset_id_count": duplicate_assets,
        "delivered_bytes_read_count": delivered_read_count,
        "unresolved_row_count": sum(bool(r["unresolved_reasons"]) for r in output),
        "unresolved_reason_counts": dict(sorted(reason_counts.items())),
    }
    # Header-first JSONL; per-row order follows the frozen input for traceability.
    return [summary, *output]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="frozen local JSON snapshot")
    parser.add_argument("--media-dir", required=True, type=Path,
                        help="operator-saved delivered bytes named by row reference")
    parser.add_argument("--output", required=True, type=Path, help="JSONL output path")
    args = parser.parse_args(argv)
    try:
        raw = args.input.read_bytes()
        snapshot = json.loads(raw.decode("utf-8"))
        identity = snapshot.get("snapshot_identity") if isinstance(snapshot, dict) else None
        identity = identity if identity is not None else {"input_sha256": hashlib.sha256(raw).hexdigest()}
        records = build_manifest(snapshot, args.media_dir, identity)
        rendered = "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                           for record in records)
        args.output.write_text(rendered, encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"historical media manifest failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
