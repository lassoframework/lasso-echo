"""
LASSO Growth Summit 2026 post queue.

Two-step workflow:
  Step 1 (Mac, with railway run):
    railway run python -m agent summit-queue \\
      --images-dir "/path/to/lasso_summit_v2_all14"
    Uploads 14 PNGs to R2 and saves summit_manifest.json next to this file.

  Step 2 (Railway container, via railway shell or railway exec):
    python -m agent summit-queue --from-manifest
    Reads summit_manifest.json, creates 28 HELD drafts (lasso_ig + lasso_fb)
    in Railway's /data/echo.db. Nothing publishes; every post waits for approval.

Run with no args to see current manifest status.
"""

import hashlib
import json
import os

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "summit_manifest.json")

# ---- Campaign data -------------------------------------------------------
# One entry per week. dates are every Monday Jul 28 -> Oct 27, 2026.

SUMMIT_POSTS = [
    {
        "week": 1,
        "date": "2026-07-28",
        "filename": "week01_v2_invitation.png",
        "caption": (
            "I saved you a seat in Nashville.\n\n"
            "The LASSO Growth Summit. November 7 and 8. Virgin Hotel Nashville. "
            "100 seats by invitation only.\n\n"
            "Two days. Ten leaders. One plan you leave with.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 2,
        "date": "2026-08-03",
        "filename": "week02_v2_deliverable.png",
        "caption": (
            "Most conferences leave you with a notebook full of highlights "
            "you never open again.\n\n"
            "Not this one.\n\n"
            "You walk out Sunday with your 2027 growth plan done. Revenue target "
            "and the member math to hit it. Your one broken funnel leg and the fix. "
            "Sales, retention, and team plays for the year. A 90 day action plan "
            "you run the Monday you get home.\n\n"
            "You leave with a plan, not a notebook.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 3,
        "date": "2026-08-10",
        "filename": "week03_v2_agenda.png",
        "caption": (
            "Ten sessions. Ten leaders. One plan you build page by page.\n\n"
            "Phase 1: where you are now, your 2027 revenue target, the funnel "
            "diagnostic, offer and positioning, lead generation.\n\n"
            "Phase 2: the sales system, retention, team and leadership, nutrition "
            "and client value, capacity and pricing.\n\n"
            "You leave with a complete 2027 growth plan.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 4,
        "date": "2026-08-17",
        "filename": "week04_v2_funnel.png",
        "caption": (
            "More ad spend will not fix a broken funnel.\n\n"
            "Walk the three legs in order. The weakest is where 2027 hides.\n\n"
            "Leads to book at 40% or above. Book to show at 50% or above. Show to "
            "close at 70% or above.\n\n"
            "Fix the weakest leg. Then scale.\n\n"
            "We build this together in Nashville. November 7 and 8.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 5,
        "date": "2026-08-24",
        "filename": "week05_v2_math.png",
        "caption": (
            "A goal without math is a wish.\n\n"
            "Set your 2027 revenue target. Subtract where you are today. "
            "That gap is the work. Divide by revenue per member. That is the "
            "members you need. Apply your close rate. That is leads per month.\n\n"
            "Now you have a number, not a goal.\n\n"
            "We run the math together in Nashville. November 7 and 8.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 6,
        "date": "2026-08-31",
        "filename": "week06_v2_room.png",
        "caption": (
            "100 gym owners. 10 leaders. 2 days.\n\n"
            "Everyone in that room is running a real business. "
            "Everyone came to leave with a plan.\n\n"
            "The LASSO Growth Summit. November 7 and 8. Virgin Hotel Nashville.\n\n"
            "100 seats only. Claim yours at lassoframework.com/summit"
        ),
    },
    {
        "week": 7,
        "date": "2026-09-07",
        "filename": "week07_v2_numbers.png",
        "caption": (
            "100 owners. 10 leaders. 2 days. 1 plan.\n\n"
            "You did not come to Nashville for notes. You came for a plan.\n\n"
            "November 7 and 8. Virgin Hotel Nashville.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 8,
        "date": "2026-09-14",
        "filename": "week08_v2_half_full.png",
        "caption": (
            "The room is more than half full.\n\n"
            "The most expensive year is the one you repeat.\n\n"
            "100 seats. November 7 and 8. Virgin Hotel Nashville.\n\n"
            "Lock in your seat at lassoframework.com/summit"
        ),
    },
    {
        "week": 9,
        "date": "2026-09-21",
        "filename": "week09_v2_moving_fast.png",
        "caption": (
            "Seats are moving fast. We are well past the halfway mark.\n\n"
            "100 seats only. If you have been thinking about it, now is the time.\n\n"
            "November 7 and 8. Virgin Hotel Nashville.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 10,
        "date": "2026-09-28",
        "filename": "week10_v2_last_seats.png",
        "caption": (
            "Down to the last few seats.\n\n"
            "When the room is full, there is no waitlist.\n\n"
            "100 seats. November 7 and 8. Virgin Hotel Nashville. $299 Early Bird.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 11,
        "date": "2026-10-05",
        "filename": "week11_v2_stakes.png",
        "caption": (
            "The gym that figures this out wins.\n\n"
            "Closing at 30 to 40% versus closing at 70% with a real sales system. "
            "Burning ad spend with no ROI versus 8 or more new members a month "
            "from profitable ads. Doing everything yourself versus a business "
            "that runs on systems, not hustle.\n\n"
            "Most gym owners are one system away from a breakthrough year.\n\n"
            "100 Owners. 10 Leaders. 2 Days. 1 Plan. November 7 and 8.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 12,
        "date": "2026-10-12",
        "filename": "week12_v2_outcome.png",
        "caption": (
            "Walk out with your 2027 growth plan.\n\n"
            "Not inspiration. Not notes. A written plan you run Monday morning.\n\n"
            "Revenue target and member math. Profitable ad system generating 8 or "
            "more new members a month. Complete sales, retention, and team playbook. "
            "90 day action plan starting the Monday you get home.\n\n"
            "$299 Early Bird. One new member pays for the ticket 10 times over.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 13,
        "date": "2026-10-19",
        "filename": "week13_v2_countdown.png",
        "caption": (
            "3 weeks out. Are you in?\n\n"
            "Early Bird is $299 and almost gone. General admission is $449 after that.\n\n"
            "10 speakers. 2 days. 1 plan. The room is nearly full.\n\n"
            "November 7 and 8. Nashville. 100 seats.\n\n"
            "lassoframework.com/summit"
        ),
    },
    {
        "week": 14,
        "date": "2026-10-26",
        "filename": "week14_v2_final_call.png",
        "caption": (
            "Final call. Doors close soon.\n\n"
            "This is the last week to claim your seat.\n\n"
            "November 7 and 8. Virgin Hotel Nashville. 100 seats.\n\n"
            "You did not come to Nashville for notes. You came for a plan.\n\n"
            "Early Bird $299. General $449. One new member pays for your ticket "
            "10 times over.\n\n"
            "lassoframework.com/summit"
        ),
    },
]

ACCOUNTS = ["lasso_ig", "lasso_fb"]


# ---- Sprint calendar (backward-anchored 5-cycle ramp) ----------------------
# Blake ruling 2026-08-05: the summit runs as a backward-anchored sprint that
# tightens as the event nears, then goes dark on the event days.
#
#   Cycle 1   Aug 21 -> Aug 30
#   Cycle 2   Sep 7  -> Sep 16
#   Cycle 3   Sep 24 -> Oct 3
#   Continuous Oct 11 -> Nov 6   (runs every day, no gaps)
#   Nov 7 + 8                    DARK (the event itself; no promo posts)
#
# Cadence ruling (Blake, today): up to 3 FEED posts per day is fine during the
# sprint. The welcome / new-client post is NOT a summit post and does NOT count
# toward this cadence — it sits on top (its own queue owns its slot). Stories run
# alongside feed posts (they also sit on top of the 3-feed cadence).
#
# This calendar is DORMANT behind AGENT_SUMMIT_CAMPAIGN_ENABLED (default OFF); it
# emits ordered slots but nothing is drafted or published until the flag is armed,
# and even then every draft is HELD for approval.

SPRINT_CYCLES = [
    ("2026-08-21", "2026-08-30"),   # cycle 1
    ("2026-09-07", "2026-09-16"),   # cycle 2
    ("2026-09-24", "2026-10-03"),   # cycle 3
    ("2026-10-11", "2026-11-06"),   # continuous run to the eve of the event
]

# Event days: no summit promo posts (the room is live).
SPRINT_DARK_DAYS = {"2026-11-07", "2026-11-08"}

# Max FEED posts per sprint day (Blake: up to 3). The welcome post does NOT count.
SPRINT_MAX_FEED_PER_DAY = 3

# Local sprint slot times (a 3rd midday slot on top of the shared morning/primary
# pair, so a day can carry up to 3 feed posts). HH:MM in POSTING_TIMEZONE.
SPRINT_SLOT_TIMES = ["07:30", "12:30", "18:30"]


def date_fromisoformat(s):
    from datetime import date
    return date.fromisoformat(s)


def _daterange(start_iso, end_iso):
    """Inclusive list of YYYY-MM-DD strings from start to end."""
    from datetime import date, timedelta
    s = date.fromisoformat(start_iso)
    e = date.fromisoformat(end_iso)
    out, cur = [], s
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def sprint_days():
    """Every posting day across the sprint cycles, in order, with the event days
    (Nov 7 + 8) removed. Ordered earliest to latest."""
    days = []
    for start, end in SPRINT_CYCLES:
        for d in _daterange(start, end):
            if d not in SPRINT_DARK_DAYS:
                days.append(d)
    return days


def _sprint_scheduled_for(day_key, hhmm):
    """ISO datetime in POSTING_TIMEZONE for a sprint slot (DST-correct)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from . import config
    d = date_fromisoformat(day_key)
    hh, mm = hhmm.split(":")
    tz = ZoneInfo(config.POSTING_TIMEZONE)
    return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=tz).isoformat()


def sprint_calendar(assets, posts_per_day=SPRINT_MAX_FEED_PER_DAY):
    """Map an ordered list of feed asset filenames onto the sprint calendar.

    Returns a list of slot dicts, one per (day, slot):
        {"date", "slot_index", "scheduled_for", "filename"}
    Assets cycle in order and repeat across the run (the sprint is longer than the
    card set by design, so cards recur; the rotation guard downstream keeps the same
    card from landing twice in a row). `posts_per_day` is capped at 3 (Blake).

    Pure and side-effect free: this is the schedule, not the drafts. Nothing here
    checks the flag or writes anything; callers gate on AGENT_SUMMIT_CAMPAIGN_ENABLED.
    """
    if not assets:
        return []
    posts_per_day = max(1, min(posts_per_day, SPRINT_MAX_FEED_PER_DAY))
    plan = []
    ai = 0
    prev = None
    for day in sprint_days():
        for slot in range(posts_per_day):
            # avoid the same card twice in a row across the day/slot boundary
            fname = assets[ai % len(assets)]
            if fname == prev and len(assets) > 1:
                ai += 1
                fname = assets[ai % len(assets)]
            plan.append({
                "date": day,
                "slot_index": slot,
                "scheduled_for": _sprint_scheduled_for(day, SPRINT_SLOT_TIMES[slot]),
                "filename": fname,
            })
            prev = fname
            ai += 1
    return plan


# ---- Helpers ---------------------------------------------------------------

def _draft_id(account_key, filename, date):
    h = hashlib.sha1(f"summit|{account_key}|{filename}|{date}".encode()).hexdigest()[:16]
    return f"summit_{h}"


def _load_manifest():
    p = os.path.normpath(MANIFEST_PATH)
    if not os.path.isfile(p):
        return {}
    with open(p) as f:
        return json.load(f)


def _save_manifest(data):
    p = os.path.normpath(MANIFEST_PATH)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)
    print(f"manifest saved: {p}")


# ---- Phase 1: upload images to R2 -----------------------------------------

def upload_images(images_dir):
    """Upload all 14 PNGs to R2. Requires hosting flag + R2 credentials (Railway env).
    Saves R2 URLs to summit_manifest.json. Safe to re-run; already-uploaded files
    are deduped by content hash."""
    from . import media_host, config

    if not config.hosting_enabled():
        raise RuntimeError(
            "AGENT_HOSTING_ENABLED is not set. Run with: "
            "AGENT_HOSTING_ENABLED=true railway run python -m agent summit-queue --images-dir PATH"
        )

    manifest = _load_manifest()
    uploaded = 0

    for post in SUMMIT_POSTS:
        fname = post["filename"]
        local = os.path.join(images_dir, fname)
        if not os.path.isfile(local):
            print(f"  MISSING: {local}")
            continue
        if fname in manifest:
            print(f"  already uploaded: {fname} -> {manifest[fname]}")
            continue
        print(f"  uploading {fname} ...", end=" ", flush=True)
        url = media_host.host_media(local, "lasso_summit")
        if url:
            manifest[fname] = url
            print(f"ok -> {url}")
            uploaded += 1
        else:
            print("FAILED (check R2 credentials and AGENT_HOSTING_ENABLED)")

    _save_manifest(manifest)
    print(f"\n{uploaded} new uploads. {len(manifest)}/14 images in manifest.")
    if len(manifest) == 14:
        print("\nAll 14 images uploaded. Now commit summit_manifest.json and run:")
        print("  railway exec python -m agent summit-queue --from-manifest")
    else:
        missing = [p["filename"] for p in SUMMIT_POSTS if p["filename"] not in manifest]
        print(f"Missing: {missing}")
    return manifest


# ---- Phase 2: create drafts from manifest ---------------------------------

def create_drafts(manifest=None):
    """Create 28 HELD draft posts (14 images x lasso_ig + lasso_fb) in the DB.
    Every draft is PENDING (held for approval). Nothing publishes automatically.
    Safe to re-run; existing draft_ids are overwritten in-place (INSERT OR REPLACE)."""
    from . import schedule as sched, accounts as _accts
    from .drafter import Draft, DraftStatus
    from .store import PendingStore
    _store = PendingStore()

    if manifest is None:
        manifest = _load_manifest()
    if not manifest:
        raise RuntimeError(
            "summit_manifest.json not found. Run upload step first:\n"
            "  railway run python -m agent summit-queue --images-dir PATH"
        )

    missing_urls = [p["filename"] for p in SUMMIT_POSTS if p["filename"] not in manifest]
    if missing_urls:
        raise RuntimeError(f"Manifest is missing URLs for: {missing_urls}")

    # Resolve platform strings once per account key
    _acct_objs = {a: _accts.get_account(a) for a in ACCOUNTS}

    created = 0
    for post in SUMMIT_POSTS:
        url = manifest[post["filename"]]
        day = post["date"]
        scheduled_for = sched.scheduled_for(day)
        for acct in ACCOUNTS:
            acct_obj = _acct_objs[acct]
            did = _draft_id(acct, post["filename"], day)
            d = Draft(
                draft_id=did,
                account_key=acct,
                platform=acct_obj.platform if acct_obj else acct,
                caption=post["caption"],
                hashtags=[],
                creative_path=post["filename"],
                creative_public_url=url,
                scheduled_for=scheduled_for,
                status=DraftStatus.PENDING,
                day_key=day,
                draft_type="feed",
            )
            _store.put(d)
            created += 1
            print(f"  queued week {post['week']:02d} {day} {acct} -> {did}")

    print(f"\n{created} drafts created. All HELD for approval. Nothing published.")
    return created


# ---- Sprint drafts (approved-caption path, flag-gated) ---------------------
# The sprint feed posts draw captions from the Blake-approved SUMMIT_CONCEPTS
# (agent/summit_rebuild.py) plus the agenda and panel captions below (verified
# facts only). This is separate from the legacy SUMMIT_POSTS block above, which
# carries pre-ruling scarcity/pricing copy and is NOT used by the sprint.

# Captions for the assets that are not in SUMMIT_CONCEPTS. Verified facts only:
# NOV 7 + 8, Virgin Hotel Nashville, 100 seats, lassoframework.com/summit. No
# times (source has none). Session titles are verbatim from 02_verified_stats.md.
_SPRINT_EXTRA_CAPTIONS = {
    "08_agenda_day1.png": (
        "Day one of the LASSO Growth Summit.\n\n"
        "State of the industry with Andrew Charlesworth. Meta ads in 2026 with Blake "
        "Ruff. The math behind scaling with Tommy Allen. Scaling from operator to "
        "owner with Stu Brauer.\n\n"
        "November 7 and 8. Virgin Hotel Nashville. 100 seats.\n\n"
        "Claim your seat.\n"
        "lassoframework.com/summit"
    ),
    "09_agenda_day2.png": (
        "Day two of the LASSO Growth Summit.\n\n"
        "Leadership that scales with Jeff Smith. Hiring that scales your gym with Scott "
        "Rammage. Building a predictable hiring machine with Brian Alexander. "
        "Increasing LTV through ancillary services with Nicole Aucoin.\n\n"
        "November 7 and 8. Virgin Hotel Nashville. 100 seats.\n\n"
        "Claim your seat.\n"
        "lassoframework.com/summit"
    ),
    "22_panel_future.png": (
        "The future of gym growth, live on the panel.\n\n"
        "Three operators on where boutique fitness goes next. Streamfit, HireVP, and "
        "Tommy Allen.\n\n"
        "November 7 and 8. Virgin Hotel Nashville. 100 seats.\n\n"
        "Claim your seat.\n"
        "lassoframework.com/summit"
    ),
}


def sprint_assets():
    """Ordered (filename, caption) for the sprint FEED cards, arc-ordered. Both
    treatments of each concept share that concept's approved caption; the agenda and
    panel cards use the verified-facts captions above. Story files are paired 1:1 to
    their feed card by name (<file>_story.png) and inherit the same caption."""
    from .summit_rebuild import SUMMIT_CONCEPTS, ARC_ORDER
    by_id = {c["id"]: c for c in SUMMIT_CONCEPTS}
    out = []
    for cid in ARC_ORDER:
        c = by_id.get(cid)
        if not c:
            continue
        for t in ("a", "b"):
            out.append((f"{cid}_{t}.png", c["caption"]))
    # agenda + panel ride the sprint too
    for fname in ("08_agenda_day1.png", "09_agenda_day2.png", "22_panel_future.png"):
        out.append((fname, _SPRINT_EXTRA_CAPTIONS[fname]))
    return out


def create_sprint_drafts(manifest=None, posts_per_day=SPRINT_MAX_FEED_PER_DAY):
    """Create HELD sprint drafts across the backward-anchored calendar.

    Gated on AGENT_SUMMIT_CAMPAIGN_ENABLED (default OFF): with the flag off this is a
    no-op that reports and returns 0, so nothing is queued. With the flag on, every
    draft is still PENDING (held for approval); nothing publishes automatically.
    Cross-posts each slot to lasso_ig + lasso_fb. Requires a sprint manifest mapping
    each rendered filename to its hosted URL."""
    from . import config, accounts as _accts
    from .drafter import Draft, DraftStatus
    from .store import PendingStore

    if not config.summit_campaign_enabled():
        print("AGENT_SUMMIT_CAMPAIGN_ENABLED is OFF. Sprint calendar is dormant; "
              "no drafts queued. (Set the flag to arm; drafts still held for approval.)")
        return 0

    if manifest is None:
        manifest = _load_manifest()

    assets = sprint_assets()
    caption_by_file = dict(assets)
    filenames = [f for f, _ in assets]

    missing = [f for f in filenames if f not in (manifest or {})]
    if missing:
        raise RuntimeError(
            "sprint manifest is missing hosted URLs for: " + ", ".join(missing) +
            "\nUpload the rendered sprint cards first (see summit-queue --images-dir)."
        )

    _store = PendingStore()
    _acct_objs = {a: _accts.get_account(a) for a in ACCOUNTS}

    created = 0
    for slot in sprint_calendar(filenames, posts_per_day=posts_per_day):
        fname = slot["filename"]
        url = manifest[fname]
        for acct in ACCOUNTS:
            acct_obj = _acct_objs[acct]
            did = _draft_id(acct, f"sprint|{fname}|{slot['slot_index']}", slot["date"])
            d = Draft(
                draft_id=did,
                account_key=acct,
                platform=acct_obj.platform if acct_obj else acct,
                caption=caption_by_file[fname],
                hashtags=[],
                creative_path=fname,
                creative_public_url=url,
                scheduled_for=slot["scheduled_for"],
                status=DraftStatus.PENDING,
                day_key=slot["date"],
                draft_type="summit",
            )
            _store.put(d)
            created += 1

    print(f"\n{created} sprint drafts created across {len(sprint_days())} days. "
          "All HELD for approval. Nothing published.")
    return created


# ---- CLI entry point -------------------------------------------------------

def run(images_dir=None, from_manifest=False, sprint=False):
    """Main entry point called from __main__.py."""
    from . import config
    manifest = _load_manifest()

    if sprint:
        # Backward-anchored sprint calendar. Gated on AGENT_SUMMIT_CAMPAIGN_ENABLED.
        create_sprint_drafts(manifest)
        return

    if not images_dir and not from_manifest:
        # Status report
        print(f"LASSO Growth Summit queue status")
        print(f"  manifest:   {os.path.normpath(MANIFEST_PATH)}")
        print(f"  images in manifest: {len(manifest)}/14")
        if manifest:
            for post in SUMMIT_POSTS:
                status = "uploaded" if post["filename"] in manifest else "MISSING"
                print(f"    week {post['week']:02d} {post['date']} {status}")
        _days = sprint_days()
        print("\nSprint calendar (backward-anchored, flag "
              f"AGENT_SUMMIT_CAMPAIGN_ENABLED={config.summit_campaign_enabled()}):")
        print(f"  {len(_days)} posting days, {len(_days) * SPRINT_MAX_FEED_PER_DAY} "
              f"feed slots at up to {SPRINT_MAX_FEED_PER_DAY}/day "
              f"({_days[0]} -> {_days[-1]}; Nov 7 + 8 dark).")
        print("\nUsage:")
        print("  Step 1 (upload, run from Mac):")
        print('    railway run python -m agent summit-queue --images-dir "PATH_TO_IMAGES"')
        print("  Step 2a (legacy weekly drafts, run on Railway):")
        print("    python -m agent summit-queue --from-manifest")
        print("  Step 2b (sprint calendar drafts, needs AGENT_SUMMIT_CAMPAIGN_ENABLED):")
        print("    python -m agent summit-queue --sprint")
        return

    if images_dir:
        manifest = upload_images(images_dir)

    if from_manifest or (images_dir and len(manifest) == len(SUMMIT_POSTS)):
        create_drafts(manifest)
