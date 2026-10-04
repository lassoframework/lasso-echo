"""Deterministic offline reconciler for historical visual-media evidence.

Inputs are saved local JSON artifacts only:

* a calendar snapshot (``visual-calendar-snapshot-v1`` from
  ``scripts/visual_calendar_snapshot.py`` or an equivalent rows payload),
* an asset snapshot (``visual-asset-snapshot-v1`` or an equivalent rows
  payload describing known source assets; real-schema
  ``public.media_asset`` rows carry ``rendition_key``/``rendition_url``
  and no ``source_media_url``/``sha256``/``md5`` — ``content_hash`` is a
  Drive MD5 hint, never original-byte proof, and ``rendition_url`` is
  never treated as a source URL),
* optional provider, postlog, and byte-observation manifests.

This command never queries a database, never reads a URL, and never reads
media bytes. Source ID and URL matches are recorded as hints only; they are
never proof of original use.

ZERO HISTORICAL CLEARANCE: every historical-use / asset-clearance obligation
is ALWAYS ``unresolved``. Matching delivered/source hashes
(``byte_identity_observed``) are recorded as evidence only; they do not prove
original use and never clear an asset. The only thing that could ever resolve
an obligation is a future, separately authenticated, immutable original-use
receipt format; no such receipt is integrated today, so no obligation can
resolve.

Fail-closed integrity checks abort the run (exit 2, no output written) on:

* duplicate, blank, missing, or ambiguously disagreeing
  calendar/provider/postlog row refs (two nonblank persisted reference
  fields on one row with different values), including collisions of any
  provided primary/alias ref across manifest rows, and including an
  explicitly present blank primary persisted reference (``id``/``row_ref``)
  even when an alias is nonblank; byte-observation rows must each be an
  object with a nonblank ``row_ref`` (duplicates rejected) whose nested
  ``delivered``/``source_observation`` values, when present, are objects
  or null (absent/null allowed; any other nested non-object rejected),
* duplicate asset IDs or duplicate source URLs (ambiguous URL matching),
* malformed digest values (sha256/md5 that are not full lowercase/uppercase
  hex of the correct length),
* an exact-URL mismatch between a byte observation and the calendar row's
  source or delivered URL,
* disagreement between md5 and sha256 when both algorithms are present on
  both compared records (one matches, the other does not).

Missing or mismatched tenant (gym) fails closed at the obligation level: the
obligation is ``unresolved`` with an explicit tenant reason.

Output is written atomically with owner-only (0600) permissions, with sorted
JSON keys and SHA-256 hashes of the raw input bytes. Replaying the same
inputs produces byte-identical output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

FORMAT = "visual-media-evidence-reconciliation-v1"

# Reason codes, in deterministic precedence order. The first applicable
# reason is the primary reason; every applicable reason is listed.
# ``original_use_unproven`` is ALWAYS present: without a separately
# authenticated immutable original-use receipt (not integrated today), no
# historical-use obligation can ever resolve.
REASON_PRECEDENCE = (
    "malformed_input",
    "unknown_date",
    "tenant_mismatch",
    "missing_tenant",
    "deleted_or_swapped_asset",
    "no_matching_asset",
    "byte_observation_error",
    "no_rendition_identity",
    "provider_obligation_unknown",
    "source_match_hint_only",
    "original_use_unproven",
)

DATE_FIELDS = ("post_date", "date")
ASSET_ID_FIELDS = ("source_media_asset_id", "asset_id")
SOURCE_URL_FIELDS = ("source_media_url",)
# Rendition identity for real-schema asset snapshots: the live
# public.media_asset table has rendition_key/rendition_url, no
# source_media_url and no sha256/md5 columns. rendition_key is a hint
# identity only; it is never proof of original bytes.
RENDITION_IDENTITY_FIELDS = ("rendition_key",)
DELIVERED_URL_FIELDS = ("image_url", "delivered_url")
HASH_FIELDS = ("sha256", "md5")
HASH_LENGTHS = {"sha256": 64, "md5": 32}
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

# Asset lifecycle states that block resolution: the original rendition is
# gone or replaced, so saved evidence cannot prove original use.
BLOCKING_ASSET_STATES = frozenset({"deleted", "swapped", "replaced", "purged"})

PROVIDER_UNKNOWN_STATES = frozenset({"unknown", "missing", "unverified", ""})


class InputError(ValueError):
    """An input artifact is missing, unreadable, malformed, or fails a
    fail-closed integrity check. The run aborts with no output written."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path, label):
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise InputError(f"{label}: unreadable input: {exc}") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise InputError(f"{label}: malformed JSON input") from None
    return raw, payload


def _rows_of(payload, label):
    """Accept a bare rows array or an object containing a ``rows`` array."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        rows = payload["rows"]
    else:
        raise InputError(f"{label}: expected a JSON array or an object with a rows array")
    return rows


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


def _first_text(row, fields):
    for field in fields:
        value = row.get(field)
        if _is_text(value):
            return value
    return None


def _validate_digest(algo, value, where):
    """Fail closed on any malformed digest value."""
    if not _is_text(value):
        raise InputError(f"{where}: malformed {algo} digest value")
    text = value.strip()
    if len(text) != HASH_LENGTHS[algo] or not _HEX_RE.match(text):
        raise InputError(
            f"{where}: malformed {algo} digest value (expected "
            f"{HASH_LENGTHS[algo]} hex characters)")
    return text.lower()


def _hashes_of(record, where):
    """Normalized lowercase hex digests present on a record, by algorithm.

    Any present digest that is not full-length hex aborts the run.
    """
    found = {}
    if not isinstance(record, dict):
        return found
    for field in HASH_FIELDS:
        value = record.get(field)
        if value is None:
            continue
        found[field] = _validate_digest(field, value, where)
    return found


def _hashes_compare(left, right, where):
    """Compare shared digests, failing closed on md5/sha256 disagreement.

    Returns True when at least one shared algorithm has equal digests. When
    both md5 and sha256 are shared and exactly one matches, the records
    disagree; that inconsistency aborts the run.
    """
    shared = set(left) & set(right)
    if not shared:
        return False
    matches = {algo for algo in shared if left[algo] == right[algo]}
    if matches and shared - matches:
        raise InputError(
            f"{where}: md5/sha256 disagreement between compared records "
            f"({sorted(matches)} match, {sorted(shared - matches)} differ)")
    return bool(matches)


def _valid_date(value):
    """A date is known only as a parseable YYYY-MM-DD (optionally with time)."""
    if not _is_text(value):
        return False
    text = value.strip()
    date_part = text[:10]
    if len(date_part) != 10 or date_part[4] != "-" or date_part[7] != "-":
        return False
    try:
        year = int(date_part[0:4])
        month = int(date_part[5:7])
        day = int(date_part[8:10])
    except ValueError:
        return False
    if not (1 <= month <= 12 and 1 <= day <= 31 and year >= 1970):
        return False
    import calendar as _calendar
    return day <= _calendar.monthrange(year, month)[1]


def _index_assets(asset_rows):
    """Fail closed on duplicate asset IDs, source URLs, or rendition keys."""
    by_id = {}
    by_source_url = {}
    by_rendition_key = {}
    for index, row in enumerate(asset_rows, 1):
        if not isinstance(row, dict):
            continue
        where = f"asset_snapshot row {index}"
        asset_id = _first_text(row, ("asset_id", "id"))
        if asset_id:
            if asset_id in by_id:
                raise InputError(
                    f"asset_snapshot: duplicate asset_id {asset_id!r}")
            by_id[asset_id] = row
        source_url = _first_text(row, SOURCE_URL_FIELDS)
        if source_url:
            if source_url in by_source_url:
                raise InputError(
                    "asset_snapshot: duplicate source_media_url "
                    f"{source_url!r} (ambiguous URL matching)")
            by_source_url[source_url] = row
        rendition_key = _first_text(row, RENDITION_IDENTITY_FIELDS)
        if rendition_key:
            if rendition_key in by_rendition_key:
                raise InputError(
                    "asset_snapshot: duplicate rendition_key "
                    f"{rendition_key!r} (ambiguous rendition matching)")
            by_rendition_key[rendition_key] = row
        _hashes_of(row, where)  # validate digest shape even if unused
    return by_id, by_source_url, by_rendition_key


def _index_byte_rows(byte_rows):
    """Fail closed on duplicate byte-observation row refs."""
    by_row_ref = {}
    for index, row in enumerate(byte_rows, 1):
        if not isinstance(row, dict):
            raise InputError(
                f"byte_observation_manifest row {index}: expected an object "
                f"with row_ref")
        ref = row.get("row_ref")
        if not _is_text(ref):
            raise InputError(
                f"byte_observation_manifest row {index}: missing or blank "
                f"row_ref")
        if ref in by_row_ref:
            raise InputError(
                f"byte_observation_manifest: duplicate row_ref {ref!r}")
        by_row_ref[ref] = row
        _hashes_of(_nested_object(row, "delivered",
                                  f"byte_observation_manifest row {index}"),
                   f"byte_observation_manifest row {index} delivered")
        _hashes_of(_nested_object(row, "source_observation",
                                  f"byte_observation_manifest row {index}"),
                   f"byte_observation_manifest row {index} source_observation")
    return by_row_ref


def _persisted_refs(row, fields, where):
    """Return every distinct nonblank persisted ref on the row.

    Fails closed on ambiguity: the primary persisted field (first in
    ``fields``) fails closed when it is explicitly present but blank/None,
    even if a supported alias carries a nonblank value (a persisted blank
    is a real observation, not an absence, and must never be silently
    substituted). When two or more fields carry nonblank values that
    disagree, the row's persisted identity is ambiguous and the run aborts:
    silently preferring one field can misassociate evidence across
    calendar/provider/postlog manifests.
    """
    if not isinstance(row, dict):
        raise InputError(
            f"{where}: expected an object with a persisted "
            f"{'/'.join(fields)} reference")
    primary = fields[0]
    if primary in row and not _is_text(row[primary]):
        raise InputError(
            f"{where}: missing or blank {primary} (explicitly present blank "
            f"primary persisted reference; aliases are not consulted)")
    refs = []
    for field in fields:
        value = row.get(field)
        if _is_text(value):
            text = value.strip()
            if text not in refs:
                refs.append(text)
    if not refs:
        raise InputError(f"{where}: missing or blank {fields[0]}")
    if len(refs) > 1:
        raise InputError(
            f"{where}: ambiguous persisted reference "
            f"({', '.join(f'{field}={row.get(field)!r}' for field in fields if _is_text(row.get(field)))} "
            f"disagree); refusing to guess which persisted ref is authoritative")
    return refs


def _persisted_ref(row, fields, where):
    """Return the single nonblank persisted ref, failing closed on ambiguity."""
    return _persisted_refs(row, fields, where)[0]


def _nested_object(row, key, where):
    """Return a nested manifest object, failing closed on malformed nesting.

    Absent or null is allowed by the documented input shape and yields an
    empty object; any other non-object value (string, number, array, bool)
    is malformed and aborts the run instead of being silently coerced to
    an empty dict or crashing on ``.get``.
    """
    if key not in row or row[key] is None:
        return {}
    value = row[key]
    if not isinstance(value, dict):
        raise InputError(
            f"{where}: {key} must be an object or null, got "
            f"{type(value).__name__}")
    return value


def _index_manifest_refs(rows, label, fields):
    """Index a manifest by persisted ref, failing closed on collisions.

    Any provided persisted ref value (primary or alias) must be unique
    across the whole manifest: two rows whose refs collide after alias
    resolution (for example ``row_ref=a, id=b`` and ``row_ref=b, id=a``)
    make evidence association ambiguous, so the run aborts.
    """
    by_row_ref = {}
    for index, row in enumerate(rows, 1):
        refs = _persisted_refs(row, fields, f"{label} row {index}")
        ref = refs[0]
        for value in refs:
            if value in by_row_ref:
                raise InputError(
                    f"{label}: duplicate row_ref {value!r} "
                    f"(rows {by_row_ref[value]} and {index})")
        by_row_ref[ref] = index
    return {ref: rows[index - 1] for ref, index in by_row_ref.items()}


def _index_provider(provider_rows):
    return _index_manifest_refs(provider_rows, "provider_manifest",
                                ("row_ref", "id"))


def _index_postlog(postlog_rows):
    return _index_manifest_refs(postlog_rows, "postlog_manifest",
                                ("row_ref", "calendar_row_id", "id"))


def _row_ref(row, index):
    if not isinstance(row, dict):
        raise InputError(
            f"calendar_snapshot row {index}: expected an object with a "
            f"persisted id")
    return _persisted_ref(row, ("id", "row_ref"),
                          f"calendar_snapshot row {index}")


def _check_unique_row_refs(calendar_rows):
    """Fail closed on missing, blank, duplicate, colliding, or ambiguous
    calendar refs (any provided primary/alias ref must be unique across rows)."""
    seen = {}
    for index, row in enumerate(calendar_rows, 1):
        for ref in _persisted_refs(row, ("id", "row_ref"),
                                   f"calendar_snapshot row {index}"):
            if ref in seen:
                raise InputError(
                    f"calendar_snapshot: duplicate row ref {ref!r} "
                    f"(rows {seen[ref]} and {index})")
            seen[ref] = index


def _match_asset(row, by_id, by_source_url, by_rendition_key):
    """Return (asset, match_kind). URL/ID/rendition matches are hints, never
    proof of original use."""
    asset_id = _first_text(row, ASSET_ID_FIELDS)
    if asset_id and asset_id in by_id:
        return by_id[asset_id], "asset_id"
    source_url = _first_text(row, SOURCE_URL_FIELDS)
    if source_url and source_url in by_source_url:
        return by_source_url[source_url], "source_url_hint"
    rendition_key = _first_text(row, RENDITION_IDENTITY_FIELDS)
    if rendition_key and rendition_key in by_rendition_key:
        return by_rendition_key[rendition_key], "rendition_key_hint"
    return None, None


def _check_byte_urls(byte_row, row, ref):
    """Fail closed on exact-URL mismatch between a byte observation and the
    calendar row's source/delivered URLs."""
    where = f"byte_observation_manifest row_ref {ref!r}"
    delivered = _nested_object(byte_row, "delivered", where)
    source = _nested_object(byte_row, "source_observation", where)
    row_delivered = _first_text(row, DELIVERED_URL_FIELDS) if isinstance(row, dict) else None
    row_source = _first_text(row, SOURCE_URL_FIELDS) if isinstance(row, dict) else None
    obs_delivered = delivered.get("exact_url")
    obs_source = source.get("exact_url")
    if _is_text(obs_delivered):
        if row_delivered is None or obs_delivered.strip() != row_delivered.strip():
            raise InputError(
                f"byte_observation_manifest row_ref {ref!r}: delivered "
                f"exact_url {obs_delivered!r} does not match the calendar "
                f"row delivered URL {row_delivered!r}")
    if _is_text(obs_source):
        if row_source is None or obs_source.strip() != row_source.strip():
            raise InputError(
                f"byte_observation_manifest row_ref {ref!r}: source "
                f"exact_url {obs_source!r} does not match the calendar "
                f"row source URL {row_source!r}")


def _reconcile_row(row, index, by_id, by_source_url, by_rendition_key,
                   byte_rows, provider_rows, postlog_rows):
    """Build one ALWAYS-unresolved historical-use obligation for a row."""
    ref = _row_ref(row, index)
    reasons = set()
    evidence = {
        "asset_match": None,
        "asset_id": None,
        "byte_identity_observed": False,
        "byte_observation_status": None,
        "provider_obligation": None,
        "postlog_entry": None,
        "original_use_receipt": None,
    }

    if not isinstance(row, dict):
        reasons.add("malformed_input")
        reasons.add("original_use_unproven")
        ordered = [code for code in REASON_PRECEDENCE if code in reasons]
        return {
            "row_ref": ref,
            "gym_id": None,
            "post_date": None,
            "resolution": "unresolved",
            "primary_reason": ordered[0],
            "reasons": ordered,
            "evidence": evidence,
        }

    gym_id = row.get("gym_id", row.get("gym"))
    post_date = _first_text(row, DATE_FIELDS)

    if not _valid_date(post_date if post_date else row.get("post_date")):
        reasons.add("unknown_date")

    asset, match_kind = _match_asset(row, by_id, by_source_url,
                                     by_rendition_key)
    evidence["asset_match"] = match_kind

    if asset is None:
        reasons.add("no_matching_asset")
    else:
        evidence["asset_id"] = _first_text(asset, ("asset_id", "id"))
        asset_gym = asset.get("gym_id", asset.get("gym"))
        if not _is_text(str(asset_gym) if asset_gym is not None else None):
            reasons.add("missing_tenant")
        elif not _is_text(str(gym_id) if gym_id is not None else None):
            reasons.add("missing_tenant")
        elif str(asset_gym) != str(gym_id):
            reasons.add("tenant_mismatch")
        state = str(asset.get("state", asset.get("status", "")) or "").strip().lower()
        if state in BLOCKING_ASSET_STATES or asset.get("deleted") is True or \
                asset.get("swapped") is True:
            reasons.add("deleted_or_swapped_asset")

    # Byte-observation evidence (visual-delivered-byte-inventory-v1 rows).
    # Hash agreement is EVIDENCE ONLY (``byte_identity_observed``); it never
    # resolves the obligation and never proves original use.
    byte_row = byte_rows.get(ref)
    if byte_row is not None:
        _check_byte_urls(byte_row, row, ref)
        byte_where = f"byte_observation_manifest row_ref {ref!r}"
        delivered_obs = _nested_object(byte_row, "delivered", byte_where)
        source_obs = _nested_object(byte_row, "source_observation", byte_where)
        evidence["byte_observation_status"] = delivered_obs.get("status")
        if delivered_obs.get("status") == "error" or \
                (isinstance(source_obs, dict) and source_obs.get("status") == "error"):
            reasons.add("byte_observation_error")
        where = f"byte_observation_manifest row_ref {ref!r}"
        delivered_hashes = _hashes_of(delivered_obs, f"{where} delivered")
        source_hashes = _hashes_of(source_obs, f"{where} source_observation")
        asset_hashes = _hashes_of(asset, f"matched asset for row_ref {ref!r}") \
            if asset else {}
        if delivered_hashes and (
                _hashes_compare(delivered_hashes, source_hashes, where) or
                _hashes_compare(delivered_hashes, asset_hashes, where)):
            evidence["byte_identity_observed"] = True

    if asset is not None and not evidence["byte_identity_observed"]:
        has_rendition_identity = bool(
            _hashes_of(asset, "matched asset") or
            _first_text(asset, RENDITION_IDENTITY_FIELDS) or
            (byte_row and _hashes_of(
                _nested_object(byte_row, "source_observation",
                               f"byte_observation_manifest row_ref {ref!r}"),
                f"byte_observation_manifest row_ref {ref!r}")))
        if match_kind in ("asset_id", "source_url_hint", "rendition_key_hint") \
                and not has_rendition_identity:
            reasons.add("no_rendition_identity")
        else:
            # ID/URL/rendition agreement without byte identity is a hint,
            # not proof of original use.
            reasons.add("source_match_hint_only")

    # The provider manifest may be omitted; a missing or unknown provider
    # obligation is recorded and can never resolve anything.
    provider_row = provider_rows.get(ref) if provider_rows else None
    if provider_rows is not None:
        if provider_row is None:
            reasons.add("provider_obligation_unknown")
            evidence["provider_obligation"] = "missing"
        else:
            obligation = str(provider_row.get("obligation",
                             provider_row.get("status", "")) or "").strip().lower()
            evidence["provider_obligation"] = obligation or "unknown"
            if obligation in PROVIDER_UNKNOWN_STATES:
                reasons.add("provider_obligation_unknown")

    postlog_row = postlog_rows.get(ref) if postlog_rows else None
    if postlog_row is not None:
        evidence["postlog_entry"] = postlog_row.get("status", "present")

    # No separately authenticated immutable original-use receipt format is
    # integrated, so original use is ALWAYS unproven.
    reasons.add("original_use_unproven")
    ordered = [code for code in REASON_PRECEDENCE if code in reasons]
    return {
        "row_ref": ref,
        "gym_id": str(gym_id) if gym_id is not None else None,
        "post_date": post_date,
        "resolution": "unresolved",
        "primary_reason": ordered[0],
        "reasons": ordered,
        "evidence": evidence,
    }


def _reconcile_asset(asset, index, calendar_refs_by_asset_id,
                     calendar_refs_by_source_url):
    """Build one ALWAYS-unresolved clearance obligation for every asset,
    including assets with no calendar row (unknown history stays unresolved).
    """
    where = f"asset_snapshot row {index}"
    asset_id = _first_text(asset, ("asset_id", "id"))
    gym = asset.get("gym_id", asset.get("gym"))
    rendition_key = _first_text(asset, RENDITION_IDENTITY_FIELDS) \
        if isinstance(asset, dict) else None
    reasons = set()
    evidence = {
        "calendar_row_refs_by_asset_id": sorted(
            calendar_refs_by_asset_id.get(asset_id, ())) if asset_id else [],
        "calendar_row_refs_by_source_url": sorted(
            calendar_refs_by_source_url.get(
                _first_text(asset, SOURCE_URL_FIELDS), ()))
        if _first_text(asset, SOURCE_URL_FIELDS) else [],
        # Rendition identity as observed in the asset snapshot (real-schema
        # public.media_asset rows carry rendition_key; legacy rows may carry
        # hashes or a source URL instead). None means no rendition identity
        # was recorded for this asset.
        "rendition_identity": rendition_key,
        "original_use_receipt": None,
    }
    if not isinstance(asset, dict):
        reasons.add("malformed_input")
    else:
        if not _is_text(str(gym) if gym is not None else None):
            reasons.add("missing_tenant")
        state = str(asset.get("state", asset.get("status", "")) or "").strip().lower()
        if state in BLOCKING_ASSET_STATES or asset.get("deleted") is True or \
                asset.get("swapped") is True:
            reasons.add("deleted_or_swapped_asset")
        if not evidence["calendar_row_refs_by_asset_id"] and \
                not evidence["calendar_row_refs_by_source_url"]:
            # No calendar history at all: unknown history remains unresolved.
            reasons.add("no_matching_asset")
        if not _first_text(asset, RENDITION_IDENTITY_FIELDS) and \
                not _first_text(asset, SOURCE_URL_FIELDS) and \
                not _hashes_of(asset, f"asset_snapshot row {index}"):
            # Real-schema rows have no hashes and no source URL; without a
            # rendition_key there is no rendition identity to compare at all.
            reasons.add("no_rendition_identity")
    reasons.add("original_use_unproven")
    ordered = [code for code in REASON_PRECEDENCE if code in reasons]
    return {
        "asset_ref": asset_id or f"asset-row-{index:06d}",
        "gym_id": str(gym) if gym is not None else None,
        "resolution": "unresolved",
        "primary_reason": ordered[0],
        "reasons": ordered,
        "evidence": evidence,
    }


def reconcile(calendar_rows, asset_rows, byte_rows=None, provider_rows=None,
              postlog_rows=None):
    """Return (calendar_obligations, asset_obligations), all unresolved."""
    _check_unique_row_refs(calendar_rows)
    by_id, by_source_url, by_rendition_key = _index_assets(asset_rows)
    byte_index = _index_byte_rows(byte_rows or [])
    provider_index = _index_provider(provider_rows) if provider_rows is not None else None
    postlog_index = _index_postlog(postlog_rows) if postlog_rows is not None else None

    calendar_obligations = [
        _reconcile_row(row, index, by_id, by_source_url, by_rendition_key,
                       byte_index, provider_index, postlog_index)
        for index, row in enumerate(calendar_rows, 1)
    ]
    calendar_obligations.sort(key=lambda item: item["row_ref"])

    # Map calendar refs to assets so per-asset obligations can cite history.
    refs_by_asset_id = {}
    refs_by_source_url = {}
    for index, row in enumerate(calendar_rows, 1):
        if not isinstance(row, dict):
            continue
        ref = _row_ref(row, index)
        asset_id = _first_text(row, ASSET_ID_FIELDS)
        if asset_id:
            refs_by_asset_id.setdefault(asset_id, set()).add(ref)
        source_url = _first_text(row, SOURCE_URL_FIELDS)
        if source_url:
            refs_by_source_url.setdefault(source_url, set()).add(ref)

    asset_obligations = [
        _reconcile_asset(asset, index, refs_by_asset_id, refs_by_source_url)
        if isinstance(asset, dict) else {
            "asset_ref": f"asset-row-{index:06d}",
            "gym_id": None,
            "resolution": "unresolved",
            "primary_reason": "malformed_input",
            "reasons": ["malformed_input", "original_use_unproven"],
            "evidence": {"calendar_row_refs_by_asset_id": [],
                         "calendar_row_refs_by_source_url": [],
                         "rendition_identity": None,
                         "original_use_receipt": None},
        }
        for index, asset in enumerate(asset_rows, 1)
    ]
    asset_obligations.sort(key=lambda item: item["asset_ref"])
    return calendar_obligations, asset_obligations


def _input_report(path, raw, rows):
    return {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "row_count": len(rows),
    }


def build_report(calendar_path, asset_path, byte_path=None, provider_path=None,
                 postlog_path=None):
    raw_calendar, calendar_payload = _load_json(calendar_path, "calendar_snapshot")
    raw_asset, asset_payload = _load_json(asset_path, "asset_snapshot")
    calendar_rows = _rows_of(calendar_payload, "calendar_snapshot")
    asset_rows = _rows_of(asset_payload, "asset_snapshot")

    optional = {}
    for label, path in (("byte_observation_manifest", byte_path),
                        ("provider_manifest", provider_path),
                        ("postlog_manifest", postlog_path)):
        if path is None:
            optional[label] = None
            continue
        raw, payload = _load_json(path, label)
        optional[label] = (path, raw, _rows_of(payload, label))

    calendar_obligations, asset_obligations = reconcile(
        calendar_rows, asset_rows,
        byte_rows=optional["byte_observation_manifest"][2] if optional["byte_observation_manifest"] else None,
        provider_rows=optional["provider_manifest"][2] if optional["provider_manifest"] else None,
        postlog_rows=optional["postlog_manifest"][2] if optional["postlog_manifest"] else None,
    )

    reason_counts = {}
    for item in calendar_obligations + asset_obligations:
        for code in item["reasons"]:
            reason_counts[code] = reason_counts.get(code, 0) + 1

    inputs = {
        "calendar_snapshot": _input_report(calendar_path, raw_calendar, calendar_rows),
        "asset_snapshot": _input_report(asset_path, raw_asset, asset_rows),
        "byte_observation_manifest": None,
        "provider_manifest": None,
        "postlog_manifest": None,
    }
    for label in ("byte_observation_manifest", "provider_manifest", "postlog_manifest"):
        if optional[label] is not None:
            path, raw, rows = optional[label]
            inputs[label] = _input_report(path, raw, rows)

    total = len(calendar_obligations) + len(asset_obligations)
    return {
        "format": FORMAT,
        "inputs": inputs,
        "summary": {
            "obligations": total,
            "calendar_obligations": len(calendar_obligations),
            "asset_obligations": len(asset_obligations),
            "resolved": 0,
            "unresolved": total,
            "reason_counts": reason_counts,
        },
        "notes": [
            "Zero historical clearance: every obligation is unresolved.",
            "No separately authenticated immutable original-use receipt "
            "format is integrated, so original use is never proven.",
            "byte_identity_observed is evidence only: matching "
            "delivered/source hashes tie a delivered object to saved source "
            "bytes; they do not establish original use or exclude prior use.",
            "Source ID and URL matches are hints, not proof of original use.",
            "This report never clears assets and never asserts no prior use.",
            "Calendar snapshots are non-atomic observed scans; deleted/orphan "
            "ledger history may be absent from every input.",
            "Duplicate row refs, duplicate asset IDs/URLs, malformed "
            "digests, exact-URL mismatches, and md5/sha256 disagreement "
            "abort the run with no output.",
        ],
        "obligations": calendar_obligations,
        "asset_obligations": asset_obligations,
    }


def write_report(path, report):
    """Atomically replace ``path`` with owner-only (0600) sorted JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                     dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, indent=2)
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
    parser.add_argument("calendar_snapshot", type=Path,
                        help="saved visual-calendar-snapshot JSON")
    parser.add_argument("asset_snapshot", type=Path,
                        help="saved visual-asset-snapshot JSON")
    parser.add_argument("report", type=Path, help="local output report JSON")
    parser.add_argument("--byte-observation-manifest", type=Path, default=None,
                        help="saved visual-delivered-byte-inventory JSON")
    parser.add_argument("--provider-manifest", type=Path, default=None,
                        help="saved provider obligation manifest JSON")
    parser.add_argument("--postlog-manifest", type=Path, default=None,
                        help="saved publish postlog manifest JSON")
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.calendar_snapshot, args.asset_snapshot,
            byte_path=args.byte_observation_manifest,
            provider_path=args.provider_manifest,
            postlog_path=args.postlog_manifest,
        )
    except InputError as exc:
        print(f"visual media evidence reconcile failed: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("visual media evidence reconcile failed: input_error", file=sys.stderr)
        return 2
    try:
        write_report(args.report, report)
    except Exception:
        print("visual media evidence reconcile failed: output_error", file=sys.stderr)
        return 2
    summary = report["summary"]
    print(f"wrote {summary['obligations']} obligations "
          f"({summary['resolved']} resolved, {summary['unresolved']} unresolved) "
          f"to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
