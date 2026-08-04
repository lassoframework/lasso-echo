"""
welcome_queue.py — the one-per-day welcome drip + the new-client trigger.

Where welcome_posts.py DEFINES a brand-new client and RENDERS the feed + story
cards, this module turns that roster into a steady drip: at most ONE welcome per
day, cross-posted to lasso_ig + lasso_fb (feed) with its 9:16 story on lasso_ig,
served by the daily runner exactly like the book queue serves one book post a day.

Two jobs, one queue:

  1. THE TRIGGER (scan_and_enqueue): the daily runner scans Stripe for brand-new
     clients (welcome_posts.backfill), renders each ready card, hosts it, and drops
     it in the queue. This covers BOTH the 45-day catch-up (every past client
     enqueues once) AND every future new client (picked up the day their first core
     subscription goes active). Stripe is the ONLY trigger; the portal only enriches.

  2. THE DRIP (build_welcome_queue_draft / build_welcome_story_draft): the runner
     serves the OLDEST queued welcome, one gym per day. The same item serves the
     feed on both accounts and the story on lasso_ig, so a gym is welcomed exactly
     once across the fan-out.

Hard rules (unchanged):
  * Behind AGENT_WELCOME_QUEUE_ENABLED, default OFF. OFF -> the runner hooks return
    None and scan_and_enqueue no-ops: byte-for-byte current behavior.
  * A gym is welcomed ONCE. enqueue stamps the welcome_posts ledger, so the next
    scan lands the gym in already_welcomed and never re-hosts or re-queues it.
  * Nothing here decides to publish. Served drafts are PENDING; they card for
    approval, or auto-publish ONLY when AGENT_AUTO_APPROVE_ENABLED is already armed
    (the runner's existing path). The story side also needs AGENT_STORIES_ENABLED.
  * No fabricated facts: the caption is a fixed, on-brand template over the gym name
    and owner only. No invented stats, offers, or prices. No dashes in the copy.
"""

import hashlib
import json
import os

from . import config, db, media_host, schedule, welcome_posts
from .drafter import Draft, DraftStatus

ACCOUNTS = ["lasso_ig", "lasso_fb"]      # feed cross-post targets
STORY_ACCOUNTS = ["lasso_ig"]            # story target (IG only, like book stories)

# The catch-up manifest: rendered + hosted from the Mac (where the logo/name overrides
# live), committed, then seeded into Railway's queue on deploy. Same two-step the book
# launch uses, so the correct cards reach Railway without shipping the override files.
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "welcome_queue_manifest.json")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS welcome_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gym_key TEXT UNIQUE,
  name TEXT,
  owner TEXT DEFAULT '',
  template TEXT DEFAULT '',
  caption TEXT DEFAULT '',
  feed_url TEXT DEFAULT '',
  story_url TEXT DEFAULT '',
  tier TEXT DEFAULT '',
  status TEXT DEFAULT 'queued',
  served_day TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')));
"""


def _conn():
    conn = db.connect()
    conn.executescript(_SCHEMA)
    return conn


# ---- caption (fixed, no fabrication, no dashes) ----------------------------------------

def welcome_caption(name, owner=""):
    """The welcome-post caption: a fixed, on-brand template over the gym name and
    owner only. No invented facts, offers, or stats. Deliberately dash-free so it
    never violates the published-copy rule (the gym name is a proper noun and is
    passed through as-is)."""
    who = f"{owner.strip()} and the {name} team" if (owner or "").strip() \
        else f"the {name} team"
    return (
        f"Welcome to the LASSO family, {name}.\n\n"
        f"We are proud to partner with {who}. Here is to more of the right members "
        f"through your doors and a gym that grows on purpose.\n\n"
        f"Let us go build something that lasts."
    )


# ---- enqueue: a rendered welcome enters the drip ---------------------------------------

def _draft_id(account_key, gym_key, kind):
    h = hashlib.sha1(f"welcome|{account_key}|{gym_key}|{kind}".encode()).hexdigest()[:12]
    return f"welc{kind[0]}_{h}"


def enqueue(entry, host_fn=None):
    """Host a rendered welcome (an `included` gym from welcome_posts.backfill) and
    add it to the drip, idempotent by gym_key. Stamps the welcome ledger so the gym
    is never re-scanned. Returns the row id, None if it was already queued, or None
    on a hosting failure (left un-stamped so the next scan retries). Requires
    AGENT_HOSTING_ENABLED (R2)."""
    key = entry["gym_key"]
    posts = entry.get("posts") or {}
    feed_path, story_path = posts.get("feed"), posts.get("story")
    if not feed_path:
        return None

    with _conn() as conn:
        row = conn.execute("SELECT id FROM welcome_queue WHERE gym_key=?",
                           (key,)).fetchone()
    if row is not None:
        return None  # already queued; idempotent

    host = host_fn or (lambda p: media_host.host_media(p, "lasso_welcome"))
    feed_url = host(feed_path)
    story_url = host(story_path) if story_path else ""
    if not feed_url:
        print(f"[welcome-queue] hosting failed for {entry['name']}; left un-queued "
              "(next scan retries)")
        return None

    caption = welcome_caption(entry["name"], entry.get("owner", ""))
    with db._lock, _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO welcome_queue "
            "(gym_key, name, owner, template, caption, feed_url, story_url, tier, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'queued')",
            (key, entry["name"], entry.get("owner", ""), entry.get("template", ""),
             caption, feed_url, story_url or "", entry.get("tier_label") or ""))
        conn.commit()
        row_id = cur.lastrowid
    # Stamp the ledger only after the row is safely in the queue: the gym is now
    # committed to be welcomed exactly once.
    welcome_posts.mark_welcomed(key)
    db.audit("welcome_enqueued", key, f"{entry['name']} queued for the drip",
             "lasso", "")
    print(f"[welcome-queue] queued {entry['name']} ({key})")
    return row_id


# ---- serving: one item per day, shared across the fan-out ------------------------------

def next_for_day(day_key):
    """The welcome to serve on day_key. Idempotent and order-independent: the first
    caller on a given day pops the OLDEST queued item and marks it served for that
    day; every later caller that day (the second account's feed, the story) gets the
    SAME item. Returns a dict, or None when the queue is empty."""
    with db._lock, _conn() as conn:
        row = conn.execute("SELECT * FROM welcome_queue WHERE served_day=? "
                           "ORDER BY id LIMIT 1", (day_key,)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM welcome_queue WHERE status='queued' "
                               "ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE welcome_queue SET status='served', served_day=? "
                         "WHERE id=?", (day_key, row["id"]))
            conn.commit()
        return dict(row)


def queue_status():
    with _conn() as conn:
        rows = conn.execute("SELECT gym_key, name, status, served_day FROM "
                            "welcome_queue ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ---- catch-up manifest (Mac builds, Railway seeds) -------------------------------------

def _load_manifest():
    p = os.path.normpath(MANIFEST_PATH)
    if not os.path.isfile(p):
        return []
    with open(p) as f:
        return json.load(f)


def _save_manifest(rows):
    p = os.path.normpath(MANIFEST_PATH)
    with open(p, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"welcome manifest saved: {p} ({len(rows)} welcomes)")
    return p


def build_manifest(reader=None, scraper=None, host_fn=None, window_days=45,
                   out_dir=None, cache_dir=None):
    """Run from the Mac (where the logo/name/force overrides live). Renders every READY
    welcome via backfill, hosts feed + story to R2, and writes the committed manifest
    Railway seeds from. Ordered oldest-client-first so the drip drains chronologically.
    Requires AGENT_HOSTING_ENABLED. Returns the manifest rows."""
    if not config.hosting_enabled():
        raise RuntimeError("AGENT_HOSTING_ENABLED not set; cannot host cards to R2.")
    report = welcome_posts.backfill(window_days=window_days, reader=reader,
                                    scraper=scraper, out_dir=out_dir, cache_dir=cache_dir)
    if report.get("error"):
        raise RuntimeError(report["error"])
    host = host_fn or (lambda p: media_host.host_media(p, "lasso_welcome"))
    included = sorted(report.get("included", []),
                      key=lambda e: e.get("start_date") or 0)  # oldest first
    rows = []
    for e in included:
        posts = e.get("posts") or {}
        feed_url = host(posts["feed"]) if posts.get("feed") else ""
        story_url = host(posts["story"]) if posts.get("story") else ""
        if not feed_url:
            print(f"[welcome-queue] host failed for {e['name']}; skipped")
            continue
        rows.append({
            "gym_key": e["gym_key"], "name": e["name"], "owner": e.get("owner", ""),
            "template": e.get("template", ""), "tier": e.get("tier_label") or "",
            "caption": welcome_caption(e["name"], e.get("owner", "")),
            "feed_url": feed_url, "story_url": story_url or "",
        })
    _save_manifest(rows)
    return rows


def create_from_manifest():
    """Run on Railway (AGENT_WELCOME_QUEUE_ON_START). Insert the manifest's welcomes into
    the queue, idempotent by gym_key, stamping the ledger so the daily Stripe scan never
    re-queues them. No hosting, no rendering. Returns the count newly seeded."""
    rows = _load_manifest()
    created = 0
    for r in rows:
        with db._lock, _conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO welcome_queue "
                "(gym_key, name, owner, template, caption, feed_url, story_url, tier, status) "
                "VALUES (?,?,?,?,?,?,?,?, 'queued')",
                (r["gym_key"], r["name"], r.get("owner", ""), r.get("template", ""),
                 r.get("caption") or welcome_caption(r["name"], r.get("owner", "")),
                 r["feed_url"], r.get("story_url", ""), r.get("tier", "")))
            conn.commit()
            if cur.rowcount:
                created += 1
        welcome_posts.mark_welcomed(r["gym_key"])
    print(f"[welcome-queue] seeded {created} welcome(s) from manifest "
          f"({len(rows)} in file). Drip serves one/day when AGENT_WELCOME_QUEUE_ENABLED is on.")
    return created


# ---- runner hooks ----------------------------------------------------------------------

def build_welcome_queue_draft(account, day_key):
    """The day's welcome FEED as a PENDING draft for a LASSO account, or None (flag
    off, not a LASSO feed account, or the queue is empty). Called by run_daily after
    the dated book queue, so a book-launch date always keeps its slot."""
    if not config.welcome_queue_enabled():
        return None
    if account.key not in ACCOUNTS:
        return None
    item = next_for_day(day_key)
    if item is None:
        return None
    platform = getattr(account, "platform", account.key)
    return Draft(
        draft_id=_draft_id(account.key, item["gym_key"], "feed"),
        account_key=account.key,
        platform=platform,
        caption=item["caption"],
        hashtags=[],
        creative_path=f"welcome_{item['gym_key']}.png",
        creative_public_url=item["feed_url"],
        scheduled_for=schedule.scheduled_for(day_key),
        status=DraftStatus.PENDING,
        day_key=day_key,
        draft_type="feed",
    )


def build_welcome_story_draft(account, day_key, feed_draft=None):
    """The 9:16 welcome STORY for lasso_ig, matched to the SAME gym the feed served
    today. Returns None unless this run's feed draft is a welcome draft (so a story
    never pops a gym the feed did not post) and a story image exists. The publisher
    still requires AGENT_STORIES_ENABLED to actually send it."""
    if not config.welcome_queue_enabled():
        return None
    if account.key not in STORY_ACCOUNTS:
        return None
    # Couple the story to the feed: only fire when today's feed was a welcome.
    if not (feed_draft is not None
            and (feed_draft.draft_id or "").startswith("welcf_")):
        return None
    item = next_for_day(day_key)
    if item is None or not item.get("story_url"):
        return None
    platform = getattr(account, "platform", account.key)
    return Draft(
        draft_id=_draft_id(account.key, item["gym_key"], "story"),
        account_key=account.key,
        platform=platform,
        caption=item["caption"],
        hashtags=[],
        creative_path=f"welcome_{item['gym_key']}_story.png",
        creative_public_url=item["story_url"],
        scheduled_for=schedule.scheduled_for(day_key),
        status=DraftStatus.PENDING,
        day_key=day_key,
        draft_type="story",
        is_story=True,
    )


# ---- the trigger: scan Stripe daily and enqueue ready welcomes -------------------------

def scan_and_enqueue(reader=None, scraper=None, host_fn=None, window_days=45,
                     bg_client=None, force=False, out_dir=None, cache_dir=None):
    """Scan Stripe for brand-new clients and enqueue every READY welcome (a resolved
    name + a usable logo). Idempotent: an already-welcomed gym lands in
    already_welcomed and is skipped, so this never re-hosts. Returns a summary dict.
    Requires AGENT_HOSTING_ENABLED to host the rendered cards.

    This is the automatic new-client trigger AND the 45-day catch-up in one: run it
    every daily cycle (the runner does) and any newly-qualified client is queued the
    day it qualifies. `force=True` bypasses the flag gate for manual CLI seeding (the
    drip still stays dark until AGENT_WELCOME_QUEUE_ENABLED is armed)."""
    if not force and not config.welcome_queue_enabled():
        return {"scanned": False, "reason": "AGENT_WELCOME_QUEUE_ENABLED off"}
    if not config.hosting_enabled():
        return {"scanned": False, "reason": "AGENT_HOSTING_ENABLED off (cannot host cards)"}

    report = welcome_posts.backfill(window_days=window_days, reader=reader,
                                    scraper=scraper, bg_client=bg_client,
                                    out_dir=out_dir, cache_dir=cache_dir)
    if report.get("error"):
        return {"scanned": False, "reason": report["error"]}

    enqueued = 0
    for entry in report.get("included", []):
        try:
            if enqueue(entry, host_fn=host_fn):
                enqueued += 1
        except Exception as e:  # one bad gym never stops the scan
            print(f"[welcome-queue] enqueue failed for {entry.get('name')}: "
                  f"{type(e).__name__}: {e}")
    return {
        "scanned": True,
        "enqueued": enqueued,
        "ready_seen": len(report.get("included", [])),
        "needs_confirmation": len(report.get("needs_confirmation", [])),
        "needs_logo": len(report.get("needs_logo", [])),
        "already_welcomed": len(report.get("already_welcomed", [])),
    }
