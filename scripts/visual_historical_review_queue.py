"""Build a read-only, per-tenant review queue from saved visual evidence.

No live reads or writes are performed. Queue categories guide human review and
never clear an asset. pHash/content_hash are intentionally ignored as proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

FORMAT = "visual-historical-review-queue-v1"
PUBLISHED_VALUES = frozenset({"published", "posted", "live"})


def _load(path):
    raw = Path(path).read_bytes()
    return raw, json.loads(raw.decode("utf-8"))


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload["rows"]
    raise ValueError("expected array or object with rows array")


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first(row, names):
    return next((_text(row.get(name)) for name in names if _text(row.get(name))), None)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest_pair(delivered, source):
    """Inventory digest agreement is observation only, not authentication."""
    if not isinstance(delivered, dict) or not isinstance(source, dict):
        return False
    if delivered.get("status") != "observed" or source.get("status") != "observed":
        return False
    return any(_text(delivered.get(key)) and
               _text(delivered.get(key)).lower() == _text(source.get(key)).lower()
               for key in ("sha256", "md5"))


def build_queue(calendar_payload, asset_payload, byte_payload=None,
                input_hashes=None):
    calendar_rows, asset_rows = _rows(calendar_payload), _rows(asset_payload)
    byte_rows = _rows(byte_payload) if byte_payload is not None else []
    by_ref = {}
    for byte_row in byte_rows:
        if not isinstance(byte_row, dict) or not _text(byte_row.get("row_ref")):
            raise ValueError("byte inventory rows require persisted row_ref")
        ref = _text(byte_row["row_ref"])
        if ref in by_ref:
            raise ValueError(f"duplicate byte inventory row_ref {ref!r}")
        by_ref[ref] = byte_row

    assets_by_id, assets_by_url = {}, {}
    for asset in asset_rows:
        if not isinstance(asset, dict):
            continue
        asset_id = _first(asset, ("id", "asset_id"))
        source_url = _first(asset, ("source_media_url",))
        tenant_key = str(asset.get("gym_id", asset.get("gym"))) if asset.get("gym_id", asset.get("gym")) is not None else None
        if asset_id:
            assets_by_id.setdefault((tenant_key, asset_id), []).append(asset)
        if source_url:
            assets_by_url.setdefault((tenant_key, source_url), []).append(asset)

    items = []
    seen_refs = set()
    excluded_unpublished = 0
    for row in calendar_rows:
        if not isinstance(row, dict):
            continue
        # In the real calendar schema status carries publication state;
        # variant_status is lifecycle state (active/candidate/archived), not
        # publication evidence. Only status may admit a row to this queue.
        status = _text(row.get("status"))
        published = bool(status and status.casefold() in PUBLISHED_VALUES)
        if not published:
            excluded_unpublished += 1
            continue
        row_ref = _text(row.get("id")) or _text(row.get("row_ref"))
        if not row_ref or row_ref in seen_refs:
            raise ValueError("calendar rows require unique persisted id/row_ref")
        seen_refs.add(row_ref)
        tenant = row.get("gym_id", row.get("gym"))
        asset_id = _first(row, ("source_media_asset_id", "asset_id"))
        source_url = _first(row, ("source_media_url",))
        matched, match_type = None, None
        tenant_key = str(tenant) if tenant is not None else None
        if asset_id:
            candidates = assets_by_id.get((tenant_key, asset_id), [])
            if len(candidates) == 1:
                matched, match_type = candidates[0], "asset_id"
            elif len(candidates) > 1:
                match_type = "ambiguous_asset_id"
            elif any(key[1] == asset_id for key in assets_by_id):
                match_type = "cross_tenant_asset_id"
        if matched is None and match_type is None and source_url:
            candidates = assets_by_url.get((tenant_key, source_url), [])
            if len(candidates) == 1:
                matched, match_type = candidates[0], "source_url"
            elif len(candidates) > 1:
                match_type = "ambiguous_source_url"
            elif any(key[1] == source_url for key in assets_by_url):
                match_type = "cross_tenant_source_url"

        asset_tenant = matched.get("gym_id", matched.get("gym")) if matched else None
        same_tenant = (tenant is not None and asset_tenant is not None and
                       str(tenant) == str(asset_tenant))
        byte = by_ref.get(row_ref)
        delivered = byte.get("delivered") if isinstance(byte, dict) else None
        source = byte.get("source_observation") if isinstance(byte, dict) else None
        observed_match = _digest_pair(delivered, source)
        # The inventory format has no authenticated lineage assertion. Support
        # only explicit signed/verified evidence fields from future manifests.
        authenticated = bool(isinstance(source, dict) and source.get("authenticated") is True
                             and source.get("lineage_verified") is True
                             and observed_match)
        if not matched:
            status, reason = "hold", (
                "tenant_mismatch" if match_type and match_type.startswith("cross_tenant")
                else "ambiguous_match" if match_type else "asset_unresolved")
        elif not same_tenant:
            status, reason = "hold", "tenant_missing_or_mismatch"
        elif match_type == "asset_id" and observed_match and authenticated:
            status, reason = "high_confidence", "same_tenant_asset_id_with_authenticated_byte_lineage"
        elif match_type == "source_url":
            status, reason = "review_required", "unique_exact_source_url_match_without_authenticated_lineage"
        elif match_type == "asset_id":
            status, reason = "hold", "asset_id_match_lacks_authenticated_byte_lineage"
        else:
            status, reason = "hold", "unresolved_or_ambiguous_identity"

        items.append({
            "row_ref": row_ref,
            "gym_id": tenant,
            "post_date": _first(row, ("post_date", "date")),
            "asset_id": _first(matched, ("id", "asset_id")) if matched else asset_id,
            "asset_match": match_type,
            "queue_status": status,
            "reason": reason,
            "evidence": {
                "source_url": source_url,
                "byte_observation_present": byte is not None,
                "delivered_source_bytes_match": observed_match,
                "authenticated_lineage": authenticated,
            },
        })
    items.sort(key=lambda item: (str(item["gym_id"] or ""), item["row_ref"]))
    counts = {key: sum(item["queue_status"] == key for item in items)
              for key in ("high_confidence", "review_required", "hold")}
    return {
        "format": FORMAT,
        "inputs": input_hashes or {},
        "summary": {"rows": len(items), "excluded_unpublished": excluded_unpublished,
                    **counts, "auto_cleared": 0},
        "items": items,
        "policy": [
            "Queue categories are review guidance only; nothing is cleared.",
            "pHash and content_hash are never original-use proof.",
            "The saved byte inventory does not authenticate lineage; absent explicit authenticated lineage, direct ID matches remain held.",
            "Only rows whose status is explicitly published, posted, or live are queued; variant_status is lifecycle metadata and is not publication evidence.",
            "The asset snapshot omits source_media_url. Any exact source-URL match requires supplemental saved asset evidence; it cannot be derived from the standard asset snapshot alone.",
            "Snapshot hashes and timestamps identify the input artifacts; scans remain non-atomic observations.",
        ],
    }


def build_from_files(calendar_path, asset_path, byte_path=None):
    c_raw, c_payload = _load(calendar_path)
    a_raw, a_payload = _load(asset_path)
    b_raw, b_payload = _load(byte_path) if byte_path else (None, None)
    inputs = {
        "calendar_snapshot": {"path": str(calendar_path), "sha256": _sha(c_raw),
                              "snapshot_at": c_payload.get("snapshot_at") if isinstance(c_payload, dict) else None},
        "asset_snapshot": {"path": str(asset_path), "sha256": _sha(a_raw),
                           "snapshot_at": a_payload.get("snapshot_at") if isinstance(a_payload, dict) else None},
        "byte_inventory": ({"path": str(byte_path), "sha256": _sha(b_raw)} if byte_path else None),
    }
    return build_queue(c_payload, a_payload, b_payload, inputs)


def write_queue(path, result):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        os.chmod(target, 0o600)
    except BaseException:
        try: os.unlink(temp)
        except FileNotFoundError: pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calendar_snapshot", type=Path)
    parser.add_argument("asset_snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--byte-inventory", type=Path)
    args = parser.parse_args(argv)
    result = build_from_files(args.calendar_snapshot, args.asset_snapshot, args.byte_inventory)
    write_queue(args.output, result)
    print(f"wrote {result['summary']['rows']} review queue items to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
