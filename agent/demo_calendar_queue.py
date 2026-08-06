"""
demo_calendar_queue.py — the 30-day done-for-you organic DEMO calendar for LASSO's
own brand.

Purpose: 30 REAL Echo drafts that flow through Echo's actual pipeline and render
approval cards, so onboarding a client onto done-for-you organic can be experienced
end to end. Each caption is assembled ONLY from approved source lines (the lasso_now
copy bank and the approved receipts); no fabrication, no dashes.

Mirrors two proven patterns:
  * book_queue.py  — the two-step manifest + ON_START seeding (Mac hosts the PNGs to
    R2 and writes demo_calendar_manifest.json; Railway seeds the queue from it on
    deploy) and the dated, one-post-per-day serve.
  * welcome_queue.py — the served_day lock, so the SAME dated item serves the feed on
    BOTH lasso_ig + lasso_fb exactly once across the fan-out, and its story on lasso_ig
    is coupled to that day's feed.

TWO POSTS PER DAY (Blake ruling): every one of the 30 days serves BOTH a feed post AND
a paired 9:16 story on lasso_ig (2 posts/day). Every DEMO_POSTS entry carries is_story,
so a story fires on every demo feed day, coupled to that day's feed draft.

Hard rules (unchanged):
  * Behind AGENT_DEMO_CALENDAR_ENABLED, default OFF. OFF -> every runner hook returns
    None: byte-for-byte current behavior. Isolated from the book / welcome / summit
    queues (its own flag, its own manifest, "demo_" namespaced draft ids).
  * Nothing here decides to publish. Served drafts are PENDING; they card for approval,
    or auto-publish ONLY when AGENT_AUTO_APPROVE_ENABLED is already armed (the runner's
    existing path). The story side also needs AGENT_STORIES_ENABLED.
  * No fabricated facts. Every hook + body line below is verbatim from an approved
    source: brand_voice/lasso_now.md (the pillar copy bank + CTAs) or the approved
    receipts in brand_voice/knowledge/08_platform_2026.md and
    brand_voice/knowledge/02_verified_stats.md. No em/en/hyphen dashes in any caption.

Two-step workflow:

  Step 1 (Mac, with railway run):
    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent demo-calendar \\
      --images-dir "PATH/TO/content_library/demo_calendar"
    Uploads the rendered PNGs to R2 and writes demo_calendar_manifest.json at the repo root.

  Step 2 (Railway container, via AGENT_DEMO_CALENDAR_ON_START):
    Set AGENT_DEMO_CALENDAR_ON_START=true in Railway Variables and deploy.
    Reads demo_calendar_manifest.json, seeds the demo queue. Nothing publishes.

Run with no args to see current status.
"""

import hashlib
import json
import os

from . import config, db, media_host, schedule
from .drafter import Draft, DraftStatus

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "demo_calendar_manifest.json")

ACCOUNTS = ["lasso_ig", "lasso_fb"]      # feed cross-post targets
STORY_ACCOUNTS = ["lasso_ig"]            # story target (IG only, like the other queues)

# Approved hashtags (3 to 5) from brand_voice/lasso_now.md line 93. The whole set is
# on-brand; every card carries the same approved five. Echo never invents new ones.
HASHTAGS = ["#LASSOFramework", "#GymMarketingMadeSimple", "#GymOwner",
            "#GymLeads", "#DoneForYou"]

# The 30 dated posts. Pillars rotate the 5 lasso_now pillars in order:
# All in one offer / Sales are now / We do the heavy lifting / The portal / Proof.
# hook + body are verbatim from the cited approved source; cta is one of the approved
# lasso_now CTAs (lines 86 to 90), cycled in order. filename -> the rendered card.
# is_story is True on EVERY day: every demo feed day is paired with a 9:16 story on
# lasso_ig, so the calendar serves two posts per day (feed + story).
DEMO_POSTS = [
    {"num": 1, "date": "2026-08-06", "pillar": "All in one offer",
     "filename": "demo_01_all_in_one.png", "is_story": True,
     "hook": "The 5 tools running your gym should be one.",
     "body": ("Ads, lead nurture, your website, your social, and your reporting. "
              "LASSO puts it all in one place, done for you, so you stop duct taping "
              "tools together."),
     "cta": "Save this for later."},
    {"num": 2, "date": "2026-08-07", "pillar": "Sales are now",
     "filename": "demo_02_sales_now.png", "is_story": True,
     "hook": "You did not open a gym to run ads at 11pm.",
     "body": ("The job is closing members, not building funnels. We get the leads and "
              "nurture them. You do the one thing only you can do: sell."),
     "cta": "Send this to a gym owner who needs it."},
    {"num": 3, "date": "2026-08-08", "pillar": "We do the heavy lifting",
     "filename": "demo_03_heavy_lifting.png", "is_story": True,
     "hook": "We run your social media for you.",
     "body": ("We plan the month, draft every post, and track what is working. A human "
              "approves every post before it goes live. You get the results without "
              "doing the work."),
     "cta": "Tag a gym owner who needs this."},
    {"num": 4, "date": "2026-08-09", "pillar": "The portal",
     "filename": "demo_04_portal.png", "is_story": True,
     "hook": "Every lead, every post, every result. One screen.",
     "body": ("Your leads, your content, and your reporting live in one place. One login "
              "runs your whole growth engine."),
     "cta": "Book a free call and we will look at your numbers."},
    {"num": 5, "date": "2026-08-10", "pillar": "Proof",
     "filename": "demo_05_proof_builtby.png", "is_story": True,
     "hook": "Built by gym owners, for gym owners.",
     "body": ("We run the same system on ourselves before we ever hand it to you. No "
              "guesswork and no bait and switch. Just the system, run for you."),
     "cta": "Take the 2 minute quiz."},
    {"num": 6, "date": "2026-08-11", "pillar": "All in one offer",
     "filename": "demo_06_six_engines.png", "is_story": True,
     "hook": "Six engines. One job: your MRR.",
     "body": ("Paid ads, Google, AI nurture plus live bookers, a website built to book, "
              "done for you social, and the LASSO Portal. One login."),
     "cta": "Save this for later."},
    {"num": 7, "date": "2026-08-12", "pillar": "Sales are now",
     "filename": "demo_07_we_chase.png", "is_story": True,
     "hook": "We chase. You close.",
     "body": ("Leads do not die in your ads. They die in the handoffs. More leads never "
              "fix a broken sales conversation."),
     "cta": "Send this to a gym owner who needs it."},
    {"num": 8, "date": "2026-08-13", "pillar": "We do the heavy lifting",
     "filename": "demo_08_heavy_lifting_b.png", "is_story": True,
     "hook": "We run your social media for you.",
     "body": ("We plan the month, draft every post, and track what is working. A human "
              "approves every post before it goes live. You get the results without "
              "doing the work."),
     "cta": "Tag a gym owner who needs this."},
    {"num": 9, "date": "2026-08-14", "pillar": "The portal",
     "filename": "demo_09_cockpit.png", "is_story": True,
     "hook": "Agencies send reports. LASSO hands you the cockpit.",
     "body": ("Your leads, your content, and your reporting live in one place. One login "
              "runs your whole growth engine."),
     "cta": "Book a free call and we will look at your numbers."},
    {"num": 10, "date": "2026-08-15", "pillar": "Proof",
     "filename": "demo_10_proof_booking.png", "is_story": True,
     "hook": "71.9% booked vs an 18.5% industry average. Same leads. Very different outcomes.",
     "body": ("We book 71.9 percent. The industry books 18.5 percent. Same leads, very "
              "different outcomes."),
     "cta": "Take the 2 minute quiz."},
    {"num": 11, "date": "2026-08-16", "pillar": "All in one offer",
     "filename": "demo_11_all_in_one_b.png", "is_story": True,
     "hook": "The 5 tools running your gym should be one.",
     "body": ("Ads, lead nurture, your website, your social, and your reporting. "
              "LASSO puts it all in one place, done for you, so you stop duct taping "
              "tools together."),
     "cta": "Save this for later."},
    {"num": 12, "date": "2026-08-17", "pillar": "Sales are now",
     "filename": "demo_12_signing_up.png", "is_story": True,
     "hook": "Your only job is signing people up.",
     "body": ("The job is closing members, not building funnels. We get the leads and "
              "nurture them. You do the one thing only you can do: sell."),
     "cta": "Send this to a gym owner who needs it."},
    {"num": 13, "date": "2026-08-18", "pillar": "We do the heavy lifting",
     "filename": "demo_13_plan_draft_track.png", "is_story": True,
     "hook": "We run your social media for you.",
     "body": ("We plan the month, draft every post, and track what is working. A human "
              "approves every post before it goes live. You get the results without "
              "doing the work."),
     "cta": "Tag a gym owner who needs this."},
    {"num": 14, "date": "2026-08-19", "pillar": "The portal",
     "filename": "demo_14_one_screen_b.png", "is_story": True,
     "hook": "Every lead, every post, every result. One screen.",
     "body": ("Your leads, your content, and your reporting live in one place. One login "
              "runs your whole growth engine."),
     "cta": "Book a free call and we will look at your numbers."},
    {"num": 15, "date": "2026-08-20", "pillar": "Proof",
     "filename": "demo_15_proof_cpl.png", "is_story": True,
     "hook": "$16 blended CPL across the portfolio; the industry pays 2x.",
     "body": "More than $35K in wasted ad spend saved; $17K flagged in one cycle.",
     "cta": "Take the 2 minute quiz."},
    {"num": 16, "date": "2026-08-21", "pillar": "All in one offer",
     "filename": "demo_16_six_engines_b.png", "is_story": True,
     "hook": "Six engines. One job: your MRR.",
     "body": ("Paid ads, Google, AI nurture plus live bookers, a website built to book, "
              "done for you social, and the LASSO Portal. One login."),
     "cta": "Save this for later."},
    {"num": 17, "date": "2026-08-22", "pillar": "Sales are now",
     "filename": "demo_17_11pm_b.png", "is_story": True,
     "hook": "You did not open a gym to run ads at 11pm.",
     "body": ("The job is closing members, not building funnels. We get the leads and "
              "nurture them. You do the one thing only you can do: sell."),
     "cta": "Send this to a gym owner who needs it."},
    {"num": 18, "date": "2026-08-23", "pillar": "We do the heavy lifting",
     "filename": "demo_18_human_approves.png", "is_story": True,
     "hook": "We run your social media for you.",
     "body": ("We plan the month, draft every post, and track what is working. A human "
              "approves every post before it goes live. You get the results without "
              "doing the work."),
     "cta": "Tag a gym owner who needs this."},
    {"num": 19, "date": "2026-08-24", "pillar": "The portal",
     "filename": "demo_19_cockpit_b.png", "is_story": True,
     "hook": "Agencies send reports. LASSO hands you the cockpit.",
     "body": ("Your leads, your content, and your reporting live in one place. One login "
              "runs your whole growth engine."),
     "cta": "Book a free call and we will look at your numbers."},
    {"num": 20, "date": "2026-08-25", "pillar": "Proof",
     "filename": "demo_20_proof_fitmamas.png", "is_story": True,
     "hook": "Fit Mamas Tribe took monthly revenue from $19K to $47K on the LASSO system.",
     "body": "Average client value up from $99 to $167 at the same time.",
     "cta": "Take the 2 minute quiz."},
    {"num": 21, "date": "2026-08-26", "pillar": "All in one offer",
     "filename": "demo_21_all_in_one_c.png", "is_story": True,
     "hook": "The 5 tools running your gym should be one.",
     "body": ("Ads, lead nurture, your website, your social, and your reporting. "
              "LASSO puts it all in one place, done for you, so you stop duct taping "
              "tools together."),
     "cta": "Save this for later."},
    {"num": 22, "date": "2026-08-27", "pillar": "Sales are now",
     "filename": "demo_22_we_chase_b.png", "is_story": True,
     "hook": "We chase. You close.",
     "body": ("Leads do not die in your ads. They die in the handoffs. More leads never "
              "fix a broken sales conversation."),
     "cta": "Send this to a gym owner who needs it."},
    {"num": 23, "date": "2026-08-28", "pillar": "We do the heavy lifting",
     "filename": "demo_23_plan_grid.png", "is_story": True,
     "hook": "We run your social media for you.",
     "body": ("We plan the month, draft every post, and track what is working. A human "
              "approves every post before it goes live. You get the results without "
              "doing the work."),
     "cta": "Tag a gym owner who needs this."},
    {"num": 24, "date": "2026-08-29", "pillar": "The portal",
     "filename": "demo_24_one_screen_c.png", "is_story": True,
     "hook": "Every lead, every post, every result. One screen.",
     "body": ("Your leads, your content, and your reporting live in one place. One login "
              "runs your whole growth engine."),
     "cta": "Book a free call and we will look at your numbers."},
    {"num": 25, "date": "2026-08-30", "pillar": "Proof",
     "filename": "demo_25_proof_courage.png", "is_story": True,
     "hook": "Courage Fitness: First $1M year. $84K MRR. Leads up from 30 to 80+ per month.",
     "body": ("Courage Fitness: 30 to 80+ leads per month and $84K MRR, evenings back "
              "included."),
     "cta": "Take the 2 minute quiz."},
    {"num": 26, "date": "2026-08-31", "pillar": "All in one offer",
     "filename": "demo_26_six_engines_c.png", "is_story": True,
     "hook": "Six engines. One job: your MRR.",
     "body": ("Paid ads, Google, AI nurture plus live bookers, a website built to book, "
              "done for you social, and the LASSO Portal. One login."),
     "cta": "Save this for later."},
    {"num": 27, "date": "2026-09-01", "pillar": "Sales are now",
     "filename": "demo_27_signing_up_b.png", "is_story": True,
     "hook": "Your only job is signing people up.",
     "body": ("The job is closing members, not building funnels. We get the leads and "
              "nurture them. You do the one thing only you can do: sell."),
     "cta": "Send this to a gym owner who needs it."},
    {"num": 28, "date": "2026-09-02", "pillar": "We do the heavy lifting",
     "filename": "demo_28_plan_draft_track_b.png", "is_story": True,
     "hook": "We run your social media for you.",
     "body": ("We plan the month, draft every post, and track what is working. A human "
              "approves every post before it goes live. You get the results without "
              "doing the work."),
     "cta": "Tag a gym owner who needs this."},
    {"num": 29, "date": "2026-09-03", "pillar": "The portal",
     "filename": "demo_29_cockpit_c.png", "is_story": True,
     "hook": "Agencies send reports. LASSO hands you the cockpit.",
     "body": ("Your leads, your content, and your reporting live in one place. One login "
              "runs your whole growth engine."),
     "cta": "Book a free call and we will look at your numbers."},
    {"num": 30, "date": "2026-09-04", "pillar": "Proof",
     "filename": "demo_30_proof_twostat.png", "is_story": True,
     "hook": "North Naples CrossFit: 14 clients in 14 days and +27% YoY.",
     "body": "Old Glory Gym: 90% close rate and +$21K.",
     "cta": "Take the 2 minute quiz."},
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_calendar_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  num INTEGER,
  day_key TEXT UNIQUE,
  pillar TEXT DEFAULT '',
  filename TEXT DEFAULT '',
  caption TEXT DEFAULT '',
  feed_url TEXT DEFAULT '',
  story_url TEXT DEFAULT '',
  is_story INTEGER DEFAULT 0,
  status TEXT DEFAULT 'queued',
  served_day TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')));
"""


def _conn():
    conn = db.connect()
    conn.executescript(_SCHEMA)
    return conn


# ---- caption (assembled from approved source only, no fabrication, no dashes) ----------

def build_caption(post):
    """hook + "\\n\\n" + body + "\\n\\n" + CTA + "\\n" + hashtags.

    Every line is verbatim from an approved source (see DEMO_POSTS). The hashtags are
    the approved set from lasso_now.md line 93. No invented text; no dashes are added by
    the assembly (the source lines are dash free)."""
    tags = " ".join(HASHTAGS)
    return f"{post['hook']}\n\n{post['body']}\n\n{post['cta']}\n{tags}"


# ---- draft ids (namespaced "demo_", never collides with book_ / welc*_) ----------------

def _draft_id(account_key, day_key, kind):
    h = hashlib.sha1(f"demo|{account_key}|{day_key}|{kind}".encode()).hexdigest()[:12]
    return f"demo{kind[0]}_{h}"


# ---- manifest (Mac builds + hosts, Railway seeds) --------------------------------------

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
    print(f"demo calendar manifest saved: {p}")
    return p


def upload_images(images_dir):
    """Host each rendered card (all 30 feed + all 30 story files) to R2 and record the URLs in
    demo_calendar_manifest.json, keyed by filename. Requires AGENT_HOSTING_ENABLED.
    Safe to re-run; already-uploaded files are skipped and content-addressed dedupe in
    media_host means the same asset is never stored twice."""
    if not config.hosting_enabled():
        raise RuntimeError(
            "AGENT_HOSTING_ENABLED is not set. Run with:\n"
            "  AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent "
            "demo-calendar --images-dir PATH")

    manifest = _load_manifest()
    # every feed filename, plus the story variant for each is_story day
    wanted = []
    for post in DEMO_POSTS:
        wanted.append(post["filename"])
        if post["is_story"]:
            wanted.append(_story_filename(post["filename"]))

    uploaded = 0
    for fname in wanted:
        local = os.path.join(images_dir, fname)
        if not os.path.isfile(local):
            print(f"  MISSING: {local}")
            continue
        if fname in manifest:
            print(f"  already uploaded: {fname} -> {manifest[fname]}")
            continue
        print(f"  uploading {fname} ...", end=" ", flush=True)
        url = media_host.host_media(local, "lasso_demo")
        if url:
            manifest[fname] = url
            print(f"ok -> {url}")
            uploaded += 1
        else:
            print("FAILED (check R2 credentials and AGENT_HOSTING_ENABLED)")

    _save_manifest(manifest)
    print(f"\n{uploaded} new uploads. {len(manifest)}/{len(wanted)} files in manifest.")
    if len(manifest) >= len(wanted):
        print("\nAll files uploaded. Commit demo_calendar_manifest.json and set:")
        print("  AGENT_DEMO_CALENDAR_ON_START=true  in Railway Variables, then deploy.")
    else:
        missing = [f for f in wanted if f not in manifest]
        print(f"Missing: {missing}")
    return manifest


def _story_filename(feed_filename):
    """The 9:16 story companion filename for a feed card."""
    stem, ext = os.path.splitext(feed_filename)
    return f"{stem}_story{ext}"


def create_from_manifest():
    """Run on Railway (AGENT_DEMO_CALENDAR_ON_START). Seed the demo queue from the
    committed manifest, idempotent by day_key. No hosting, no rendering. A post whose
    feed URL is not in the manifest is skipped (loud). Returns the count newly seeded."""
    manifest = _load_manifest()
    created = 0
    for post in DEMO_POSTS:
        feed_url = manifest.get(post["filename"], "")
        if not feed_url:
            print(f"[demo-calendar] manifest missing feed URL for post "
                  f"{post['num']:02d} ({post['filename']}); skipped")
            continue
        story_url = ""
        if post["is_story"]:
            story_url = manifest.get(_story_filename(post["filename"]), "")
        caption = build_caption(post)
        with db._lock, _conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO demo_calendar_queue "
                "(num, day_key, pillar, filename, caption, feed_url, story_url, is_story, status) "
                "VALUES (?,?,?,?,?,?,?,?, 'queued')",
                (post["num"], post["date"], post["pillar"], post["filename"], caption,
                 feed_url, story_url, 1 if post["is_story"] else 0))
            conn.commit()
            if cur.rowcount:
                created += 1
    print(f"[demo-calendar] seeded {created} demo post(s) from manifest "
          f"({len(DEMO_POSTS)} defined). Served one/day when AGENT_DEMO_CALENDAR_ENABLED is on.")
    return created


def queue_status():
    with _conn() as conn:
        rows = conn.execute("SELECT num, day_key, pillar, status, served_day FROM "
                            "demo_calendar_queue ORDER BY day_key").fetchall()
    return [dict(r) for r in rows]


# ---- serving: the dated item for the day, shared across the fan-out --------------------

def _row_for_day(day_key):
    """The seeded demo row scheduled for day_key (from the manifest seed), or None."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM demo_calendar_queue WHERE day_key=?",
                           (day_key,)).fetchone()
    return dict(row) if row is not None else None


def _post_for_day(day_key):
    """The static DEMO_POSTS entry for day_key, or None (used as a fallback source of
    the caption when no seeded row exists, e.g. local dev with the manifest empty)."""
    return next((p for p in DEMO_POSTS if p["date"] == day_key), None)


def _mark_served(day_key):
    """Idempotent, order-independent served stamp: the first hook on a given day flips
    the row to served; later hooks that day are no-ops. Only touches a seeded row."""
    with db._lock, _conn() as conn:
        row = conn.execute("SELECT status FROM demo_calendar_queue WHERE day_key=?",
                           (day_key,)).fetchone()
        if row is not None and row["status"] == "queued":
            conn.execute("UPDATE demo_calendar_queue SET status='served', served_day=? "
                         "WHERE day_key=?", (day_key, day_key))
            conn.commit()


# ---- runner hooks ----------------------------------------------------------------------

def build_demo_calendar_draft(account, day_key):
    """The day's dated demo FEED as a PENDING draft for a LASSO account, or None (flag
    off, not a LASSO feed account, not a demo date, or no hosted URL). Called by the
    runner after the dated book queue and welcome drip, before the rotation chain, so a
    book-launch date always keeps its slot. The SAME item serves both accounts."""
    if not config.demo_calendar_enabled():
        return None
    if account.key not in ACCOUNTS:
        return None
    row = _row_for_day(day_key)
    if row is None:
        return None
    feed_url = row.get("feed_url") or ""
    if not feed_url:
        print(f"[demo-calendar] no hosted feed URL for {day_key}; skipping")
        return None
    _mark_served(day_key)
    platform = getattr(account, "platform", account.key)
    return Draft(
        draft_id=_draft_id(account.key, day_key, "feed"),
        account_key=account.key,
        platform=platform,
        caption=row["caption"],
        hashtags=[],
        creative_path=row["filename"],
        creative_public_url=feed_url,
        scheduled_for=schedule.scheduled_for(day_key),
        status=DraftStatus.PENDING,
        day_key=day_key,
        draft_type="feed",
        # DEMO = always card for approve/deny/edit, never auto-publish, even when
        # AGENT_AUTO_APPROVE_ENABLED is armed. Blake reviews every demo post.
        force_approval=True,
    )


def build_demo_calendar_story_draft(account, day_key, feed_draft=None):
    """The 9:16 demo STORY for lasso_ig, matched to the SAME dated post the feed served
    today. Returns None unless this run's feed draft is a demo feed draft (so a story
    never pops a day the demo feed did not post) and a story image exists. The publisher
    still requires AGENT_STORIES_ENABLED to actually send it."""
    if not config.demo_calendar_enabled():
        return None
    if account.key not in STORY_ACCOUNTS:
        return None
    # Couple the story to the feed: only fire when today's feed was a demo feed draft.
    if not (feed_draft is not None
            and (getattr(feed_draft, "draft_id", "") or "").startswith("demof_")):
        return None
    row = _row_for_day(day_key)
    if row is None or not row.get("story_url"):
        return None
    platform = getattr(account, "platform", account.key)
    return Draft(
        draft_id=_draft_id(account.key, day_key, "story"),
        account_key=account.key,
        platform=platform,
        caption=row["caption"],
        hashtags=[],
        creative_path=_story_filename(row["filename"]),
        creative_public_url=row["story_url"],
        scheduled_for=schedule.scheduled_for(day_key),
        status=DraftStatus.PENDING,
        day_key=day_key,
        draft_type="story",
        is_story=True,
        force_approval=True,
    )


# ---- CLI entry point -------------------------------------------------------------------

def run(images_dir=None, from_manifest=False):
    """Main entry point called from __main__.py."""
    manifest = _load_manifest()

    if not images_dir and not from_manifest:
        print("Demo calendar queue status")
        print(f"  manifest:  {os.path.normpath(MANIFEST_PATH)}")
        print(f"  posts defined: {len(DEMO_POSTS)}")
        print(f"  files in manifest: {len(manifest)}")
        print(f"  flag AGENT_DEMO_CALENDAR_ENABLED: {config.demo_calendar_enabled()}")
        rows = queue_status()
        if rows:
            print("  queued/served rows:")
            for r in rows:
                print(f"    {r['num']:02d} {r['day_key']} {r['pillar']:>22} "
                      f"{r['status']}")
        print("\nUsage:")
        print("  Step 1 (upload, run from Mac):")
        print('    AGENT_HOSTING_ENABLED=true railway run .venv/bin/python -m agent '
              'demo-calendar \\')
        print('      --images-dir "PATH/TO/content_library/demo_calendar"')
        print("  Step 2 (seed drafts, set Railway Variable + deploy):")
        print("    AGENT_DEMO_CALENDAR_ON_START=true")
        return

    if images_dir:
        manifest = upload_images(images_dir)

    if from_manifest:
        create_from_manifest()
