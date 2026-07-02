"""
regen-library: rebuild the seed content library in the LOCKED v2 house style.

RUN BY HAND by Blake in the container (like capture-baseline and draft-bible):
never scheduled, no flag arms it into the daily path, nothing in the agent
imports it at runtime.

    /opt/venv/bin/python -m agent regen-library
    /opt/venv/bin/python -m agent regen-library --only one_screen
    /opt/venv/bin/python -m agent regen-library --dry-run

Every card goes through the LOCKED house-style spec in creative_studio
(cream canvas, one navy headline top, line-icon illustrated diagram with small
uppercase labels and flow arrows, red as exactly one accent, one idea per card,
no slabs, no stacked text). Feed cards are 4:5; concepts marked story=True also
get a from-scratch 9:16 story variant (same style, own composition, never a
stretched feed card). Output lands in content_library as lasso_v2_<key>.png with
a matching .json sidecar, hosts through the existing R2 pipeline, and PRINTS the
public URL per card so Blake can eyeball each one in the browser.

GATES UNCHANGED, stated plainly:
  - fabrication gate: every concept below is NON STAT by design (no numbers, no
    percent claims); captions are NOT generated here. Cards enter the normal
    drafter flow where captions come only from approved sources.
  - approval gate + publish flag: nothing this command makes posts anywhere.
    A card can only reach Meta through the daily draft plus Blake's tap.
  - rotation guard: this command never touches style_exclusions.json. Old cards
    stay excluded; new lasso_v2_ cards are picked up only because they exist and
    are gate-clean. Story variants (*_story.png) are never feed candidates.
"""

import json
import os
from datetime import date

from . import config, creative_studio, media_host

V2_PREFIX = "lasso_v2_"
STORY_ASPECT = ("9:16", "1080x1920", "story post")
HOST_TENANT = "lasso_library"

# The starter batch: 8 concepts, NON STAT only. Headlines are short LASSO voice
# with no em dashes, no en dashes, no hyphens. The concept lines are diagram
# CONTEXT for the illustration (build_prompt never renders them as text).
CONCEPTS = {
    "built_by_gym_owners": {
        "headline": "Built by gym owners, for gym owners.",
        "concept": ["Flow diagram: our gym, then the proven system, then your gym.",
                    "Three stations connected left to right by flow arrows."],
        "story": True,
        "set": "brand",
        "archetype": "flow",
    },
    "one_screen": {
        "headline": "Every lead, every post, every result. One screen.",
        "concept": ["Leads, posts, and results flow from three sides into one dashboard.",
                    "One central screen icon receiving three labeled streams."],
        "story": False,
        "set": "brand",
        "archetype": "hero",
    },
    "three_step_path": {
        "headline": "Three steps. One path.",
        "concept": ["A simple path diagram with three labeled stops along one road.",
                    "No numbered text list; the path itself carries the three steps."],
        "story": True,
        "set": "brand",
        "archetype": "path",
    },
    "follow_up_problem": {
        "headline": "Most gyms don't have a lead problem. They have a follow up problem.",
        "concept": ["Leads pooling beside an unanswered phone on one side.",
                    "On the other side an answered phone with leads flowing through."],
        "story": False,
        "set": "brand",
        "archetype": "split",
    },
    "posting_cadence": {
        "headline": "Consistency beats intensity.",
        "concept": ["Two calendars side by side: one nearly empty and quiet, one with a",
                    "steady daily rhythm of posts. Illustrated, no counts, no percentages."],
        "story": False,
        "set": "brand",
        "archetype": "split",
    },
    "speed_to_lead_concept": {
        "headline": "Speed to lead wins.",
        "concept": ["A clock and a lead handoff: speed as an idea.",
                    "No hours or minutes claim text anywhere on the card."],
        "story": False,
        "set": "brand",
        "archetype": "hero",
    },
    "system_runs_itself": {
        "headline": "The system runs itself.",
        "concept": ["Gears turning a calendar that produces a checkmark.",
                    "Done for you as a machine that quietly works."],
        "story": False,
        "set": "brand",
        "archetype": "flow",
    },
    "coach_in_your_corner": {
        "headline": "A coach in your corner.",
        "concept": ["A gym owner and a guide figure pointing together at one simple plan.",
                    "StoryBrand guide framing: the owner is the hero, the guide has the plan."],
        "story": False,
        "set": "brand",
        "archetype": "headline",
    },
    "ads_done_for_you": {
        "headline": "We run your ads. You run your gym.",
        "concept": ["Split: on one side the owner coaching members on the gym floor,",
                    "on the other side ad campaigns quietly running on their own."],
        "story": False,
        "set": "service",
        "archetype": "split",
    },
    "follow_up_system": {
        "headline": "Every lead gets a follow up.",
        "concept": ["Flow: a lead comes in, then text, call, and email touches,",
                    "then a booked session on the calendar."],
        "story": False,
        "set": "service",
        "archetype": "flow",
    },
    "booked_to_close": {
        "headline": "From lead to member.",
        "concept": ["A journey with labeled stops: lead, booked, showed, closed, member.",
                    "The growth scorecard stages as one path."],
        "story": False,
        "set": "service",
        "archetype": "path",
    },
    "sales_training": {
        "headline": "We train your sales team.",
        "concept": ["One coach figure handing a phone playbook to a team member.",
                    "Sales training as a service, not a lecture."],
        "story": False,
        "set": "service",
        "archetype": "hero",
    },
    "funnel_diagnostic": {
        "headline": "We find the leak in your funnel.",
        "concept": ["Flow: a funnel checked stage by stage, close rate, show rate,",
                    "booking, with ONE stage flagged as the leak."],
        "story": False,
        "set": "service",
        "archetype": "flow",
    },
    "social_done_for_you": {
        "headline": "Your social posts itself.",
        "concept": ["Split: a quiet page on one side, a page with a steady daily",
                    "posting rhythm on the other, the owner's phone in their pocket."],
        "story": False,
        "set": "service",
        "archetype": "split",
    },
    "one_partner": {
        "headline": "Ads, sales, and social. One place.",
        "concept": ["Three labeled streams, ads, sales, and social, flowing into one",
                    "LASSO mark. All in one place, done for you."],
        "story": False,
        "set": "service",
        "archetype": "hero",
    },
    "website_done_for_you": {
        "headline": "Your website, done for you.",
        "concept": ["Typography forward with one small browser window icon.",
                    "A StoryBrand website built and handled for the owner."],
        "story": False,
        "set": "service",
        "archetype": "headline",
    },
}


def parse_args(args):
    """
    STRICT CLI parsing for regen-library. Supports `--only KEY`, `--only=KEY`,
    `--set brand|service|all` (both forms), and `--dry-run`. Returns
    (only, set_name, dry_run, error). Any unrecognized token is an ERROR, never a
    silent fall-through: a typo'd flag must NOT quietly run the full batch (the
    exact live bug this guards against).
    """
    only, set_name, dry_run = None, "all", False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--only" and i + 1 < len(args):
            only = args[i + 1]
            i += 2
            continue
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--set" and i + 1 < len(args):
            set_name = args[i + 1]
            i += 2
            continue
        if a.startswith("--set="):
            set_name = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--dry-run":
            dry_run = True
            i += 1
            continue
        return None, "all", False, (f"unrecognized argument: {a}\n"
                                    "usage: python -m agent regen-library "
                                    "[--only <concept>] [--set brand|service|all] "
                                    "[--dry-run]")
    if set_name not in ("brand", "service", "all"):
        return None, "all", False, f"unknown set: {set_name} (brand, service, or all)"
    if only is not None and only not in CONCEPTS:
        return None, "all", False, (f"unknown concept: {only}\n"
                                    "known concepts: " + ", ".join(CONCEPTS))
    return only, set_name, dry_run, None


def assemble_prompts(key):
    """[(variant, prompt)] for one concept through the LOCKED house-style spec,
    composed by the concept's assigned layout ARCHETYPE (story variants inherit the
    archetype, recomposed for 9:16 with the safe zones)."""
    spec = CONCEPTS[key]
    arch = spec.get("archetype", "flow")
    out = [("feed", creative_studio.build_prompt(spec["headline"], spec["concept"],
                                                 archetype=arch))]
    if spec.get("story"):
        aspect, pixels, surface = STORY_ASPECT
        out.append(("story", creative_studio.build_prompt(
            spec["headline"], spec["concept"],
            aspect=aspect, pixels=pixels, surface=surface, archetype=arch)))
    return out


def _generate_one(key, variant, nano_client, out_dir):
    spec = CONCEPTS[key]
    suffix = "" if variant == "feed" else "_story"
    out_path = os.path.join(out_dir, f"{V2_PREFIX}{key}{suffix}.png")
    kwargs = {"archetype": spec.get("archetype", "flow")}
    if variant == "story":
        aspect, pixels, surface = STORY_ASPECT
        kwargs.update({"aspect": aspect, "pixels": pixels, "surface": surface})
    return creative_studio.generate(spec["headline"], spec["concept"],
                                    client=nano_client, out_path=out_path, **kwargs)


def run(only=None, dry_run=False, nano_client=None, s3_client=None, out_dir=None,
        set_name="all"):
    """
    Regenerate the batch (one concept via `only`; one SET via set_name, brand or
    service, default all). Returns a summary dict per concept. dry_run prints the
    assembled prompts and spends NOTHING (no Gemini call, no hosting, no files).
    """
    if only:
        keys = [only]
    elif set_name in ("brand", "service"):
        keys = [k for k, v in CONCEPTS.items() if v.get("set") == set_name]
    else:
        keys = list(CONCEPTS)
    unknown = [k for k in keys if k not in CONCEPTS]
    if unknown:
        print(f"unknown concept(s): {', '.join(unknown)}")
        print("known concepts: " + ", ".join(CONCEPTS))
        return {}
    if only:
        # Single-concept mode: regenerate ONLY this concept fresh (its story
        # variant included) and print only its new URL(s). Never the full library.
        print(f"regenerating ONLY {only} "
              f"({'feed + story' if CONCEPTS[only].get('story') else 'feed'})")

    out_dir = out_dir or config.LIBRARY_PATH
    results = {}
    for key in keys:
        spec = CONCEPTS[key]
        variants = assemble_prompts(key)
        if dry_run:
            for variant, prompt in variants:
                print(f"\n===== {key} ({variant}) =====\n{prompt}")
            results[key] = {"dry_run": True, "variants": [v for v, _ in variants]}
            continue

        results[key] = {"files": [], "urls": []}
        for variant, _prompt in variants:
            art = _generate_one(key, variant, nano_client, out_dir)
            if art is None:
                print(f"[regen] {key} ({variant}): generation unavailable "
                      "(arm AGENT_NANO_ENABLED + AGENT_NANO_API_KEY). Stopping.")
                return results
            hosted = media_host.host_media(art["path"], HOST_TENANT, client=s3_client)
            sidecar = {
                "concept": key,
                "headline": spec["headline"],
                "generated": date.today().isoformat(),
                "style": "v2",
                "archetype": spec.get("archetype", "flow"),
                "set": spec.get("set", "brand"),
            }
            if hosted:
                sidecar["public_url"] = hosted
            sidecar_path = os.path.splitext(art["path"])[0] + ".json"
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                json.dump(sidecar, fh, indent=2)
            results[key]["files"].append(os.path.basename(art["path"]))
            results[key]["urls"].append(hosted or "(hosting unavailable)")
            print(f"{key} ({variant}): {hosted or 'HOSTING UNAVAILABLE, local only'}")
    return results
