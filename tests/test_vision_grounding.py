"""
P4 core — crop-verify (§3.5) + the grounding contradiction gate (§5/§7). Contradiction-only:
absence passes, only an assertion the verified analysis says is FALSE (or a high-risk
identity/number claim) fails. Offline (verify reader stubbed).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import vision  # noqa: E402


def _analysis(**over):
    a = {"version": 2, "one_line": "three people in a class", "setting": "gym_floor",
         "subjects": ["class"], "people": {"bucket": "small_group"}, "activity": "class",
         "visible_details": [{"detail": "chalk on hands", "confidence": 0.92},
                             {"detail": "a wall clock", "confidence": 0.6}],
         "quality": {"usable": True}, "avatar_fit": "genpop", "safety_flags": []}
    a.update(over)
    return a


# ---- crop_verify -----------------------------------------------------------------------

def test_crop_verify_keeps_only_confirmed_details():
    a = _analysis()
    # the reader confirms crowd + the chalk detail, denies nothing else eligible
    def reader(img, prompt):
        return '{"people_bucket":"small_group","details_present":{"chalk on hands":true}}'
    v = vision.crop_verify(b"imgbytes", a, reader=reader)
    assert v["ok"] and v["bucket"] == "small_group"
    assert v["verified_details"] == ["chalk on hands"]     # 0.6 clock was never eligible


def test_crop_verify_detail_dropped_when_denied():
    a = _analysis()
    def reader(img, prompt):
        return '{"people_bucket":"solo","details_present":{"chalk on hands":false}}'
    v = vision.crop_verify(b"x", a, reader=reader)
    assert v["ok"] and v["bucket"] == "solo" and v["verified_details"] == []  # crop changed the count


def test_crop_verify_failure_is_safe():
    a = _analysis()
    def boom(img, prompt):
        raise RuntimeError("verify down")
    v = vision.crop_verify(b"x", a, reader=boom)
    assert v == {"bucket": None, "verified_details": [], "ok": False}   # degrade safe
    v2 = vision.crop_verify(b"x", {"analysis_failed": True}, reader=lambda i, p: "{}")
    assert v2["ok"] is False


# ---- grounding_contradictions ----------------------------------------------------------

def test_gate_count_honesty():
    a = _analysis(people={"bucket": "solo"})
    v = {"bucket": "solo", "verified_details": [], "ok": True}
    assert any("crowd word" in i for i in
               vision.grounding_contradictions("Our packed classes are electric", a, verified=v))
    # a solo image + one-on-one caption is CONSISTENT -> no contradiction
    assert vision.grounding_contradictions("One on one coaching that fits your life", a,
                                           verified=v) == []


def test_gate_crowd_word_ok_when_crowd():
    a = _analysis(people={"bucket": "crowd"})
    v = {"bucket": "crowd", "verified_details": [], "ok": True}
    assert vision.grounding_contradictions("A packed house every morning", a, verified=v) == []


def test_gate_outdoor_contradiction():
    a = _analysis(setting="gym_floor")
    assert any("outdoor" in i for i in
               vision.grounding_contradictions("Train outside in the sunshine", a))


def test_gate_object_rejected_by_crop():
    a = _analysis()
    # crop verify CONFIRMED the bucket but did NOT confirm 'chalk on hands'
    v = {"bucket": "small_group", "verified_details": [], "ok": True}
    assert any("chalk on hands" in i for i in
               vision.grounding_contradictions("Chalk on hands, ready to lift", a, verified=v))
    # if the crop confirmed it, no contradiction
    v2 = {"bucket": "small_group", "verified_details": ["chalk on hands"], "ok": True}
    assert vision.grounding_contradictions("Chalk on hands, ready to lift", a, verified=v2) == []


def test_gate_identity_and_numbers_are_high_risk():
    a = _analysis()
    assert any("identity" in i for i in
               vision.grounding_contradictions("Our strongest woman crushed it", a))
    # an unsupported number fails; a gym-record number passes
    assert any("number" in i for i in
               vision.grounding_contradictions("Members lost 40 lbs this month", a))
    assert vision.grounding_contradictions("Members lost 40 lbs this month", a,
                                           gym_claims=["Members lost 40 lbs this month"]) == []


def test_gate_absence_passes():
    a = _analysis()
    # a generic, claim-free caption never contradicts (absence passes, not fails)
    assert vision.grounding_contradictions(
        "Real coaching for busy people who want a plan that sticks", a) == []


def test_post_quality_gate_rejects_grounding_contradiction():
    from agent import post_quality
    from agent.drafter import Draft
    a = _analysis(people={"bucket": "solo"})
    v = {"bucket": "solo", "verified_details": [], "ok": True}
    d = Draft(draft_id="x", account_key="gritx_ig", platform="instagram",
              caption="Our packed classes stay electric every single morning for busy "
                      "parents who want real results",
              hashtags=[], creative_path="", creative_public_url="https://r2/x.jpg",
              scheduled_for="")
    d.grounding = {"analysis": a, "verified": v, "claims": []}
    assert post_quality.is_a_plus(d) is False        # crowd word on a solo image -> not A+
    d.caption = ("Real strength coaching for busy parents on a schedule that fits your "
                 "life, built step by step so you actually stick with it and feel progress")
    assert post_quality.is_a_plus(d) is True         # clean grounded caption -> A+


def test_post_quality_gate_inert_without_grounding():
    # a non-vision draft (no .grounding) is unaffected by the grounding gate
    from agent import post_quality
    from agent.drafter import Draft
    d = Draft(draft_id="x", account_key="eng_ig", platform="instagram",
              caption="Our packed house stays electric every single morning for busy "
                      "parents who want real results",
              hashtags=[], creative_path="", creative_public_url="https://r2/x.jpg",
              scheduled_for="")
    assert post_quality.is_a_plus(d) is True          # no grounding -> gate does not fire


# ---- P4 audit fixes --------------------------------------------------------------------

def test_unconfirmed_bucket_fails_crowd_closed():
    a = _analysis(people={"bucket": "crowd"})   # ingest said crowd
    v = {"bucket": None, "verified_details": [], "ok": True}   # crop could NOT confirm
    # a crowd word must FAIL against an unconfirmed bucket (no stale-ingest license, audit 3b)
    assert any("crowd word" in i for i in
               vision.grounding_contradictions("A packed house every morning here", a, verified=v))


def test_no_false_positive_on_private_outside_sunshine():
    a = _analysis(people={"bucket": "small_group"}, setting="gym_floor")
    v = {"bucket": "small_group", "verified_details": [], "ok": True}
    for clean in ("A private space to train the way you want",
                  "Outside of class we still support your goals",
                  "Sunshine and good vibes fill the room every session"):
        assert vision.grounding_contradictions(clean, a, verified=v) == [], clean


def test_program_duration_numbers_not_gated():
    a = _analysis()
    for ok in ("Get stronger in 6 weeks with a simple plan",
               "12 reps, 3 sets, real progress every session",
               "Your first 7 day on ramp is easy"):
        assert vision.grounding_contradictions(ok, a) == [], ok


def test_weight_and_percent_numbers_still_gated():
    a = _analysis()
    assert any("number" in i for i in vision.grounding_contradictions("Lose 40 lbs fast", a))
    assert any("number" in i for i in vision.grounding_contradictions("Boost results 80%", a))


def test_number_format_variants_match_claim():
    a = _analysis()
    # a claim of "40 lbs" clears "40lbs" and "40 pounds" (normalized match, audit 5 strict)
    for variant in ("Members lost 40lbs", "Members lost 40 pounds"):
        assert vision.grounding_contradictions(variant, a, gym_claims=["lost 40 lbs"]) == [], variant


# ---- P5 consent exception + policy screen ----------------------------------------------

def test_consent_allows_client_written_identity_and_number():
    a = _analysis()
    ctx = "our member sarah lost 40 lbs and is now a strong woman"
    cap = "40 lbs down and stronger than ever, that is one proud woman"
    # WITHOUT consent -> identity + number flagged
    assert vision.grounding_contradictions(cap, a) != []
    # WITH consent AND the content in the client's own context -> allowed
    assert vision.grounding_contradictions(cap, a, consent=True, client_context=ctx,
                                           gym_claims=[]) == []


def test_consent_does_not_launder_uncontexted_identity():
    a = _analysis()
    # consent granted but the term is NOT in the client context -> still flagged
    assert any("identity" in i for i in
               vision.grounding_contradictions("a muscular man crushed it", a,
                                               consent=True, client_context="great session"))


def test_policy_screen_flags_health_reviewbait_weightpromise():
    assert vision.policy_screen("this cures anxiety and depression")
    assert vision.policy_screen("leave us a 5 star review for a free month")
    assert vision.policy_screen("lose 30 lbs guaranteed in 30 days")
    assert vision.policy_screen("Great community and real coaching") == []


def test_context_usable_gate():
    ok, reasons = vision.context_usable("Sarah had an amazing first month with us")
    assert ok and reasons == []
    bad_ok, bad_reasons = vision.context_usable("we cured her diabetes, guaranteed")
    assert bad_ok is False and bad_reasons
    assert vision.context_usable("") == (True, [])          # empty context is fine
