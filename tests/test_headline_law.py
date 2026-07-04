"""
Clear-not-cute headline law tests. Asserts: the law rides every archetype's
prompt (plain statement, two second test, all-three-or-fails); the headline map
matches the shipped rewrites; the retired slogans are absent from every
assembled prompt; the standing rules (no digits, no percent, no dashes) hold for
every headline in both sets. Offline.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent import creative_studio, regen_library  # noqa: E402

# the full expected headline map after the law (both sets)
EXPECTED_HEADLINES = {
    "built_by_gym_owners": "Built by gym owners, for gym owners.",
    "one_screen": "Every lead, every post, every result. One screen.",
    "three_step_path": "How gyms grow with LASSO",
    "follow_up_problem": "Most gyms don't have a lead problem. They have a follow up problem.",
    "posting_cadence": "Post every day. Grow every month.",
    "speed_to_lead_concept": "Answer leads fast. Close more of them.",
    "system_runs_itself": "Your follow up runs on autopilot",
    "coach_in_your_corner": "You run the gym. We bring the plan.",
    "ads_done_for_you": "We run your ads. You run your gym.",
    "follow_up_system": "Every lead gets a follow up.",
    "booked_to_close": "From lead to member.",
    "sales_training": "We train your sales team.",
    "funnel_diagnostic": "We find the leak in your funnel.",
    "social_done_for_you": "Your social posts itself.",
    "one_partner": "Ads, sales, and social in one place",
    "website_done_for_you": "Your website, done for you.",
}

RETIRED_SLOGANS = (
    "Three steps. One path.",
    "Consistency beats intensity.",
    "Speed to lead wins.",
    "The system runs itself.",
    "A coach in your corner.",
    "Ads, sales, and social. One place.",
)


def test_headline_law_rides_every_archetype():
    for arch in creative_studio.ARCHETYPES:
        p = creative_studio.build_prompt("H", ["Tension: x.", "Resolution: y."],
                                         archetype=arch)
        low = p.lower()
        assert "be clear, not cute" in low, arch
        assert "no slogans" in low, arch
        assert "two second" in low, arch
        assert "all three or the card fails" in low, arch


def test_headline_map_matches_report():
    # the 16 house concepts exactly; the cited swipe sets (b2b, platform)
    # have their own test files
    actual = {k: v["headline"] for k, v in regen_library.CONCEPTS.items()
              if v.get("set") not in ("b2b", "platform", "platform_ads")}
    assert actual == EXPECTED_HEADLINES


def test_retired_slogans_absent_from_all_prompts():
    for key in regen_library.CONCEPTS:
        for _variant, prompt in regen_library.assemble_prompts(key):
            for slogan in RETIRED_SLOGANS:
                assert slogan not in prompt, f"{key}: retired slogan {slogan!r}"


def test_headlines_keep_the_standing_rules():
    for key, spec in regen_library.CONCEPTS.items():
        h = spec["headline"]
        if spec.get("set") in ("b2b", "platform", "platform_ads"):
            # the cited exception: a swipe set stat headline is allowed digits
            # ONLY with a cite into the approved claims source (each set's own
            # test file proves every cite resolves and clears the gate)
            if re.search(r"[\d%]", h):
                assert spec.get("cite"), key
        else:
            assert not re.search(r"[\d%]", h), key  # no digits, no percent
        assert not re.search(r"[—–-]", h), key      # no dash characters
        assert len(h) <= 80, key                    # short
