"""Deterministic spacing for client-uploaded photo bursts.

The intake path gives every upload an immutable UTC batch stamp and preserves the
ordered camera filename. Those two non-biometric facts are enough to avoid
placing several frames from one shoot on adjacent days without depending on the
optional Vision pipeline.

This module is deliberately selection-only. It never changes moderation,
approval, denial, calendar, or publish state. When fewer than two trustworthy
cohorts are present it returns the input unchanged, preserving the legacy thin-
library fallback.
"""

import os
import re
from collections import defaultdict
from datetime import datetime
from functools import lru_cache

from . import config, dam


_STAMP_RE = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)_(?P<name>[^/]+)$")
_SEPARATED_CAMERA_SEQUENCE_RE = re.compile(
    r"^(?P<family>[A-Za-z][A-Za-z0-9]{0,11})[_-](?P<sequence>\d{3,8})"
    r"(?:[_-].*)?$",
    re.IGNORECASE,
)
_COMPACT_CAMERA_SEQUENCE_RE = re.compile(
    r"^(?P<family>[A-Za-z](?:[A-Za-z0-9]{0,10}[A-Za-z])?)"
    r"(?P<sequence>\d{3,8})(?:[_-].*)?$",
    re.IGNORECASE,
)
_MAX_SEQUENCE_GAP = 4
_MAX_CAMERA_SEQUENCE = 99_999_999
MAX_BATCH_POSITION = 1_000_000
_CAMERA_FAMILY_RE = re.compile(r"[a-z][a-z0-9]{0,11}\Z")
INTAKE_METADATA_FIELDS = (
    "intake_batch_timestamp", "intake_batch_position",
    "intake_camera_family", "intake_camera_sequence",
)


def normalize_batch_timestamp(value):
    """Return the canonical portal timestamp, or ``None`` when it is not real."""
    if not isinstance(value, str):
        return None
    stamp = value.strip()
    if not re.fullmatch(r"\d{8}T\d{6}Z", stamp):
        return None
    try:
        parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return parsed.strftime("%Y%m%dT%H%M%SZ")


def normalize_intake_metadata(values):
    """Validate one complete persisted intake-metadata record.

    Batch timestamp and upload position are mandatory. Camera family/sequence
    are optional as a pair, but a partial or malformed pair invalidates the
    whole record so corrupt fields cannot be combined with filename fallback.
    """
    if not isinstance(values, dict):
        return None
    batch = normalize_batch_timestamp(values.get("intake_batch_timestamp"))
    position = values.get("intake_batch_position")
    if (batch is None or type(position) is not int
            or not 0 <= position <= MAX_BATCH_POSITION):
        return None
    family_present = "intake_camera_family" in values
    sequence_present = "intake_camera_sequence" in values
    if family_present != sequence_present:
        return None
    normalized = {
        "intake_batch_timestamp": batch,
        "intake_batch_position": position,
    }
    if family_present:
        family = values.get("intake_camera_family")
        sequence = values.get("intake_camera_sequence")
        if (not isinstance(family, str) or family != family.strip().lower()
                or not _CAMERA_FAMILY_RE.fullmatch(family)
                or type(sequence) is not int
                or not 0 <= sequence <= _MAX_CAMERA_SEQUENCE):
            return None
        normalized["intake_camera_family"] = family
        normalized["intake_camera_sequence"] = sequence
    return normalized


def _validated_stamped_name(value):
    """Return ``(canonical_stamp, source_name)`` for a valid stamped basename."""
    match = _STAMP_RE.search(os.path.basename(str(value or "")))
    if not match:
        return None
    stamp = normalize_batch_timestamp(match.group("stamp"))
    if stamp is None:
        return None
    return stamp, match.group("name")


def parse_camera_sequence(value):
    """Return ``(family, sequence)`` from one conservative camera filename.

    Separator-free camera names need their complete trailing digit run treated
    as the sequence. A single optional-separator regex made the family greedy,
    so ``DSCN0999`` became family ``dscn0`` / sequence ``999`` and then
    ``DSCN1000`` became family ``dscn1`` / sequence ``000``. Keeping the
    separated and compact shapes explicit preserves rollover identity.
    """
    name = os.path.basename(str(value or ""))
    stamped = _validated_stamped_name(name)
    if stamped:
        _stamp, name = stamped
    stem = os.path.splitext(name)[0]
    for pattern in (_SEPARATED_CAMERA_SEQUENCE_RE,
                    _COMPACT_CAMERA_SEQUENCE_RE):
        match = pattern.match(stem)
        if match:
            return match.group("family").lower(), int(match.group("sequence"))
    return None


def _sidecar_signature(path):
    """Cheap cache invalidator for one asset's cohort metadata."""
    try:
        stat = os.stat(dam.sidecar_path(path))
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except OSError:
        return None


@lru_cache(maxsize=4096)
def _metadata_cached(path, sidecar_signature):
    """Return an intake batch plus conservative camera sequence metadata."""
    side = dam.read_sidecar(path)
    has_intake_metadata = any(key in side for key in INTAKE_METADATA_FIELDS)
    if has_intake_metadata:
        trusted = normalize_intake_metadata(side)
        if trusted is None:
            return None
        batch = trusted["intake_batch_timestamp"]
        position = trusted["intake_batch_position"]
        family = trusted.get("intake_camera_family", "")
        sequence = trusted.get("intake_camera_sequence")
    else:
        batch, family, sequence, position = "", "", None, None

    source_names = (
        side.get("original_key"), side.get("current_key"),
        side.get("intake_source_key"), os.path.basename(path),
    )
    source_name = ""
    for raw in source_names:
        stamped = _validated_stamped_name(raw)
        if stamped:
            fallback_batch, source_name = stamped
            batch = batch or fallback_batch
            break
    if not batch:
        return None

    if not family or sequence is None:
        parsed = parse_camera_sequence(source_name or os.path.basename(path))
        if parsed:
            parsed_family, parsed_sequence = parsed
            family = family or parsed_family
            if sequence is None:
                sequence = parsed_sequence
    return batch, family, sequence, position


def _metadata(path):
    return _metadata_cached(path, _sidecar_signature(path))


def intake_order_key(path):
    """Stable within-cohort order, preferring the portal's upload order."""
    metadata = _metadata(path)
    if metadata is None:
        return (1, 0, 1, 0, os.path.basename(path).casefold())
    _batch, _family, sequence, position = metadata
    return (
        position is None, position or 0,
        sequence is None, sequence or 0,
        os.path.basename(path).casefold(),
    )


@lru_cache(maxsize=128)
def _cohort_items(signature):
    """Compute immutable cohort pairs once per unchanged library snapshot."""
    grouped = defaultdict(list)
    result = {}
    for media_type, path, sidecar_state in signature:
        if media_type != "image":
            continue
        meta = _metadata_cached(path, sidecar_state)
        if meta is None:
            continue
        batch, family, sequence, _position = meta
        grouped[(batch, family)].append((sequence, path))

    for (batch, family), rows in grouped.items():
        sequenced = sorted((seq, path) for seq, path in rows if seq is not None)
        unsequenced = [path for seq, path in rows if seq is None]
        if not sequenced:
            key = f"intake:{batch}:{family or 'batch'}"
            result.update({path: key for path in unsequenced})
            continue

        cluster = 0
        prior = None
        for sequence, path in sequenced:
            if prior is not None and sequence - prior > _MAX_SEQUENCE_GAP:
                cluster += 1
            result[path] = f"intake:{batch}:{family or 'camera'}:{cluster}"
            prior = sequence
        batch_key = f"intake:{batch}:{family or 'batch'}:unknown"
        result.update({path: batch_key for path in unsequenced})
    return tuple(sorted(result.items()))


def cohort_map(creatives):
    """Map creative path to a stable burst cohort, omitting unknown assets.

    The signature stats sidecars but does not reread them. Repeated picks from an
    unchanged month-build library reuse both parsed metadata and cohort grouping.
    """
    signature = tuple(
        (getattr(creative, "media_type", ""), creative.path,
         _sidecar_signature(creative.path))
        for creative in creatives
    )
    return dict(_cohort_items(signature))


def choose_spaced_pool(pool, catalog, served, account_key, day_key):
    """Return one maximally-spaced photo cohort from ``pool``.

    The cohort used least recently wins. Never-used cohorts are round-robined by
    day for deterministic rebuilds. If videos, unknown metadata, or only one
    cohort make spacing unsafe or meaningless, the legacy pool is returned.
    """
    pool = list(pool)
    if not config.burst_spacing_enabled_for(account_key):
        return pool
    if len(pool) < 2 or any(getattr(c, "media_type", "") != "image" for c in pool):
        return pool
    cohorts = cohort_map(catalog)
    # Fail open to the legacy picker when even one eligible candidate cannot be
    # placed in a trustworthy intake cohort. Filtering only the known paths would
    # silently starve legacy media in an otherwise cohort-rich mixed library.
    if any(c.path not in cohorts for c in pool):
        return pool
    available = sorted({cohorts.get(c.path) for c in pool if cohorts.get(c.path)})
    if len(available) < 2:
        return pool

    from . import rotation
    rotation_to_cohorts = defaultdict(set)
    for creative in catalog:
        if creative.path in cohorts:
            rotation_to_cohorts[dam.rotation_key(creative.path)].add(
                cohorts[creative.path])
    base = rotation._base_account_key(account_key)
    last_date = {}
    for served_account, entries in (served or {}).items():
        if rotation._base_account_key(served_account) != base:
            continue
        for entry in entries:
            when = str(entry.get("date") or "")
            # A near-dupe rotation key can (legitimately or through stale
            # metadata) span more than one intake cohort. Attribute that served
            # record to every possible cohort. Picking an arbitrary last writer
            # would make the other cohort look never used and defeat spacing.
            for cohort in rotation_to_cohorts.get(entry.get("key"), ()):
                if cohort in available and when > last_date.get(cohort, ""):
                    last_date[cohort] = when

    never = [cohort for cohort in available if cohort not in last_date]
    if never:
        candidates = never
    else:
        oldest = min(last_date.get(cohort, "") for cohort in available)
        candidates = [cohort for cohort in available if last_date.get(cohort, "") == oldest]
    from .client_content import _day_ordinal
    chosen = candidates[_day_ordinal(day_key) % len(candidates)]
    spaced = [creative for creative in pool if cohorts.get(creative.path) == chosen]
    spaced.sort(key=lambda creative: intake_order_key(creative.path))
    return spaced or pool
