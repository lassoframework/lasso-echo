#!/usr/bin/env python3
"""Deterministic read-only legacy photo-obligation manifest builder.

Consumes the saved evidence pages under ``evidence/media-20261004/`` (or an
equivalent directory with a ``MANIFEST_INDEX.json``), validates every page's
SHA-256, kind/offset, row counts, unique IDs and project/snapshot metadata,
and emits a per-asset / per-published-row obligation ledger as deterministic
JSON with an atomic 0600 local write.

This tool is a bounded, local-only evidence collector. It never reads the
live database, never downloads media, and never grants clearance. Missing
provider confirmations, deleted/swapped history and original-to-delivered
byte proof keep every obligation unresolved; no counter, URL similarity or
absent join is treated as proof of non-use or uniqueness.

The underlying pages were fetched in separate transactions, so the output is
an internally checked inventory of a NON-ATOMIC snapshot, not a clearance
receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

ASSET_REQUIRED_FIELDS = (
    "id", "gym_id", "source_gym_id", "source_id", "source_kind", "kind",
    "review_status", "content_hash", "review_content_hash", "source_active",
    "eligible", "used_count", "sync_status", "rendition_key", "rendition_url",
)
PUBLISHED_REQUIRED_FIELDS = (
    "id", "gym_id", "account", "status", "image_url",
    "source_media_asset_id", "source_media_url", "published_at",
    "variant_status", "publish_claim_token",
)
EXPECTED_PAGE_KIND = {
    "assets": "approved_photo_inventory",
    "published": "published_calendar_inventory_nontransactional",
}

# Reasons that can only be resolved by evidence we do not have. These are
# attached to every obligation; nothing in the saved pages can remove them.
UNRESOLVED_HISTORICAL_REASONS = (
    "missing_provider_confirmation",
    "missing_deleted_or_swapped_history",
    "missing_original_to_delivered_byte_proof",
)

DISCLAIMER = (
    "Internally checked inventory of a non-atomic paginated snapshot. "
    "Pages were fetched in separate transactions; intervening row mutation "
    "is possible. This manifest is NOT a clearance receipt: zero assets are "
    "cleared for legacy reuse from this evidence."
)


class ManifestError(Exception):
    """Raised for any validation failure in the input evidence."""


def _fail(msg):
    raise ManifestError(msg)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _require(cond, msg):
    if not cond:
        _fail(msg)


def load_and_validate_pages(manifest_path):
    """Load MANIFEST_INDEX.json plus every referenced page, fully validated.

    Returns (manifest, groups) where groups maps name -> list of row dicts
    in page order. Raises ManifestError on any inconsistency.
    """
    manifest_path = os.path.abspath(manifest_path)
    root = os.path.dirname(manifest_path)
    manifest = _load_json(manifest_path)
    _require(isinstance(manifest, dict), "manifest index is not an object")
    project_id = manifest.get("project_id")
    _require(isinstance(project_id, str) and project_id,
             "manifest index missing project_id")
    groups_meta = manifest.get("groups")
    _require(isinstance(groups_meta, dict) and groups_meta,
             "manifest index missing groups")
    _require(set(groups_meta) == set(EXPECTED_PAGE_KIND),
             "manifest index must contain exactly the groups "
             f"{sorted(EXPECTED_PAGE_KIND)}; got {sorted(groups_meta)}")

    groups = {}
    for name in sorted(groups_meta):
        _require(name in EXPECTED_PAGE_KIND, f"unknown group {name!r}")
        gmeta = groups_meta[name]
        files = gmeta.get("files")
        _require(isinstance(files, list) and files,
                 f"group {name}: missing files list")
        rows = []
        expected_offset = 0
        seen_ids = set()
        for finfo in files:
            rel = finfo.get("path")
            _require(isinstance(rel, str) and rel.startswith(f"{name}/"),
                     f"group {name}: bad page path {rel!r}")
            _require(os.path.normpath(rel).split(os.sep)[0] == name
                     and ".." not in rel.split("/"),
                     f"group {name}: unsafe page path {rel!r}")
            fpath = os.path.join(root, rel)
            _require(os.path.isfile(fpath),
                     f"group {name}: missing page file {rel}")
            digest = _sha256_file(fpath)
            _require(digest == finfo.get("sha256"),
                     f"group {name}: sha256 mismatch for {rel}")
            page = _load_json(fpath)
            _require(isinstance(page, dict),
                     f"group {name}: page {rel} is not an object")
            _require(page.get("kind") == EXPECTED_PAGE_KIND[name],
                     f"group {name}: page {rel} kind mismatch")
            _require(page.get("offset") == expected_offset,
                     f"group {name}: page {rel} offset "
                     f"{page.get('offset')!r} != expected {expected_offset}")
            _require(page.get("project_id") == project_id,
                     f"group {name}: page {rel} project_id mismatch")
            page_rows = page.get("rows")
            _require(isinstance(page_rows, list),
                     f"group {name}: page {rel} rows not a list")
            _require(len(page_rows) == finfo.get("rows"),
                     f"group {name}: page {rel} row count "
                     f"{len(page_rows)} != manifest {finfo.get('rows')}")
            _require(page.get("snapshot_utc") == finfo.get("snapshot_utc"),
                     f"group {name}: page {rel} snapshot_utc mismatch")
            required = (ASSET_REQUIRED_FIELDS if name == "assets"
                        else PUBLISHED_REQUIRED_FIELDS)
            for row in page_rows:
                _require(isinstance(row, dict),
                         f"group {name}: non-object row in {rel}")
                for field in required:
                    _require(field in row,
                             f"group {name}: row missing {field!r} in {rel}")
                rid = row["id"]
                _require(isinstance(rid, str) and rid,
                         f"group {name}: blank row id in {rel}")
                _require(rid not in seen_ids,
                         f"group {name}: duplicate id {rid!r}")
                seen_ids.add(rid)
                _require(isinstance(row.get("gym_id"), str)
                         and row["gym_id"],
                         f"group {name}: blank gym_id for {rid!r}")
                if name == "assets":
                    _require(row.get("kind") == "photo",
                             f"group {name}: non-photo kind "
                             f"{row.get('kind')!r} for {rid!r}")
                    _require(row.get("review_status") == "approved",
                             f"group {name}: non-approved review_status "
                             f"{row.get('review_status')!r} for {rid!r}")
                    for fld in ("source_gym_id", "source_id"):
                        _require(isinstance(row.get(fld), str) and row[fld],
                                 f"group {name}: blank {fld} for {rid!r}")
            rows.extend(page_rows)
            expected_offset += len(page_rows)
        _require(len(rows) == gmeta.get("rows"),
                 f"group {name}: total rows {len(rows)} != manifest "
                 f"{gmeta.get('rows')}")
        _require(len(seen_ids) == gmeta.get("unique_ids"),
                 f"group {name}: unique ids {len(seen_ids)} != manifest "
                 f"{gmeta.get('unique_ids')}")
        groups[name] = rows
    return manifest, groups


def _input_checksum(manifest):
    """Deterministic consistency checksum over the declared per-page hashes.

    Consistency check only: the manifest index is a mutable local file that
    is not independently anchored or signed, so this checksum does NOT make
    the evidence tamper-proof. An editor who changes both a page and the
    index would pass it. It only detects accidental drift between the index
    and the page files as saved."""
    h = hashlib.sha256()
    for name in sorted(manifest["groups"]):
        for finfo in manifest["groups"][name]["files"]:
            h.update(finfo["sha256"].encode("ascii"))
    return h.hexdigest()


def build_obligations(manifest, groups):
    assets = groups.get("assets", [])
    published = groups.get("published", [])

    approved_ids = {a["id"] for a in assets}
    rows_by_asset = {}
    for row in published:
        aid = row.get("source_media_asset_id")
        if aid:
            rows_by_asset.setdefault(aid, []).append(row["id"])

    asset_obligations = []
    for a in sorted(assets, key=lambda r: r["id"]):
        reasons = list(UNRESOLVED_HISTORICAL_REASONS)
        mismatch = a["gym_id"] != a["source_gym_id"]
        if mismatch:
            reasons.append("tenant_source_mismatch")
        linked = sorted(rows_by_asset.get(a["id"], []))
        asset_obligations.append({
            "asset_id": a["id"],
            "tenant_gym_id": a["gym_id"],
            "source_gym_id": a["source_gym_id"],
            "source_id": a["source_id"],
            "source_kind": a["source_kind"],
            "review_status": a["review_status"],
            "eligible": bool(a["eligible"]),
            "used_count": a["used_count"],
            "has_rendition": bool(a.get("rendition_key")
                                  or a.get("rendition_url")),
            "tenant_source_mismatch": mismatch,
            "linked_published_row_ids": linked,
            "cleared": False,
            "unresolved_reasons": reasons,
            "evidence_refs": [
                f"assets:{a['id']}",
                *(f"published:{rid}" for rid in linked),
            ],
        })

    assets_by_id = {a["id"]: a for a in assets}
    published_obligations = []
    cross_tenant_linked = 0
    for row in sorted(published, key=lambda r: r["id"]):
        aid = row.get("source_media_asset_id")
        aurl = row.get("source_media_url")
        reasons = list(UNRESOLVED_HISTORICAL_REASONS)
        if not aid and not aurl:
            reasons.append("missing_source_reference")
        if aid and aid not in approved_ids:
            reasons.append("source_asset_id_absent_from_approved_snapshot")
        linked = assets_by_id.get(aid) if aid else None
        linked_asset_gym = linked["gym_id"] if linked else None
        linked_source_gym = linked["source_gym_id"] if linked else None
        cross_tenant = bool(linked) and (
            row["gym_id"] != linked_asset_gym
            or row["gym_id"] != linked_source_gym)
        if cross_tenant:
            reasons.append("cross_tenant_linked_source")
            cross_tenant_linked += 1
        published_obligations.append({
            "published_row_id": row["id"],
            "tenant_gym_id": row["gym_id"],
            "account": row["account"],
            "status": row["status"],
            "source_media_asset_id": aid,
            "has_source_media_url": bool(aurl),
            "source_asset_in_approved_snapshot": bool(aid)
            and aid in approved_ids,
            "linked_asset_gym_id": linked_asset_gym,
            "linked_asset_source_gym_id": linked_source_gym,
            "cross_tenant_linked_source": cross_tenant,
            "cleared": False,
            "unresolved_reasons": reasons,
            "evidence_refs": ([f"published:{row['id']}"]
                              + ([f"assets:{aid}"] if aid else [])),
        })

    distinct_source_ids = {r.get("source_media_asset_id") for r in published}
    distinct_source_ids.discard(None)
    present_ids = distinct_source_ids & approved_ids
    summary = {
        "approved_photo_assets": len(assets),
        "published_rows": len(published),
        "approved_photos_tenant_source_mismatch": sum(
            1 for a in assets if a["gym_id"] != a["source_gym_id"]),
        "published_rows_with_source_asset_id": sum(
            1 for r in published if r.get("source_media_asset_id")),
        "published_rows_with_source_url": sum(
            1 for r in published if r.get("source_media_url")),
        "published_rows_with_neither_source_reference": sum(
            1 for r in published
            if not r.get("source_media_asset_id")
            and not r.get("source_media_url")),
        "distinct_source_asset_ids_in_published": len(distinct_source_ids),
        "source_asset_ids_present_in_approved_snapshot": len(present_ids),
        "published_rows_cross_tenant_linked_source": cross_tenant_linked,
        "cleared_assets": 0,
        "cleared_published_rows": 0,
    }
    return asset_obligations, published_obligations, summary


def build_manifest(manifest_path, optional_snapshots=None):
    manifest, groups = load_and_validate_pages(manifest_path)
    assets_ob, pub_ob, summary = build_obligations(manifest, groups)

    coverage = {
        "groups": {
            name: {
                "pages": len(manifest["groups"][name]["files"]),
                "rows": manifest["groups"][name]["rows"],
                "unique_ids": manifest["groups"][name]["unique_ids"],
            }
            for name in sorted(manifest["groups"])
        },
        "optional_snapshots": {},
    }
    for label, path in sorted((optional_snapshots or {}).items()):
        if path is None:
            continue
        _require(os.path.isfile(path),
                 f"optional snapshot {label}: missing file {path}")
        payload = _load_json(path)  # must at least be valid JSON
        _require(isinstance(payload, (dict, list)),
                 f"optional snapshot {label}: not JSON object/array")
        coverage["optional_snapshots"][label] = {
            "path": os.path.basename(path),
            "sha256": _sha256_file(path),
            "note": "recorded as supplied evidence only; does not clear any "
                    "obligation",
        }

    return {
        "schema": "visual_legacy_obligation_manifest/1",
        "disclaimer": DISCLAIMER,
        "project_id": manifest["project_id"],
        "snapshot_assembled_utc": manifest.get("assembled_utc"),
        "snapshot_scope": manifest.get("snapshot_scope"),
        "input_evidence_checksum": _input_checksum(manifest),
        "input_evidence_checksum_note": (
            "Consistency check only over a mutable, locally generated "
            "manifest index that is not independently anchored or signed; "
            "NOT tamper-proof and NOT a clearance anchor."),
        "coverage": coverage,
        "summary": summary,
        "asset_obligations": assets_ob,
        "published_row_obligations": pub_ob,
    }


def write_atomic_0600(payload, out_path):
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".obligations-", dir=out_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, out_path)
        os.chmod(out_path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True,
                        help="path to MANIFEST_INDEX.json")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--ledger-snapshot", default=None,
                        help="optional saved usage-ledger JSON snapshot")
    parser.add_argument("--provider-snapshot", default=None,
                        help="optional saved provider-posts JSON snapshot")
    parser.add_argument("--deleted-history-snapshot", default=None,
                        help="optional saved deleted/swapped history JSON")
    args = parser.parse_args(argv)

    optional = {
        "usage_ledger": args.ledger_snapshot,
        "provider_posts": args.provider_snapshot,
        "deleted_history": args.deleted_history_snapshot,
    }
    try:
        payload = build_manifest(args.manifest, optional)
        write_atomic_0600(payload, args.out)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    s = payload["summary"]
    print(f"assets={s['approved_photo_assets']} "
          f"published={s['published_rows']} "
          f"mismatches={s['approved_photos_tenant_source_mismatch']} "
          f"no_source_ref={s['published_rows_with_neither_source_reference']} "
          f"cleared=0 -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
