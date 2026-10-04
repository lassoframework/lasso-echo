"""scene_claim_wave: hamming band boundaries via agent/visual_scene.py.

The claim-wave SQL and writers must classify with these exact bands (the
pure module is the shared authority; its semantics do not change):

* hamming <= 6  -> near_frame: BLOCKS reuse (same-tenant different-date and
  cross-tenant); only same-tenant same-date siblings of the same group are
  legal, and that policy lives in the claim transaction, not here.
* 7..30         -> scene_candidate: durable reviewable HOLD, cannot silently
  pass, never an approval.
* > 30          -> distinct.
* unknown / no evidence -> fail closed, NEVER distinct.

All hashes below are synthetic 64-bit patterns chosen for exact hamming
distances; no image decode is involved.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import visual_scene  # noqa: E402

BASE = "scene:phash64:" + "0" * 16


def _flip(bare_hex, bit):
    """Flip one bit of a bare 16-hex hash."""
    value = int(bare_hex, 16) ^ (1 << bit)
    return f"{value:016x}"


def _at_distance(distance):
    bare = "0" * 16
    for bit in range(distance):
        bare = _flip(bare, bit)
    return "scene:phash64:" + bare


@pytest.mark.parametrize("distance", [0, 1, 5, 6])
def test_near_band_up_to_six_blocks(distance):
    c = visual_scene.classify_scene_all(BASE, [_at_distance(distance)])
    assert c.kind == visual_scene.NEAR_FRAME
    assert c.distance == distance
    assert len(c.near_matches) == 1
    assert c.candidate_matches == ()


@pytest.mark.parametrize("distance", [7, 8, 28, 30])
def test_uncertain_band_seven_to_thirty_holds(distance):
    c = visual_scene.classify_scene_all(BASE, [_at_distance(distance)])
    assert c.kind == visual_scene.SCENE_CANDIDATE
    assert c.distance == distance
    assert c.near_matches == ()
    assert len(c.candidate_matches) == 1


@pytest.mark.parametrize("distance", [31, 32, 48, 64])
def test_above_thirty_is_distinct(distance):
    c = visual_scene.classify_scene_all(BASE, [_at_distance(distance)])
    assert c.kind == visual_scene.DISTINCT
    assert c.near_matches == ()
    assert c.candidate_matches == ()
    assert c.matched is None


def test_unknown_candidate_fails_closed_never_distinct():
    for candidate in (None, "", "phash", "scene:phash64:zzz", 123):
        c = visual_scene.classify_scene_all(candidate, [_at_distance(0)])
        assert c.kind == visual_scene.UNKNOWN
        assert c.distance == 999


def test_no_usable_knowns_fails_closed_never_distinct():
    c = visual_scene.classify_scene_all(BASE, [None, "garbage", 42])
    assert c.kind == visual_scene.UNKNOWN


def test_empty_known_set_fails_closed_never_distinct():
    c = visual_scene.classify_scene_all(BASE, [])
    assert c.kind == visual_scene.UNKNOWN


def test_worst_case_band_wins_and_reports_every_match():
    """A review can never hide behind the single nearest match: every <=6 and
    every 7..30 match is reported, sorted by (distance, phash)."""
    near2 = _at_distance(2)
    near6 = _at_distance(6)
    cand7 = _at_distance(7)
    cand28 = _at_distance(28)
    far = _at_distance(40)
    c = visual_scene.classify_scene_all(BASE, [cand28, far, near6, cand7, near2])
    assert c.kind == visual_scene.NEAR_FRAME
    assert {m[1] for m in c.near_matches} == {2, 6}
    assert {m[1] for m in c.candidate_matches} == {7, 28}
    assert c.distance == 2
    assert all(m[1] != 40 for m in c.near_matches + c.candidate_matches)


def test_candidate_band_worst_case_when_no_near_match():
    c = visual_scene.classify_scene_all(BASE, [_at_distance(40), _at_distance(9)])
    assert c.kind == visual_scene.SCENE_CANDIDATE
    assert c.distance == 9


def test_classify_scene_nearest_only_backward_compat():
    c = visual_scene.classify_scene(BASE, [_at_distance(6), _at_distance(7)])
    assert c.kind == visual_scene.NEAR_FRAME
    assert c.distance == 6


def test_hamming_distance_malformed_is_999_no_evidence():
    assert visual_scene.hamming_distance("junk", BASE) == 999
    assert visual_scene.hamming_distance(BASE, None) == 999
    assert visual_scene.hamming_distance(BASE, _at_distance(6)) == 6
    # bare hashes are accepted as evidence on both sides
    assert visual_scene.hamming_distance("0" * 16, _flip("0" * 16, 3)) == 1


def test_band_constants_match_the_documented_policy():
    assert visual_scene.SCENE_NEAR_MAX == 6
    assert visual_scene.SCENE_CANDIDATE_MIN == 7
    assert visual_scene.SCENE_CANDIDATE_MAX == 30
