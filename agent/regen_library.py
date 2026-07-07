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
import re
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
    "b2b_five_companies": {
        "headline": "Five companies. Five invoices. Zero answers.",
        "concept": ["Tension: an owner buried at the front desk under five separate service invoices, five app windows open, none of them agreeing.",
                    "Resolution: the same owner reading one calm platform screen where every lead lands in one place, labeled ADS, SALES, SOCIAL.",
                    "Support copy (caption, never rendered): One platform. Every lead. Zero blind spots.",
                    "Art note (never rendered): the hand holding the phone is a natural realistic skin tone, not a navy silhouette.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "layout": "contrast",
        "canvas": "split",
        "archetype": "split",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_speed_to_lead": {
        "headline": "The gym that answers first wins the member.",
        "concept": ["Tension: a fresh lead's message glowing unanswered on a phone while a rival gym's reply already lands.",
                    "Resolution: this gym answers in seconds and a live person books the appointment on the calendar, labeled ANSWERED, BOOKED.",
                    "Support copy (caption, never rendered): AI follows up in seconds. A live person books the appointment.",
                    "Art note (never rendered): hands holding phones are natural realistic skin tone, matched to b2b_five_companies for set consistency.",
                    "Kicker copy (caption, never rendered): We chase. You close.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "layout": "poster",
        "canvas": "red",
        "archetype": "hero",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
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
        "layout": "stat_hero",
        "canvas": "navy",
        "archetype": "headline",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
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
        "layout": "framework",
        "canvas": "split",
        "archetype": "flow",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_booking_gap": {
        "headline": "We book 71.9 percent. The industry books 18.5 percent.",
        "cite": ["We book 71.9 percent. The industry books 18.5 percent. Same leads, very different outcomes."],
        "concept": ["Tension: a gym owner watching leads arrive but only a fraction ever reach the calendar, the gap invisible and unnamed.",
                    "Resolution: the booking rate compared side by side, LASSO at 71.9 against an 18.5 industry average, the gap closed by a live booker and AI follow up working together, labeled OUR RATE, INDUSTRY.",
                    "Support copy (caption, never rendered): Same leads. Very different outcomes.",
                    "Support copy (caption, never rendered): Source: platform_2026_receipts, verified booking rate data.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "layout": "framework",
        "canvas": "split",
        "archetype": "flow",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_16_cpl": {
        "headline": "$16 blended cost per lead. Verified.",
        "cite": ["$16 blended cost per lead. Blended across the LASSO "
                 "portfolio, roughly half typical industry cost.",
                 "Trusted by 500+ gym owners"],
        "concept": ["Tension: most gyms overpaying for every single lead and never checking the math.",
                    "Resolution: the blended portfolio number verified line by line, the owner reading it calm at a glance.",
                    "Art note (never rendered): person shown is a GYM OWNER in athletic wear standing in the gym, turf and rig and equipment behind, not a businessman in a suit. This gym owner is the consistent brand person used across every b2b concept that shows a person.",
                    "Support copy (caption, never rendered): Most of the industry pays double.",
                    "CTA copy (caption, never rendered): Trusted by 500+ gym owners"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "layout": "stat_hero",
        "canvas": "cream",
        "archetype": "headline",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
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
        "layout": "framework",
        "canvas": "cream",
        "archetype": "path",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
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
        "layout": "poster",
        "canvas": "navy",
        "archetype": "hero",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
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
        "layout": "stat_hero",
        "canvas": "red",
        "archetype": "split",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
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
        "layout": "stat_hero",
        "canvas": "navy",
        "archetype": "hero",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
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
        "layout": "checklist",
        "canvas": "navy",
        "archetype": "path",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    # ---- B2B expansion set (July 2026) -----------------------------------------
    # Ten additional b2b concepts. Specs only; render by hand via regen-library.
    # No digits in non-cited headlines. Source fragments listed in concept copy.
    "b2b_flat_revenue": {
        "headline": "Flat for a year is not a talent problem. It is math.",
        "concept": ["Tension: a gym owner twelve months into a plateau, blaming coaches, the season, the market, every cause except the funnel math.",
                    "Resolution: the same owner reading a simple model that names the exact leg holding revenue flat, labeled CLOSE RATE, LEAD VOLUME, REVENUE.",
                    "Support copy (caption, never rendered): The funnel does not lie.",
                    "Support copy (caption, never rendered): Source: The Full Gym, growth is math chapter.",
                    "CTA copy (caption, never rendered): Free 20 minute funnel diagnosis"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "layout": "stat_hero",
        "canvas": "navy",
        "archetype": "headline",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_dead_leads": {
        "headline": "Your old leads were never called.",
        "concept": ["Tension: a long list of leads sitting in a CRM unworked, names aging in silence, the gym blaming a bad month.",
                    "Resolution: the same list worked automatically by an AI follow up engine, every name contacted, every reply routed to a live booker, labeled FOLLOWED UP, BOOKED.",
                    "Support copy (caption, never rendered): Leads do not die in your ads. They die in the handoffs.",
                    "Support copy (caption, never rendered): Source: follow up problem doctrine, platform overview.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "layout": "contrast",
        "canvas": "cream",
        "archetype": "split",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_monday_numbers": {
        "headline": "Every Monday. Every number. Zero blind spots.",
        "concept": ["Tension: an owner guessing at performance from a spreadsheet already two weeks old, flying blind into the week.",
                    "Resolution: a single portal screen showing leads, shows, closes, ad spend, and runway all live, the owner reading it in one glance, labeled LEADS, SHOWS, CLOSES.",
                    "Support copy (caption, never rendered): Agencies send reports. LASSO hands you the cockpit.",
                    "Support copy (caption, never rendered): Source: LASSO Portal reporting, 2026 platform positioning.",
                    "CTA copy (caption, never rendered): See the cockpit live"],
        "story": False,
        "set": "b2b",
        "pillar": "The AI agents",
        "layout": "device",
        "canvas": "navy",
        "archetype": "hero",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_owner_brain": {
        "headline": "We think like owners, not marketers.",
        "concept": ["Tension: a marketing agency celebrating impressions while the gym owner watches revenue stand still.",
                    "Resolution: the owner and a partner reading the same numbers and asking the same question: which leg of the funnel moves the revenue, labeled CLOSE RATE, REVENUE.",
                    "Support copy (caption, never rendered): Your only job is signing people up.",
                    "Support copy (caption, never rendered): Source: LASSO funnel diagnostic order doctrine.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "layout": "poster",
        "canvas": "cream",
        "archetype": "hero",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_thirty_day_diagnose": {
        "headline": "In thirty days you will know where your funnel leaks.",
        "concept": ["Tension: a gym six weeks into a new ad campaign with no idea whether the problem is leads, bookings, shows, or closes.",
                    "Resolution: thirty days of clean data revealing the exact step where revenue disappears, labeled CLOSE RATE, SHOW RATE, BOOKING RATE, LEAD VOLUME, the fix named and sequenced.",
                    "List copy (caption, never rendered): Close rate, Show rate, Booking rate, Lead volume",
                    "Support copy (caption, never rendered): Diagnose in order: close 70%+, show 50%+, book 50%+, leads 40%+.",
                    "Support copy (caption, never rendered): Source: funnel diagnostic thresholds 70/50/50/40.",
                    "CTA copy (caption, never rendered): Free 20 minute funnel diagnosis"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "layout": "framework",
        "canvas": "navy",
        "archetype": "flow",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_duct_tape": {
        "headline": "Stop duct taping tools together.",
        "concept": ["Tension: five browser tabs, three separate invoices, a follow up tool that does not talk to the ad account, leads dying in every handoff.",
                    "Resolution: one platform where ads, follow up, booking, reporting, and the site connect on their own, labeled ONE PLATFORM.",
                    "Support copy (caption, never rendered): Six engines. One job: your MRR.",
                    "Support copy (caption, never rendered): Source: one platform positioning, six engines doctrine.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "layout": "contrast",
        "canvas": "red",
        "archetype": "split",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_one_partner": {
        "headline": "One partner. Ads, sales, nurture, site, social.",
        "concept": ["Tension: an owner managing an ad agency, a separate sales trainer, a website shop, and a social media person, four invoices and nobody accountable for the whole number.",
                    "Resolution: one partner accountable for all six engines at once, the revenue number the only metric anyone tracks, labeled ADS, NURTURE, SALES, SITE, SOCIAL.",
                    "Support copy (caption, never rendered): One platform. Every lead. Zero blind spots.",
                    "Support copy (caption, never rendered): Source: six engines platform overview.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "layout": "diagram",
        "canvas": "navy",
        "archetype": "flow",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_done_for_you": {
        "headline": "Done for you.",
        "concept": ["Tension: an owner trying to learn ads, manage follow up, rebuild the website, train the sales staff, and post on social all in the same week they were supposed to be coaching.",
                    "Resolution: every piece handed off and handled, the checklist complete without the owner touching it, labeled DONE on each item.",
                    "List copy (caption, never rendered): Ads managed, Follow up automated, Site built and audited, Sales team trained, Social handled",
                    "Support copy (caption, never rendered): Your only job is signing people up.",
                    "Support copy (caption, never rendered): Source: done for you service definition, platform overview.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "All in one offer",
        "layout": "checklist",
        "canvas": "cream",
        "archetype": "path",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_speed_decay": {
        "headline": "The lead you call in 5 minutes beats 10 you call tomorrow.",
        "cite": ["Contact a new lead within 5 minutes and you can lift conversions up to 80 percent."],
        "concept": ["Tension: a stack of yesterday's leads with a gym owner working through them while a fresh lead from this morning sits unopened.",
                    "Resolution: a fresh lead answered in seconds by AI and routed to a live booker before the hour turns, labeled ANSWERED, BOOKED.",
                    "Support copy (caption, never rendered): Contact a new lead within 5 minutes and you can lift conversions up to 80 percent.",
                    "Support copy (caption, never rendered): Source: speed to lead doctrine.",
                    "CTA copy (caption, never rendered): We chase. You close."],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "layout": "chart",
        "canvas": "navy",
        "archetype": "headline",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    "b2b_retention_first": {
        "headline": "Members you keep beat members you replace.",
        "concept": ["Tension: a gym running hard acquisition campaigns to cover members slipping out the back door each month, the bucket refilling instead of growing.",
                    "Resolution: the churn rate named and reduced, the same acquisition budget now building net growth instead of refilling a leaky tank, retain first then scale.",
                    "Support copy (caption, never rendered): healthy churn is 3 to 5 percent monthly. Fix the back door first.",
                    "Support copy (caption, never rendered): Source: churn and retention doctrine.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "b2b",
        "pillar": "Sales are now",
        "layout": "stat_hero",
        "canvas": "cream",
        "archetype": "headline",
        "art_directive": "Photographic realism throughout. Fewer visual elements, cleaner composition. Where a person appears, use the consistent gym owner brand person: athletic wear, standing in the gym, turf and rig and equipment visible. V3 palette: navy #121E3C, red #FF0000, sky #5EB9E6, cream #FAF6F0.",
    },
    # ---- PLATFORM set (LASSO Platform Overview 2026; source 08_platform_2026.md).
    # Copy is VERBATIM from the approved brief; every stat carries a `cite`
    # that resolves against a platform_2026 USE line. Canvas + layout are the
    # brief's explicit assignments through the locked variant system.
    "platform_stuck_lasso": {
        "headline": "Revenue stuck for months?",
        "concept": ["Tension: the STUCK zone, five vendors, leads dying in handoffs, gut feel numbers.",
                    "Resolution: the WITH LASSO zone, one platform, every lead worked, honest numbers.",
                    "CTA copy (caption, never rendered): Your only job is signing people up."],
        "story": False,
        "set": "platform",
        "layout": "contrast",
        "canvas": "split",
        "archetype": "split",
    },
    "platform_719_booking": {
        "headline": "71.9% booked",
        "cite": ["71.9% booked vs an 18.5% industry average."],
        "concept": ["Tension: the industry books 18.5% of its leads; most never hear back at all.",
                    "Resolution: the same leads at a LASSO gym, 71.9% booked onto the calendar.",
                    "Support copy (caption, never rendered): vs an 18.5% industry average. Same leads. Very different outcomes.",
                    "CTA copy (caption, never rendered): We chase. You close."],
        "story": False,
        "set": "platform",
        "layout": "stat_hero",
        "canvas": "navy",
        "archetype": "hero",
    },
    "platform_six_engines": {
        "headline": "Six engines. One job: your MRR.",
        "concept": ["Tension: an owner running six separate tools that never talk to each other.",
                    "Resolution: six engines in one system, each feeding the next, one login, labeled ADS, GOOGLE, NURTURE, WEBSITE, SOCIAL, PORTAL.",
                    "List copy (caption, never rendered): Paid ads, Google, AI nurture + live bookers, Website, Social, Portal",
                    "CTA copy (caption, never rendered): One platform. Every lead. Zero blind spots."],
        "story": False,
        "set": "platform",
        "layout": "framework",
        "canvas": "cream",
        "archetype": "flow",
    },
    "platform_nurture_proof": {
        "headline": "What happens to every lead at a LASSO gym",
        "cite": ["297 nurtured, 141 responded, 100+ appointments across four gyms.",
                 "71.9% top booking rate vs the 18.5% industry average."],
        "concept": ["Tension: leads arriving and going quiet, nobody following up.",
                    "Resolution: the nurture rail carrying them down, stops labeled NURTURED, RESPONDED, BOOKED.",
                    "List copy (caption, never rendered): 297 nurtured, 141 responded, 100+ booked, 71.9% top booking rate",
                    "Support copy (caption, never rendered): Nearly four times the intros from the exact same leads.",
                    "CTA copy (caption, never rendered): Free 20 minute Growth Call"],
        "story": False,
        "set": "platform",
        "layout": "framework",
        "canvas": "navy",
        "archetype": "flow",
    },
    "platform_8_of_10": {
        "headline": "8 of 10",
        "cite": ["8 of 10 paid leads never even reach the average gym calendar."],
        "concept": ["Tension: ten paid leads arriving, eight of them evaporating before the calendar.",
                    "Resolution: the LASSO path where every lead is worked to the calendar.",
                    "Support copy (caption, never rendered): paid leads never even reach the average gym calendar.",
                    "CTA copy (caption, never rendered): Stop paying for leads nobody calls."],
        "story": False,
        "set": "platform",
        "layout": "stat_hero",
        "canvas": "red",
        "archetype": "hero",
    },
    "platform_fit_mamas": {
        "headline": "$19K to $47K",
        "cite": ["Fit Mamas Tribe took monthly revenue from $19K to $47K on the "
                 "LASSO system. Average client value up from $99 to $167 at the "
                 "same time."],
        "concept": ["Tension: a gym stuck at its old monthly number.",
                    "Resolution: the same gym on the LASSO system at more than double, the owner reading honest numbers.",
                    "Support copy (caption, never rendered): Fit Mamas Tribe monthly revenue on the LASSO system. Average client value up from $99 to $167 at the same time.",
                    "CTA copy (caption, never rendered): Real gyms. Real receipts."],
        "story": False,
        "set": "platform",
        "layout": "stat_hero",
        "canvas": "cream",
        "archetype": "hero",
    },
    "platform_courage_million": {
        "headline": "First $1M year",
        "cite": ["Courage Fitness: First $1M year. $84K MRR.",
                 "Courage Fitness: 30 to 80+ leads per month and $84K MRR, "
                 "evenings back included."],
        "concept": ["Tension: a strong gym that could never crack the next revenue level.",
                    "Resolution: Courage Fitness over the line, the calendar full, the owner off the front desk.",
                    "Support copy (caption, never rendered): Courage Fitness: 30 to 80+ leads per month and $84K MRR, evenings back included.",
                    "CTA copy (caption, never rendered): Real gyms. Real receipts."],
        "story": False,
        "set": "platform",
        "layout": "stat_hero",
        "canvas": "navy",
        "archetype": "hero",
    },
    "platform_cockpit": {
        "headline": "Agencies send reports. LASSO hands you the cockpit.",
        "concept": ["Tension: a monthly PDF report the owner cannot act on, already out of date.",
                    "Resolution: a live cockpit view, every lead, every dollar, every booking on one screen.",
                    "Support copy (caption, never rendered): Every lead, every dollar, every booking. One login, updated live.",
                    "CTA copy (caption, never rendered): Honest numbers or no numbers."],
        "story": False,
        "set": "platform",
        "layout": "poster",
        "canvas": "navy",
        "archetype": "headline",
    },
    "platform_handoffs": {
        "headline": "Leads do not die in your ads. They die in the handoffs.",
        "concept": ["Tension: a lead passed between vendors, falling through the crack between two hands.",
                    "Resolution: one team carrying the same lead straight to the calendar, no handoff, no crack.",
                    "Support copy (caption, never rendered): Every vendor you add creates another crack for a lead to fall through.",
                    "CTA copy (caption, never rendered): One team. One system. One number to call."],
        "story": False,
        "set": "platform",
        "layout": "poster",
        "canvas": "red",
        "archetype": "headline",
    },
    "platform_close_first": {
        "headline": "More leads never fix a broken sales conversation.",
        "cite": ["Diagnose in order: close 70%+, show 50%+, book 50%+, leads 40%+."],
        "concept": ["Tension: an owner pouring more leads into a funnel that leaks at the close.",
                    "Resolution: the funnel diagnosed from the close upward, the failing leg flagged, labeled CLOSE, SHOW, BOOK, LEADS.",
                    "List copy (caption, never rendered): 1 Close 70%+, 2 Show 50%+, 3 Book 50%+, 4 Leads 40%+",
                    "Support copy (caption, never rendered): The first leg that fails is where your revenue hides.",
                    "CTA copy (caption, never rendered): Free 20 minute funnel diagnosis"],
        "story": False,
        "set": "platform",
        "layout": "framework",
        "canvas": "split",
        "archetype": "path",
    },
    # ---- PLATFORM_ADS set (grammar V2; source 08_platform_2026.md). Copy is
    # VERBATIM from the approved brief. Every concept cites a platform_2026
    # USE line; every CTA routes the quiz (the shared destination line).
    # Dashboard/mockup numerals inside a VISUAL line are illustrative device
    # content from the approved brief, never caption copy.
    "platform_ads_stuck": {
        "headline": "Revenue stuck? It is not you.",
        "cite": ["One platform. Every lead. Zero blind spots."],
        "concept": ["Tension: a flat gray revenue line labeled STUCK, months of it.",
                    "Resolution: a red line bending up labeled WITH LASSO, axis label MONTHLY REVENUE.",
                    "CTA copy (caption, never rendered): Find the leak in 2 minutes",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "chart",
        "canvas": "navy",
        "archetype": "hero",
    },
    "platform_ads_handoffs": {
        "headline": "Your leads are dying in the handoffs.",
        "cite": ["Leads do not die in your ads. They die in the handoffs."],
        "concept": ["Tension: a vertical funnel LEADS, BOOKED, SHOWED, SIGNED with red leaks escaping between every stage, label EVERY GAP = LOST MEMBERS.",
                    "Resolution: the same funnel sealed by one team, every stage handing off clean.",
                    "CTA copy (caption, never rendered): One team. One system. One login.",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "diagram",
        "canvas": "navy",
        "archetype": "flow",
    },
    "platform_ads_booking_bars": {
        "headline": "Same leads. Four times the intros.",
        "cite": ["71.9% booked vs an 18.5% industry average.",
                 "71.9% top booking rate vs the 18.5% industry average."],
        "concept": ["Tension: a short gray bar labeled INDUSTRY 18.5%, leads going nowhere.",
                    "Resolution: a tall red bar labeled LASSO GYMS 71.9% beside it, the gap unmistakable.",
                    "CTA copy (caption, never rendered): Top lead booking rate, real LASSO gyms",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "chart",
        "canvas": "split",
        "archetype": "split",
    },
    "platform_ads_six_engines": {
        "headline": "Six engines. One job: your MRR.",
        "cite": ["Six engines. One job: your MRR.",
                 "Paid ads, Google, AI nurture plus live bookers, a website "
                 "built to book, done for you social, and the LASSO Portal. "
                 "One login."],
        "concept": ["Tension: six disconnected tools pulling in six directions.",
                    "Resolution: a hub and spoke with a red center YOUR MRR and nodes ADS, GOOGLE, AI + BOOK, WEB, SOCIAL, PORTAL.",
                    "CTA copy (caption, never rendered): One platform. Every lead. Zero blind spots.",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "diagram",
        "canvas": "cream",
        "archetype": "hero",
    },
    "platform_ads_watched": {
        "headline": "Who watched your ad account yesterday?",
        "cite": ["$16 blended CPL across the portfolio; the industry pays 2x.",
                 "Honest numbers or no numbers."],
        "concept": ["Tension: an ad account running for weeks with nobody looking at it.",
                    "Resolution: a dashboard of four KPI tiles with red live dots: LEADS 214, BOOKED 61%, SPEND $4,120, CPL $16 (illustrative device content from the approved brief).",
                    "Kicker copy (caption, never rendered): Ours gets reviewed every single day.",
                    "CTA copy (caption, never rendered): Honest numbers or no numbers",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "device",
        "canvas": "navy",
        "archetype": "hero",
    },
    "platform_ads_35k": {
        "headline": "$35K+ Found. Reclaimed. Reinvested.",
        "cite": ["$35K+ in ad spend saved and put back to work. Over $17,000 "
                 "in one audit cycle alone."],
        "concept": ["Tension: ad spend quietly leaking out of unwatched campaigns.",
                    "Resolution: the colossal figure front and center, the waste found and put back to work.",
                    "Support copy (caption, never rendered): in ad spend saved and put back to work. Over $17,000 in one audit cycle alone.",
                    "CTA copy (caption, never rendered): Watched daily. Receipts included.",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "stat_hero",
        "canvas": "red",
        "archetype": "headline",
    },
    "platform_ads_budget_flow": {
        "headline": "Your ad budget should follow signups.",
        "cite": ["Paid ads, Google, AI nurture plus live bookers, a website "
                 "built to book, done for you social, and the LASSO Portal. "
                 "One login."],
        "concept": ["Tension: budget parked by habit in channels that stopped producing.",
                    "Resolution: FACEBOOK + IG and GOOGLE boxes joined to a red SIGNUPS circle at center by two way red arrows, money moving toward what signs people up.",
                    "Kicker copy (caption, never rendered): Not habits. Not hunches. Signups.",
                    "CTA copy (caption, never rendered): Money moves in near real time",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "diagram",
        "canvas": "split",
        "archetype": "flow",
    },
    "platform_ads_five_minutes": {
        "headline": "Answer in seconds or lose them forever.",
        "cite": ["297 nurtured, 141 responded, 100+ appointments across four gyms."],
        "concept": ["Tension: a fresh lead cooling in an unanswered inbox.",
                    "Resolution: a phone SMS mockup, bubbles: Hey! Saw you want to get started. When works? / Tomorrow after work? / a red bubble: Booked you 5:30pm with Coach Sam!",
                    "Kicker copy (caption, never rendered): AI in seconds. A live person books.",
                    "CTA copy (caption, never rendered): No lead ever sits cold",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "device",
        "canvas": "cream",
        "archetype": "hero",
    },
    "platform_ads_quiet_page": {
        "headline": "The gym is packed. Your page should prove it.",
        "cite": ["Paid ads, Google, AI nurture plus live bookers, a website "
                 "built to book, done for you social, and the LASSO Portal. "
                 "One login."],
        "concept": ["Tension: a thriving gym floor next to a social page that went quiet weeks ago.",
                    "Resolution: a phone IG profile grid mockup, handle @yourgym, a red banner POSTED FOR YOU across it.",
                    "CTA copy (caption, never rendered): Every post created and published for you",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "device",
        "canvas": "navy",
        "archetype": "hero",
    },
    "platform_ads_websites": {
        "headline": "Most gym websites describe. Ours convert.",
        "cite": ["A 25 point website launch audit on every build."],
        "concept": ["Tension: a pretty gym website that tells the story and books nobody.",
                    "Resolution: a browser mockup with a red BOOK YOUR INTRO button and a label row STORY . SEO . AI READY . BOOKING.",
                    "CTA copy (caption, never rendered): Verified with a 25 point launch audit",
                    "CTA destination (caption, never rendered): quiz.lassoframework.com"],
        "story": False,
        "set": "platform_ads",
        "layout": "device",
        "canvas": "split",
        "archetype": "headline",
    },
    # ---- SUMMIT_CAMPAIGN set (LASSO Growth Summit Nashville, November 7 and 8, 2026) ----
    # Source: LASSO Growth Summit Sponsor Promotion Guide + LASSO Email Sequence PDFs.
    "summit_announce": {
        "headline": "One room. 100 owners. Nashville is open.",
        "cite": ["100 seats only. When the room is full there is no waitlist and no encore."],
        "concept": [
            "Tension: a gym owner who knows the gap between grinding and scaling is not talent, it is proximity to operators who already solved what they are solving.",
            "Resolution: a seat claimed at the LASSO Growth Summit, one room with one hundred serious operators and ten industry leaders, two days to build the plan that changes the next year.",
            "Support copy (caption, never rendered): Capped at 100 on purpose. This is not a stadium event.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The seat",
        "layout": "poster",
        "canvas": "navy",
        "archetype": "hero",
    },
    "summit_playbook": {
        "headline": "You leave with a plan. Not a notebook.",
        "concept": [
            "Tension: a gym owner who has done the conferences and bought the courses and filled the notebooks, and come home to the same gym.",
            "Resolution: two days at the LASSO Growth Summit and you walk out with a complete growth playbook, offer, marketing, sales, retention, and systems, mapped to your gym and ready for January.",
            "Support copy (caption, never rendered): Most events end in energy. This one ends in a plan.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The playbook",
        "layout": "poster",
        "canvas": "cream",
        "archetype": "headline",
    },
    "summit_room": {
        "headline": "Ten leaders. Ninety nine other serious operators.",
        "concept": [
            "Tension: a gym owner trying to solve hard problems alone, without proximity to anyone who has already solved them.",
            "Resolution: two days at the LASSO Growth Summit, ten leaders at the table answering the questions that decide your next move, and ninety nine serious operators solving exactly what you are solving, close enough to actually learn from each other.",
            "Support copy (caption, never rendered): Capped at 100. Small enough to actually know the room.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The room",
        "layout": "contrast",
        "canvas": "navy",
        "archetype": "split",
    },
    "summit_flat_year": {
        "headline": "The flat year is the expensive one.",
        "concept": [
            "Tension: the gym owner repeating the same year, same revenue, same stress, same effort that never reaches the bank account, and calling it stability.",
            "Resolution: two days in Nashville where the flat year gets a name and a fix, surrounded by operators who already broke the pattern and built the plan to end it.",
            "Support copy (caption, never rendered): Staying the same is not free.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The playbook",
        "layout": "stat_hero",
        "canvas": "red",
        "archetype": "headline",
    },
    "summit_leaders": {
        "headline": "10 leaders who built and scaled. One room.",
        "cite": ["10 industry leaders who have built and scaled at the highest level."],
        "concept": [
            "Tension: a gym owner surrounded by advice from people who have never run a gym at the level they are trying to reach.",
            "Resolution: ten industry leaders who have built and scaled at the highest level, in one room in Nashville, at the table, answering the questions that decide your next move.",
            "Support copy (caption, never rendered): Not a guru with a slide deck. Operators who already solved it.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The room",
        "layout": "framework",
        "canvas": "navy",
        "archetype": "hero",
    },
    "summit_handbook": {
        "headline": "Walk out with a complete 2027 growth playbook.",
        "cite": ["Walk out with a complete 2027 growth playbook. Offer, marketing, sales, and systems mapped to your gym."],
        "concept": [
            "Tension: a gym owner heading into a new year with the same rough idea and no mapped plan, knowing that intention without a system just repeats the previous year.",
            "Resolution: two days at the LASSO Growth Summit and you walk out with your 2027 growth playbook in hand, offer, marketing, sales, and systems mapped to your gym and ready to run Monday morning.",
            "Support copy (caption, never rendered): That is the difference between inspiration and a year that actually changes.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The playbook",
        "layout": "checklist",
        "canvas": "cream",
        "archetype": "path",
    },
    "summit_seats_left": {
        "headline": "When the room is full there is no waitlist.",
        "concept": [
            "Tension: a gym owner who has watched the right opportunity close because they waited too long to decide.",
            "Resolution: the LASSO Growth Summit, one hundred seats, no waitlist and no encore when it fills, the leaders, the plan, and the room happening once and then gone.",
            "Support copy (caption, never rendered): The room is aiming to be sold out well before the event.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The seat",
        "layout": "stat_hero",
        "canvas": "red",
        "archetype": "headline",
    },
    "summit_final_call": {
        "headline": "Final seats. The next room is a year away.",
        "concept": [
            "Tension: a gym owner whose gut has been telling them to be there, and who is watching the last available seats disappear before they act.",
            "Resolution: one of the final seats claimed at the LASSO Growth Summit, the room that closes for good when it fills, knowing that after this the next chance to be in this room is a year away.",
            "Support copy (caption, never rendered): 100 owners, ten leaders, two days, a complete plan for your best year yet.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The seat",
        "layout": "poster",
        "canvas": "navy",
        "archetype": "hero",
    },
    "summit_sold_out": {
        "headline": "We built this room to sell out early. On purpose.",
        "concept": [
            "Tension: a gym owner who hears the summit is filling fast and assumes it is marketing pressure, the same urgency script every event runs.",
            "Resolution: the LASSO Growth Summit, a hard one hundred seat cap with no exceptions, built to sell out at least a month before the event so the operators who are in can plan their travel and fall around it.",
            "Support copy (caption, never rendered): That is the thing about a 100 seat cap. It feels far away until it is suddenly close.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The seat",
        "layout": "contrast",
        "canvas": "split",
        "archetype": "split",
    },
    "summit_countdown": {
        "headline": "Two days in Nashville. The room decides your next year.",
        "concept": [
            "Tension: a gym owner who can feel the difference between owners who are growing and owners who are grinding, and knows the gap is not talent but the room they have been in.",
            "Resolution: two days at Virgin Hotels Nashville, ten leaders and ninety nine other serious operators, the exact environment where the next year gets decided.",
            "Support copy (caption, never rendered): It is rarely talent. It is the room they are in.",
            "Support copy (caption, never rendered): Source: LASSO Growth Summit Email Sequence, July 2026.",
            "Footer (on image): Nashville, November 7 and 8 2026, lassoframework.com",
        ],
        "story": False,
        "set": "summit_campaign",
        "pillar": "The room",
        "layout": "poster",
        "canvas": "cream",
        "archetype": "hero",
    },
}


# ---- variant assignment (the LOCKED variant system, creative_studio docstring) ----
def canvas_for(key):
    """The concept's canvas: an explicit `canvas` field wins; otherwise the
    key hashes deterministically over CANVAS_ORDER, so a re-render always
    lands on the same canvas and the library distributes roughly evenly."""
    from .creative_studio import CANVAS_ORDER
    spec = CONCEPTS.get(key, {})
    if spec.get("canvas"):
        return spec["canvas"]
    digest = hashlib.sha256(key.encode()).hexdigest()
    return CANVAS_ORDER[int(digest, 16) % len(CANVAS_ORDER)]


def preferred_layout(spec):
    """The concept's layout: an explicit `layout` field wins; else stat
    concepts (a cite) take stat_hero, ordered lists take framework, outcome
    lists take checklist, opposition hooks take contrast, everything else
    poster (the current default look)."""
    if spec.get("layout"):
        return spec["layout"]
    if spec.get("cite"):
        return "stat_hero"
    joined = " ".join(spec.get("concept", []))
    if "List copy" in joined:
        # a numbered list is a framework; an outcome/benefit list a checklist
        return "framework" if re.search(r"List copy[^:]*: 1 ", joined) else "checklist"
    if " vs " in joined.lower() or " vs " in spec.get("headline", "").lower():
        return "contrast"
    return "poster"


def variant_for(key):
    """(canvas, layout) for one concept, or (None, None) while the concept
    declares NO variant field: such a concept renders through the ORIGINAL
    path byte for byte (zero visual change to already approved cards)."""
    spec = CONCEPTS.get(key, {})
    if not (spec.get("canvas") or spec.get("layout")):
        return None, None
    return canvas_for(key), preferred_layout(spec)


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
    if set_name not in ("brand", "service", "b2b", "platform",
                        "platform_ads", "summit_campaign", "all"):
        return None, "all", False, (f"unknown set: {set_name} (brand, service, "
                                    "b2b, platform, platform_ads, or all)")
    if only is not None and only not in CONCEPTS:
        return None, "all", False, (f"unknown concept: {only}\n"
                                    "known concepts: " + ", ".join(CONCEPTS))
    return only, set_name, dry_run, None


def assemble_prompts(key):
    """[(variant, prompt)] for one concept. A concept with variant fields
    (canvas/layout) composes through the LOCKED VARIANT SYSTEM; one without
    renders through the original archetype path byte for byte (zero visual
    change to already approved cards). Story variants inherit the archetype,
    recomposed for 9:16 with the safe zones."""
    spec = CONCEPTS[key]
    arch = spec.get("archetype", "flow")
    canvas, layout = variant_for(key)
    out = [("feed", creative_studio.build_prompt(spec["headline"], spec["concept"],
                                                 archetype=arch,
                                                 canvas=canvas, layout=layout))]
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
    if variant == "feed":
        canvas, layout = variant_for(key)
        if canvas:
            kwargs.update({"canvas": canvas, "layout": layout})
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
    elif set_name in ("brand", "service", "b2b", "platform", "platform_ads",
                      "summit_campaign"):
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
            v_canvas, v_layout = variant_for(key)
            if v_canvas and variant == "feed":
                sidecar["canvas"] = v_canvas
                sidecar["layout"] = v_layout
            if hosted:
                sidecar["public_url"] = hosted
            sidecar_path = os.path.splitext(art["path"])[0] + ".json"
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                json.dump(sidecar, fh, indent=2)
            results[key]["files"].append(os.path.basename(art["path"]))
            results[key]["urls"].append(hosted or "(hosting unavailable)")
            print(f"{key} ({variant}): {hosted or 'HOSTING UNAVAILABLE, local only'}")
    return results
