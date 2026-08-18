"""
P3 — planner content scoring (ECHO_VISION_SPEC §4): vision.content_score slot affinity + BTS
restriction, and client_content.pick_image's vision path (flagged excluded, reuse-window
excluded, scored to the slot job, below-floor -> weak_match). Legacy rotation unchanged when
vision is off. Offline.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import vision, client_content  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "echo.db"))   # fresh served ledger
    yield


def _asset(lib, name, analysis, *, public_url="https://r2/x.jpg"):
    """An image file + a DAM sidecar carrying a v2 analysis (+ a public_url for the card)."""
    open(os.path.join(lib, name), "wb").close()
    stem = os.path.splitext(name)[0]
    side = {"media_analysis": analysis, "public_url": public_url}
    with open(os.path.join(lib, stem + ".json"), "w") as fh:
        json.dump(side, fh)


def _analysis(**over):
    a = {"version": 2, "one_line": "x", "setting": "gym_floor", "subjects": [],
         "people": {"bucket": "small_group"}, "activity": "class", "visible_details": [],
         "text_in_image": None, "contains_person_name": False,
         "quality": {"sharp": True, "well_lit": True, "usable": True, "reject_reason": None},
         "avatar_fit": "genpop", "safety_flags": [], "identity_flag": False,
         "phash": "0" * 16}
    # allow people bucket override as a plain string
    if "people_bucket" in over:
        a["people"] = {"bucket": over.pop("people_bucket")}
    a.update(over)
    return a


# ---- content_score ---------------------------------------------------------------------

def test_content_score_affinity_matches_slot_job():
    coach = _analysis(activity="coaching", people_bucket="pair", setting="gym_floor")
    # Transformation (testimonial): coaching + solo/pair -> high
    hi, ok = vision.content_score(coach, "testimonial")
    # Community: wants class + crowd -> lower for this coaching/pair image
    lo, _ = vision.content_score(coach, "community")
    assert ok and hi > lo


def test_content_score_bts_restriction():
    # only `unclear` is BTS-restricted now (athlete/athlete_leaning are unrestricted)
    unclear = _analysis(avatar_fit="unclear", activity="coaching")
    assert vision.content_score(unclear, "testimonial")[1] is False   # not a BTS slot
    assert vision.content_score(unclear, "behind")[1] is True         # BTS slot ok
    # an athlete shot now fills a normal slot like any other photo (Blake 2026-08-18)
    athlete = _analysis(avatar_fit="athlete", activity="strength")
    assert vision.content_score(athlete, "testimonial")[1] is True


def test_content_score_failed_analysis():
    assert vision.content_score({"analysis_failed": True}, "offer") == (-1.0, False)
    assert vision.content_score(None, "offer")[1] is False


# ---- pick_image vision path ------------------------------------------------------------

def test_pick_image_excludes_flagged_and_scores(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYMS", "gritx")
    lib = str(tmp_path)
    # flagged (safety) -> never picked
    _asset(lib, "flagged.png", _analysis(safety_flags=["pii_visible"]))
    # a strong transformation fit
    _asset(lib, "good.png", _analysis(activity="coaching", people_bucket="pair"))
    # a weak fit for the slot
    _asset(lib, "meh.png", _analysis(activity="cardio", people_bucket="crowd",
                                     setting="exterior"))
    pick = client_content.pick_image("gritx_ig", "2026-09-01", lib, pillar="testimonial")
    assert pick is not None and os.path.basename(pick.path) == "good.png"


def test_pick_image_weak_match_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYMS", "gritx")
    lib = str(tmp_path)
    # only a poor-fit image exists (below the score floor for this slot)
    _asset(lib, "weak.png", _analysis(activity="food", people_bucket="none",
                                      setting="other",
                                      quality={"sharp": False, "well_lit": False,
                                               "usable": True, "reject_reason": None}))
    pick = client_content.pick_image("gritx_ig", "2026-09-01", lib, pillar="testimonial")
    assert pick is not None                       # never silent: best available is planned
    assert getattr(pick, "weak_match", False) is True


def test_pick_image_none_when_all_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_VISION_GYMS", "gritx")
    lib = str(tmp_path)
    _asset(lib, "a.png", _analysis(safety_flags=["pii_visible"]))       # excluded
    _asset(lib, "b.png", _analysis(safety_flags=["minor_prominent"]))   # excluded
    assert client_content.pick_image("gritx_ig", "2026-09-01", lib, pillar="testimonial") is None


def test_pick_image_vision_off_ignores_analysis(tmp_path, monkeypatch):
    # gym NOT in AGENT_VISION_GYMS -> legacy rotation, even a flagged image is eligible
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)
    lib = str(tmp_path)
    _asset(lib, "only.png", _analysis(safety_flags=["pii_visible"]))
    pick = client_content.pick_image("gritx_ig", "2026-09-01", lib, pillar="testimonial")
    assert pick is not None and os.path.basename(pick.path) == "only.png"  # legacy ignores flags


def test_pick_image_shadow_ships_legacy_pick(tmp_path, monkeypatch, capsys):
    # §9.4: a SHADOW (not enabled) gym logs the vision pick but SHIPS the legacy pick.
    monkeypatch.delenv("AGENT_VISION_GYMS", raising=False)
    monkeypatch.setenv("AGENT_VISION_SHADOW", "gritx")
    lib = str(tmp_path)
    # 'flagged.png' is an athlete shot (vision would EXCLUDE it); legacy least-recently-served
    # sorts by basename, so 'a_flagged.png' is the legacy pick even though vision rejects it.
    _asset(lib, "a_flagged.png", _analysis(avatar_fit="athlete"))
    _asset(lib, "z_clean.png", _analysis(activity="coaching"))
    pick = client_content.pick_image("gritx_ig", "2026-09-01", lib, pillar="testimonial")
    # shadow does NOT change the ship: legacy still returns the name-first athlete photo
    assert os.path.basename(pick.path) == "a_flagged.png"
    out = capsys.readouterr().out
    assert "[vision-shadow]" in out and "gritx_ig" in out


def test_prefs_exact_match_first():
    # a single-token client pillar resolves by EXACT match (no substring ambiguity)
    prefs, key = vision._prefs_for("offer")
    assert key == "offer"
    # a multi-word GBP pillar falls through to substring
    _, key2 = vision._prefs_for("All in one offer")
    assert key2 == "offer"
    # unknown pillar -> default profile, empty key
    _, key3 = vision._prefs_for("the portal")
    assert key3 == ""
