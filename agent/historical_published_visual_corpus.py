"""Read-only visual census of an explicitly complete published-row snapshot.

This module emits private, hash-only evidence for later human visual review.
It cannot establish media clearance: unreadable rows stay unknown, and absence
from a corpus must never be interpreted as unused. No database or provider
write path exists here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import warnings
from datetime import datetime
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

SCHEMA_VERSION = 1
MAX_ROWS = 100_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
READ_TIMEOUT = (5, 15)
_REVISION = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")


class CorpusError(ValueError):
    """Snapshot, allowlist, or resume evidence is not safe to use."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _private_file(path):
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise CorpusError("resume_manifest_permissions_not_private")


def load_snapshot(snapshot, expected_row_refs=None):
    """Validate complete row inventory and optional exact revision allowlist.

    Snapshot row fields: row_id, revision, gym_id, published_at, image_url.
    The snapshot itself must assert complete=true and an exact row_count.
    """
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise CorpusError("snapshot_schema_invalid")
    rows = snapshot.get("rows")
    if snapshot.get("complete") is not True or type(snapshot.get("row_count")) is not int:
        raise CorpusError("snapshot_not_explicitly_complete")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS or snapshot["row_count"] != len(rows):
        raise CorpusError("snapshot_row_count_mismatch")
    refs, normalized = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise CorpusError("snapshot_row_invalid")
        rid, revision = row.get("row_id"), row.get("revision")
        if not isinstance(rid, str) or not rid or len(rid) > 256 or not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise CorpusError("snapshot_row_identity_invalid")
        ref = (rid, revision)
        if ref in refs:
            raise CorpusError("snapshot_duplicate_row_revision")
        refs.add(ref)
        if not isinstance(row.get("gym_id"), str) or not row["gym_id"]:
            raise CorpusError("snapshot_gym_invalid")
        raw_published = row.get("published_at")
        if raw_published is None:
            published_value = None
        else:
            try:
                published = datetime.fromisoformat(str(raw_published).replace("Z", "+00:00"))
                if published.tzinfo is None:
                    raise ValueError
                published_value = published.isoformat()
            except (TypeError, ValueError):
                raise CorpusError("snapshot_published_at_invalid") from None
        normalized.append({"row_id": rid, "revision": revision, "gym_id": row["gym_id"],
                           "published_at": published_value, "image_url": row.get("image_url")})
    if expected_row_refs is not None:
        expected = {(str(x[0]), str(x[1])) for x in expected_row_refs}
        if len(expected) != len(expected_row_refs) or expected != refs:
            raise CorpusError("snapshot_allowlist_mismatch")
    normalized.sort(key=lambda r: (r["row_id"], r["revision"]))
    digest = _sha(_canonical(normalized))
    return normalized, digest


def _valid_url(url, allowed_hosts):
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname is not None and p.hostname.lower() in allowed_hosts
                and p.username is None and p.password is None and p.port in (None, 443)
                and not p.fragment)
    except ValueError:
        return False


def _read_url(url, allowed_hosts, session):
    if not _valid_url(url, allowed_hosts):
        raise ValueError("hosted_url_not_allowlisted")
    response = session.get(url, timeout=READ_TIMEOUT, stream=True, allow_redirects=False)
    try:
        if response.status_code != 200:
            raise ValueError("hosted_image_unavailable")
        if response.headers.get("Content-Length"):
            try:
                if int(response.headers["Content-Length"]) > MAX_IMAGE_BYTES:
                    raise ValueError("hosted_image_exceeds_bound")
            except ValueError:
                raise ValueError("hosted_image_length_invalid") from None
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_IMAGE_BYTES:
                raise ValueError("hosted_image_exceeds_bound")
        if not body:
            raise ValueError("hosted_image_empty")
        return bytes(body)
    finally:
        response.close()


def _visual_fingerprint(data):
    """Return format, dimensions, and 64-bit difference hash; decode failure is unknown."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        image_context = Image.open(__import__("io").BytesIO(data))
    with image_context as image:
        if image.format not in {"JPEG", "PNG", "WEBP", "GIF", "TIFF", "BMP"}:
            raise ValueError("unsupported_image_format")
        image.seek(0)
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("image_exceeds_pixel_bound")
        image = ImageOps.exif_transpose(image)
        image = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(image.get_flattened_data())
        bits = [pixels[y * 9 + x] > pixels[y * 9 + x + 1] for y in range(8) for x in range(8)]
        value = sum((1 << (63 - i)) for i, bit in enumerate(bits) if bit)
        return value.to_bytes(8, "big").hex(), width, height


def _validate_resume_records(result, rows, snapshot_digest):
    """Reject stale, malformed, overbroad, or non-hash-only cached evidence."""
    if (set(result) - {"schema_version", "snapshot_sha256", "row_count", "records",
                       "hashed_count", "unknown_count", "complete"}
            or result.get("snapshot_sha256") != snapshot_digest
            or result.get("row_count") != len(rows)
            or type(result.get("complete", False)) is not bool):
        raise CorpusError("resume_manifest_invalid")
    records = result.get("records")
    if not isinstance(records, dict):
        raise CorpusError("resume_manifest_invalid")
    expected = {}
    for row in rows:
        key = _sha(_canonical([row["row_id"], row["revision"]]))
        expected[key] = {
            "row_ref_sha256": key,
            "revision_sha256": _sha(row["revision"]),
            "gym_sha256": _sha(row["gym_id"]),
            "published_date_sha256": _sha(row["published_at"][:10] if row["published_at"] else "null"),
            "published_date_known": row["published_at"] is not None,
        }
    if not set(records).issubset(expected):
        raise CorpusError("resume_manifest_invalid")
    hashed_count = sum(isinstance(r, dict) and r.get("status") == "hashed" for r in records.values())
    unknown_count = sum(isinstance(r, dict) and r.get("status") == "unknown" for r in records.values())
    if (("hashed_count" in result and (type(result["hashed_count"]) is not int or result["hashed_count"] != hashed_count))
            or ("unknown_count" in result and (type(result["unknown_count"]) is not int or result["unknown_count"] != unknown_count))
            or ("complete" in result and result["complete"] != (len(records) == len(rows)))):
        raise CorpusError("resume_manifest_invalid")
    sha_pattern, dhash_pattern = r"[0-9a-f]{64}", r"[0-9a-f]{16}"
    for key, record in records.items():
        if not isinstance(key, str) or not re.fullmatch(sha_pattern, key) or not isinstance(record, dict):
            raise CorpusError("resume_manifest_invalid")
        status = record.get("status")
        expected_base = expected[key]
        if (any(record.get(field) != value for field, value in expected_base.items())
                or type(record.get("published_date_known")) is not bool):
            raise CorpusError("resume_manifest_invalid")
        if status == "unknown":
            if (set(record) != set(expected_base) | {"status", "unknown_reason"}
                    or not isinstance(record.get("unknown_reason"), str)
                    or not re.fullmatch(r"[a-z0-9_]{1,80}", record["unknown_reason"])):
                raise CorpusError("resume_manifest_invalid")
        elif status == "hashed":
            if (set(record) != set(expected_base) | {"status", "image_sha256", "byte_length", "dhash64", "width", "height"}
                    or not isinstance(record.get("image_sha256"), str)
                    or not re.fullmatch(sha_pattern, record["image_sha256"])
                    or not isinstance(record.get("dhash64"), str)
                    or not re.fullmatch(dhash_pattern, record["dhash64"])
                    or type(record.get("byte_length")) is not int or not 0 < record["byte_length"] <= MAX_IMAGE_BYTES
                    or type(record.get("width")) is not int or type(record.get("height")) is not int
                    or not 0 < record["width"] <= MAX_IMAGE_PIXELS
                    or not 0 < record["height"] <= MAX_IMAGE_PIXELS
                    or record["width"] * record["height"] > MAX_IMAGE_PIXELS):
                raise CorpusError("resume_manifest_invalid")
        else:
            raise CorpusError("resume_manifest_invalid")
    return records


def _record_path(path, record):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".visual-corpus-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_canonical(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def collect(snapshot, *, allowed_hosts, manifest_path, expected_row_refs=None, session=None):
    """Hash every row in a complete snapshot; resume only same exact snapshot.

    Each result contains hashes of row, revision, gym and date, plus content
    hashes and dHash. URL and raw identifiers are never written to the manifest.
    """
    hosts = {str(host).lower().rstrip(".") for host in allowed_hosts}
    if not hosts or any(not h or "/" in h for h in hosts):
        raise CorpusError("host_allowlist_invalid")
    rows, snapshot_digest = load_snapshot(snapshot, expected_row_refs)
    path = os.path.abspath(manifest_path)
    if os.path.exists(path):
        _private_file(path)
        try:
            with open(path, encoding="utf-8") as f:
                result = json.load(f)
        except Exception:
            raise CorpusError("resume_manifest_unreadable") from None
        if result.get("schema_version") != SCHEMA_VERSION or result.get("snapshot_sha256") != snapshot_digest:
            raise CorpusError("resume_snapshot_changed")
        records = _validate_resume_records(result, rows, snapshot_digest)
    else:
        result = {"schema_version": SCHEMA_VERSION, "snapshot_sha256": snapshot_digest,
                  "row_count": len(rows), "records": {}}
        records = result["records"]
    http = session or requests.Session()
    for row in rows:
        key = _sha(_canonical([row["row_id"], row["revision"]]))
        if key in records:
            continue
        entry = {"row_ref_sha256": key, "revision_sha256": _sha(row["revision"]),
                 "gym_sha256": _sha(row["gym_id"]),
                 "published_date_sha256": _sha(row["published_at"][:10] if row["published_at"] else "null"),
                 "published_date_known": row["published_at"] is not None,
                 "status": "unknown"}
        try:
            data = _read_url(row["image_url"], hosts, http)
            dhash, width, height = _visual_fingerprint(data)
            entry.update(status="hashed", image_sha256=_sha(data), byte_length=len(data),
                         dhash64=dhash, width=width, height=height)
        except Exception as exc:
            # Static reason code only; exception text may contain the private URL.
            reason = str(exc) if isinstance(exc, ValueError) else "image_read_or_decode_failed"
            entry["unknown_reason"] = reason if re.fullmatch(r"[a-z0-9_]+", reason) else "image_read_or_decode_failed"
        records[key] = entry
        _record_path(path, result)
    result["hashed_count"] = sum(r["status"] == "hashed" for r in records.values())
    result["unknown_count"] = sum(r["status"] == "unknown" for r in records.values())
    result["complete"] = len(records) == len(rows)
    _record_path(path, result)
    return result


def compare_signals(manifest):
    """Describe exact-byte and near-scene groups; never infer unused status."""
    records = manifest.get("records", {}).values()
    hashed = [r for r in records if r.get("status") == "hashed"]
    byte_groups, scene_groups = {}, []
    for record in hashed:
        byte_groups.setdefault(record["image_sha256"], []).append(record)
    def group_members(group):
        return [{"row_ref_sha256": r["row_ref_sha256"], "gym_sha256": r["gym_sha256"],
                 "published_date_sha256": r["published_date_sha256"]} for r in group]
    for i, left in enumerate(hashed):
        a = int(left["dhash64"], 16)
        for right in hashed[i + 1:]:
            if (a ^ int(right["dhash64"], 16)).bit_count() <= 8:
                scene_groups.append(group_members([left, right]))
    return {"exact_byte_groups": [group_members(v) for v in byte_groups.values() if len(v) > 1],
            "near_scene_pairs_hamming_le_8": scene_groups,
            "candidate_absence_means_unused": False}
