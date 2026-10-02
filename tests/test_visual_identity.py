"""Focused tests for agent/visual_identity.py (global no-repeat media repair).

Run: PYTHONPATH=. python3 -m pytest tests/test_visual_identity.py

Honesty note: except for test_measured_incident_pair_lands_in_scene_band
(which uses the REAL measured pHashes from the Swift River incident), distances
here come from SYNTHETIC images and SYNTHETIC pHash bit patterns constructed to
land in each band. They exercise the tier logic; they are not proof that the
live Swift River repair is complete — that still requires operator
scene-confirmation, re-plan, the applied (currently DRAFT) migration, and
backfill.
"""

import io
import itertools
import random

import pytest
from PIL import Image, ImageDraw

from agent import vision
from agent.visual_identity import (
    PHASH_NEAR_DUPLICATE_THRESHOLD,
    PHASH_SCENE_MATCH_MAX,
    PHASH_SCENE_MATCH_MIN,
    AssetRef,
    Classification,
    VisualGroup,
    assign_group,
    assign_manual_group,
    build_groups,
    classify_candidate,
    confirm_scene_match,
    group_key_for,
    manual_group_key,
    merge_groups,
    reject_scene_match,
)

GYM = "gym_swift_river"
OTHER_GYM = "gym_other"

# MEASURED incident anchor (not synthetic): original Drive thumbnails
# JCK_6328 vs JCK_6331 via the portal, visibly the same rig/class scene.
JCK_6328_PHASH = "a1c7e3a2583fa81e"
JCK_6331_PHASH = "b8c1e09b0cfd9be0"


# ---------------------------------------------------------------------------
# Deterministic synthetic scenes: structured shapes so distinct seeds produce
# genuinely different visuals (synthetic observation: same-seed variants
# measure hamming 0-2; the seeds used below are verified pairwise > 30).
# ---------------------------------------------------------------------------
def scene_bytes(seed, *, noise=0, scale=None, fmt="PNG", quality=85):
    r = random.Random(seed)
    img = Image.new("L", (256, 256), r.randint(80, 200))
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = r.randint(0, 200), r.randint(0, 200)
        x1, y1 = x0 + r.randint(20, 60), y0 + r.randint(20, 60)
        shade = r.randint(0, 255)
        if r.random() < 0.5:
            d.rectangle([x0, y0, x1, y1], fill=shade)
        else:
            d.ellipse([x0, y0, x1, y1], fill=shade)
    if noise:
        px = img.load()
        rr = random.Random(seed * 1000 + 1)
        for y in range(256):
            for x in range(256):
                px[x, y] = min(255, max(0, px[x, y] + rr.randint(-noise, noise)))
    if scale:
        img = img.resize((scale, scale))
    buf = io.BytesIO()
    img.save(buf, fmt, quality=quality if fmt == "JPEG" else None)
    return buf.getvalue()


def asset_at_distance(base_int: int, distance: int, asset_id: str, gym_id: str = GYM) -> AssetRef:
    """SYNTHETIC pHash at an exact Hamming distance from base_int (flips the
    `distance` lowest bits), with distinct fake byte lineage so the byte tier
    cannot interfere."""
    flipped = base_int ^ ((1 << distance) - 1)
    assert vision.hamming(f"{flipped:016x}", f"{base_int:016x}") == distance
    return AssetRef(
        gym_id=gym_id,
        asset_id=asset_id,
        phash=f"{flipped:016x}",
        md5=f"md5-{asset_id}",
        sha256=f"sha-{asset_id}",
        drive_file_id=f"drive-{asset_id}",
    )


def hash_group(asset: AssetRef) -> VisualGroup:
    """A hash-formed group exactly as assign_group founds it."""
    return VisualGroup(
        gym_id=asset.gym_id,
        group_id=f"h-{asset.stable_identity()}",
        members=[asset],
    )


def swift_river_scene_band_assets():
    """The incident SHAPE, modeled with SYNTHETIC pHashes: four DISTINCT Drive
    file IDs and four DISTINCT MD5s whose pHashes sit 18-30 bits from the
    first. Only ONE real pair was ever measured (JCK_6328 vs JCK_6331,
    hamming 28); the 18-30 spread here is synthetic, chosen to sample the
    conservative 7-30 policy band — NOT a measured scene range."""
    base = int(vision.dct_phash(scene_bytes(7)), 16)
    distances = (18, 24, 30)
    first = AssetRef(
        gym_id=GYM,
        asset_id="sr_oct7",
        phash=f"{base:016x}",
        md5="md5-oct7",
        sha256="sha-oct7",
        drive_file_id="drive-file-0000",
        storage_key="swift-river/oct7.png",
    )
    rest = [
        asset_at_distance(base, d, f"sr_oct{7 + i + 1}")
        for i, d in enumerate(distances)
    ]
    assets = [first] + rest
    assert len({a.md5 for a in assets}) == 4
    assert len({a.drive_file_id for a in assets}) == 4
    return assets


def test_measured_incident_pair_lands_in_scene_band():
    """REAL measured evidence, not synthetic: JCK_6328 vs JCK_6331 (hamming 28)
    must classify as SCENE_MATCH_CANDIDATE — a hold, never an auto-merge."""
    assert vision.hamming(JCK_6328_PHASH, JCK_6331_PHASH) == 28
    member = AssetRef(
        gym_id=GYM, asset_id="jck_6328", phash=JCK_6328_PHASH,
        md5="md5-6328", sha256="sha-6328", drive_file_id="drive-6328",
    )
    candidate = AssetRef(
        gym_id=GYM, asset_id="jck_6331", phash=JCK_6331_PHASH,
        md5="md5-6331", sha256="sha-6331", drive_file_id="drive-6331",
    )
    assert candidate.md5 != member.md5  # distinct bytes, as in the incident
    groups = [hash_group(member)]
    result = classify_candidate(candidate, groups)
    assert result.classification is Classification.SCENE_MATCH_CANDIDATE
    assert result.distance == 28
    assert result.group_key == groups[0].group_key  # suspect recorded, not merged
    _, unchanged = assign_group(candidate, groups)
    assert unchanged is groups and len(groups[0].members) == 1  # still held


def test_swift_river_shape_synthetic_scene_band_holds_until_operator_confirmation():
    """SYNTHETIC shape test: distances 18/24/30 are constructed, not measured;
    only the JCK pair (28) is real measured evidence."""
    assets = swift_river_scene_band_assets()
    groups = build_groups([assets[0]])
    for later in assets[1:]:
        result, groups_after = assign_group(later, groups)
        assert result.classification is Classification.SCENE_MATCH_CANDIDATE
        assert result.distance in (18, 24, 30)
        assert result.group_key == groups[0].group_key
        assert groups_after is groups and len(groups[0].members) == 1  # hold
    # sanctioned resolution: operator confirms each held sibling into the
    # suspect group -> ONE group, key unchanged, corroboration audited
    key_before = groups[0].group_key
    for a in assets[1:]:
        result, groups = confirm_scene_match(
            a, groups, key_before, note="op: visual review 2026-10-02"
        )
        assert result.classification is Classification.SAME_GROUP
        assert result.group_key == key_before
    assert groups[0].group_key == key_before  # frozen through growth
    assert {m.asset_id for m in groups[0].members} == {a.asset_id for a in assets}
    corroboration = [n for n in groups[0].audit if "hamming" in n]
    assert len(corroboration) == 3  # phashes + distance recorded per confirm


def test_exact_byte_repeat_across_days_is_same_group():
    data = scene_bytes(11)
    day1 = AssetRef.from_bytes(
        gym_id=GYM, asset_id="oct7_feed", data=data, drive_file_id="d-a"
    )
    day2 = AssetRef.from_bytes(
        gym_id=GYM, asset_id="oct8_story", data=data, drive_file_id="d-b"
    )
    assert day1.md5 == day2.md5 and day1.drive_file_id != day2.drive_file_id
    groups = build_groups([day1])
    result = classify_candidate(day2, groups)
    assert result.classification is Classification.SAME_GROUP
    assert result.distance == 0
    assert "exact bytes" in result.reason


def test_near_frame_variants_of_one_frame_are_same_group():
    variants = [
        scene_bytes(7),
        scene_bytes(7, noise=6),
        scene_bytes(7, scale=128),
        scene_bytes(7, fmt="JPEG"),
    ]
    assets = [
        AssetRef.from_bytes(
            gym_id=GYM, asset_id=f"v{i}", data=data, drive_file_id=f"df-{i}"
        )
        for i, data in enumerate(variants)
    ]
    assert len({a.md5 for a in assets}) == 4  # distinct bytes/md5s, still one group
    groups = build_groups(assets)
    assert len(groups) == 1
    for later in assets[1:]:
        assert (
            classify_candidate(later, groups).classification
            is Classification.SAME_GROUP
        )


def test_genuinely_different_scenes_get_different_groups():
    assets = [
        AssetRef.from_bytes(gym_id=GYM, asset_id=f"a{s}", data=scene_bytes(s))
        for s in (20, 22, 23, 26)  # verified pairwise hamming 32-38
    ]
    groups = build_groups(assets)
    assert len(groups) == 4
    assert len({g.group_key for g in groups}) == 4


def test_tier_boundaries_synthetic_phashes():
    base = int(vision.dct_phash(scene_bytes(51)), 16)
    member = AssetRef(
        gym_id=GYM, asset_id="base", phash=f"{base:016x}", sha256="s-base"
    )
    groups = [hash_group(member)]
    cases = [
        (PHASH_NEAR_DUPLICATE_THRESHOLD - 1, Classification.SAME_GROUP),
        (PHASH_NEAR_DUPLICATE_THRESHOLD, Classification.SAME_GROUP),  # inclusive
        (PHASH_SCENE_MATCH_MIN, Classification.SCENE_MATCH_CANDIDATE),  # 7
        (18, Classification.SCENE_MATCH_CANDIDATE),
        (30, Classification.SCENE_MATCH_CANDIDATE),
        (PHASH_SCENE_MATCH_MAX, Classification.SCENE_MATCH_CANDIDATE),  # inclusive
        (PHASH_SCENE_MATCH_MAX + 1, Classification.NEW_GROUP),  # 31
    ]
    for distance, expected in cases:
        result = classify_candidate(asset_at_distance(base, distance, f"d{distance}"), groups)
        assert result.classification is expected, f"distance {distance}"
        if expected is Classification.SCENE_MATCH_CANDIDATE:
            assert result.group_key == groups[0].group_key


# ---------------------------------------------------------------------------
# Immutable group keys (defect 1)
# ---------------------------------------------------------------------------
def test_group_key_frozen_when_smaller_identity_member_appended():
    """Regression: previously the key derived from the smallest member identity,
    so appending a smaller-sha member silently changed it. Now frozen."""
    founder = AssetRef(
        gym_id=GYM, asset_id="founder",
        phash=f"{0xAAAA:016x}", sha256="f" * 64,  # large identity
    )
    groups = build_groups([founder])
    key_at_creation = groups[0].group_key
    assert key_at_creation == f"{GYM}::h-{'f' * 64}"
    smaller = AssetRef(
        gym_id=GYM, asset_id="smaller",
        phash=f"{0xAAAA:016x}", sha256="0" * 64,  # lexicographically smaller
    )
    result, groups = assign_group(smaller, groups)
    assert result.classification is Classification.SAME_GROUP
    assert len(groups[0].members) == 2
    assert groups[0].group_key == key_at_creation  # UNCHANGED
    # canonical member is display-only and may reflect the smaller identity
    assert groups[0].canonical_member == "0" * 64
    assert groups[0].canonical_member not in groups[0].group_key


def test_group_keys_deterministic_per_construction_sequence():
    assets = [
        AssetRef.from_bytes(gym_id=GYM, asset_id=f"m{i}", data=scene_bytes(seed))
        for i, seed in enumerate((20, 22, 23, 26))
    ]
    keys_a = sorted(g.group_key for g in build_groups(list(assets)))
    keys_b = sorted(g.group_key for g in build_groups(list(assets)))
    assert keys_a == keys_b  # same sequence -> same ids
    for key in keys_a:
        assert key.startswith(f"{GYM}::h-")


def test_membership_partition_is_order_independent_even_if_founder_ids_differ():
    """Founding-derived ids legitimately depend on construction order (documented;
    persisted storage must store group_id explicitly). The PARTITION of assets
    into groups must not."""
    seeds = (20, 22, 23, 26)
    assets = [
        AssetRef.from_bytes(gym_id=GYM, asset_id=f"m{i}", data=scene_bytes(seed))
        for i, seed in enumerate(seeds)
    ]
    reference = sorted(
        tuple(sorted(m.asset_id for m in g.members)) for g in build_groups(list(assets))
    )
    for perm in itertools.islice(itertools.permutations(assets), 24):
        partition = sorted(
            tuple(sorted(m.asset_id for m in g.members)) for g in build_groups(list(perm))
        )
        assert partition == reference


def test_merge_groups_keeps_target_key_and_audits_absorbed_key():
    base = int(vision.dct_phash(scene_bytes(71)), 16)
    g1 = hash_group(
        AssetRef(gym_id=GYM, asset_id="m1", phash=f"{base:016x}", md5="x1")
    )
    g2 = hash_group(asset_at_distance(base, 4, "m2"))
    g2.members.append(asset_at_distance(base, 6, "m3"))
    groups = [g1, g2]
    target_key, absorbed_key = g1.group_key, g2.group_key
    merge_groups(groups, gym_id=GYM, into_key=target_key, from_key=absorbed_key)
    assert len(groups) == 1
    assert g1.group_key == target_key  # frozen
    assert {m.asset_id for m in g1.members} == {"m1", "m2", "m3"}
    assert any(absorbed_key in n for n in g1.audit)  # retired key recorded
    with pytest.raises(KeyError):
        merge_groups(groups, gym_id=GYM, into_key=target_key, from_key="nope")
    with pytest.raises(ValueError):
        merge_groups(groups, gym_id=GYM, into_key=target_key, from_key=target_key)


def test_group_key_requires_creation_id_unless_manual():
    with pytest.raises(ValueError):
        _ = VisualGroup(gym_id=GYM).group_key
    manual = VisualGroup(gym_id=GYM, manual_label="empty-yet")
    assert manual.group_key == manual_group_key(GYM, "empty-yet")
    hashed = VisualGroup(gym_id=GYM, group_id="h-abc")
    assert hashed.group_key == f"{GYM}::h-abc"


# ---------------------------------------------------------------------------
# Scene confirmation / rejection (defect 2)
# ---------------------------------------------------------------------------
def test_confirm_scene_match_moves_and_audits_evidence():
    base = int(vision.dct_phash(scene_bytes(81)), 16)
    member = AssetRef(gym_id=GYM, asset_id="base", phash=f"{base:016x}")
    groups = [hash_group(member)]
    held = asset_at_distance(base, 22, "held")
    held_key_before = groups[0].group_key
    assert (
        classify_candidate(held, groups).classification
        is Classification.SCENE_MATCH_CANDIDATE
    )
    result, groups = confirm_scene_match(
        held, groups, held_key_before, note="op: same Drive upload session"
    )
    assert result.classification is Classification.SAME_GROUP
    assert result.distance == 22
    assert result.matched_member_id == "base"
    assert groups[0].group_key == held_key_before
    assert held in groups[0].members
    entry = groups[0].audit[-1]
    assert held.phash in entry and member.phash in entry and "hamming 22" in entry
    assert "same Drive upload session" in entry
    # after confirmation the asset classifies SAME_GROUP on future passes
    assert (
        classify_candidate(held, groups).classification
        is Classification.SAME_GROUP
    )


def test_confirm_scene_match_validates_target():
    base = int(vision.dct_phash(scene_bytes(82)), 16)
    groups = [hash_group(AssetRef(gym_id=GYM, asset_id="base", phash=f"{base:016x}"))]
    held = asset_at_distance(base, 20, "held")
    with pytest.raises(KeyError):
        confirm_scene_match(held, groups, f"{GYM}::h-nonexistent")
    foreign = asset_at_distance(base, 20, "foreign", gym_id=OTHER_GYM)
    with pytest.raises(KeyError):  # target key doesn't exist for the foreign gym
        confirm_scene_match(foreign, groups, groups[0].group_key)


def test_reject_scene_match_stops_reholding():
    base = int(vision.dct_phash(scene_bytes(83)), 16)
    member = AssetRef(gym_id=GYM, asset_id="base", phash=f"{base:016x}")
    groups = [hash_group(member)]
    held = asset_at_distance(base, 20, "held")
    assert (
        classify_candidate(held, groups).classification
        is Classification.SCENE_MATCH_CANDIDATE
    )
    reject_scene_match(
        held, groups, groups[0].group_key, note="op: different class, different day"
    )
    result = classify_candidate(held, groups)
    assert result.classification is Classification.NEW_GROUP
    assert "rejected" in result.reason
    assert any("scene rejected" in n for n in groups[0].audit)
    # rejection keys on stable identity: same asset under a NEW asset id
    # (re-fetch) is still rejected
    refetch = AssetRef(
        gym_id=GYM, asset_id="held-refetched",
        phash=held.phash, md5=held.md5, sha256=held.sha256,
    )
    assert (
        classify_candidate(refetch, groups).classification
        is Classification.NEW_GROUP
    )
    # rejection of one suspect does not hide a different group
    other = hash_group(asset_at_distance(base ^ 0xFFFF, 3, "other-member"))
    groups.append(other)
    near_other = AssetRef(
        gym_id=GYM, asset_id="held2",
        phash=other.members[0].phash, sha256="sha-held2",
    )
    r2 = classify_candidate(near_other, groups)
    assert r2.classification is Classification.SAME_GROUP
    assert r2.group_key == other.group_key


def test_reject_then_confirm_clears_rejection():
    base = int(vision.dct_phash(scene_bytes(84)), 16)
    groups = [hash_group(AssetRef(gym_id=GYM, asset_id="base", phash=f"{base:016x}"))]
    held = asset_at_distance(base, 15, "held")
    reject_scene_match(held, groups, groups[0].group_key)
    assert held.stable_identity() in groups[0].rejections
    confirm_scene_match(held, groups, groups[0].group_key, note="op: recanted")
    assert held.stable_identity() not in groups[0].rejections
    assert (
        classify_candidate(held, groups).classification
        is Classification.SAME_GROUP
    )


# ---------------------------------------------------------------------------
# Manual groups, tenant isolation, fail-closed
# ---------------------------------------------------------------------------
def test_manual_group_overrides_hash_evidence():
    a = AssetRef.from_bytes(gym_id=GYM, asset_id="s1", data=scene_bytes(60))
    b = AssetRef.from_bytes(gym_id=GYM, asset_id="s2", data=scene_bytes(61))
    assert vision.hamming(a.phash, b.phash) > PHASH_SCENE_MATCH_MAX
    groups = build_groups([a, b])
    assert len(groups) == 2
    result, groups = assign_manual_group(
        b, groups, "confirmed-shoot", note="op: EXIF same device/session"
    )
    assert result.classification is Classification.SAME_GROUP
    assert result.group_key == manual_group_key(GYM, "confirmed-shoot")
    manual = _find_manual(groups)
    assert b in manual.members
    assert all(b not in g.members for g in groups if g is not manual)
    assign_manual_group(a, groups, "confirmed-shoot", note="op: same session")
    assert {m.asset_id for m in manual.members} == {"s1", "s2"}
    assert len(manual.audit) >= 2


def test_manual_group_label_normalization_and_key():
    assert manual_group_key(GYM, "scene-a") == f"{GYM}::manual:scene-a"
    assert manual_group_key(GYM, "manual:scene-a") == f"{GYM}::manual:scene-a"
    assert manual_group_key(OTHER_GYM, "scene-a") != manual_group_key(GYM, "scene-a")
    with pytest.raises(ValueError):
        manual_group_key(GYM, "   ")


def test_cross_gym_manual_label_isolation():
    a = AssetRef(gym_id=GYM, asset_id="a", phash="0" * 16)
    b = AssetRef(gym_id=OTHER_GYM, asset_id="b", phash="0" * 16)
    groups: list[VisualGroup] = []
    assign_manual_group(a, groups, "launch", note="op-a")
    assign_manual_group(b, groups, "launch", note="op-b")
    assert len(groups) == 2
    assert {g.group_key for g in groups} == {
        manual_group_key(GYM, "launch"),
        manual_group_key(OTHER_GYM, "launch"),
    }
    assert (
        classify_candidate(b, [groups[0]]).classification
        is Classification.NEW_GROUP
    )


def test_unknown_identity_fails_closed_and_creates_nothing():
    groups = build_groups(
        [AssetRef.from_bytes(gym_id=GYM, asset_id="known", data=scene_bytes(41))]
    )
    before_keys = [g.group_key for g in groups]
    mystery = AssetRef(gym_id=GYM, asset_id="no-bytes-no-phash")
    result, updated = assign_group(mystery, groups)
    assert result.classification is Classification.UNKNOWN_IDENTITY
    assert result.group_key is None
    assert updated is groups
    assert [g.group_key for g in groups] == before_keys
    empty = []
    result2, empty_after = assign_group(mystery, empty)
    assert result2.classification is Classification.UNKNOWN_IDENTITY
    assert empty_after == []
    garbage = AssetRef.from_bytes(
        gym_id=GYM, asset_id="garbage", data=b"this is not an image"
    )
    assert garbage.phash is None
    assert (
        classify_candidate(garbage, groups).classification
        is Classification.UNKNOWN_IDENTITY
    )


def test_tenant_isolation_same_visual_other_gym_is_new_group():
    data = scene_bytes(91)
    mine = AssetRef.from_bytes(gym_id=GYM, asset_id="mine", data=data)
    theirs = AssetRef.from_bytes(gym_id=OTHER_GYM, asset_id="theirs", data=data)
    groups = build_groups([mine])
    assert classify_candidate(theirs, groups).classification is Classification.NEW_GROUP
    _, groups2 = assign_group(theirs, groups)
    assert {g.gym_id for g in groups2} == {GYM, OTHER_GYM}
    assert len({g.group_key for g in groups2}) == 2


def test_group_key_for_returns_none_for_scene_candidate_and_unknown():
    base = int(vision.dct_phash(scene_bytes(101)), 16)
    member = AssetRef(gym_id=GYM, asset_id="base", phash=f"{base:016x}")
    groups = [hash_group(member)]
    near = asset_at_distance(base, 2, "near")
    scene = asset_at_distance(base, 20, "scene")
    unknown = AssetRef(gym_id=GYM, asset_id="unknown")
    assert group_key_for(near, groups) == groups[0].group_key
    assert group_key_for(scene, groups) is None  # suspected != member
    assert group_key_for(unknown, groups) is None


def test_byte_equality_wins_over_divergent_phash_and_rejection():
    data = scene_bytes(111)
    member = AssetRef.from_bytes(gym_id=GYM, asset_id="orig", data=data)
    twin = AssetRef(
        gym_id=GYM,
        asset_id="twin",
        phash="f" * 16,
        md5=member.md5,
        sha256=member.sha256,
        drive_file_id="d-twin",
    )
    groups = [hash_group(member)]
    # even a prior scene rejection does not override byte-level proof
    reject_scene_match(twin, groups, groups[0].group_key)
    result = classify_candidate(twin, groups)
    assert result.classification is Classification.SAME_GROUP
    assert result.distance == 0


def test_nearest_group_wins_deterministically_when_multiple_match():
    base = int(vision.dct_phash(scene_bytes(121)), 16)
    g1 = hash_group(asset_at_distance(base, 2, "near"))
    g2 = hash_group(asset_at_distance(base, 4, "far"))
    candidate = AssetRef(gym_id=GYM, asset_id="cand", phash=f"{base:016x}")
    for order in ([g1, g2], [g2, g1]):
        result = classify_candidate(candidate, order)
        assert result.classification is Classification.SAME_GROUP
        assert result.matched_member_id == "near"
        assert result.distance == 2


def _find_manual(groups):
    return next(g for g in groups if g.manual_label)
