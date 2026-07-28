"""
The Full Gym — book launch infographic queue.

14 posts (lasso_ig + lasso_fb) driving to:
https://fullgym.lassoframework.com/waitlist

Book launches September 8, 2026. Posts 01-12 are dated (two per week,
Jul 28 through Sep 7). Posts 13 and 14 are the flexible cover cards and
default to Sep 2 and Sep 3 respectively (both weekdays, before launch).

NOTE: Cards 13 and 14 use a stand-in cover image, not the final cover art.
Swap in the real cover before approving those two posts.

Two-step workflow:

  Step 1 (Mac, with railway run):
    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-queue \\
      --images-dir "/path/to/The-Full-Gym-Infographics"
    Uploads 14 PNGs to R2 and saves book_manifest.json at the repo root.

  Step 2 (Railway container, via AGENT_BOOK_QUEUE_ON_START):
    Set AGENT_BOOK_QUEUE_ON_START=true in Railway Variables and deploy.
    Reads book_manifest.json, creates 28 PENDING drafts in Railway's DB.
    Nothing publishes; every post waits for approval.

Run with no args to see current manifest status.
"""

import hashlib
import json
import os

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "book_manifest.json")

ACCOUNTS = ["lasso_ig", "lasso_fb"]

BOOK_POSTS = [
    {
        "num": 1,
        "date": "2026-07-28",
        "filename": "01_cant-out-coach.png",
        "caption": (
            "Membership hasn't moved in six months.\n\n"
            "So you tighten onboarding. You rewrite the programming. You buy another mentorship.\n\n"
            "None of it works, because none of it is the problem. You're gaining five and losing five. "
            "That's math, not effort.\n\n"
            "Fix churn. Fix sales. Then, and only then, buy more leads.\n\n"
            "The Full Gym drops September 8. Get on the list.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 2,
        "date": "2026-07-31",
        "filename": "02_easiest-leads.png",
        "caption": (
            "Referrals. Walk ins. Website inquiries. Google.\n\n"
            "Those are the warmest leads you'll ever touch. They're already leaning in. "
            "You don't have to sell them. You just have to not lose them.\n\n"
            "If you're closing under 70 percent of those, more ad spend won't save you. "
            "It'll just put your leak on a bigger stage.\n\n"
            "Chapter 1 shows you what to fix first.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 3,
        "date": "2026-08-04",
        "filename": "03_donation-to-facebook.png",
        "caption": (
            "Ibrahim ran a premium boxing studio at 470 dollars a month per member "
            "and was stuck at 5,000 dollars in revenue.\n\n"
            "His marketing plan was 300 dollars a month, one week of patience, four leads, "
            "panic, shut it off, try something else. On repeat.\n\n"
            "We didn't touch his creative. We didn't touch his targeting. We changed the commitment.\n\n"
            "1,500 a month. A 14 day window with hands off the dashboard. Scale 20 percent a week.\n\n"
            "Sixty days later he was over 12,000 a month.\n\n"
            "Chapter 9 has the whole build.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 4,
        "date": "2026-08-07",
        "filename": "04_ibrahim-doubled.png",
        "caption": (
            "Same gym. Same coaches. Same price. Same city.\n\n"
            "The only thing that changed was how long he let the system learn before he touched it.\n\n"
            "Most owners aren't losing to the algorithm. They're losing to their own impatience.\n\n"
            "Budget doesn't just buy reach. It buys speed of clarity.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 5,
        "date": "2026-08-11",
        "filename": "05_ads-were-never-the-problem.png",
        "caption": (
            "Your ads did exactly what you paid them to do. Someone stopped scrolling and raised their hand.\n\n"
            "Then nobody called for two days. The confirmation text never went out. "
            "The coach who ran the intro had never been trained on it.\n\n"
            "Meta can't dial the phone at 6pm. It can't confirm the appointment. "
            "It can't handle the question about her schedule.\n\n"
            "That gap between a lead landing and a human reaching them is where most gyms "
            "lose the money they just spent.\n\n"
            "Chapter 11 closes it.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 6,
        "date": "2026-08-14",
        "filename": "06_five-step-path.png",
        "caption": (
            "Every lead you get walks this path whether you built it or not.\n\n"
            "Few consultations booked? That's the contact step, not your ads.\n"
            "Booked but no shows? That's expectation setting, not your targeting.\n"
            "Showed up and didn't join? That's the conversation, not lead quality.\n\n"
            "Most owners only notice the drop off at the very end, then work backward. "
            "By then the damage is spread across four steps.\n\n"
            "It's never one big failure. It's a series of small avoidable ones.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 7,
        "date": "2026-08-18",
        "filename": "07_tommy-handoff.png",
        "caption": (
            "Tommy was getting 50 to 60 leads a month and closing 12.\n\n"
            "He blamed the leads. He was about to raise his ad budget to fix a conversion problem. "
            "We stopped him.\n\n"
            "The ads were never the problem. Nobody called within 24 hours. "
            "Nobody confirmed the appointment. Every coach ran the intro differently.\n\n"
            "He had a marketing engine and a broken bridge.\n\n"
            "We added a personal text within two hours, a confirmation sequence, "
            "one consultation framework, and a no show rescue.\n\n"
            "Same ad spend. Same leads. Completely different results.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 8,
        "date": "2026-08-21",
        "filename": "08_need-certainty.png",
        "caption": (
            "They walked in hoping someone would help them decide.\n\n"
            "You gave them a tour, the class times, the packages, and a price sheet. "
            "Then you left them to figure it out alone.\n\n"
            "So they said they'd think about it. Most of them never came back.\n\n"
            "That's not a lead quality problem. That's a consultation with no spine.\n\n"
            "Unsure people don't commit. Confident ones do. And that confidence is your job to build, "
            "not theirs to bring.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 9,
        "date": "2026-08-25",
        "filename": "09_stop-apologizing.png",
        "caption": (
            "I know it's a lot, but.\n"
            "We can probably work something out.\n"
            "It's a little more than most places.\n\n"
            "Every one of those tells the client the price is a problem before they'd decided it was one.\n\n"
            "Clients don't set value on their own. They take the cue from you. Presented with confidence "
            "it lands as an investment. Presented with hesitation it lands as an expense, "
            "and expenses get cut.\n\n"
            "Nobody apologizes for the price of a surgeon.\n\n"
            "Chapter 15 rebuilds how you present it.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 10,
        "date": "2026-08-28",
        "filename": "10_four-numbers.png",
        "caption": (
            "Screenshot this. It's the order we diagnose every gym in.\n\n"
            "Close rate at least 70 percent.\n"
            "Show rate at least 50 percent.\n"
            "Booking rate at least 50 percent.\n"
            "Lead flow at least 40 percent.\n\n"
            "Work top down and stop at the first one that fails. "
            "That's your bottleneck. Everything below it is a distraction.\n\n"
            "And if any leg is broken, don't add ad spend. "
            "You'll just pay more to lose more people faster.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 11,
        "date": "2026-09-01",
        "filename": "11_whats-inside.png",
        "caption": (
            "Nineteen chapters. Nineteen frameworks. Nineteen tools you can actually download "
            "and use this week.\n\n"
            "The KPI tracker we run with clients. The ad budget calculator. Thirty StoryBrand ad templates. "
            "The booked to close calculator. The consultation script that closes at least 70 percent. "
            "The floor sales scorecard. A fourteen day follow up sequence for every lead outcome. "
            "Role play personas so your team gets better every week.\n\n"
            "This isn't a book you read once and shelve. "
            "It's the manual you open when a number stops making sense.\n\n"
            "September 8.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 12,
        "date": "2026-09-07",
        "filename": "12_tomorrow.png",
        "caption": (
            "Tomorrow.\n\n"
            "Two former gym owners. More than 1,000 gyms. Over 2 million dollars in gym ad spend "
            "and more than 30 million dollars in tracked client revenue.\n\n"
            "All of it in one manual. Where your funnel actually leaks. What to fix first. "
            "What to never touch. And how to stop guessing whether your marketing is working.\n\n"
            "The Full Gym. Out tomorrow.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 13,
        "date": "2026-09-02",
        "filename": "13_cover-reveal.png",
        "caption": (
            "Here it is.\n\n"
            "The Full Gym. Sherman and I wrote the book we wish someone had handed us when we owned gyms "
            "and could not figure out why the numbers would not move.\n\n"
            "Nineteen chapters. Nineteen frameworks. Nineteen tools you can download and use "
            "the same week you read them.\n\n"
            "September 8. Waitlist link below, and the QR goes to the same place.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 14,
        "date": "2026-09-03",
        "filename": "14_inside-youll-discover.png",
        "caption": (
            "Five things you'll walk away with.\n\n"
            "The three levers that decide whether your gym grows. "
            "Why more leads won't fix a broken sales process. "
            "How to know when you're actually ready for paid ads. "
            "The messaging that pulls in the right members instead of everyone. "
            "And the system that turns prospects into members who stay.\n\n"
            "Not a mindset book. A manual you'll keep coming back to.\n\n"
            "September 8.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
]


# ---- Helpers ---------------------------------------------------------------

def _draft_id(account_key, filename, date):
    h = hashlib.sha1(f"book|{account_key}|{filename}|{date}".encode()).hexdigest()[:16]
    return f"book_{h}"


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


# ---- Phase 1: upload images to R2 ------------------------------------------

def upload_images(images_dir):
    """Upload all 14 PNGs to R2. Requires AGENT_HOSTING_ENABLED + R2 credentials.
    Saves R2 URLs to book_manifest.json. Safe to re-run; already-uploaded files
    are deduped by content hash."""
    from . import media_host, config

    if not config.hosting_enabled():
        raise RuntimeError(
            "AGENT_HOSTING_ENABLED is not set. Run with:\n"
            "  AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-queue --images-dir PATH"
        )

    manifest = _load_manifest()
    uploaded = 0

    for post in BOOK_POSTS:
        fname = post["filename"]
        local = os.path.join(images_dir, fname)
        if not os.path.isfile(local):
            print(f"  MISSING: {local}")
            continue
        if fname in manifest:
            print(f"  already uploaded: {fname} -> {manifest[fname]}")
            continue
        print(f"  uploading {fname} ...", end=" ", flush=True)
        url = media_host.host_media(local, "lasso_book")
        if url:
            manifest[fname] = url
            print(f"ok -> {url}")
            uploaded += 1
        else:
            print("FAILED (check R2 credentials and AGENT_HOSTING_ENABLED)")

    _save_manifest(manifest)
    print(f"\n{uploaded} new uploads. {len(manifest)}/14 images in manifest.")
    if len(manifest) == 14:
        print("\nAll 14 images uploaded. Now commit book_manifest.json and set:")
        print("  AGENT_BOOK_QUEUE_ON_START=true  in Railway Variables, then deploy.")
    else:
        missing = [p["filename"] for p in BOOK_POSTS if p["filename"] not in manifest]
        print(f"Missing: {missing}")
    return manifest


# ---- Phase 2: create drafts from manifest ----------------------------------

def create_drafts(manifest=None):
    """Create 28 PENDING draft posts (14 images x lasso_ig + lasso_fb) in the DB.
    Every draft is PENDING (held for approval). Nothing publishes automatically.
    Safe to re-run; existing draft_ids are overwritten in-place (INSERT OR REPLACE).

    NOTE: Cards 13 and 14 use a stand-in cover. Do not approve those until
    the real cover image is swapped in."""
    from . import schedule as sched, accounts as _accts
    from .drafter import Draft, DraftStatus
    from .store import PendingStore
    _store = PendingStore()

    if manifest is None:
        manifest = _load_manifest()
    if not manifest:
        raise RuntimeError(
            "book_manifest.json not found. Run upload step first:\n"
            "  AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-queue --images-dir PATH"
        )

    missing_urls = [p["filename"] for p in BOOK_POSTS if p["filename"] not in manifest]
    if missing_urls:
        raise RuntimeError(f"Manifest is missing URLs for: {missing_urls}")

    _acct_objs = {a: _accts.get_account(a) for a in ACCOUNTS}

    created = 0
    for post in BOOK_POSTS:
        url = manifest[post["filename"]]
        day = post["date"]
        scheduled_for = sched.scheduled_for(day)
        cover_note = " [STAND-IN COVER — swap real art before approving]" if post["num"] >= 13 else ""
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
            print(f"  queued post {post['num']:02d} {day} {acct} -> {did}{cover_note}")

    print(f"\n{created} drafts created. All PENDING for approval. Nothing published.")
    print("NOTE: Posts 13 and 14 use a stand-in cover. Swap the real cover before approving.")
    return created


# ---- CLI entry point -------------------------------------------------------

def run(images_dir=None, from_manifest=False):
    """Main entry point called from __main__.py."""
    manifest = _load_manifest()

    if not images_dir and not from_manifest:
        print("The Full Gym book queue status")
        print(f"  manifest:   {os.path.normpath(MANIFEST_PATH)}")
        print(f"  images in manifest: {len(manifest)}/14")
        if manifest:
            for post in BOOK_POSTS:
                status = "uploaded" if post["filename"] in manifest else "MISSING"
                print(f"    post {post['num']:02d} {post['date']} {status}")
        print("\nUsage:")
        print("  Step 1 (upload, run from Mac):")
        print('    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-queue --images-dir "PATH"')
        print("  Step 2 (create drafts, set Railway Variable + deploy):")
        print("    AGENT_BOOK_QUEUE_ON_START=true")
        return

    if images_dir:
        manifest = upload_images(images_dir)

    if from_manifest or (images_dir and len(manifest) == len(BOOK_POSTS)):
        create_drafts(manifest)
