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
  - fabrication gate: the brand and service concepts are NON STAT by design (no
    numbers, no percent claims); captions are NOT generated here. Cards enter
    the normal drafter flow where captions come only from approved sources.
    The b2b swipe set is the ONE exception: a b2b concept may carry a stat ONLY
    with a `cite` naming its receipt in the approved claims source
    (02_verified_stats.md, LASSO B2B Ad Swipe File); every cited stat clears
    rotation.is_gate_clean against that source and an uncited stat never ships.
  - approval gate + publish flag: nothing this command makes posts anywhere.
    A card can only reach Meta through the daily draft plus Blake's tap.
  - rotation guard: this command never touches style_exclusions.json. Old cards
    stay excluded; new lasso_v2_ cards are picked up only because they exist and
    are gate-clean. Story variants (*_story.png) are never feed candidates.
"""

import hashlib
import json
import os
import time
from datetime import date, datetime, timezone

from . import config, creative_studio, media_host

V2_PREFIX = "lasso_v2_"
STORY_ASPECT = ("9:16", "1080x1920", "story post")
HOST_TENANT = "lasso_library"

# ---- the regen lock: one live batch at a time (a second invocation refuses) ----
# Stale safe: the lock clears itself when its holder pid is gone or the lock is
# older than LOCK_STALE_SECONDS (a crashed run never wedges the next one).
LOCK_FILE = ".regen_lock.json"
LOCK_STALE_SECONDS = 2 * 60 * 60
LAST_RUN_FILE = ".regen_last.json"


def _lock_path(out_dir):
    return os.path.join(out_dir, LOCK_FILE)


def _holder_is_stale(holder):
    try:
        os.kill(int(holder.get("pid", -1)), 0)
    except (OSError, ValueError, TypeError):
        return True  # the holder process is gone
    return time.time() - float(holder.get("ts", 0)) > LOCK_STALE_SECONDS


def _acquire_lock(out_dir):
    """(acquired, holder). Refuses while a live run holds the lock; a stale
    lock (dead pid or too old) auto clears with a printed note."""
    path = _lock_path(out_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            holder = json.load(fh) or {}
    except (OSError, ValueError):
        holder = None
    if holder is not None:
        if not _holder_is_stale(holder):
            return False, holder
        print(f"[regen] clearing stale lock (holder pid {holder.get('pid')} "
              f"since {holder.get('started', 'unknown')})")
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "ts": time.time(),
                   "started": datetime.now(timezone.utc).isoformat(
                       timespec="seconds")}, fh)
    return True, None


def _release_lock(out_dir):
    try:
        os.remove(_lock_path(out_dir))
    except OSError:
        pass


def _sha16(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError:
        return "unavailable"


def _print_summary(out_dir, results):
    """The end of batch table: concept, final content hash, url, one row per
    concept. A re-run notes that the prior run's hashes are superseded."""
    rows = {k: v for k, v in results.items() if "files" in v}
    if not rows:
        return
    last_path = os.path.join(out_dir, LAST_RUN_FILE)
    prior = {}
    try:
        with open(last_path, encoding="utf-8") as fh:
            prior = json.load(fh) or {}
    except (OSError, ValueError):
        pass
    superseded = sorted(set(prior) & set(rows))
    print("\nregen summary (concept, final hash, url):")
    stamped = dict(prior)
    for key, out in sorted(rows.items()):
        f = out["files"][0] if out["files"] else ""
        h = _sha16(os.path.join(out_dir, f)) if f else "not rendered"
        url = out["urls"][0] if out["urls"] else "(hosting unavailable)"
        print(f"  {key}  {h}  {url}")
        stamped[key] = h
    if superseded:
        print(f"note: this run supersedes prior hashes for {len(superseded)} "
              f"concept(s): {', '.join(superseded)}")
    try:
        with open(last_path, "w", encoding="utf-8") as fh:
            json.dump(stamped, fh)
    except OSError as e:
        print(f"[regen] could not persist run hashes: {type(e).__name__}: {e}")

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
        "headline": "You run the gym. We bring the plan.",
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
    # ---- B2B swipe set (July 2026 brief). Copy is VERBATIM from the approved
    # brief; "Support/Kicker/List/CTA copy" lines are caption copy the builder
    # never renders (the one-headline law). A stat headline carries `cite`, the
    # exact receipt line(s) in the approved claims source (02_verified_stats.md,
    # LASSO B2B Ad Swipe File section) that clear it through the gate.
    "b2b_five_vendors": {
        "headline": "Five vendors. Five invoices. Zero answers.",
        "concept": ["Tension: an owner buried at the front desk under five separate vendor invoices, five app windows open, none of them agreeing.",
                    "Resolution: the same owner reading one calm platform screen where every lead lands in one place, labeled ADS, SALES, SOCIAL.",
                    "Support copy (caption, never rendered): One platform. Every lead. Zero blind spots.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "archetype": "split",
    },
    "b2b_speed_to_lead": {
        "headline": "The gym that answers first wins the member.",
        "concept": ["Tension: a fresh lead's message glowing unanswered on a phone while a rival gym's reply already lands.",
                    "Resolution: this gym answers in seconds and a live person books the appointment on the calendar, labeled ANSWERED, BOOKED.",
                    "Support copy (caption, never rendered): AI follows up in seconds. A live person books the appointment.",
                    "Kicker copy (caption, never rendered): We chase. You close.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "archetype": "hero",
    },
    "b2b_35k_caught": {
        "headline": "$35,000 in wasted gym ad spend. Found. Named. Fixed.",
        "cite": ["The Ad Engine has caught more than $35,000 in wasted gym ad "
                 "spend. One recent audit cycle flagged over $17,000."],
        "concept": ["Tension: ad spend leaking out of a campaign dashboard nobody reads, money dripping away unnoticed.",
                    "Resolution: the engine reading every campaign line and flagging the leak, the waste caught and named.",
                    "Support copy (caption, never rendered): Our engine reads every campaign every single day.",
                    "CTA copy (caption, never rendered): Receipts, not reports"],
        "story": False,
        "set": "b2b",
        "pillar": "The AI agents",
        "archetype": "headline",
    },
    "b2b_dynamic_spend": {
        "headline": "Your ad budget should follow signups.",
        "concept": ["Tension: a budget stuck on yesterday's habits, spend flowing to ads nobody signs up from.",
                    "Resolution: budget moving on its own toward the campaigns producing signups, labeled SIGNUPS, BUDGET MOVES.",
                    "Support copy (caption, never rendered): Not habits. Not hunches. Signups.",
                    "Kicker copy (caption, never rendered): Money moves automatically. In near real time.",
                    "CTA copy (caption, never rendered): Dynamic ad spend, standard on every account"],
        "story": False,
        "set": "b2b",
        "pillar": "The AI agents",
        "archetype": "flow",
    },
    "b2b_16_cpl": {
        "headline": "$16 blended cost per lead. Verified.",
        "cite": ["$16 blended cost per lead. Blended across the LASSO "
                 "portfolio, roughly half typical industry cost.",
                 "Trusted by 500+ gym owners"],
        "concept": ["Tension: most gyms overpaying for every single lead and never checking the math.",
                    "Resolution: the blended portfolio number verified line by line, the owner reading it calm at a glance.",
                    "Support copy (caption, never rendered): Most of the industry pays double.",
                    "CTA copy (caption, never rendered): Trusted by 500+ gym owners"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "archetype": "headline",
    },
    "b2b_diagnosed_in_order": {
        "headline": "More leads will not fix a broken sales conversation.",
        "concept": ["Tension: an owner pouring more leads into a funnel while the sales conversation at the bottom stays broken.",
                    "Resolution: the journey diagnosed in order from the close upward, stops labeled CLOSE RATE, SHOW RATE, BOOKING BEHAVIOR, LEAD VOLUME, the bottleneck flagged.",
                    "List copy (caption, never rendered): 1 Close rate, 2 Show rate, 3 Booking behavior, 4 Lead volume",
                    "Support copy (caption, never rendered): We find the bottleneck before we spend a dollar.",
                    "CTA copy (caption, never rendered): Free 20 minute funnel diagnosis"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "archetype": "path",
    },
    "b2b_ai_search": {
        "headline": "Last night someone asked AI which gym to join.",
        "concept": ["Tension: a future member typing the question into an AI chat at night, your gym nowhere in the answer.",
                    "Resolution: your gym written into the answer before the question is asked, the seeker heading to your door.",
                    "Kicker copy (caption, never rendered): Were you the answer?",
                    "Support copy (caption, never rendered): We write the answer before your future member asks the question.",
                    "CTA copy (caption, never rendered): Free website audit"],
        "story": False,
        "set": "b2b",
        "pillar": "The AI agents",
        "archetype": "hero",
    },
    "b2b_dead_buttons": {
        "headline": "7 dead buttons on one gym website. The main one went nowhere.",
        "cite": ["A recent gym website audit found 7 dead buttons including "
                 "the primary CTA."],
        "concept": ["Tension: a visitor tapping the main call to action on a gym website and nothing happening, the lead walking away in silence.",
                    "Resolution: every button checked and wired, each tap landing on a working booking page, labeled TAP, BOOKED.",
                    "Support copy (caption, never rendered): Silent lead loss on every single visit.",
                    "CTA copy (caption, never rendered): Get your free website audit"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "archetype": "split",
    },
    "b2b_500_gyms": {
        "headline": "500+ gym owners deep. We know exactly why gyms stall.",
        "cite": ["Trusted by 500+ gym owners"],
        "concept": ["Tension: a stalled gym owner assuming the coaching is the problem.",
                    "Resolution: across hundreds of gyms the same pattern, the gaps between the leads and the floor, found and closed.",
                    "Support copy (caption, never rendered): It is almost never the coaching. It is the gaps.",
                    "CTA copy (caption, never rendered): One platform. Every lead. Zero blind spots."],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "archetype": "hero",
    },
    "b2b_ninety_days": {
        "headline": "Picture your gym ninety days from now.",
        "concept": ["Tension: today's owner buried in follow up, evenings gone, the numbers a mystery.",
                    "Resolution: ninety days on the journey ends at a full intro calendar and every lead worked in seconds, labeled FULL CALENDAR, EVENINGS BACK.",
                    "List copy (caption, never rendered): Full intro calendar, Numbers you trust at a glance, Every lead worked in seconds, Your evenings back",
                    "Support copy (caption, never rendered): The only thing left on your plate is signing people up.",
                    "CTA copy (caption, never rendered): You coach. We fill the room."],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "archetype": "path",
    },
}


def parse_args(args):
    """
    STRICT CLI parsing for regen-library. Supports `--only KEY`, `--only=KEY`,
    `--set brand|service|b2b|all` (both forms), and `--dry-run`. Returns
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
    if set_name not in ("brand", "service", "b2b", "all"):
        return None, "all", False, (f"unknown set: {set_name} "
                                    "(brand, service, b2b, or all)")
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
    elif set_name in ("brand", "service", "b2b"):
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
    # One live batch at a time (dry runs spend nothing and never lock): a
    # second invocation refuses instead of double rendering the library.
    locked = False
    if not dry_run:
        locked, holder = _acquire_lock(out_dir)
        if not locked:
            print(f"regen already running since {holder.get('started', 'unknown')} "
                  f"(pid {holder.get('pid', '?')}). Nothing started.")
            return {}
    results = {}
    try:
        results = _run_batch(keys, dry_run, nano_client, s3_client, out_dir)
        if not dry_run and len(keys) > 1:
            _print_summary(out_dir, results)
    finally:
        if locked:
            _release_lock(out_dir)
    return results


def _run_batch(keys, dry_run, nano_client, s3_client, out_dir):
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
            if spec.get("pillar"):
                sidecar["pillar"] = spec["pillar"]
            if spec.get("cite"):
                sidecar["cite"] = list(spec["cite"])
            if hosted:
                sidecar["public_url"] = hosted
            sidecar_path = os.path.splitext(art["path"])[0] + ".json"
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                json.dump(sidecar, fh, indent=2)
            results[key]["files"].append(os.path.basename(art["path"]))
            results[key]["urls"].append(hosted or "(hosting unavailable)")
            print(f"{key} ({variant}): {hosted or 'HOSTING UNAVAILABLE, local only'}")
    return results
