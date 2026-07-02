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
        "concept": ["Tension: a gym floor where the owner once struggled, empty slots and scattered leads, labeled OUR GYM.",
                    "Resolution: the same proven system carried by arrows into a thriving gym labeled YOUR GYM, members training."],
        "story": True,
        "set": "brand",
        "archetype": "flow",
    },
    "one_screen": {
        "headline": "Every lead, every post, every result. One screen.",
        "concept": ["Tension: a desk chaotic with scattered app windows and sticky notes, the owner lost in the mess.",
                    "Resolution: all of it resolving into one calm dashboard the owner reads at a glance, labeled LEADS, POSTS, RESULTS."],
        "story": False,
        "set": "brand",
        "archetype": "hero",
    },
    "three_step_path": {
        "headline": "How gyms grow with LASSO",
        "concept": ["Tension: at the bottom of the path an empty gym floor with scattered leads, labeled LEADS.",
                    "Resolution: the path climbs through a working follow up system labeled SYSTEM to a full gym floor labeled MEMBERS."],
        "story": True,
        "set": "brand",
        "archetype": "path",
    },
    "follow_up_problem": {
        "headline": "Most gyms don't have a lead problem. They have a follow up problem.",
        "concept": ["Tension: leads pooling beside an unanswered phone, going cold.",
                    "Resolution: on the other side an answered phone with follow up flowing the same leads through to members."],
        "story": False,
        "set": "brand",
        "archetype": "split",
    },
    "posting_cadence": {
        "headline": "Post every day. Grow every month.",
        "concept": ["Tension: a gym page gone quiet, an empty calendar, nobody watching.",
                    "Resolution: the same page posting daily, the calendar full, people engaging. No counts, no percentages."],
        "story": False,
        "set": "brand",
        "archetype": "split",
    },
    "speed_to_lead_concept": {
        "headline": "Answer leads fast. Close more of them.",
        "concept": ["Tension: a new lead's message sits unanswered while a clock runs and the lead drifts toward another gym.",
                    "Resolution: the gym that answers first welcomes that lead at the door. No hours or minutes claim text."],
        "story": False,
        "set": "brand",
        "archetype": "hero",
    },
    "system_runs_itself": {
        "headline": "Your follow up runs on autopilot",
        "concept": ["Tension: the owner used to be chained to the front desk, chasing leads instead of coaching.",
                    "Resolution: now they coach members on the floor while behind them a small machine quietly posts, follows up, and books."],
        "story": False,
        "set": "brand",
        "archetype": "flow",
    },
    "coach_in_your_corner": {
        "headline": "We coach your sales team",
        "concept": ["Tension: an owner slumped at a messy desk, out of answers.",
                    "Resolution: a guide beside them handing over one simple visible plan, the owner sitting up."],
        "story": False,
        "set": "brand",
        "archetype": "headline",
    },
    "ads_done_for_you": {
        "headline": "We run your ads. You run your gym.",
        "concept": ["Tension: an owner stuck at a laptop wrestling ad dashboards while the gym floor waits behind them.",
                    "Resolution: the owner back coaching on the floor while the ads run on their own and leads arrive."],
        "story": False,
        "set": "service",
        "archetype": "split",
    },
    "follow_up_system": {
        "headline": "Every lead gets a follow up.",
        "concept": ["Tension: a fresh lead about to go cold, the phone silent.",
                    "Resolution: text, call, and email touches carry the lead to a booked session on the calendar, labeled LEAD, FOLLOW UP, BOOKED."],
        "story": False,
        "set": "service",
        "archetype": "flow",
    },
    "booked_to_close": {
        "headline": "From lead to member.",
        "concept": ["Tension: a lead standing at the first stop of the journey, not yet a member.",
                    "Resolution: stops labeled LEAD, BOOKED, SHOWED, CLOSED end with a member training on the gym floor, labeled MEMBER."],
        "story": False,
        "set": "service",
        "archetype": "path",
    },
    "sales_training": {
        "headline": "We train your sales team.",
        "concept": ["Tension: a team member frozen on a sales call, no words coming.",
                    "Resolution: a coach hands them the phone playbook and the next call books."],
        "story": False,
        "set": "service",
        "archetype": "hero",
    },
    "funnel_diagnostic": {
        "headline": "We find the leak in your funnel.",
        "concept": ["Tension: a funnel full of people leaking at one stage, would be members dripping away.",
                    "Resolution: the leaking stage flagged and fixed, people flowing through to the gym floor."],
        "story": False,
        "set": "service",
        "archetype": "flow",
    },
    "social_done_for_you": {
        "headline": "Your social posts itself.",
        "concept": ["Tension: a gym page quiet for weeks, the owner's phone heavy in their pocket.",
                    "Resolution: the page posting daily on its own while the owner coaches, phone still in the pocket."],
        "story": False,
        "set": "service",
        "archetype": "split",
    },
    "one_partner": {
        "headline": "Ads, sales, and social in one place",
        "concept": ["Tension: an owner juggling separate vendors for ads, for sales, for social, arms full.",
                    "Resolution: three streams labeled ADS, SALES, SOCIAL flowing into one LASSO mark, all in one place, done for you."],
        "story": False,
        "set": "service",
        "archetype": "hero",
    },
    "website_done_for_you": {
        "headline": "Your website, done for you.",
        "concept": ["Tension: an owner poking at a half built website late at night, nothing working.",
                    "Resolution: a clean finished site in one small browser window, built and handled for them."],
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
