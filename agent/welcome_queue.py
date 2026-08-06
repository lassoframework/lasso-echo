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
import re

from PIL import Image

from . import config, db, media_host, ops_alerts, portal_gyms, schedule, welcome_posts
from . import welcome_templates as wt
from . import website_scan
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


# ---- story asset guard (defense in depth, host layer) ----------------------------------

def _local_story_is_9_16(story_path):
    """GUARD (layer b, host): True only when the LOCAL story render is a genuine 9:16
    (1080x1920). A missing path, a None, or any off-size image (a square feed image
    at 1080x1080) returns False so the caller never hosts a square as a story_url. A
    story_url must always point at a real 9:16 asset, never a cropped feed card."""
    if not story_path or not os.path.isfile(story_path):
        return False
    try:
        with Image.open(story_path) as im:
            return wt.is_story_size(im.size)
    except Exception:
        return False


# ---- caption (fixed, no fabrication, no dashes) ----------------------------------------

# Welcome caption variants. StoryBrand voice: welcome the gym, name the
# partnership, a forward-looking line. The ONLY variables are the gym name and the
# owner (via {name} and {who}); NO invented facts, offers, prices, stats, or member
# numbers. Every variant is DASH-FREE (no em/en dash, no hyphen-as-punctuation) and
# never uses the word "vendor". A given gym always draws the SAME variant (stable
# hash of the gym name), but different gyms differ, so no two welcomes read identical.
_WELCOME_VARIANTS = (
    ("Welcome to the LASSO family, {name}.\n\n"
     "We are proud to partner with {who}. Here is to more of the right members "
     "through your doors and a gym that grows on purpose.\n\n"
     "Let us go build something that lasts."),

    ("{name} is officially part of the LASSO family.\n\n"
     "It is an honor to be in your corner. Working with {who}, we are focused on "
     "bringing the right people through your doors and building growth that holds.\n\n"
     "The best is ahead."),

    ("Say hello to our newest partner, {name}.\n\n"
     "We could not be more excited to work with {who}. Together we are going after "
     "steady, dependable growth and a community that keeps coming back.\n\n"
     "Let us get to work."),

    ("A big LASSO welcome to {name}.\n\n"
     "Partnering with {who} is a privilege, and we are all in on your success. More "
     "of the right members, a stronger community, a gym that grows with intention.\n\n"
     "Here is to the road ahead."),

    ("We are proud to welcome {name} to LASSO.\n\n"
     "Behind every great gym is a team that shows up, and {who} do exactly that. "
     "Now we build the kind of growth that lasts and a membership that feels like "
     "home.\n\n"
     "Onward."),

    ("{name}, welcome to LASSO.\n\n"
     "Standing alongside {who} means everything to us. Our promise is simple: the "
     "right members, real momentum, and a gym that grows the way you always wanted "
     "it to.\n\n"
     "Let us build it together."),

    ("Thrilled to have {name} in the LASSO family.\n\n"
     "Getting to work with {who} is why we do this. We are set on bringing the right "
     "people through your doors and turning that into growth you can count on.\n\n"
     "This is just the beginning."),
)


def _welcome_variant_index(name):
    """Deterministic variant index for a gym: a stable hash of the gym name, so the
    SAME gym always draws the SAME caption on a re-run, while different gyms differ.
    Hashed on the normalized name (lowercased, whitespace-collapsed) so trivial
    spacing differences do not shift the pick."""
    key = " ".join((name or "").lower().split())
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    return h % len(_WELCOME_VARIANTS)


def welcome_caption(name, owner=""):
    """The welcome-post caption: one of several on-brand StoryBrand variants selected
    DETERMINISTICALLY per gym (stable hash of the gym name), so no two gyms read
    identically but a given gym is stable across re-runs. The ONLY fill values are the
    gym name and the owner. No invented facts, offers, prices, or stats. Every variant
    is deliberately dash-free (never an em/en dash or a hyphen-as-punctuation) so it
    can never violate the published-copy rule; the gym name is a proper noun passed
    through as-is."""
    who = f"{owner.strip()} and the {name} team" if (owner or "").strip() \
        else f"the {name} team"
    template = _WELCOME_VARIANTS[_welcome_variant_index(name)]
    return template.format(name=name, who=who)


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
    # GUARD (layer b, host): only host the story if the LOCAL render is a genuine 9:16
    # (1080x1920). A square/None/off-size story is skipped loudly; story_url stays
    # empty, so a cropped feed card can never enter the queue as a story_url.
    if story_path and _local_story_is_9_16(story_path):
        story_url = host(story_path)
    else:
        story_url = ""
        if story_path:
            print(f"[welcome-queue] story for {entry['name']} is not a genuine 9:16 "
                  f"({story_path}); NOT hosting a square/off-size as a story_url")
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
        # GUARD (layer b, host): only host the story if the LOCAL render is a genuine
        # 9:16 (1080x1920). A square/None/off-size story is skipped loudly and its
        # story_url stays empty, so a cropped feed card can never reach R2 as a story.
        story_path = posts.get("story")
        if story_path and _local_story_is_9_16(story_path):
            story_url = host(story_path)
        else:
            story_url = ""
            if story_path:
                print(f"[welcome-queue] story for {e['name']} is not a genuine 9:16 "
                      f"({story_path}); NOT hosting a square/off-size as a story_url")
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


def build_welcome_story_draft(account, day_key, feed_draft=None, verify_dims=None):
    """The 9:16 welcome STORY for lasso_ig, matched to the SAME gym the feed served
    today. Returns None unless this run's feed draft is a welcome draft (so a story
    never pops a gym the feed did not post) and a GENUINE 9:16 story asset exists. The
    publisher still requires AGENT_STORIES_ENABLED to actually send it.

    GUARD (layer c, publish backstop, mirrors commit 2c21a10): a welcome story draft
    is ONLY ever produced from a genuine 9:16 asset. story_url is populated upstream
    ONLY by the host guard (layer b), which refuses to host a square/off-size render,
    so an empty story_url means no genuine 9:16 asset was ever hosted; we return None
    rather than post a cropped feed card. When `verify_dims(url) -> (w, h)` is provided
    (a cheap dimension probe), a non-9:16 hosted asset is BLOCKED and one ops alert is
    fired naming the account/day, instead of going out."""
    if not config.welcome_queue_enabled():
        return None
    if account.key not in STORY_ACCOUNTS:
        return None
    # Couple the story to the feed: only fire when today's feed was a welcome.
    if not (feed_draft is not None
            and (feed_draft.draft_id or "").startswith("welcf_")):
        return None
    item = next_for_day(day_key)
    if item is None:
        return None
    story_url = item.get("story_url")
    # A missing/empty story_url means no genuine 9:16 asset was hosted (layer b never
    # hosts a square). Never build a story draft from a None/square/feed asset.
    if not story_url:
        return None
    # Optional cheap publish-time dimension verification: if a probe is supplied and it
    # reports a non-9:16 hosted asset, block + ops-alert rather than post a bad story.
    if verify_dims is not None:
        try:
            dims = verify_dims(story_url)
        except Exception:
            dims = None
        if dims is not None and not wt.is_story_size(dims):
            ops_alerts.alert(
                f"welcome story blocked for {account.key} on {day_key}: hosted story "
                f"asset for {item.get('name')} is {tuple(dims)}, not 9:16 "
                f"{wt.STORY_SIZE}. A story is never a cropped feed card."
            )
            return None
    platform = getattr(account, "platform", account.key)
    return Draft(
        draft_id=_draft_id(account.key, item["gym_key"], "story"),
        account_key=account.key,
        platform=platform,
        caption=item["caption"],
        hashtags=[],
        creative_path=f"welcome_{item['gym_key']}_story.png",
        creative_public_url=story_url,
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


# ---- the SECOND trigger: scan the portal gyms table and enqueue ready welcomes ---------

def _norm_name(name):
    """Normalized gym name for cross-source dedup: lowercased, whitespace-collapsed."""
    return " ".join((name or "").lower().split())


def _queue_has_name(name):
    """True when a welcome_queue row (queued OR already-served) carries this gym name
    (normalized). The queue row is never deleted once it exists, so this catches a gym
    that Stripe already queued/served under a domain/cust key, preventing a second
    welcome of the SAME gym via the portal source (dedup Stripe + portal)."""
    target = _norm_name(name)
    if not target:
        return False
    with _conn() as conn:
        rows = conn.execute("SELECT name FROM welcome_queue").fetchall()
    return any(_norm_name(r["name"]) == target for r in rows)


def scan_portal_and_enqueue(reader=None, scraper=None, host_fn=None, window_days=45,
                            bg_client=None, force=False, out_dir=None, cache_dir=None):
    """Scan the PORTAL `gyms` table for recently added clients and enqueue every READY
    welcome the SAME way the Stripe scan does (feed + a genuine 9:16 story, hosted).
    This is the fix for portal-added clients who have no Stripe record and so were
    never welcomed by the Stripe-only trigger.

    Idempotent and deduped:
      * already in the ledger (portal:<gym_id>) -> skipped.
      * same gym already in welcome_queue (by normalized name, from either source) ->
        skipped, so a client present in BOTH Stripe and portal is welcomed exactly once.
      * a gym with no usable logo (no override AND no scrapable domain) is reported
        needs_logo and NOT enqueued (never a logo-less card).

    Creds absent (no SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY) -> list is empty and this
    no-ops: the Stripe path is byte-for-byte unchanged. Requires AGENT_HOSTING_ENABLED to
    host the cards; `force=True` bypasses the flag gate for manual CLI seeding (the drip
    still stays dark until AGENT_WELCOME_QUEUE_ENABLED is armed)."""
    if not force and not config.welcome_queue_enabled():
        return {"scanned": False, "reason": "AGENT_WELCOME_QUEUE_ENABLED off"}
    if not config.hosting_enabled():
        return {"scanned": False, "reason": "AGENT_HOSTING_ENABLED off (cannot host cards)"}

    scraper = scraper or website_scan.fetch_logo
    out_dir = out_dir or os.path.join(cache_dir or wt._cache_dir(None), "welcome_client")

    try:
        gyms = portal_gyms.list_recent_portal_gyms(days=window_days, reader=reader)
    except Exception as e:
        return {"scanned": False, "reason": f"portal read failed: {type(e).__name__}: {e}"}

    enqueued = needs_logo = already = deduped = 0
    for g in gyms:
        try:
            gym_id = g.get("gym_id")
            gym_key = "portal:" + str(gym_id)
            # resolve the name: a portal name override wins, else the portal name
            name = welcome_posts.portal_name_override(gym_key) or g["name"]

            # idempotent: this gym already welcomed under its portal key
            if welcome_posts.already_welcomed(gym_key):
                already += 1
                continue
            # cross-source dedup: the SAME gym is already in the queue (Stripe or portal)
            if _queue_has_name(name):
                deduped += 1
                continue

            # resolve the logo: human-dropped override wins; else scrape a domain IF the
            # portal carries one (it does not today); else needs_logo, NOT enqueued.
            ak = "portal_" + re.sub(r"[^a-z0-9]+", "_", str(gym_id).lower()).strip("_")
            override = welcome_posts.portal_logo_override(ak, gym_key=gym_key)
            domain = (g.get("domain") or "").strip()
            logo = scraper(domain, ak, override_path=override, out_dir=None)
            if not logo.ok:
                needs_logo += 1
                print(f"[welcome-queue] portal gym {name} has no usable logo "
                      f"(override/scrape); NOT enqueued (drop a logo override)")
                continue

            template = welcome_posts.pick_template(gym_key)
            posts = welcome_posts.generate_posts(template, name, "", logo.path,
                                                 out_dir, bg_client=bg_client,
                                                 cache_dir=cache_dir)
            entry = {"gym_key": gym_key, "name": name, "owner": "",
                     "template": template, "tier_label": "", "posts": posts}
            if enqueue(entry, host_fn=host_fn):
                enqueued += 1
        except Exception as e:  # one bad gym never stops the scan
            print(f"[welcome-queue] portal enqueue failed for {g.get('name')}: "
                  f"{type(e).__name__}: {e}")
    return {
        "scanned": True,
        "source": "portal",
        "enqueued": enqueued,
        "portal_seen": len(gyms),
        "needs_logo": needs_logo,
        "already_welcomed": already,
        "deduped_with_stripe": deduped,
    }
