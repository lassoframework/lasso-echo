"""tests/test_playbook_bounds.py — Wave 7.5 bounds, non-negotiable.

Required by spec:
  - the optimizer cannot lower a quota floor (PROTECTED_KEYS refused)
  - it cannot touch avatar / ask / consent / copy-gate keys
  - it cannot exceed plus or minus 20% drift (clamped)
  - versions increment; old versions are immutable
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import copy

import pytest

from agent import playbook as pb


class FakeStore:
    """Insert-only, like the real one: no update or delete method exists."""

    def __init__(self, rows=None):
        self.rows = [copy.deepcopy(r) for r in (rows or [])]
        self.inserts = []

    def latest(self, gym_id):
        rows = [r for r in self.rows if r["gym_id"] == gym_id]
        return copy.deepcopy(max(rows, key=lambda r: r["version"])) if rows else None

    def insert_version(self, row):
        self.rows.append(copy.deepcopy(row))
        self.inserts.append(copy.deepcopy(row))
        return row


_EVIDENCE = ["instagram:p1:d7", "instagram:p2:d7"]


# ---------------------------------------------------------------------------
# protected keys refused
# ---------------------------------------------------------------------------

def test_optimizer_cannot_lower_a_quota_floor():
    bounded, refused = pb.apply_bounds({}, {"quota_floors": {"proof": 0}})
    assert "quota_floors" not in bounded
    assert "quota_floors" in refused


@pytest.mark.parametrize("key", [
    "avatar_rail", "avatar", "ask_rules", "ask_rule", "consent_rules",
    "consent", "copy_gate", "offer_rules", "category_floors", "floors",
    "approval_gate", "publish_gate", "auto_approve",
])
def test_protected_keys_refused(key):
    bounded, refused = pb.apply_bounds({}, {key: "anything"})
    assert key not in bounded
    assert key in refused


def test_protected_key_nested_inside_a_weight_dict_is_refused():
    bounded, refused = pb.apply_bounds(
        {}, {"pillar_weights": {"community": 1.0, "quota_floor_proof": 0.0}})
    assert bounded["pillar_weights"] == {"community": 1.0}
    assert "pillar_weights.quota_floor_proof" in refused


def test_unknown_keys_are_dropped_not_smuggled():
    bounded, refused = pb.apply_bounds({}, {"brand_new_rail_bypass": 1})
    assert bounded == {}
    assert "brand_new_rail_bypass" in refused


def test_propose_update_with_only_protected_changes_writes_nothing():
    store = FakeStore()
    result = pb.propose_update("gym1", {"consent_rules": "off"},
                               _EVIDENCE, store=store)
    assert result["wrote"] is False
    assert "consent_rules" in result["refused"]
    assert store.inserts == []


# ---------------------------------------------------------------------------
# drift cap: plus or minus 20% per weight per month, clamped
# ---------------------------------------------------------------------------

def test_drift_cap_clamps_up_and_down():
    current = {"hook_family_weights": {"question": 1.0, "story_open": 1.0}}
    proposed = {"hook_family_weights": {"question": 3.0, "story_open": 0.01}}
    bounded, _ = pb.apply_bounds(current, proposed)
    assert bounded["hook_family_weights"]["question"] == pytest.approx(1.2)
    assert bounded["hook_family_weights"]["story_open"] == pytest.approx(0.8)


def test_drift_cap_via_propose_update():
    store = FakeStore(rows=[{
        "gym_id": "gym1", "version": 1, "updated_by": "monthly_retro",
        "playbook": {"hook_family_weights": {"question": 1.0}},
        "evidence": _EVIDENCE}])
    result = pb.propose_update(
        "gym1", {"hook_family_weights": {"question": 9.0}}, _EVIDENCE, store=store)
    assert result["wrote"] is True
    assert result["playbook"]["hook_family_weights"]["question"] == pytest.approx(1.2)


def test_new_weight_seeds_without_a_drift_baseline():
    bounded, _ = pb.apply_bounds({}, {"hook_family_weights": {"question": 0.7}})
    assert bounded["hook_family_weights"]["question"] == 0.7


# ---------------------------------------------------------------------------
# versioning: increments, insert-only, old versions immutable
# ---------------------------------------------------------------------------

def test_version_increments_and_old_versions_are_immutable():
    v1_playbook = {"hook_family_weights": {"question": 1.0}}
    store = FakeStore(rows=[{
        "gym_id": "gym1", "version": 1, "updated_by": "monthly_retro",
        "playbook": copy.deepcopy(v1_playbook), "evidence": _EVIDENCE}])
    before = copy.deepcopy(store.rows)

    result = pb.propose_update(
        "gym1", {"hook_family_weights": {"question": 1.15}}, _EVIDENCE, store=store)
    assert result["wrote"] is True
    assert result["version"] == 2
    inserted = store.inserts[0]
    assert inserted["version"] == 2
    assert inserted["updated_by"] == "monthly_retro"
    assert inserted["evidence"] == _EVIDENCE
    # the old version row is byte-identical to before the write
    assert store.rows[0] == before[0]
    assert store.rows[0]["playbook"] == v1_playbook
    # and a further update lands version 3
    result3 = pb.propose_update(
        "gym1", {"hook_family_weights": {"question": 1.3}}, _EVIDENCE, store=store)
    assert result3["version"] == 3


def test_no_op_change_writes_no_new_version():
    store = FakeStore(rows=[{
        "gym_id": "gym1", "version": 4, "updated_by": "monthly_retro",
        "playbook": {"hook_family_weights": {"question": 1.0}},
        "evidence": _EVIDENCE}])
    result = pb.propose_update(
        "gym1", {"hook_family_weights": {"question": 1.0}}, _EVIDENCE, store=store)
    assert result["wrote"] is False
    assert result["version"] == 4
    assert store.inserts == []


def test_write_without_evidence_is_refused():
    store = FakeStore()
    with pytest.raises(pb.PlaybookRefused):
        pb.propose_update("gym1", {"hook_family_weights": {"question": 1.1}},
                          [], store=store)
    assert store.inserts == []


def test_load_playbook_empty_default_and_store_failure_degrades():
    assert pb.load_playbook("newgym", store=FakeStore()) == pb.EMPTY_PLAYBOOK

    class Boom:
        def latest(self, gym_id):
            raise RuntimeError("down")

    assert pb.load_playbook("gym1", store=Boom()) == pb.EMPTY_PLAYBOOK


# ---------------------------------------------------------------------------
# priors: two jobs only; own evidence wins
# ---------------------------------------------------------------------------

def test_priors_exclude_tainted_gyms_and_stay_anonymous():
    priors = pb.compute_priors([
        {"gym_id": "clean", "tainted": False, "lever_scores": {
            "hook_family": {"question": {"n": 10, "mean_score": 2.0}}}},
        {"gym_id": "dirty", "tainted": True, "lever_scores": {
            "hook_family": {"question": {"n": 100, "mean_score": 99.0}}}},
    ])
    assert priors["hook_family"]["question"]["n"] == 10
    assert priors["hook_family"]["question"]["mean_score"] == 2.0
    assert "clean" not in str(priors)  # anonymous


def test_own_evidence_above_floor_beats_the_prior():
    own = {"question": {"n": 8, "mean_score": 1.0},
           "story_open": {"n": 7, "mean_score": 2.0}}
    priors = {"question": {"n": 500, "mean_score": 9.0}}
    assert pb.break_tie(own, priors) == "story_open"


def test_prior_breaks_tie_only_under_the_sample_floor():
    own = {"question": {"n": 2, "mean_score": 5.0}}  # under the floor
    priors = {"story_open": {"n": 500, "mean_score": 2.0},
              "question": {"n": 400, "mean_score": 1.0}}
    assert pb.break_tie(own, priors) == "story_open"
