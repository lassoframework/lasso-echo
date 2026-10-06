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

from . import dam


_STAMP_RE = re.compile(r"(?P<stamp>\d{8}T\d{6}Z)_(?P<name>[^/]+)$")
_CAMERA_SEQUENCE_RE = re.compile(
    r"^(?P<family>[A-Za-z][A-Za-z0-9]{1,11})[_-]?(?P<sequence>\d{3,8})"
    r"(?:[_-].*)?$",
    re.IGNORECASE,
)
_MAX_SEQUENCE_GAP = 4


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
        stem = os.path.splitext(source_name)[0]
        match = _CAMERA_SEQUENCE_RE.match(stem)
        if match:
            family = family or match.group("family").lower()
            if sequence is None:
                sequence = int(match.group("sequence"))
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
    if len(pool) < 2 or any(getattr(c, "media_type", "") != "image" for c in pool):
        return pool
    cohorts = cohort_map(catalog)
    available = sorted({cohorts.get(c.path) for c in pool if cohorts.get(c.path)})
    if len(available) < 2:
        return pool

    from . import rotation
    rotation_to_cohort = {
        dam.rotation_key(c.path): cohorts[c.path]
        for c in catalog if c.path in cohorts
    }
    base = rotation._base_account_key(account_key)
    last_date = {}
    for served_account, entries in (served or {}).items():
        if rotation._base_account_key(served_account) != base:
            continue
        for entry in entries:
            cohort = rotation_to_cohort.get(entry.get("key"))
            when = str(entry.get("date") or "")
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
