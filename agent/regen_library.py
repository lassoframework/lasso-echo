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
        "archetype": "flow",
    },
    "one_screen": {
        "headline": "Every lead, every post, every result. One screen.",
        "concept": ["Leads, posts, and results flow from three sides into one dashboard.",
                    "One central screen icon receiving three labeled streams."],
        "story": False,
        "archetype": "hero",
    },
    "three_step_path": {
        "headline": "Three steps. One path.",
        "concept": ["A simple path diagram with three labeled stops along one road.",
                    "No numbered text list; the path itself carries the three steps."],
        "story": True,
        "archetype": "path",
    },
    "follow_up_problem": {
        "headline": "Most gyms don't have a lead problem. They have a follow up problem.",
        "concept": ["Leads pooling beside an unanswered phone on one side.",
                    "On the other side an answered phone with leads flowing through."],
        "story": False,
        "archetype": "split",
    },
    "posting_cadence": {
        "headline": "Consistency beats intensity.",
        "concept": ["Two calendars side by side: one nearly empty and quiet, one with a",
                    "steady daily rhythm of posts. Illustrated, no counts, no percentages."],
        "story": False,
        "archetype": "split",
    },
    "speed_to_lead_concept": {
        "headline": "Speed to lead wins.",
        "concept": ["A clock and a lead handoff: speed as an idea.",
                    "No hours or minutes claim text anywhere on the card."],
        "story": False,
        "archetype": "hero",
    },
    "system_runs_itself": {
        "headline": "The system runs itself.",
        "concept": ["Gears turning a calendar that produces a checkmark.",
                    "Done for you as a machine that quietly works."],
        "story": False,
        "archetype": "flow",
    },
    "coach_in_your_corner": {
        "headline": "A coach in your corner.",
        "concept": ["A gym owner and a guide figure pointing together at one simple plan.",
                    "StoryBrand guide framing: the owner is the hero, the guide has the plan."],
        "story": False,
        "archetype": "headline",
    },
}


def parse_args(args):
    """
    STRICT CLI parsing for regen-library. Supports `--only KEY`, `--only=KEY`,
    and `--dry-run`. Returns (only, dry_run, error). Any unrecognized token is an
    ERROR, never a silent fall-through: a typo'd --only must NOT quietly run the
    full 10-card batch (the exact live bug this guards against).
    """
    only, dry_run = None, False
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
        if a == "--dry-run":
            dry_run = True
            i += 1
            continue
        return None, False, (f"unrecognized argument: {a}\n"
                             "usage: python -m agent regen-library "
                             "[--only <concept>] [--dry-run]")
    if only is not None and only not in CONCEPTS:
        return None, False, (f"unknown concept: {only}\n"
                             "known concepts: " + ", ".join(CONCEPTS))
    return only, dry_run, None


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


def run(only=None, dry_run=False, nano_client=None, s3_client=None, out_dir=None):
    """
    Regenerate the starter batch (or one concept via `only`). Returns a summary
    dict per concept. dry_run prints the assembled prompts and spends NOTHING
    (no Gemini call, no hosting, no files).
    """
    keys = [only] if only else list(CONCEPTS)
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
