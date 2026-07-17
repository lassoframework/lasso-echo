"""
Layout archetype tests. The composition varies, the brand never does. Asserts:
each archetype's prompt carries its structural constraints AND the brand
constants; the regen assignment map is honored with no archetype used more than
twice; story variants inherit the concept's archetype recomposed with the 9:16
safe zones; the daily studio rotates archetypes deterministically; rotation logs
the served archetype and softly prefers alternation (never overriding the
no-repeat window). Offline.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import config, creative_studio, regen_library, rotation  # noqa: E402

BRAND_HEXES = ("#121E3C", "#FF0000", "#5EB9E6", "#FAF6F0")

STRUCTURE_MARKS = {
    "flow": ["archetype flow", "vertical", "flow arrows", "headline at the top"],
    "split": ["archetype split", "two side contrast", "one side muted",
              "winning side", "headline at the top"],
    "hero": ["archetype hero", "one large central illustration",
             "maximum negative space", "two small uppercase labels",
             "headline at the top"],
    "path": ["archetype path", "winding journey path", "begins at the bottom",
             "ends at the top", "final stop"],
    "headline": ["archetype headline", "typography forward", "centered in the middle",
                 "no diagram", "use sparingly"],
}


# ---- 1. every archetype: its structure + the shared brand constants -------------
def test_each_archetype_prompt_carries_structure_and_brand():
    for arch, marks in STRUCTURE_MARKS.items():
        p = creative_studio.build_prompt("A headline.", ["a concept line"], archetype=arch)
        low = p.lower()
        for mark in marks:
            assert mark in low, f"{arch}: missing {mark!r}"
        # the brand never varies
        for hexcode in BRAND_HEXES:
            assert hexcode in p, arch
        assert "cream #faf6f0: the canvas" in low, arch
        assert "never a full bleed solid color slab" in low, arch
        assert "one idea per card" in low, arch
        assert "no em dashes" in low, arch
        assert "—" not in p and "–" not in p, arch


def test_unknown_archetype_falls_back_to_flow():
    p = creative_studio.build_prompt("H", ["x"], archetype="cubism")
    assert "Archetype FLOW" in p


def test_default_prompt_is_flow_unchanged():
    p = creative_studio.build_prompt("H", ["x"])
    assert "Archetype FLOW" in p
    assert creative_studio.COMPOSITION_STYLE in p    # back-compat block intact


# ---- 2. the regen assignment map -------------------------------------------------
def test_assignment_map_honored_no_archetype_more_than_twice():
    expected = {
        "built_by_gym_owners": "editorial", "one_screen": "hero",
        "three_step_path": "path", "follow_up_problem": "split",
        "posting_cadence": "split", "speed_to_lead_concept": "hero",
        "system_runs_itself": "flow", "coach_in_your_corner": "headline",
    }
    counts = {}
    for key, arch in expected.items():
        assert regen_library.CONCEPTS[key]["archetype"] == arch, key
        counts[arch] = counts.get(arch, 0) + 1
    assert max(counts.values()) <= 2                  # no archetype more than twice


# ---- 3. story variants inherit the archetype, recomposed with safe zones --------
def test_story_variant_inherits_archetype_with_safe_zones():
    # three_step_path is +STORY with the PATH archetype
    prompts = dict(regen_library.assemble_prompts("three_step_path"))
    assert "Archetype PATH" in prompts["feed"] and "Archetype PATH" in prompts["story"]
    story = prompts["story"]
    assert "TOP 250 pixels" in story and "BOTTOM 250 pixels" in story
    assert "never a cropped, stretched, or reused feed card" in story
    assert "9:16" in story and "1080x1920" in story


# ---- 5. the daily studio rotates archetypes deterministically --------------------
def test_daily_archetype_rotates_not_random():
    days = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
    picks = [creative_studio.archetype_for_day(d) for d in days]
    assert len(set(picks)) == 5                       # five days, five archetypes
    assert picks == [creative_studio.archetype_for_day(d) for d in days]  # stable


# ---- 4. rotation logs the archetype and softly prefers alternation ---------------
def _lib(tmp_path, cards):
    lib = tmp_path / "library"
    lib.mkdir(parents=True, exist_ok=True)
    for name, note, arch in cards:
        (lib / name).write_bytes(b"img-" + name.encode())
        if note:
            (lib / (os.path.splitext(name)[0] + ".txt")).write_text(note, encoding="utf-8")
        if arch:
            (lib / (os.path.splitext(name)[0] + ".json")).write_text(
                json.dumps({"archetype": arch}), encoding="utf-8")
    return str(lib)


def _arm(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ROTATION_ENABLED", "true")
    monkeypatch.setenv("AGENT_ROTATION_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(exist_ok=True)
    src = tmp_path / "empty_now.md"
    src.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_DOC_PATH", str(src))
    monkeypatch.delenv("AGENT_KNOWLEDGE_ENABLED", raising=False)


def test_rotation_logs_archetype(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    rotation.record_served("lasso_ig", "a.jpg", "p1", "2026-07-06", archetype="hero")
    entry = rotation.load_served()["lasso_ig"][0]
    assert entry["archetype"] == "hero"


def test_rotation_prefers_a_different_archetype_than_yesterday(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    # yesterday served a HERO card; today two never-served candidates: another HERO
    # (alphabetically first, would win the old tiebreak) and a SPLIT
    lib = _lib(tmp_path, [
        ("lasso_p1_aaa_hero.jpg", "clean note", "hero"),
        ("lasso_p2_zzz_split.jpg", "clean note", "split"),
    ])
    rotation.record_served("lasso_ig", "served_yesterday.jpg", "p9", "2026-07-06",
                           archetype="hero")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert os.path.basename(creative.path) == "lasso_p2_zzz_split.jpg"  # alternation won


def test_archetype_preference_is_soft_never_blocks(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    # every eligible candidate shares yesterday's archetype: one still gets picked
    lib = _lib(tmp_path, [("lasso_p1_only_hero.jpg", "clean note", "hero")])
    rotation.record_served("lasso_ig", "served_yesterday.jpg", "p9", "2026-07-06",
                           archetype="hero")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert kind == "library"
    assert os.path.basename(creative.path) == "lasso_p1_only_hero.jpg"


def test_alternation_never_overrides_no_repeat_window(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    # the alternation-preferred SPLIT card is inside the no-repeat window: the
    # window wins and the same-archetype fresh card is chosen instead
    lib = _lib(tmp_path, [
        ("lasso_p1_fresh_hero.jpg", "clean note", "hero"),
        ("lasso_p2_recent_split.jpg", "clean note", "split"),
    ])
    rotation.record_served("lasso_ig", "lasso_p2_recent_split.jpg", "p2", "2026-07-05",
                           archetype="split")
    rotation.record_served("lasso_ig", "served_yesterday.jpg", "p9", "2026-07-06",
                           archetype="hero")
    kind, creative = rotation.choose("lasso_ig", "2026-07-07", lib)
    assert os.path.basename(creative.path) == "lasso_p1_fresh_hero.jpg"
