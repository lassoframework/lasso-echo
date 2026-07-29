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
            "Walk the four legs in order. The first fail is where 2027 hides.\n\n"
            "Close rate at 70% or above. Show rate at 50% or above. Booking "
            "behavior at 50% or above. Lead volume at 40% or above.\n\n"
            "Fix the leg that is broken. Then scale.\n\n"
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


# ---- CLI entry point -------------------------------------------------------

def run(images_dir=None, from_manifest=False):
    """Main entry point called from __main__.py."""
    manifest = _load_manifest()

    if not images_dir and not from_manifest:
        # Status report
        print(f"LASSO Growth Summit queue status")
        print(f"  manifest:   {os.path.normpath(MANIFEST_PATH)}")
        print(f"  images in manifest: {len(manifest)}/14")
        if manifest:
            for post in SUMMIT_POSTS:
                status = "uploaded" if post["filename"] in manifest else "MISSING"
                print(f"    week {post['week']:02d} {post['date']} {status}")
        print("\nUsage:")
        print("  Step 1 (upload, run from Mac):")
        print('    railway run python -m agent summit-queue --images-dir "PATH_TO_IMAGES"')
        print("  Step 2 (create drafts, run on Railway):")
        print("    python -m agent summit-queue --from-manifest")
        return

    if images_dir:
        manifest = upload_images(images_dir)

    if from_manifest or (images_dir and len(manifest) == len(SUMMIT_POSTS)):
        create_drafts(manifest)
