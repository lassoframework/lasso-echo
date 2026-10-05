"""Unit tests for agent.visual_scene plus the DRAFT scene-pHash migration's
static release boundary.

The image fixtures are architecturally robust (see commits 91276a5/56e6ecc for
the prior near-flat/median-boundary and resize-interpolation flakes): the base
scene is a high-structure, high-contrast composition at 128x128, and every
assertion uses a wide band — same-scene variants must land in
near_frame/scene_candidate (hamming <= 30) with measured distances <= 22 on
this host, and the different scene must land in distinct (hamming > 30) with a
measured 42-44 — so platform interpolation rounding of a few bits cannot flip
a result.
"""
import io
import math
import re
from pathlib import Path

import pytest

from agent import visual_scene as vs

MIGRATION = (Path(__file__).resolve().parent.parent
             / "migrations" / "DRAFT_visual_scene_phash_20261003.sql")

FP_RE = re.compile(r"^scene:phash64:[0-9a-f]{16}$")


# --------------------------------------------------------------------------
# Synthetic scenes (high structure, far from the low-variance flat guard and
# far from the DCT median boundary).
# --------------------------------------------------------------------------

def _png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _scene_a(size=128):
    """Bright block + circle + diagonal stripes on a dark field."""
    Image = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw
    img = Image.new("L", (size, size), 30)
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 70, 60], fill=220)
    d.ellipse([70, 60, 120, 110], fill=200)
    d.rectangle([80, 10, 118, 40], fill=90)
    for i in range(0, 60, 8):
        d.line([(10 + i, 118), (10, 118 - i)], fill=180, width=3)
    return img


def _scene_b(size=128):
    """Half/half wedges — measured hamming 42-44 from scene A and from its
    resize/crop variants, a wide margin above the distinct boundary of 30."""
    Image = pytest.importorskip("PIL.Image")
    img = Image.new("L", (size, size))
    c = size / 2
    img.putdata([240 if int((math.atan2(y - c, x - c) + math.pi) / (math.pi / 2)) % 2
                 else 10
                 for y in range(size) for x in range(size)])
    return img


@pytest.fixture(scope="module")
def scene_a_bytes():
    return _png(_scene_a())


@pytest.fixture(scope="module")
def scene_a_fp(scene_a_bytes):
    fp = vs.scene_fingerprint(scene_a_bytes)
    assert fp is not None
    return fp


# --------------------------------------------------------------------------
# scene_fingerprint
# --------------------------------------------------------------------------

def test_fingerprint_none_on_empty_and_non_image_bytes():
    assert vs.scene_fingerprint(b"") is None
    assert vs.scene_fingerprint(b"not an image") is None
    assert vs.scene_fingerprint(None) is None
    assert vs.scene_fingerprint("scene:phash64:" + "0" * 16) is None
    assert vs.scene_fingerprint(12345) is None


def test_fingerprint_real_image_is_namespaced_and_deterministic(scene_a_bytes):
    fp = vs.scene_fingerprint(scene_a_bytes)
    assert FP_RE.fullmatch(fp)
    assert vs.scene_fingerprint(scene_a_bytes) == fp


# --------------------------------------------------------------------------
# normalize_scene strictness (mirrors visual_fingerprint.normalize)
# --------------------------------------------------------------------------

def test_normalize_accepts_only_namespaced_well_formed():
    good = "scene:phash64:" + "a1" * 8
    assert vs.normalize_scene(good) == good
    assert vs.normalize_scene("  " + good.upper() + " ") == good  # canonicalized


@pytest.mark.parametrize("bad", [
    None, 42, "", "   ",
    "a1" * 8,                                  # bare hash rejected
    "phash64:" + "a1" * 8,                     # missing scene: namespace
    "scene:" + "a1" * 8,                       # missing phash64
    "scene:phash64:" + "a1" * 7,               # too short
    "scene:phash64:" + "a1" * 9,               # too long
    "scene:phash64:" + "g1" * 8,               # non-hex
    "md5:" + "0" * 32,                         # wrong evidence family
])
def test_normalize_rejects_everything_else(bad):
    assert vs.normalize_scene(bad) is None


# --------------------------------------------------------------------------
# hamming_distance
# --------------------------------------------------------------------------

def test_hamming_namespaced_and_bare_equivalent():
    bare = "f" * 16
    assert vs.hamming_distance(bare, bare) == 0
    assert vs.hamming_distance("scene:phash64:" + bare, bare) == 0
    assert vs.hamming_distance("scene:phash64:" + "0" * 16, bare) == 64


@pytest.mark.parametrize("a,b", [
    ("not-a-hash", "0" * 16),
    ("scene:phash64:xyz", "0" * 16),
    ("0" * 16, None),
    (None, None),
    ("scene:phash64:" + "0" * 15, "scene:phash64:" + "0" * 16),
])
def test_hamming_malformed_is_999_never_a_match(a, b):
    assert vs.hamming_distance(a, b) == 999


# --------------------------------------------------------------------------
# classify_scene bands on real image evidence
# --------------------------------------------------------------------------

def test_resized_and_cropped_variants_never_classify_distinct(scene_a_fp):
    base = _scene_a()
    variants = {
        "resize_up": base.resize((200, 200)),
        "resize_down": base.resize((48, 48)),
        "center_crop_112": base.crop((8, 8, 120, 120)),
    }
    for name, img in variants.items():
        fp = vs.scene_fingerprint(_png(img))
        match = vs.classify_scene(fp, [scene_a_fp])
        assert match.kind in (vs.NEAR_FRAME, vs.SCENE_CANDIDATE), (
            f"{name} variant of the same scene must cluster, got "
            f"{match.kind} at {match.distance}")
        assert match.distance <= 30
        assert match.matched == scene_a_fp


def test_exact_resize_classifies_near_frame(scene_a_fp):
    fp = vs.scene_fingerprint(_png(_scene_a().resize((200, 200))))
    match = vs.classify_scene(fp, [scene_a_fp])
    assert match.kind == vs.NEAR_FRAME
    assert match.distance <= vs.SCENE_NEAR_MAX


def test_clearly_different_scene_classifies_distinct(scene_a_fp):
    fp = vs.scene_fingerprint(_png(_scene_b()))
    match = vs.classify_scene(fp, [scene_a_fp])
    assert match.kind == vs.DISTINCT
    assert match.distance > vs.SCENE_CANDIDATE_MAX
    assert match.matched is None


def test_transformed_different_scene_still_distinct():
    """A crop/resize of scene B vs scene A must not cluster either."""
    a_fp = vs.scene_fingerprint(_png(_scene_a()))
    b_cropped = vs.scene_fingerprint(_png(_scene_b().crop((8, 8, 120, 120))))
    match = vs.classify_scene(b_cropped, [a_fp])
    assert match.kind == vs.DISTINCT


# --------------------------------------------------------------------------
# UNKNOWN is never DISTINCT (fail closed)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("candidate", [
    None, "", "garbage",
    "scene:phash64:" + "0" * 15,
])
def test_unknown_candidate_is_not_distinct(candidate):
    match = vs.classify_scene(candidate, ["scene:phash64:" + "f" * 16])
    assert match.kind == vs.UNKNOWN
    assert match.kind != vs.DISTINCT
    assert match.matched is None


def test_bare_candidate_hash_is_usable_evidence():
    # _bare_hash accepts bare 16-hex on both sides; strict namespacing is
    # enforced by normalize_scene for stored evidence, not by the comparator.
    match = vs.classify_scene("0" * 16, ["scene:phash64:" + "0" * 14 + "03"])
    assert match.kind == vs.NEAR_FRAME
    assert match.distance == 2


def test_all_known_hashes_unusable_is_unknown_not_distinct(scene_a_fp):
    match = vs.classify_scene(scene_a_fp, ["junk", None, "scene:phash64:zz"])
    assert match.kind == vs.UNKNOWN
    assert match.kind != vs.DISTINCT


def test_empty_known_is_distinct_only_with_a_usable_candidate(scene_a_fp):
    # No evidence against = distinct; the empty-ledger fail-open distinction is
    # the selector's provenance concern (proven-empty table), not the
    # classifier's.
    match = vs.classify_scene(scene_a_fp, [])
    assert match.kind == vs.UNKNOWN or match.kind == vs.DISTINCT
    assert match.kind != vs.NEAR_FRAME
    assert match.kind != vs.SCENE_CANDIDATE


def test_matched_is_the_min_distance_known_hash():
    near = "scene:phash64:" + "0" * 14 + "03"   # hamming 2
    far = "scene:phash64:" + "f" * 16           # hamming 64
    match = vs.classify_scene("scene:phash64:" + "0" * 16, [far, near])
    assert match.kind == vs.NEAR_FRAME
    assert match.matched == near
    assert match.distance == 2


# --------------------------------------------------------------------------
# DRAFT migration static release boundary
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def migration_sql():
    # migrations/ is owned by the parent milestone; until the DRAFT scene
    # migration lands there, the static release-boundary tests skip instead
    # of erroring.
    if not MIGRATION.exists():
        pytest.skip(f"{MIGRATION.name} not present (parent-owned scope)")
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_is_marked_draft_and_unapplied(migration_sql):
    assert MIGRATION.name.startswith("DRAFT_")
    head = migration_sql[:600].lower()
    assert "draft" in head and "unapplied" in head


def test_migration_is_additive_only(migration_sql):
    sql = migration_sql.lower()
    assert "create table if not exists public.visual_scene_phash" in sql
    assert "alter table" not in sql.replace(
        "alter table public.visual_scene_phash enable row level security", "")
    assert "drop table" not in sql
    # The frozen md5-keyed exact-byte ledger is untouched.
    assert "visual_global_usage" not in re.sub(r"--[^\n]*", "", sql)


def test_migration_never_writes_scene_links(migration_sql):
    sql = re.sub(r"--[^\n]*", "", migration_sql).lower()
    # No DML or call path to the human-only scene-link machinery (the words
    # may appear in comments as a prohibition, never as an operation).
    assert not re.search(r"\b(insert\s+into|update|delete\s+from)\s+\S*visual_group_scene_link", sql)
    assert not re.search(r"\b(perform|select)\s+\S*visual_group_link_scene", sql)
    # NO working write path exists: both record functions are the REJECTED
    # prep-time architecture and their bodies only RAISE 0A000. There is no
    # executable INSERT anywhere in the sketch.
    assert not re.search(r"\binsert\s+into\b", sql)
    for fn in ("visual_scene_record_use(", "visual_scene_record_use_tx("):
        body = sql.split(f"create or replace function public.{fn}", 1)[1]
        body = body.split("$$", 1)[1]
        assert re.search(r"raise exception .* using errcode\s*=\s*'0a000'", body)
        assert "rejected prep-time write path" in body
    # The table sketch keeps its NOT NULL used_date and date-dimension index.
    assert re.search(r"used_date\s+date\s+not\s+null", sql)
    assert "visual_scene_phash_tenant_date_idx" in sql
    # Table grants stay read-only; EXECUTE on record_use to service_role only;
    # record_use_tx is granted to NO role (revoked from everyone incl.
    # service_role). All moot while unapplied; pinned so a redesign starts
    # from this shape.
    assert re.search(r"grant\s+select\s+on\s+public\.visual_scene_phash\s+to\s+service_role", sql)
    assert not re.search(r"grant\s+(insert|update|delete)", sql)
    assert re.search(r"grant\s+execute\s+on\s+function\s+public\.visual_scene_record_use\("
                     r"text,uuid,text,date,jsonb\)\s+to\s+service_role", sql)
    assert not re.search(r"grant\s+execute\s+on\s+function\s+public\.visual_scene_record_use_tx", sql)
    assert re.search(r"revoke\s+all\s+on\s+function\s+public\.visual_scene_record_use_tx\("
                     r"text,uuid,text,date,jsonb\)\s+from\s+public,anon,authenticated,service_role", sql)
    assert not re.search(r"grant\s+execute.*\bto\s+(public|anon|authenticated)\b", sql)


def test_migration_is_an_incomplete_do_not_apply_sketch(migration_sql):
    head = migration_sql[:2000].lower()
    assert "incomplete" in head
    assert "do not apply" in head and "do not activate" in head
    # The STATUS section lists the required redesign before anything here may
    # ever be applied or activated.
    assert "status: incomplete" in head
    for item in ("staging", "claim transaction", "review-hold",
                 "server-side", "backfill"):
        assert item in head, f"redesign item {item!r} missing from STATUS"
    # Rollback is trivial: nothing is applied anywhere.
    assert "delete the file" in migration_sql.lower()


def test_global_history_migration_carries_no_scene_params():
    """The exact-byte global ledger migration is reverted to base: the prepare
    RPCs have their original signatures and no p_scene_* surface at all."""
    sql = (MIGRATION.parent / "DRAFT_visual_global_history_20261002.sql") \
        .read_text(encoding="utf-8").lower()
    assert "p_scene_phash" not in sql
    assert "visual_scene_record_use" not in sql
    assert "visual_scene_phash" not in sql


# --------------------------------------------------------------------------
# classify_scene_all: EVERY match, never masked by the nearest
# --------------------------------------------------------------------------

def test_classify_scene_all_reports_every_near_match_sorted():
    zero = "scene:phash64:" + "0" * 16
    one_bit = "scene:phash64:" + "0" * 15 + "1"     # hamming 1
    two_bit = "scene:phash64:" + "0" * 14 + "03"    # hamming 2
    far = "scene:phash64:" + "f" * 16               # hamming 64, not reported
    c = vs.classify_scene_all(zero, [far, two_bit, one_bit])
    assert c.kind == vs.NEAR_FRAME
    assert c.matched == one_bit and c.distance == 1
    assert c.near_matches == ((one_bit, 1), (two_bit, 2))  # (distance, phash) sort
    assert c.candidate_matches == ()


def test_classify_scene_all_worst_case_kind_wins_and_bands_stay_complete():
    zero = "scene:phash64:" + "0" * 16
    near = "scene:phash64:" + "0" * 15 + "1"        # hamming 1
    cand_a = "scene:phash64:" + "0" * 14 + "ff"     # hamming 8
    cand_b = "scene:phash64:" + "0" * 12 + "ffff"   # hamming 16
    c = vs.classify_scene_all(zero, [cand_b, near, cand_a])
    # A near match exists, so the worst-case kind is NEAR_FRAME even though
    # candidate-band matches are also present — and BOTH are still reported.
    assert c.kind == vs.NEAR_FRAME
    assert c.near_matches == ((near, 1),)
    assert c.candidate_matches == ((cand_a, 8), (cand_b, 16))


def test_classify_scene_all_candidate_only_band():
    zero = "scene:phash64:" + "0" * 16
    cand = "scene:phash64:" + "0" * 14 + "ff"       # hamming 8
    c = vs.classify_scene_all(zero, [cand])
    assert c.kind == vs.SCENE_CANDIDATE
    assert c.matched == cand and c.distance == 8
    assert c.near_matches == ()
    assert c.candidate_matches == ((cand, 8),)


def test_classify_scene_all_unknown_never_distinct():
    c = vs.classify_scene_all(None, ["scene:phash64:" + "f" * 16])
    assert c.kind == vs.UNKNOWN and c.kind != vs.DISTINCT
    assert c.near_matches == () and c.candidate_matches == ()
    assert c.matched is None and c.distance == 999
    # All known unusable: UNKNOWN too.
    c2 = vs.classify_scene_all("scene:phash64:" + "0" * 16, ["junk"])
    assert c2.kind == vs.UNKNOWN


def test_classify_scene_remains_the_nearest_only_wrapper():
    zero = "scene:phash64:" + "0" * 16
    one_bit = "scene:phash64:" + "0" * 15 + "1"
    two_bit = "scene:phash64:" + "0" * 14 + "03"
    m = vs.classify_scene(zero, [two_bit, one_bit])
    assert (m.kind, m.distance, m.matched) == (vs.NEAR_FRAME, 1, one_bit)
    assert not hasattr(m, "near_matches")
