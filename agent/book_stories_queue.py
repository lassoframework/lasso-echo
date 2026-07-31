"""
The Full Gym — book launch story queue.

14 pre-made IG Story cards on lasso_ig, spaced every 2-3 days from
Aug 2 through Sep 7 (launch eve). Arc: hook -> case study -> framework
-> value reveal -> cover/waitlist CTA.

Images live at:
  The-Full-Gym-Infographics 2/02-STORIES-1080x1920/

Two-step workflow:

  Step 1 (Mac, with railway run):
    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-stories \\
      --images-dir "/Users/blakeruff/LASSO Dropbox/Blake Ruff/Mac/Downloads/lasso-summit-skill/assets/The-Full-Gym-Infographics 2/02-STORIES-1080x1920"
    Uploads 14 PNGs to R2 and saves book_stories_manifest.json at the repo root.

  Step 2 (Railway container, via AGENT_BOOK_STORIES_ON_START):
    Set AGENT_BOOK_STORIES_ON_START=true in Railway Variables and deploy.
    Reads book_stories_manifest.json, creates 14 PENDING story drafts in the DB.
    Nothing publishes; auto-publish fires on each scheduled date
    (AGENT_AUTO_APPROVE_ENABLED must be armed).

Run with no args to see current manifest status.
"""

import hashlib
import json
import os

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "book_stories_manifest.json")

ACCOUNTS = ["lasso_ig"]

STORY_POSTS = [
    {
        "num": 1,
        "date": "2026-08-02",
        "filename": "01_cant-out-coach.png",
        "caption": (
            "You can't out coach a growth plateau. You can't out system it either.\n\n"
            "If churn is healthy and sales are tight, it's a top of funnel problem.\n\n"
            "The Full Gym drops September 8. Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 2,
        "date": "2026-08-04",
        "filename": "02_easiest-leads.png",
        "caption": (
            "If you can't close the easiest leads, you're not ready to close cold ones.\n\n"
            "Chapter One. The Full Gym.\n\n"
            "September 8. Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 3,
        "date": "2026-08-06",
        "filename": "03_donation-to-facebook.png",
        "caption": (
            "$300 a month isn't a marketing budget. It's a donation to Facebook.\n\n"
            "Chapter Nine teaches you what a real budget looks like and how to spend it "
            "without guessing.\n\n"
            "The Full Gym. September 8. Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 4,
        "date": "2026-08-09",
        "filename": "04_ibrahim-doubled.png",
        "caption": (
            "Ibrahim was spending $300 a month on ads and killing them after one week.\n\n"
            "Switched to $1,500. Left them alone for 14 days. The only thing that changed "
            "was patience.\n\n"
            "$5,000 to $12,000 in 60 days.\n\n"
            "The Full Gym. September 8. Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 5,
        "date": "2026-08-11",
        "filename": "05_ads-were-never-the-problem.png",
        "caption": (
            "The ads were never the problem.\n\n"
            "The Full Gym. Available September 8.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 6,
        "date": "2026-08-13",
        "filename": "06_five-step-path.png",
        "caption": (
            "Growth doesn't break at one point. It breaks at every step you haven't "
            "clearly defined.\n\n"
            "Lead. Contact. Conversation. Consult. Decision. One weak link is all it takes.\n\n"
            "Chapter Eleven. The Full Gym. September 8."
        ),
    },
    {
        "num": 7,
        "date": "2026-08-16",
        "filename": "07_tommy-handoff.png",
        "caption": (
            "Same ads. Same leads.\n\n"
            "Tommy went from 40 to 68 percent show rate and 35 to 55 percent close rate "
            "in 60 days. No extra ad spend. The only change was the system.\n\n"
            "The Full Gym. September 8. Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 8,
        "date": "2026-08-18",
        "filename": "08_need-certainty.png",
        "caption": (
            "People rarely need more information. They need more certainty.\n\n"
            "Chapter Eleven. The Full Gym.\n\n"
            "September 8 on Amazon. Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 9,
        "date": "2026-08-20",
        "filename": "09_stop-apologizing.png",
        "caption": (
            "Stop apologizing for your price.\n\n"
            "Explaining a price sounds like justifying it. Owning it sounds like you "
            "believe every dollar of it. Because you do.\n\n"
            "Chapter Fifteen. The Full Gym. September 8."
        ),
    },
    {
        "num": 10,
        "date": "2026-08-23",
        "filename": "10_four-numbers.png",
        "caption": (
            "Four numbers. In this order.\n\n"
            "Close Rate 70 percent. Show Rate 50 percent. Booking Rate 50 percent. "
            "Lead Flow 40 percent.\n\n"
            "Fix the top one first. Never scale spend over a broken leg.\n\n"
            "The Full Gym. September 8."
        ),
    },
    {
        "num": 11,
        "date": "2026-08-25",
        "filename": "11_whats-inside.png",
        "caption": (
            "Not a mindset book. A manual.\n\n"
            "19 chapters. 19 visual frameworks. 19 downloadable tools.\n\n"
            "The KPI Tracker. The Ad Budget Calculator. The 70 percent close call script. "
            "A 14 day follow up sequence. All of it.\n\n"
            "The Full Gym. September 8. Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 12,
        "date": "2026-08-27",
        "filename": "13_cover-reveal.png",
        "caption": (
            "The Full Gym.\n\n"
            "The Boutique Gym Owner's Guide to Predictable Monthly Growth.\n\n"
            "Sherman Merricks and Blake Ruff. September 8 on Amazon.\n\n"
            "Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 13,
        "date": "2026-09-02",
        "filename": "14_inside-youll-discover.png",
        "caption": (
            "Inside, you'll discover the three levers that decide whether your gym grows, "
            "why more leads won't fix a broken sales process, and how to build a system "
            "that turns prospects into long term members.\n\n"
            "The Full Gym. September 8. Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 14,
        "date": "2026-09-07",
        "filename": "12_tomorrow.png",
        "caption": (
            "The Full Gym drops September 8.\n\n"
            "Sherman Merricks and Blake Ruff.\n\n"
            "The Boutique Gym Owner's Guide to Predictable Monthly Growth.\n\n"
            "Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
]


# ---- Helpers ---------------------------------------------------------------

def _draft_id(account_key, filename, date):
    h = hashlib.sha1(f"book_story|{account_key}|{filename}|{date}".encode()).hexdigest()[:16]
    return f"bks_{h}"


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
    """Upload all 14 story PNGs to R2. Requires AGENT_HOSTING_ENABLED + R2 credentials.
    Saves R2 URLs to book_stories_manifest.json. Safe to re-run; deduped by content hash."""
    from . import media_host, config

    if not config.hosting_enabled():
        raise RuntimeError(
            "AGENT_HOSTING_ENABLED is not set. Run with:\n"
            "  AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-stories "
            "--images-dir PATH"
        )

    manifest = _load_manifest()
    uploaded = 0
    n = len(STORY_POSTS)

    for post in STORY_POSTS:
        fname = post["filename"]
        local = os.path.join(images_dir, fname)
        if not os.path.isfile(local):
            print(f"  MISSING: {local}")
            continue
        if fname in manifest:
            print(f"  already uploaded: {fname} -> {manifest[fname]}")
            continue
        print(f"  uploading {fname} ...", end=" ", flush=True)
        url = media_host.host_media(local, "lasso_book_stories")
        if url:
            manifest[fname] = url
            print(f"ok -> {url}")
            uploaded += 1
        else:
            print("FAILED (check R2 credentials and AGENT_HOSTING_ENABLED)")

    _save_manifest(manifest)
    print(f"\n{uploaded} new uploads. {len(manifest)}/{n} images in manifest.")
    if len(manifest) == n:
        print(f"\nAll {n} images uploaded. Now commit book_stories_manifest.json and set:")
        print("  AGENT_BOOK_STORIES_ON_START=true  in Railway Variables, then deploy.")
    else:
        missing = [p["filename"] for p in STORY_POSTS if p["filename"] not in manifest]
        print(f"Missing: {missing}")
    return manifest


# ---- Phase 2: create drafts from manifest ----------------------------------

def create_drafts(manifest=None):
    """Create 14 PENDING story drafts in the DB.
    Each draft is PENDING. Nothing publishes automatically.
    Safe to re-run; existing draft_ids are overwritten in-place."""
    from . import schedule as sched, accounts as _accts
    from .drafter import Draft, DraftStatus
    from .store import PendingStore
    _store = PendingStore()

    if manifest is None:
        manifest = _load_manifest()
    if not manifest:
        raise RuntimeError(
            "book_stories_manifest.json not found. Run upload step first:\n"
            "  AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-stories "
            "--images-dir PATH"
        )

    missing_urls = [p["filename"] for p in STORY_POSTS if p["filename"] not in manifest]
    if missing_urls:
        raise RuntimeError(f"Manifest is missing URLs for: {missing_urls}")

    _acct_objs = {a: _accts.get_account(a) for a in ACCOUNTS}

    created = 0
    for post in STORY_POSTS:
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
                draft_type="story",
            )
            _store.put(d)
            created += 1
            print(f"  queued story {post['num']:02d} {day} {acct} -> {did}")

    print(f"\n{created} story drafts created. All PENDING. Nothing published.")
    return created


# ---- Daily runner hook -----------------------------------------------------

def build_book_story_draft(account, day_key):
    """Return a story Draft if this account has a scheduled story for day_key.

    Called by runner.run_daily() before build_story_draft. Returns None when
    this is not a book story day or the manifest is missing."""
    if account.key not in ACCOUNTS:
        return None
    post = next((p for p in STORY_POSTS if p["date"] == day_key), None)
    if post is None:
        return None
    manifest = _load_manifest()
    if not manifest:
        return None
    url = manifest.get(post["filename"])
    if not url:
        print(f"[book-stories] manifest missing URL for {post['filename']} — skipping")
        return None
    from . import schedule as sched
    from .drafter import Draft, DraftStatus
    did = _draft_id(account.key, post["filename"], day_key)
    platform = getattr(account, "platform", account.key)
    return Draft(
        draft_id=did,
        account_key=account.key,
        platform=platform,
        caption=post["caption"],
        hashtags=[],
        creative_path=post["filename"],
        creative_public_url=url,
        scheduled_for=sched.scheduled_for(day_key),
        status=DraftStatus.PENDING,
        day_key=day_key,
        draft_type="story",
    )


# ---- Expire existing Slack cards -------------------------------------------

def expire_existing_drafts():
    """Mark all PENDING bks_ drafts EXPIRED so existing Slack cards are inert."""
    from .store import PendingStore
    from .drafter import DraftStatus
    from .slack_poster import SlackPoster

    store = PendingStore()
    poster = SlackPoster()
    pending = getattr(store, "list_pending", None)
    if pending is None:
        print("PendingStore has no list_pending — nothing to expire.")
        return 0

    expired_n = 0
    for d in pending():
        if not (d.draft_id or "").startswith("bks_"):
            continue
        if d.status != DraftStatus.PENDING:
            continue
        d.status = DraftStatus.EXPIRED
        store.put(d)
        try:
            poster.mark_expired(d)
        except Exception:
            pass
        print(f"  expired {d.draft_id} ({d.account_key}, {d.day_key})")
        expired_n += 1

    print(f"\n{expired_n} bks_ draft(s) expired.")
    return expired_n


# ---- CLI entry point -------------------------------------------------------

def run(images_dir=None, from_manifest=False, expire_only=False):
    """Main entry point called from __main__.py."""
    if expire_only:
        expire_existing_drafts()
        return

    manifest = _load_manifest()

    if not images_dir and not from_manifest:
        print("The Full Gym book stories queue status")
        n = len(STORY_POSTS)
        print(f"  manifest:   {os.path.normpath(MANIFEST_PATH)}")
        print(f"  images in manifest: {len(manifest)}/{n}")
        if manifest:
            for post in STORY_POSTS:
                status = "uploaded" if post["filename"] in manifest else "MISSING"
                print(f"    story {post['num']:02d} {post['date']} {post['filename']} {status}")
        print("\nUsage:")
        print("  Step 1 (upload, run from Mac):")
        print('    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-stories \\')
        print('      --images-dir "/Users/blakeruff/LASSO Dropbox/Blake Ruff/Mac/Downloads/'
              'lasso-summit-skill/assets/The-Full-Gym-Infographics 2/02-STORIES-1080x1920"')
        print("  Step 2 (create drafts, set Railway Variable + deploy):")
        print("    AGENT_BOOK_STORIES_ON_START=true")
        return

    if images_dir:
        manifest = upload_images(images_dir)

    if from_manifest or (images_dir and len(manifest) == len(STORY_POSTS)):
        create_drafts(manifest)
