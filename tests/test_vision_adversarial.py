"""
P0 — the STANDING adversarial photo test set (ECHO_VISION_SPEC §9.3, acceptance bar).

Each fixture is a raw Gemini-style analysis payload representing an adversarial photo; it is
run through the real coerce_analysis + routing, and MUST route correctly (flag/exclude, or
caption-safe with a clean details set). This harness is the go/no-go gate: 100% correct
routing before any per-gym vision flag defaults on. Every later phase extends it.

No network, no real model — this pins the DECISION logic given each kind of image.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import vision  # noqa: E402


def _analysis(**over):
    """A clean baseline v2 payload (as Gemini would return it) that IS auto-plannable."""
    base = {
        "one_line": "Three people mid squat in a group class, a coach adjusting form",
        "setting": "gym_floor", "subjects": ["group_class", "barbell"],
        "people_bucket": "small_group", "includes_children": False,
        "activity": "class",
        "visible_details": [{"detail": "chalk on hands", "confidence": 0.92},
                            {"detail": "wall clock", "confidence": 0.6}],
        "text_in_image": None, "activity_confidence": 0.9,
        "quality": {"sharp": True, "well_lit": True, "usable": True, "reject_reason": None},
        "avatar_fit": "genpop", "safety_flags": [],
    }
    base.update(over)
    return base


# ---- the §9.3 fixture set: (name, raw payload, must_plan, reason_substr) ----------------
FIXTURES = [
    # 1. legible name tag on a person -> person_name_in_image -> EXCLUDED
    ("name_tag", _analysis(text_in_image="Coach Sarah", contains_person_name=True),
     False, "person_name"),
    # 2. whiteboard with member names + PII -> pii_visible + name -> EXCLUDED
    ("whiteboard_pii", _analysis(text_in_image="Members: Sarah 405, Mike 315",
                                 safety_flags=["pii_visible"]),
     False, "pii_visible"),
    # 3. before/after collage -> plannable, caption-safe (the NO-NUMBERS rule is the caption
    #    gate's job; routing must not exclude a clean collage)
    ("before_after_collage", _analysis(one_line="A side by side of a member in the gym",
                                       subjects=["collage"], activity="community"),
     True, None),
    # 4. athlete competition shot -> avatar_fit athlete -> EXCLUDED
    ("athlete_comp", _analysis(avatar_fit="athlete",
                               one_line="A person lifting a heavily loaded barbell on a "
                                        "competition platform"),
     False, "athlete"),
    # 5. minor prominent -> EXCLUDED
    ("minor_prominent", _analysis(includes_children=True,
                                  safety_flags=["minor_prominent"]),
     False, "minor_prominent"),
    # 6. blurry burst duplicate -> unusable -> EXCLUDED
    ("blurry", _analysis(quality={"sharp": False, "well_lit": False, "usable": False,
                                  "reject_reason": "motion blur"}),
     False, "unusable"),
    # 7. empty-gym shot -> facility, no people -> plannable, caption-safe
    ("empty_gym", _analysis(one_line="An empty gym floor with racks and rowers",
                            people_bucket="none", activity="facility",
                            subjects=["squat_rack", "rowers"]),
     True, None),
    # 8. identity leak in the description (model slipped a gender word) -> EXCLUDED
    ("gender_leak", _analysis(one_line="A muscular man doing pull ups"),
     False, "identity_leak"),
    # 9. third-party brand prominent -> EXCLUDED
    ("third_party_brand", _analysis(safety_flags=["third_party_brand"]),
     False, "third_party_brand"),
]


def test_adversarial_set_routes_100_percent():
    """Every adversarial fixture routes correctly. This is the acceptance bar — a single
    miss blocks any per-gym default-on."""
    misroutes = []
    for name, raw, must_plan, reason_substr in FIXTURES:
        analysis = vision.coerce_analysis(raw, phash="0" * 16)
        ok, reasons = vision.auto_plannable(analysis)
        if ok != must_plan:
            misroutes.append(f"{name}: expected plannable={must_plan}, got {ok} ({reasons})")
        elif reason_substr and not any(reason_substr in r for r in reasons):
            misroutes.append(f"{name}: expected reason ~'{reason_substr}', got {reasons}")
    assert not misroutes, "adversarial misroutes:\n" + "\n".join(misroutes)


def test_before_after_collage_yields_no_identity_or_number_leak():
    # a caption-safe collage must carry NO identity terms in its eligible details
    a = vision.coerce_analysis(_analysis(one_line="A side by side of a member",
                                         subjects=["collage"]))
    assert a["identity_flag"] is False
    assert vision.identity_issues(a["one_line"]) == []


def test_name_tag_photo_sets_contains_person_name_even_without_model_flag():
    # the model forgot contains_person_name; the heuristic on text_in_image still catches it
    a = vision.coerce_analysis(_analysis(text_in_image="Great work Marcus"))
    assert a["contains_person_name"] is True
    assert vision.auto_plannable(a)[0] is False


def test_pii_whiteboard_excluded_and_text_captured_verbatim():
    a = vision.coerce_analysis(_analysis(text_in_image="Sarah 405  Mike 315",
                                         safety_flags=["pii_visible"]))
    # text captured verbatim for the record, but excluded from planning, and firewalled from
    # the drafter (the drafter never reads text_in_image — enforced in P4)
    assert a["text_in_image"] == "Sarah 405  Mike 315"
    assert vision.auto_plannable(a)[0] is False
