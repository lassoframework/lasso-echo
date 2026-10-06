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

from . import config, dam


_STAMP_RE = re.compile(r"(?P<stamp>\d{8}T\d{6}Z)_(?P<name>[^/]+)$")
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


def parse_camera_sequence(value):
    """Return ``(family, sequence)`` from one conservative camera filename.

    Separator-free camera names need their complete trailing digit run treated
    as the sequence. A single optional-separator regex made the family greedy,
    so ``DSCN0999`` became family ``dscn0`` / sequence ``999`` and then
    ``DSCN1000`` became family ``dscn1`` / sequence ``000``. Keeping the
    separated and compact shapes explicit preserves rollover identity.
    """
    name = os.path.basename(str(value or ""))
    stamped = _STAMP_RE.search(name)
    if stamped:
        name = stamped.group("name")
    stem = os.path.splitext(name)[0]
    for pattern in (_SEPARATED_CAMERA_SEQUENCE_RE,
                    _COMPACT_CAMERA_SEQUENCE_RE):
        match = pattern.match(stem)
        if match:
            return match.group("family").lower(), int(match.group("sequence"))
    return None


def _metadata(path):
    """Return an intake batch plus conservative camera sequence metadata."""
    side = dam.read_sidecar(path)
    batch = str(side.get("intake_batch_timestamp") or "").strip()
    family = str(side.get("intake_camera_family") or "").strip().lower()
    sequence = side.get("intake_camera_sequence")
    try:
        sequence = int(sequence) if sequence is not None else None
    except (TypeError, ValueError):
        sequence = None

    source_names = (
        side.get("original_key"), side.get("current_key"),
        side.get("intake_source_key"), os.path.basename(path),
    )
    source_name = ""
    for raw in source_names:
        match = _STAMP_RE.search(str(raw or ""))
        if match:
            batch = batch or match.group("stamp")
            source_name = match.group("name")
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
    return batch, family, sequence


def cohort_map(creatives):
    """Map creative path to a stable burst cohort, omitting unknown assets."""
    grouped = defaultdict(list)
    result = {}
    for creative in creatives:
        if getattr(creative, "media_type", "") != "image":
            continue
        meta = _metadata(creative.path)
        if meta is None:
            continue
        batch, family, sequence = meta
        grouped[(batch, family)].append((sequence, creative.path))

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
    return result


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
    return spaced or pool
