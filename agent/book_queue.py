"""
The Full Gym — book launch card queue.

8 posts (lasso_ig + lasso_fb) = 16 PENDING drafts, driving to:
https://fullgym.lassoframework.com/waitlist

Book launches September 8, 2026. Posts run twice a week (Fri/Tue)
from Aug 1 through Aug 26. Arc: announcement -> premise -> product ->
author credibility (Blake, Sherman, combined) -> pre-launch reminders.

Images live in Blake's Mac Downloads folder. Pass that path to Step 1.

Two-step workflow:

  Step 1 (Mac, with railway run):
    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-queue \\
      --images-dir "/Users/blakeruff/LASSO Dropbox/Blake Ruff/Mac/Downloads"
    Uploads 8 PNGs to R2 and saves book_manifest.json at the repo root.

  Step 2 (Railway container, via AGENT_BOOK_QUEUE_ON_START):
    Set AGENT_BOOK_QUEUE_ON_START=true in Railway Variables and deploy.
    Reads book_manifest.json, creates 16 PENDING drafts in Railway's DB.
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
        "date": "2026-08-01",
        "filename": "Book Post 1-1.png",
        "caption": (
            "The Full Gym. Dropping September 8th on Amazon.\n\n"
            "The Boutique Gym Owner's Guide to Predictable Monthly Growth.\n\n"
            "Written by Sherman Merricks and Blake Ruff.\n\n"
            "Pre-order link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 2,
        "date": "2026-08-05",
        "filename": "Author Highlight - Book.png",
        "caption": (
            "Most boutique gym owners don't have a marketing problem or a sales problem. "
            "They have an alignment problem.\n\n"
            "And it is quietly capping their growth no matter how hard they work.\n\n"
            "The Full Gym gives you the complete system to fix it.\n\n"
            "September 8th on Amazon. Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 3,
        "date": "2026-08-08",
        "filename": "Book Post 2-1.png",
        "caption": (
            "Available September 8th. Print and digital.\n\n"
            "The manual you open when a number stops making sense. "
            "Nineteen chapters. Nineteen frameworks. Every tool available to download "
            "and use the same week you read them.\n\n"
            "No gimmicks. No six week challenges. No tactics that cheapen your brand.\n\n"
            "Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 4,
        "date": "2026-08-12",
        "filename": "Author Highlight - Blake.png",
        "caption": (
            "Blake Ruff is a StoryBrand Certified Guide, gym growth strategist, and "
            "host of the Gym Marketing Made Simple podcast with 137 episodes.\n\n"
            "He has managed over $2M in Facebook ad spend specifically for gyms and "
            "helped hundreds of gym owners install the LASSO platform.\n\n"
            "Direct, data driven, and built on real world results. Not theory.\n\n"
            "The Full Gym drops September 8th. Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 5,
        "date": "2026-08-15",
        "filename": "Author Highlight - Sherman.png",
        "caption": (
            "Sherman Merricks built the sales systems behind LASSO's 70 percent close rates "
            "on cold traffic.\n\n"
            "As a former gym owner himself, he understands the daily challenges of scaling a "
            "fitness business. His lead nurture systems and objection handling scripts have "
            "been installed in hundreds of gyms across the country.\n\n"
            "The Full Gym drops September 8th. Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 6,
        "date": "2026-08-19",
        "filename": "Author Highlight - Combined.png",
        "caption": (
            "Sherman Merricks and Blake Ruff are the cofounders of LASSO Framework. "
            "A gym growth and sales consulting company that has helped more than 1,000 "
            "gym owners build predictable systems for lead generation, sales, and long "
            "term growth.\n\n"
            "Two former gym owners. Two StoryBrand Certified Guides. The systems are "
            "real because they lived the problem first.\n\n"
            "The Full Gym. September 8th on Amazon. Link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 7,
        "date": "2026-08-22",
        "filename": "Book Post 1.png",
        "caption": (
            "The Full Gym. Coming to Amazon September 8th.\n\n"
            "Two former gym owners. More than 1,000 gyms. A manual built from everything "
            "that actually moved the number.\n\n"
            "The Boutique Gym Owner's Guide to Predictable Monthly Growth.\n\n"
            "Pre-order at the link in bio.\n"
            "https://fullgym.lassoframework.com/waitlist"
        ),
    },
    {
        "num": 8,
        "date": "2026-08-26",
        "filename": "Book Post 2.png",
        "caption": (
            "It is almost here.\n\n"
            "September 8th. Available in print and digital on Amazon.\n\n"
            "Nineteen chapters. Nineteen frameworks. Every tool downloadable. "
            "The KPI tracker. The ad budget calculator. The consultation script. "
            "A fourteen day follow up sequence. All of it.\n\n"
            "Pre-order at the link in bio.\n"
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
    n = len(BOOK_POSTS)

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
    print(f"\n{uploaded} new uploads. {len(manifest)}/{n} images in manifest.")
    if len(manifest) == n:
        print(f"\nAll {n} images uploaded. Now commit book_manifest.json and set:")
        print("  AGENT_BOOK_QUEUE_ON_START=true  in Railway Variables, then deploy.")
    else:
        missing = [p["filename"] for p in BOOK_POSTS if p["filename"] not in manifest]
        print(f"Missing: {missing}")
    return manifest


# ---- Phase 2: create drafts from manifest ----------------------------------

def create_drafts(manifest=None):
    """Create 16 PENDING draft posts (8 images x lasso_ig + lasso_fb) in the DB.
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
            print(f"  queued post {post['num']:02d} {day} {acct} -> {did}")

    print(f"\n{created} drafts created. All PENDING for approval. Nothing published.")
    return created


# ---- Daily runner hook -----------------------------------------------------

def build_book_queue_draft(account, day_key):
    """Return a Draft if this account has a scheduled book post for day_key.

    Called by runner.run_daily() before the campaign builder chain. Returns
    None when this is not a book queue day or the manifest is missing."""
    if account.key not in ACCOUNTS:
        return None
    post = next((p for p in BOOK_POSTS if p["date"] == day_key), None)
    if post is None:
        return None
    manifest = _load_manifest()
    if not manifest:
        return None
    url = manifest.get(post["filename"])
    if not url:
        print(f"[book-queue] manifest missing URL for {post['filename']} — skipping")
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
        draft_type="feed",
    )


# ---- Expire existing Slack cards -------------------------------------------

def expire_existing_drafts():
    """Mark all PENDING book_ drafts EXPIRED so existing Slack cards are inert.

    The daily runner re-surfaces each post on its scheduled date and
    auto-publishes it (AGENT_AUTO_APPROVE_ENABLED). Run this once after the
    initial book_manifest seeding so stale Slack cards can't be accidentally
    approved early.

    Usage (Railway CLI):
      railway run .venv/bin/python -m agent book-queue --expire-book-queue
    """
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
        if not (d.draft_id or "").startswith("book_"):
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

    print(f"\n{expired_n} book_ draft(s) expired. "
          "Existing Slack cards are now inert (approve tap is a no-op).\n"
          "The daily runner will publish each post on its scheduled date.\n"
          "You can now remove AGENT_BOOK_QUEUE_ON_START from Railway Variables.")
    return expired_n


# ---- CLI entry point -------------------------------------------------------

def run(images_dir=None, from_manifest=False, expire_only=False):
    """Main entry point called from __main__.py."""
    if expire_only:
        expire_existing_drafts()
        return

    manifest = _load_manifest()

    if not images_dir and not from_manifest:
        print("The Full Gym book queue status")
        n = len(BOOK_POSTS)
        print(f"  manifest:   {os.path.normpath(MANIFEST_PATH)}")
        print(f"  images in manifest: {len(manifest)}/{n}")
        if manifest:
            for post in BOOK_POSTS:
                status = "uploaded" if post["filename"] in manifest else "MISSING"
                print(f"    post {post['num']:02d} {post['date']} {status}")
        print("\nUsage:")
        print("  Step 1 (upload, run from Mac):")
        print('    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent book-queue \\')
        print('      --images-dir "/Users/blakeruff/LASSO Dropbox/Blake Ruff/Mac/Downloads"')
        print("  Step 2 (create drafts, set Railway Variable + deploy):")
        print("    AGENT_BOOK_QUEUE_ON_START=true")
        return

    if images_dir:
        manifest = upload_images(images_dir)

    if from_manifest or (images_dir and len(manifest) == len(BOOK_POSTS)):
        create_drafts(manifest)
