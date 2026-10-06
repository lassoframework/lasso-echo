"""Offline source candidate planning, never attestation or historical clearance.

Accept saved classifier/calendar/catalog and optional normalized delivery files.
Raw SHA256 prefix and exact source references are candidates only. Delivery
receipts are context, never a source identity. No network or database access.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


HEX64 = re.compile(r"[0-9a-f]{64}")
HEX32 = re.compile(r"[0-9a-f]{32}")
FEED = re.compile(r"([0-9a-f]{12})__feed\.jpg")
SRC = re.compile(r"(?:.*_)?src_([0-9a-f]{16})\.[A-Za-z0-9]+(?:_gbp\.[A-Za-z0-9]+)?")


def text(value):
    return value if isinstance(value, str) and value and value == value.strip() else None


def reference(row):
    values = {row[k] for k in ("id", "row_id", "row_ref") if text(row.get(k))}
    if len(values) != 1:
        raise ValueError("missing or conflicting row reference")
    return next(iter(values))


def index(rows, label, asset=False):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{label}: malformed row")
        key = text(row.get("id")) if asset else reference(row)
        if not key or key in result:
            raise ValueError(f"{label}: blank or duplicate reference")
        if not text(row.get("gym_id")):
            raise ValueError(f"{label}: missing gym")
        result[key] = row
    return result


def bound_sha256(asset):
    """Validate projected source hash binding, not safety, use or freshness.

    The minimal catalog deliberately excludes people metadata. This is not the
    selector's full moderation authorization check and cannot authorize reuse.
    """
    evidence = asset.get("moderation_json")
    if not isinstance(evidence, dict):
        return None
    digest = evidence.get("sha256")
    content = asset.get("content_hash")
    try:
        observed = datetime.fromisoformat(str(evidence.get("observed_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if (asset.get("kind") != "photo" or asset.get("review_status") != "approved"
            or asset.get("moderation_status") != "clean"
            or not isinstance(content, str) or not HEX32.fullmatch(content)
            or asset.get("review_content_hash") != content
            or evidence.get("content_hash") != content
            or evidence.get("asset_id") != asset.get("id")
            or evidence.get("gym_id") != asset.get("gym_id")
            or asset.get("source_gym_id") != asset.get("gym_id")
            or evidence.get("verdict") != "clean" or not text(evidence.get("provider"))
            or observed.tzinfo is None
            or not isinstance(digest, str) or not HEX64.fullmatch(digest)):
        return None
    return digest


def basename(url):
    if not text(url):
        return ""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
            return ""
        return unquote(parts.path.rsplit("/", 1)[-1])
    except ValueError:
        return ""


def build_plan(classifier, catalog, calendar=None, captures=None):
    classified = index(classifier, "classifier")
    assets = index(catalog, "catalog", asset=True)
    calendars = index(calendar, "calendar") if calendar is not None else classified
    if set(classified) != set(calendars):
        raise ValueError("classifier/calendar reference coverage differs")
    deliveries = index(captures, "captures") if captures else {}
    if set(deliveries) - set(classified):
        raise ValueError("capture reference absent from classifier")
    hashes = defaultdict(list)
    for asset in assets.values():
        digest = bound_sha256(asset)
        if digest:
            hashes[digest[:12]].append((digest, asset))
    url_donors = defaultdict(list)
    for rid, row in calendars.items():
        source = text(row.get("source_media_url"))
        if source:
            url_donors[hashlib.sha1(source.encode()).hexdigest()[:16]].append((rid, row))
    records = []
    for rid in sorted(classified):
        row, cl = calendars[rid], classified[rid]
        for field in ("gym_id", "post_date", "late_post_id", "source_media_asset_id", "source_media_url"):
            left, right = cl.get(field), row.get(field)
            same_blank = left in (None, "") and right in (None, "")
            if field in cl and left != right and not same_blank:
                raise ValueError(f"classifier/calendar disagree: {rid} {field}")
        gym, url = row["gym_id"], row.get("image_url")
        cap = deliveries.get(rid)
        if cap:
            for field in ("gym_id", "post_date", "late_post_id"):
                left, right = cap.get(field), row.get(field)
                if left != right and not (left in (None, "") and right in (None, "")):
                    raise ValueError(f"capture/calendar disagree: {rid} {field}")
        candidates, reasons = [], []
        explicit = text(row.get("source_media_asset_id"))
        if explicit:
            a = assets.get(explicit)
            if not a:
                reasons.append("explicit_asset_absent_from_catalog")
            elif a["gym_id"] != gym or ("source_gym_id" in a and a["source_gym_id"] != gym):
                reasons.append("cross_gym_explicit_asset_rejected")
            else:
                candidates.append({"basis": "explicit_calendar_asset_id", "asset_id": explicit,
                                   "calendar_row_ref": rid, "content_hash": a.get("content_hash"),
                                   "source_sha256": bound_sha256(a)})
        feed = FEED.fullmatch(basename(url))
        if feed:
            matches = hashes.get(feed[1], [])
            digests = {digest for digest, _ in matches}
            same_gym = [(digest, a) for digest, a in matches if a["gym_id"] == gym]
            if len(digests) > 1 or len(same_gym) > 1:
                reasons.append("ambiguous_source_prefix_rejected")
            elif not matches:
                reasons.append("raw_sha256_prefix_not_in_catalog")
            elif not same_gym:
                reasons.append("cross_gym_source_prefix_rejected")
            else:
                digest, a = same_gym[0]
                candidates.append({"basis": "raw_sha256_prefix_render_name", "asset_id": a["id"],
                                   "prefix": feed[1], "source_sha256": digest,
                                   "content_hash": a["content_hash"],
                                   "moderation_observed_at": a["moderation_json"]["observed_at"],
                                   "delivered_exact_url": url})
        src = SRC.fullmatch(basename(url))
        if src:
            donors = url_donors.get(src[1], [])
            urls = {d["source_media_url"] for _, d in donors}
            valid = []
            for donor_id, donor in donors:
                aid = text(donor.get("source_media_asset_id"))
                asset = assets.get(aid)
                if (donor["gym_id"] == gym and asset and asset["gym_id"] == gym
                        and bound_sha256(asset) and donor["source_media_url"] != donor.get("image_url")):
                    valid.append((donor_id, donor, asset))
            identities = {(d["source_media_url"], a["id"]) for _, d, a in valid}
            if len(urls) > 1 or len(identities) > 1:
                reasons.append("ambiguous_source_url_hash_rejected")
            elif not valid:
                reasons.append("url_hash_without_bound_same_gym_source_rejected")
            else:
                donor_id, donor, asset = sorted(valid, key=lambda x: x[0])[0]
                candidates.append({"basis": "source_url_sha1_prefix", "asset_id": asset["id"],
                                   "source_exact_url": donor["source_media_url"],
                                   "source_sha256": bound_sha256(asset), "prefix": src[1],
                                   "donor_row_refs": sorted(d for d, _, _ in valid)})
        if not explicit and not feed and not src:
            reasons.append("no_supported_source_identity_hint")
        identities = {c["asset_id"] for c in candidates}
        if len(identities) > 1:
            candidates = []
            reasons.append("conflicting_candidate_assets_rejected")
        if any(reason.endswith("_rejected") for reason in reasons):
            candidates = []
        records.append({"record_type": "source_candidate", "row_id": rid, "gym_id": gym,
                        "post_date": row.get("post_date"), "late_post_id": row.get("late_post_id"),
                        "delivered_exact_url": url, "candidates": candidates,
                        "capture_context": {"delivery_status": cap.get("delivery_status"),
                                            "raw_receipts": cap.get("raw_receipts", [])} if cap else None,
                        "reasons": sorted(set(reasons)), "source_lineage_verified": False,
                        "historical_use_attested": False, "clearance": False})
    summary = {"record_type": "source_candidate_summary", "schema_version": 1,
               "row_count": len(records), "catalog_count": len(assets),
               "bound_raw_sha256_assets": sum(len(v) for v in hashes.values()),
               "rows_with_candidates": sum(bool(r["candidates"]) for r in records),
               "candidate_basis_counts": dict(sorted(Counter(c["basis"] for r in records for c in r["candidates"]).items())),
               "reason_counts": dict(sorted(Counter(s for r in records for s in r["reasons"]).items())),
               "policy": "Candidates only. No attestation, historical clearance, or production mutation."}
    return [summary, *records]


def load(path, kinds=None):
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    rows, evidence = [], []
    for file in files:
        raw = file.read_bytes()
        payload = [json.loads(line) for line in raw.decode().splitlines() if line.strip()] if file.suffix == ".jsonl" else json.loads(raw)
        if isinstance(payload, dict):
            if "expected_count" in payload and (not isinstance(payload.get("rows"), list)
                    or payload["expected_count"] != len(payload["rows"])):
                raise ValueError(f"{file}: catalog snapshot count mismatch")
            payload = payload.get("rows", payload.get("asset_before_images"))
        if not isinstance(payload, list):
            raise ValueError(f"{file}: expected rows array")
        rows.extend(r for r in payload if not kinds or isinstance(r, dict) and r.get("record_type") in kinds)
        evidence.append({"path": str(file.resolve()), "sha256": hashlib.sha256(raw).hexdigest()})
    return rows, evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classifier", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, help="Saved calendar JSON or page directory; required for render-name hints")
    parser.add_argument("--captures", type=Path, help="Saved normalized delivery receipts, never raw source proof")
    args = parser.parse_args(argv)
    try:
        classifier, ce = load(args.classifier, {"row"})
        catalog, ae = load(args.catalog)
        calendar, ke = load(args.calendar) if args.calendar else (None, [])
        captures, pe = load(args.captures, {"delivery_receipt"}) if args.captures else (None, [])
        result = build_plan(classifier, catalog, calendar, captures)
        result[0]["input_files"] = ce + ae + ke + pe
    except (ValueError, OSError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for record in result:
        print(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
