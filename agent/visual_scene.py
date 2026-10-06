"""Scene-level perceptual similarity namespace, layered on exact byte identity.

Identity and similarity are deliberately separate, exactly as in
``agent/visual_fingerprint.py``:

* ``scene:phash64:<16 hex>`` is SIMILARITY EVIDENCE (a 64-bit DCT pHash from
  the already-merged ``agent/vision.dct_phash``). It is never byte identity:
  resized/cropped variants of one scene cluster, but so can unrelated frames,
  so a scene match can only ever route a candidate to manual review — never
  auto-approve and never write ``visual_group_scene_link`` (the existing SQL
  forbids any pHash-inferred link).
* Unknown/empty/undecodable bytes fail closed (return None) instead of
  producing an invented identifier.

This module is pure: no I/O, no storage, no environment reads at import.
Pillow is imported lazily inside ``vision.dct_phash``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from . import vision

SCENE_PHASH64 = "scene:phash64"

_HEX16 = re.compile(r"^[0-9a-f]{16}$")

# Classification bands (policy thresholds from PR232's documented constants).
# hamming <= 6: near-frame (the §3 burst near-dupe cluster radius).
# 7..30: scene-match CANDIDATE — calibrated on exactly one measured incident
# pair (Swift River JCK_6328/JCK_6331, hamming 28) and is HOLD-ONLY: it routes
# to manual review and must never auto-approve or write visual_group_scene_link.
# > 30: distinct.
SCENE_NEAR_MAX = 6
SCENE_CANDIDATE_MIN = 7
SCENE_CANDIDATE_MAX = 30

NEAR_FRAME = "near_frame"
SCENE_CANDIDATE = "scene_candidate"
DISTINCT = "distinct"
UNKNOWN = "unknown"

_PREFIX = f"{SCENE_PHASH64}:"


@dataclass(frozen=True)
class SceneMatch:
    """Immutable nearest-only result of classify_scene (kept for backward
    compat). ``matched`` is the namespaced known hash at the minimum distance
    (None for UNKNOWN/DISTINCT)."""
    kind: str
    distance: int
    matched: Optional[str]


@dataclass(frozen=True)
class SceneClassification:
    """Immutable full result of classify_scene_all. ``near_matches`` and
    ``candidate_matches`` carry EVERY qualifying known hash as
    (namespaced_phash, distance) tuples, sorted by distance then phash for
    determinism, so a review can never hide behind the single nearest match.
    ``distance``/``matched`` are the minimum, mirroring SceneMatch."""
    kind: str
    distance: int
    matched: Optional[str]
    near_matches: tuple
    candidate_matches: tuple


def scene_fingerprint(data: bytes) -> str | None:
    """The namespaced scene fingerprint of image bytes, or None for
    unreadable/non-image/empty bytes. NEVER raises, NEVER invents an
    identifier."""
    if not isinstance(data, bytes) or not data:
        return None
    try:
        digest = vision.dct_phash(data)
    except Exception:  # noqa: BLE001 - decode/hash failure fails closed
        return None
    if not isinstance(digest, str) or not _HEX16.fullmatch(digest.strip().lower()):
        return None
    return f"{_PREFIX}{digest.strip().lower()}"


def normalize_scene(value) -> str | None:
    """Validate a namespaced scene fingerprint without guessing its namespace.

    Bare hashes are rejected (mirrors ``visual_fingerprint.normalize``)."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v.startswith(_PREFIX):
        return None
    digest = v[len(_PREFIX):]
    return v if _HEX16.fullmatch(digest) else None


def _bare_hash(value) -> str | None:
    """Bare 16-hex of a namespaced or bare scene hash, or None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v.startswith(_PREFIX):
        v = v[len(_PREFIX):]
    return v if _HEX16.fullmatch(v) else None


def hamming_distance(a, b) -> int:
    """Hamming distance between two scene hashes (bare 16-hex or namespaced).
    999 on anything malformed — treated as 'no evidence', never as a match."""
    ha = _bare_hash(a)
    hb = _bare_hash(b)
    if ha is None or hb is None:
        return 999
    try:
        d = vision.hamming(ha, hb)
    except Exception:  # noqa: BLE001 - malformed evidence fails closed
        return 999
    return d if isinstance(d, int) and 0 <= d <= 64 else 999


def classify_scene_all(candidate, known: Iterable) -> SceneClassification:
    """Classify one candidate scene fingerprint against known scene hashes,
    returning EVERY qualifying match, not just the nearest.

    ``candidate`` is a namespaced scene fingerprint or None; ``known`` is an
    iterable of namespaced or bare hashes. ``kind`` is the worst-case band:
    NEAR_FRAME if ANY usable known is within SCENE_NEAR_MAX, else
    SCENE_CANDIDATE if any is in the 7..30 band, else DISTINCT, else UNKNOWN
    (candidate None/unusable, or every known hash unusable). UNKNOWN is the
    fail-closed case and must never be treated as DISTINCT. A SCENE_CANDIDATE
    result is a hold-for-manual-review signal only — never an approval."""
    cand_hash = _bare_hash(candidate)
    if cand_hash is None:
        return SceneClassification(UNKNOWN, 999, None, (), ())
    near = []
    candidate_matches = []
    usable = 0
    best_distance = 999
    best_known: Optional[str] = None
    for item in known or ():
        bare = _bare_hash(item)
        if bare is None:
            continue
        d = vision.hamming(cand_hash, bare)
        if not isinstance(d, int) or not 0 <= d <= 64:
            continue
        usable += 1
        namespaced = f"{_PREFIX}{bare}"
        if d <= SCENE_NEAR_MAX:
            near.append((namespaced, d))
        elif SCENE_CANDIDATE_MIN <= d <= SCENE_CANDIDATE_MAX:
            candidate_matches.append((namespaced, d))
        if d < best_distance:
            best_distance = d
            best_known = namespaced
    if usable == 0:
        return SceneClassification(UNKNOWN, 999, None, (), ())
    near.sort(key=lambda m: (m[1], m[0]))
    candidate_matches.sort(key=lambda m: (m[1], m[0]))
    if near:
        kind = NEAR_FRAME
        matched = near[0][0]
    elif candidate_matches:
        kind = SCENE_CANDIDATE
        matched = candidate_matches[0][0]
    else:
        kind = DISTINCT
        matched = None
    return SceneClassification(kind, best_distance, matched,
                               tuple(near), tuple(candidate_matches))


def classify_scene(candidate, known: Iterable) -> SceneMatch:
    """Nearest-only classification, behavior unchanged (backward compat).

    Thin wrapper over :func:`classify_scene_all`; ``known`` hashes beyond the
    nearest are hidden here — callers that must not mask matches use
    classify_scene_all directly."""
    c = classify_scene_all(candidate, known)
    return SceneMatch(c.kind, c.distance, c.matched)
