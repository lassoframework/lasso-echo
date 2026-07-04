"""
Story-first card tests (the stranger test). Asserts: the STORY REQUIREMENT rides
every archetype's prompt (concrete gym-world scene, tension + resolution, the
banned generic label list, the stranger test itself); every concept context is a
Tension/Resolution micro story; no banned generic label appears anywhere in an
assembled prompt OUTSIDE the ban statement itself. Offline.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import creative_studio, regen_library  # noqa: E402

BANNED_LABELS = ("STEP 1", "STEP 2", "STEP 3", "PLAN", "GROW", "LEARN",
                 "DISCOVER", "LAUNCH", "START", "FINISH")


def _without_ban_statement(prompt):
    """The prompt with the story-requirement block removed, so the ban list's own
    wording does not count against the check."""
    return prompt.replace(creative_studio.STORY_REQUIREMENT, "")


# ---- the spec carries the story requirement on every archetype -----------------
def test_story_requirement_on_every_archetype():
    for arch in creative_studio.ARCHETYPES:
        p = creative_studio.build_prompt("A headline.", ["Tension: x.", "Resolution: y."],
                                         archetype=arch)
        low = p.lower()
        assert "concrete scene" in low, arch
        assert "gym owner's world" in low, arch
        assert "tension and a resolution" in low, arch
        assert "never heard of lasso" in low, arch        # the stranger test, stated
        assert "banned generic process labels" in low, arch
        for label in BANNED_LABELS:
            assert label in p, arch                        # the ban list itself is present


def test_story_requirement_rides_story_surface_too():
    p = creative_studio.build_prompt("H", ["Tension: x.", "Resolution: y."],
                                     archetype="split", aspect="9:16",
                                     pixels="1080x1920", surface="story")
    assert "stranger test" in p.lower() or "never heard of lasso" in p.lower()


# ---- every concept context is a tension/resolution micro story ------------------
def test_every_concept_context_has_tension_and_resolution():
    for key, spec in regen_library.CONCEPTS.items():
        lines = spec["concept"]
        assert any(l.startswith("Tension:") for l in lines), key
        assert any(l.startswith("Resolution:") for l in lines), key
        # still clean: no dash characters anywhere; digits stay banned in the
        # house sets (the b2b swipe copy may carry cited digits, own test file)
        for line in lines:
            if spec.get("set") != "b2b":
                assert "%" not in line and not re.search(r"\d", line), key
            assert not re.search(r"[—–]", line), key


def test_benchmark_and_rewritten_offenders():
    c = regen_library.CONCEPTS
    # the benchmark keeps its unanswered-phone story
    assert "unanswered phone" in c["follow_up_problem"]["concept"][0]
    # three_step_path: real labels, not GROW/PLAN/LEARN
    joined = " ".join(c["three_step_path"]["concept"])
    assert "LEADS" in joined and "SYSTEM" in joined and "MEMBERS" in joined
    assert "GROW" not in joined and "PLAN" not in joined and "LEARN" not in joined
    # one_screen: chaos resolving into one calm dashboard
    assert "sticky notes" in c["one_screen"]["concept"][0]
    assert "calm dashboard" in c["one_screen"]["concept"][1]
    # system_runs_itself: owner on the floor, machine behind
    assert "coach" in c["system_runs_itself"]["concept"][1]
    assert "machine" in c["system_runs_itself"]["concept"][1]
    # coach_in_your_corner: slumped owner, guide with one visible plan
    assert "slumped" in c["coach_in_your_corner"]["concept"][0]


# ---- banned labels never appear outside the ban statement ------------------------
def test_banned_labels_absent_from_assembled_prompts():
    for key in regen_library.CONCEPTS:
        for variant, prompt in regen_library.assemble_prompts(key):
            body = _without_ban_statement(prompt)
            for label in BANNED_LABELS:
                assert label not in body, f"{key} ({variant}): banned label {label}"
