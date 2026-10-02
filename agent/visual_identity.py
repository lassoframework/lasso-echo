"""Deterministic per-gym visual scene grouping ("visual identity") for media assets.

Why this module exists (global no-repeat media repair): a gym must publish a
DIFFERENT underlying visual group on different dates across IG/FB/Story/GBP.
Same-date siblings may share one group. Once published, a group is permanently
used. The scheduler therefore needs one canonical answer to "is this asset the
same visual as something this gym already used?"

SCOPE / HONESTY
---------------
This module is the in-memory identity engine only. The global fix is NOT
complete here: the DB RPC/migration is still DRAFT
(migrations/DRAFT_media_group_usage_20261002.sql), the in-process ledger built
by these functions is NOT cross-worker authoritative, and automated acceptance
of the live Swift River repair is NOT claimed — that requires operator
scene-confirmation (confirm_scene_match), re-plan, the applied migration, and
backfill.

CRITICAL CORRECTNESS NOTES — read before wiring callers
-------------------------------------------------------
* Distinct Drive file IDs and distinct MD5/SHA byte hashes do NOT prove visual
  uniqueness. The Swift River incident: the Oct 7-10 rows (still pending, all
  with source_media_asset_id NULL) used four different Drive IDs and four
  different MD5s; the user-reported screenshot shows visually the same
  neighboring scene, while the currently staged rows carry distinct generated
  graphics — i.e. they are pending rows awaiting operator review, NOT a visible
  media HOLD rendered by this system. Byte identity only proves byte identity.
* pHash threshold 6 catches ONLY near-identical frames — recompressed, resized,
  lightly re-edited copies of one frame. It does NOT catch neighboring frames
  of the same scene. MEASURED EVIDENCE (exactly one real pair, not synthetic):
  the original Drive thumbnails JCK_6328 vs JCK_6331, retrieved via the portal,
  visibly the same rig/class scene, hash to dct_phash a1c7e3a2583fa81e vs
  b8c1e09b0cfd9be0 — Hamming distance 28. No other neighboring-frame distances
  have been measured on real media; earlier 18-30 figures came from distinct
  generated graphics / synthetic bands. The 7-30 scene band is therefore a
  CONSERVATIVE POLICY choice that includes the one measured pair — it is NOT a
  calibrated complete scene range. pHash alone cannot prove scene identity in
  this band (genuinely different scenes can also land there), so the band is a
  HOLD state (SCENE_MATCH_CANDIDATE): never an automatic merge, never a free
  pass to reuse.
* UNKNOWN_IDENTITY fails closed: no usable bytes and no usable pHash means the
  asset is held visibly, never silently minted into a fresh group.
* The sanctioned resolutions for the hold band are confirm_scene_match
  (operator/corroborated assertion "same scene", recorded with both phashes
  and the Hamming distance) or reject_scene_match (human-reviewed, judged
  DIFFERENT — recorded so the pair never re-holds). assign_manual_group mints
  a fresh operator-labeled group independent of any suspect.

Classification tiers (Hamming distance between 64-bit DCT pHashes,
vision.dct_phash / vision.hamming; byte checks first):
  1. EXACT_BYTES — candidate sha256/md5 equals any member's sha256/md5.
     -> SAME_GROUP, strongest evidence, distance 0.
  2. NEAR_FRAME band — hamming <= PHASH_NEAR_DUPLICATE_THRESHOLD (6,
     inclusive). Same frame, recompressed/resized/noised. -> SAME_GROUP,
     high confidence. Matches the clustering rule in agent/vision.py
     ("cluster at <=6"). Synthetic structured-scene variants measured 0-2.
  3. SCENE band — PHASH_SCENE_MATCH_MIN (7) <= hamming <=
     PHASH_SCENE_MATCH_MAX (30). Conservative policy band that includes the
     single measured incident pair above (28, in-band); NOT a calibrated
     complete scene range. -> SCENE_MATCH_CANDIDATE. Distinct
     outcome, NOT same group, NOT new group. Callers must hold the asset
     visibly and resolve via confirm_scene_match / reject_scene_match /
     assign_manual_group. Default = hold.
  4. hamming > PHASH_SCENE_MATCH_MAX, or the suspect pair was rejected by an
     operator -> NEW_GROUP.
  5. No byte hashes AND no usable pHash -> UNKNOWN_IDENTITY (fail closed).

GROUP IDENTITY (immutable keys)
-------------------------------
Every group gets a stable `group_id` AT CREATION TIME and it never changes as
membership grows or merges: group_key = f"{gym_id}::{group_id}" is frozen
forever, so persisted reservations keyed on it never dangle. Hash-formed
groups derive group_id from the FOUNDING member's stable identity
("h-<identity>"); manual groups use the explicit label ("manual:<label>").
In-process ids are deterministic for a given construction sequence (the same
assets folded in the same order yield the same ids); different construction
orders can found a group from a different member, so PERSISTED STORAGE MUST
STORE group_id EXPLICITLY rather than re-deriving it. The canonical member
(smallest stable identity) remains available as the display-only
`canonical_member` property and never feeds the key. merge_groups keeps the
target's key unchanged and records the absorbed key in audit.

MANUAL GROUPS
-------------
assign_manual_group(asset, groups, label, note=...) forces an asset into a
group keyed f"{gym_id}::manual:{label}" regardless of hash evidence — the
sanctioned path when pHash cannot prove identity (e.g. Swift River's
visually-same neighboring scene, whose one measured pair lands at Hamming 28)
and no suitable suspect group exists.

Everything here is pure and in-memory: no DB, no network, no clock, no
randomness. Tenant isolation is structural: the gym id is part of every key
and classification only ever matches same-gym groups. Source lineage: every
AssetRef carries drive_file_id, storage_key, md5, sha256 so siblings trace
back to their origins.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from agent import vision

# ---------------------------------------------------------------------------
# Tier bounds (documented, with calibration, in the module docstring).
# ---------------------------------------------------------------------------
PHASH_EXACT_MATCH = 0
PHASH_NEAR_DUPLICATE_THRESHOLD = 6  # inclusive; matches vision.py "cluster at <=6"
PHASH_SCENE_MATCH_MIN = PHASH_NEAR_DUPLICATE_THRESHOLD + 1  # 7, inclusive
# Inclusive upper bound; conservative policy band chosen to include the single
# measured incident pair JCK_6328 vs JCK_6331 (a1c7e3a2583fa81e vs
# b8c1e09b0cfd9be0, hamming 28) — NOT a calibrated complete scene range.
PHASH_SCENE_MATCH_MAX = 30

MANUAL_KEY_PREFIX = "manual:"
HASH_KEY_PREFIX = "h-"


class Classification(str, Enum):
    """Result of classifying a candidate asset against a gym's known groups."""

    NEW_GROUP = "NEW_GROUP"
    SAME_GROUP = "SAME_GROUP"
    SCENE_MATCH_CANDIDATE = "SCENE_MATCH_CANDIDATE"  # hold: corroborate first
    UNKNOWN_IDENTITY = "UNKNOWN_IDENTITY"  # fail closed


@dataclass(frozen=True)
class AssetRef:
    """One media asset with its identity evidence and source lineage.

    gym_id scopes the asset to a tenant; it is part of every derived group
    key. asset_id is the caller's stable identifier (calendar row id, library
    id, etc.). drive_file_id / storage_key / md5 / sha256 are lineage +
    byte-identity evidence carried through for tracing. phash is a 16-char hex
    DCT pHash as produced by vision.dct_phash, or None when the bytes were
    unavailable or unreadable.
    """

    gym_id: str
    asset_id: str
    phash: str | None = None
    drive_file_id: str | None = None
    storage_key: str | None = None
    md5: str | None = None
    sha256: str | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        gym_id: str,
        asset_id: str,
        data: bytes | None,
        drive_file_id: str | None = None,
        storage_key: str | None = None,
    ) -> "AssetRef":
        """Build an AssetRef from raw bytes: computes sha256/md5 lineage and the
        DCT pHash. data=None (or unreadable image bytes) yields phash=None,
        which downstream classification treats as fail-closed evidence."""
        if not data:
            return cls(
                gym_id=gym_id,
                asset_id=asset_id,
                drive_file_id=drive_file_id,
                storage_key=storage_key,
            )
        return cls(
            gym_id=gym_id,
            asset_id=asset_id,
            phash=vision.dct_phash(data),
            drive_file_id=drive_file_id,
            storage_key=storage_key,
            md5=hashlib.md5(data).hexdigest(),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def byte_hashes(self) -> frozenset[str]:
        """All byte-identity hashes present for this asset (may be empty)."""
        return frozenset(h for h in (self.sha256, self.md5) if h)

    def stable_identity(self) -> str:
        """Deterministic identity string used for founding ids, rejection
        matching and canonical display ordering. Prefers the strongest lineage
        available so it survives asset-id churn."""
        for ident in (self.sha256, self.md5, self.drive_file_id, self.storage_key):
            if ident:
                return ident
        return self.asset_id


@dataclass(frozen=True)
class MatchResult:
    """Outcome of classifying one candidate against known groups."""

    classification: Classification
    group_key: str | None = None  # SAME_GROUP: the group; SCENE_MATCH_CANDIDATE: nearest suspect
    distance: int | None = None  # min phash Hamming distance when phash compared
    matched_member_id: str | None = None  # member that produced the match (lineage)
    reason: str = ""


@dataclass
class VisualGroup:
    """One visual scene group for one gym.

    group_id is assigned AT CREATION and never changes: group_key is frozen
    forever (see module docstring, "GROUP IDENTITY"). members retains full
    source lineage. rejections holds stable identities of assets an operator
    judged DIFFERENT from this group after a scene-band hold — those assets
    never re-hold against this group. audit is an ordered list of free-text
    notes (who/when/why), append-only by convention.
    """

    gym_id: str
    group_id: str | None = None  # frozen at creation; required unless manual_label
    members: list[AssetRef] = field(default_factory=list)
    manual_label: str | None = None
    rejections: set[str] = field(default_factory=set)
    audit: list[str] = field(default_factory=list)

    @property
    def group_key(self) -> str:
        """Immutable key: f"{gym_id}::{group_id}" (or the manual label). Never
        derived from current membership, so it cannot drift as members are
        appended or merged."""
        if self.manual_label:
            return f"{self.gym_id}::{MANUAL_KEY_PREFIX}{self.manual_label}"
        if not self.group_id:
            raise ValueError("group_key requires a group_id assigned at creation")
        return f"{self.gym_id}::{self.group_id}"

    @property
    def canonical_member(self) -> str | None:
        """Display-only: the lexicographically smallest stable member identity.
        Never feeds group_key."""
        if not self.members:
            return None
        return min(m.stable_identity() for m in self.members)

    def byte_hashes(self) -> frozenset[str]:
        out: set[str] = set()
        for m in self.members:
            out |= m.byte_hashes()
        return frozenset(out)

    def phashes(self) -> list[str]:
        return [m.phash for m in self.members if m.phash]


def manual_group_key(gym_id: str, label: str) -> str:
    """The canonical key for an operator-assigned manual group, namespaced per
    gym so identical labels in different gyms never collide."""
    label = str(label or "").strip()
    if not label:
        raise ValueError("manual group label must be non-empty")
    if label.startswith(MANUAL_KEY_PREFIX):
        label = label[len(MANUAL_KEY_PREFIX):]
    return f"{gym_id}::{MANUAL_KEY_PREFIX}{label}"


def _find_group(known_groups: list[VisualGroup], gym_id: str, group_key: str) -> VisualGroup | None:
    for g in known_groups:
        if g.gym_id != gym_id:
            continue
        if not g.group_id and not g.manual_label:
            continue  # keyless shell, nothing to match
        if g.group_key == group_key:
            return g
    return None


def _rejected_by(asset: AssetRef, group: VisualGroup) -> bool:
    """True when an operator has judged this asset DIFFERENT from this group."""
    return asset.stable_identity() in group.rejections or asset.asset_id in group.rejections


def group_key_for(
    asset: AssetRef,
    known_groups: list[VisualGroup],
    threshold: int = PHASH_NEAR_DUPLICATE_THRESHOLD,
) -> str | None:
    """Return the group_key of the group this asset PROVABLY belongs to, or
    None. SCENE_MATCH_CANDIDATE and UNKNOWN_IDENTITY both return None — a
    suspected scene match is not membership until confirmed."""
    result = classify_candidate(asset, known_groups, threshold=threshold)
    if result.classification is Classification.SAME_GROUP:
        return result.group_key
    return None


def classify_candidate(
    asset: AssetRef,
    known_groups: list[VisualGroup],
    threshold: int = PHASH_NEAR_DUPLICATE_THRESHOLD,
    scene_max: int = PHASH_SCENE_MATCH_MAX,
) -> MatchResult:
    """Classify a candidate against one gym's known groups (see module docstring
    for the tier table). Only groups belonging to asset.gym_id are considered;
    foreign-tenant groups are ignored even if passed in. Groups that carry an
    operator rejection for this asset are skipped in pHash matching, so a
    rejected scene pair resolves NEW_GROUP instead of re-holding forever.
    Byte-level proof (EXACT_BYTES) is unaffected by rejections."""
    own_groups = [g for g in known_groups if g.gym_id == asset.gym_id]

    # Tier 1: EXACT_BYTES — strongest evidence.
    candidate_hashes = asset.byte_hashes()
    if candidate_hashes:
        for g in own_groups:
            shared = candidate_hashes & g.byte_hashes()
            if shared:
                member = next(
                    (m for m in g.members if m.byte_hashes() & shared), None
                )
                return MatchResult(
                    classification=Classification.SAME_GROUP,
                    group_key=g.group_key,
                    distance=0,
                    matched_member_id=member.asset_id if member else None,
                    reason="exact bytes (sha256/md5 match)",
                )

    if asset.phash:
        best: tuple[int, str, str] | None = None  # (distance, group_key, member_id)
        rejected_suspect = False
        for g in own_groups:
            if _rejected_by(asset, g):
                if any(m.phash for m in g.members):
                    rejected_suspect = True
                continue
            for m in g.members:
                if not m.phash:
                    continue
                d = vision.hamming(asset.phash, m.phash)
                cand = (d, g.group_key, m.asset_id)
                if best is None or cand < best:
                    best = cand
        if best is not None:
            distance, key, member_id = best
            # Tier 2: NEAR_FRAME band — same frame, high confidence.
            if distance <= threshold:
                return MatchResult(
                    classification=Classification.SAME_GROUP,
                    group_key=key,
                    distance=distance,
                    matched_member_id=member_id,
                    reason=f"near-identical frame (hamming {distance} <= {threshold})",
                )
            # Tier 3: SCENE band — suspected same scene, hold by default.
            # pHash alone cannot prove identity here (measured same-scene
            # incident pair: hamming 28); corroboration or operator
            # confirmation required.
            if distance <= scene_max:
                return MatchResult(
                    classification=Classification.SCENE_MATCH_CANDIDATE,
                    group_key=key,
                    distance=distance,
                    matched_member_id=member_id,
                    reason=(
                        f"suspected same scene (hamming {distance} in "
                        f"{threshold + 1}-{scene_max}); hold pending corroboration"
                    ),
                )
        # Tier 4: provably different from everything comparable (or the only
        # suspect was operator-rejected).
        reason = "pHash differs from every known group beyond the scene band"
        if rejected_suspect:
            reason += "; operator-rejected scene suspect(s) excluded"
        return MatchResult(classification=Classification.NEW_GROUP, reason=reason)

    # Tier 5: no byte match above and no usable pHash — cannot prove this
    # asset is new, so never mint a group for it. Hold visibly.
    return MatchResult(
        classification=Classification.UNKNOWN_IDENTITY,
        reason="no byte hashes and no usable pHash; identity cannot be verified",
    )


def _move_into(asset: AssetRef, known_groups: list[VisualGroup], target: VisualGroup, why: str) -> None:
    """Move an asset into target, removing it from any other same-gym group and
    pruning hash-formed groups left empty. Lineage stays on the AssetRef."""
    for g in list(known_groups):
        if g is not target and g.gym_id == asset.gym_id and asset in g.members:
            g.members.remove(asset)
            g.audit.append(f"member {asset.asset_id} moved to {target.group_key} ({why})")
            if not g.members and not g.manual_label:
                known_groups.remove(g)
    if asset not in target.members:
        target.members.append(asset)
    target.rejections.discard(asset.stable_identity())
    target.rejections.discard(asset.asset_id)


def assign_group(
    asset: AssetRef,
    known_groups: list[VisualGroup],
    threshold: int = PHASH_NEAR_DUPLICATE_THRESHOLD,
    scene_max: int = PHASH_SCENE_MATCH_MAX,
) -> tuple[MatchResult, list[VisualGroup]]:
    """Classify the asset and return an updated group list.

    * SAME_GROUP: the asset is appended to the matched group (lineage
      preserved); the group's key is unchanged.
    * NEW_GROUP: a new VisualGroup is founded with group_id derived from this
      asset's stable identity — frozen forever from this moment.
    * SCENE_MATCH_CANDIDATE: the list is returned UNCHANGED — suspected scene
      matches are holds, not memberships. Resolve via confirm_scene_match,
      reject_scene_match, or assign_manual_group.
    * UNKNOWN_IDENTITY: the list is returned unchanged (fail closed).
    """
    result = classify_candidate(asset, known_groups, threshold=threshold, scene_max=scene_max)

    if result.classification in (
        Classification.UNKNOWN_IDENTITY,
        Classification.SCENE_MATCH_CANDIDATE,
    ):
        return result, known_groups

    if result.classification is Classification.SAME_GROUP:
        group = _find_group(known_groups, asset.gym_id, result.group_key)
        if group is not None and asset not in group.members:
            group.members.append(asset)
        return result, known_groups

    group = VisualGroup(
        gym_id=asset.gym_id,
        group_id=f"{HASH_KEY_PREFIX}{asset.stable_identity()}",
        members=[asset],
    )
    known_groups.append(group)
    return (
        MatchResult(
            classification=Classification.NEW_GROUP,
            group_key=group.group_key,
            reason=result.reason,
        ),
        known_groups,
    )


def assign_manual_group(
    asset: AssetRef,
    known_groups: list[VisualGroup],
    label: str,
    *,
    note: str | None = None,
) -> tuple[MatchResult, list[VisualGroup]]:
    """Force an asset into the operator-assigned group `manual:{label}` for its
    gym, regardless of hash evidence — the sanctioned path when pHash cannot
    prove identity and no suitable suspect group exists. Distinct from
    confirm_scene_match, which resolves a SPECIFIC held suspect pair.

    If the asset already sits in another group of the same gym, it is MOVED
    (lineage intact) into the manual group; a hash-formed group left empty by
    the move is pruned. Creates the manual group on first use. `note` should
    record who/when/why (free text)."""
    key = manual_group_key(asset.gym_id, label)
    group = _find_group(known_groups, asset.gym_id, key)
    if group is None:
        group = VisualGroup(
            gym_id=asset.gym_id,
            manual_label=key.split(MANUAL_KEY_PREFIX, 1)[1],
        )
        known_groups.append(group)
    _move_into(asset, known_groups, group, "operator manual assignment")
    if note:
        group.audit.append(note)
    return (
        MatchResult(
            classification=Classification.SAME_GROUP,
            group_key=key,
            distance=0,
            matched_member_id=asset.asset_id,
            reason=f"operator-assigned manual group ({key})",
        ),
        known_groups,
    )


def confirm_scene_match(
    asset: AssetRef,
    known_groups: list[VisualGroup],
    into_group_key: str,
    *,
    note: str | None = None,
) -> tuple[MatchResult, list[VisualGroup]]:
    """Operator/corroborated assertion: "this asset IS the same scene as the
    group it was held against." Moves the asset into the specified EXISTING
    same-gym group (hash evidence notwithstanding) and records the
    corroboration in audit: the note plus the asset's phash, the nearest
    member's phash, and their Hamming distance. The group's key is unchanged.

    Raises KeyError if the target group does not exist for this gym, and
    ValueError if the asset belongs to a different gym than the target."""
    target = _find_group(known_groups, asset.gym_id, into_group_key)
    if target is None:
        raise KeyError(f"scene-confirm target not found for {asset.gym_id}: {into_group_key}")
    if target.gym_id != asset.gym_id:
        raise ValueError("cross-gym scene confirmation is not allowed")

    distance = None
    nearest: AssetRef | None = None
    if asset.phash:
        for m in target.members:
            if not m.phash:
                continue
            d = vision.hamming(asset.phash, m.phash)
            if distance is None or d < distance:
                distance, nearest = d, m

    _move_into(asset, known_groups, target, "operator scene confirmation")
    evidence = (
        f"scene confirmed: {asset.asset_id} (phash {asset.phash}) vs "
        f"{nearest.asset_id if nearest else 'no comparable member'} "
        f"(phash {nearest.phash if nearest else None}), hamming {distance}"
    )
    target.audit.append(f"{evidence}; {note}" if note else evidence)
    return (
        MatchResult(
            classification=Classification.SAME_GROUP,
            group_key=target.group_key,
            distance=distance,
            matched_member_id=nearest.asset_id if nearest else None,
            reason=f"operator-confirmed scene match into {target.group_key}",
        ),
        known_groups,
    )


def reject_scene_match(
    asset: AssetRef,
    known_groups: list[VisualGroup],
    suspect_group_key: str,
    *,
    note: str | None = None,
) -> list[VisualGroup]:
    """Operator assertion: "this held scene-band candidate is DIFFERENT from the
    suspect group." Records the rejection on the suspect group (keyed by the
    asset's stable identity, so re-fetches under new asset ids stay rejected)
    plus an audit note. Future classification of this asset skips the rejected
    group in pHash matching, resolving NEW_GROUP instead of re-holding
    forever. The asset is NOT moved. Byte-level proof would still match.

    Raises KeyError if the suspect group does not exist for this gym."""
    suspect = _find_group(known_groups, asset.gym_id, suspect_group_key)
    if suspect is None:
        raise KeyError(f"scene-reject suspect not found for {asset.gym_id}: {suspect_group_key}")
    suspect.rejections.add(asset.stable_identity())
    suspect.rejections.add(asset.asset_id)
    entry = f"scene rejected: {asset.asset_id} (phash {asset.phash}) judged different"
    suspect.audit.append(f"{entry}; {note}" if note else entry)
    return known_groups


def merge_groups(
    known_groups: list[VisualGroup],
    *,
    gym_id: str,
    into_key: str,
    from_key: str,
    note: str | None = None,
) -> list[VisualGroup]:
    """Fold one group into another within one gym, preserving all member
    lineage. The TARGET's group_key is unchanged (keys are frozen); the
    absorbed group's key is recorded in the target's audit trail. The emptied
    source group is removed from the list. Raises KeyError if either key is
    absent, ValueError on same-key merge."""
    if into_key == from_key:
        raise ValueError("cannot merge a group into itself")
    target = _find_group(known_groups, gym_id, into_key)
    source = _find_group(known_groups, gym_id, from_key)
    if target is None:
        raise KeyError(f"merge target not found: {into_key}")
    if source is None:
        raise KeyError(f"merge source not found: {from_key}")
    for m in source.members:
        if m not in target.members:
            target.members.append(m)
    target.rejections |= source.rejections
    target.audit.append(
        note
        or f"merged {from_key} ({len(source.members)} members) into {into_key}; "
        f"absorbed key {from_key} retired"
    )
    known_groups.remove(source)
    return known_groups


def build_groups(
    assets: list[AssetRef],
    threshold: int = PHASH_NEAR_DUPLICATE_THRESHOLD,
    scene_max: int = PHASH_SCENE_MATCH_MAX,
) -> list[VisualGroup]:
    """Fold a list of assets into groups via assign_group. Deterministic for a
    fixed input sequence — founding-member-derived group ids depend on
    construction order, so persisted storage must store group_id explicitly
    rather than rebuild from reordered input. Assets classified
    UNKNOWN_IDENTITY or SCENE_MATCH_CANDIDATE form no group (fail closed /
    hold) — callers must surface them."""
    groups: list[VisualGroup] = []
    for asset in assets:
        assign_group(asset, groups, threshold=threshold, scene_max=scene_max)
    return groups
