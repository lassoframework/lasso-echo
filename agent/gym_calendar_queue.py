"""
gym_calendar_queue.py — the per-gym generalization of the demo calendar engine
(Part A of the portal client-social backend).

The demo calendar (demo_calendar_queue.py) is ONE gym: LASSO's own brand, keyed by
its two feed accounts. This module keeps that exact shape and its served-once-per-day
lock, but keys every row by a GYM (gym_id + zernio_profile_id + account_key) so client
gyms are simply additional gyms alongside the LASSO demo. The LASSO demo remains one
gym; client gyms are additional gyms.

WHAT THIS MODULE IS (Part A scope):
  * the ENGINE: a per-gym dated queue, mirroring demo_calendar_queue's table shape.
  * per-gym KEYING: (gym_id, zernio_profile_id) with account_key on the gym row.
  * the served-once-per-day LOCK: at most one served post per (account_key, day_key),
    order-independent and idempotent, same as the demo queue's served_day lock.
  * RULING 1 collision-shift: the live book queue (book_queue.build_book_queue_draft)
    WINS any contested served_day for a LASSO account. When this engine would serve on
    a day the book queue already occupies (or any queue already served that account
    that day), Echo SHIFTS its post to the NEXT open day in the same pillar rotation.
    NEVER two posts on one served_day per account.

WHAT THIS MODULE IS NOT (out of scope for Part A):
  * client CONTENT generation. Nothing here writes a client caption or invents a fact.
    A gym row carries its identity and its dated slots; the content brain (a later
    phase) fills captions. Part A ships the keying + the lock + the shift.
  * a new publish path. Every draft this engine ever emits is PENDING and carries
    force_approval=True (client posts always card, never auto-publish). Client drafts
    route to the PORTAL approval surface (see approval_surface); LASSO drafts to Slack.

HARD RULES (unchanged from the whole build):
  * Behind AGENT_PORTAL_SOCIAL_ENABLED, default OFF. OFF -> every runner hook returns
    None: byte-for-byte current behavior. Isolated from the book / welcome / demo /
    summit queues (its own flag, its own table).
  * Nothing here decides to publish. Served drafts are PENDING; they card for approval
    on their surface.
  * No em/en/hyphen dashes and never the word "vendor" in any client-facing string.
"""

import hashlib

from . import config, db, schedule
from .drafter import Draft, DraftStatus

# The five LASSO pillars, in rotation order. A gym's calendar rotates these so a
# collision-shift can land on the NEXT open day carrying the SAME pillar's turn in
# the rotation. Client gyms reuse the rotation shape; their content is a later phase.
PILLAR_ROTATION = [
    "All in one offer",
    "Sales are now",
    "We do the heavy lifting",
    "The portal",
    "Proof",
]

# How many days ahead the collision-shift will look for an open slot before it gives
# up (and skips, loud, rather than double-post). A generous ceiling; a real calendar
# never contests more than a handful of days in a row.
_MAX_SHIFT_DAYS = 60


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gym_calendar_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gym_id TEXT NOT NULL,
  zernio_profile_id TEXT DEFAULT '',
  account_key TEXT NOT NULL,
  num INTEGER,
  day_key TEXT,
  pillar TEXT DEFAULT '',
  filename TEXT DEFAULT '',
  caption TEXT DEFAULT '',
  feed_url TEXT DEFAULT '',
  story_url TEXT DEFAULT '',
  is_story INTEGER DEFAULT 0,
  status TEXT DEFAULT 'queued',
  served_day TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(gym_id, account_key, day_key));

CREATE TABLE IF NOT EXISTS served_ledger (
  account_key TEXT NOT NULL,
  day_key TEXT NOT NULL,
  source TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY(account_key, day_key));
"""


def _conn():
    conn = db.connect()
    conn.executescript(_SCHEMA)
    return conn


# ---- draft ids (namespaced "gcal_", never collides with book_ / demo_ / welc_) ---------

def _draft_id(gym_id, account_key, day_key, kind):
    h = hashlib.sha1(
        f"gcal|{gym_id}|{account_key}|{day_key}|{kind}".encode()).hexdigest()[:12]
    return f"gcal{kind[0]}_{h}"


# ---- approval surface routing ----------------------------------------------------------

def approval_surface_for(account):
    """"slack" for a LASSO account (key starts "lasso"), "portal" for a client gym.

    One draft lifecycle, two surfaces: a LASSO draft posts a Slack approval card as
    always; a client-gym draft is approved through the portal, so its Slack approval
    card is SKIPPED. ops_alerts (failures) still go to Slack for every gym. Flag off
    OR no account -> "slack", so nothing about today's behavior changes while off."""
    if not config.portal_social_enabled():
        return "slack"
    key = getattr(account, "key", "") or ""
    return "slack" if key.startswith("lasso") else "portal"


# ---- served ledger: at most one served post per (account_key, day_key) -----------------

def account_served_on(account_key, day_key, conn=None):
    """True if some queue already served this account on day_key (ledger hit)."""
    def _check(c):
        row = c.execute(
            "SELECT 1 FROM served_ledger WHERE account_key=? AND day_key=?",
            (account_key, day_key)).fetchone()
        return row is not None
    if conn is not None:
        return _check(conn)
    with _conn() as c:
        return _check(c)


def mark_account_served(account_key, day_key, source=""):
    """Idempotent per (account_key, day_key): the FIRST queue to serve an account on
    a day claims the slot; later attempts that day are no-ops (INSERT OR IGNORE). This
    is the one-post-per-day-per-account lock the collision-shift reads."""
    with db._lock, _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO served_ledger (account_key, day_key, source) "
            "VALUES (?,?,?)", (account_key, day_key, source))
        conn.commit()


def _book_queue_occupies(account_key, day_key):
    """True if the live dated book queue owns this account+day. The book queue WINS
    (Ruling 1): its dates are read from book_queue.BOOK_POSTS so a book-launch date is
    always contested and this engine yields it, regardless of ledger seeding order."""
    from . import book_queue
    if account_key not in book_queue.ACCOUNTS:
        return False
    return any(p["date"] == day_key for p in book_queue.BOOK_POSTS)


def _day_contested(account_key, day_key, conn=None):
    """A day is contested for an account when the book queue owns it OR the served
    ledger already records a post for that account that day. Either means: do not add
    a second post; shift to the next open day."""
    if _book_queue_occupies(account_key, day_key):
        return True
    return account_served_on(account_key, day_key, conn=conn)


def _next_open_day(account_key, day_key, conn=None):
    """The next day at/after day_key that is NOT contested for this account, keeping
    the pillar rotation (each forward day is the next pillar's turn). Returns None if
    no open day is found within _MAX_SHIFT_DAYS (skip loud rather than double-post)."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in day_key.split("-"))
    cur = date(y, m, d)
    for _ in range(_MAX_SHIFT_DAYS + 1):
        k = cur.isoformat()
        if not _day_contested(account_key, k, conn=conn):
            return k
        cur = cur + timedelta(days=1)
    return None


# ---- gym rows + seeding ----------------------------------------------------------------

def upsert_gym_post(gym_id, account_key, day_key, num, pillar="", filename="",
                    caption="", feed_url="", story_url="", is_story=False,
                    zernio_profile_id=""):
    """Seed/replace one dated slot for a gym. Idempotent by (gym_id, account_key,
    day_key). Content generation is out of scope; caption/urls may be empty in Part A
    (the ENGINE + keying is what ships). Returns True if a row was written."""
    with db._lock, _conn() as conn:
        conn.execute(
            "INSERT INTO gym_calendar_queue "
            "(gym_id, zernio_profile_id, account_key, num, day_key, pillar, filename, "
            " caption, feed_url, story_url, is_story, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'queued') "
            "ON CONFLICT(gym_id, account_key, day_key) DO UPDATE SET "
            " zernio_profile_id=excluded.zernio_profile_id, num=excluded.num, "
            " pillar=excluded.pillar, filename=excluded.filename, "
            " caption=excluded.caption, feed_url=excluded.feed_url, "
            " story_url=excluded.story_url, is_story=excluded.is_story",
            (gym_id, zernio_profile_id, account_key, num, day_key, pillar, filename,
             caption, feed_url, story_url, 1 if is_story else 0))
        conn.commit()
    return True


def _row_for(gym_id, account_key, day_key, conn=None):
    def _get(c):
        row = c.execute(
            "SELECT * FROM gym_calendar_queue WHERE gym_id=? AND account_key=? "
            "AND day_key=?", (gym_id, account_key, day_key)).fetchone()
        return dict(row) if row is not None else None
    if conn is not None:
        return _get(conn)
    with _conn() as c:
        return _get(c)


def _mark_row_served(gym_id, account_key, day_key, served_day):
    """Flip a gym's queued row to served, stamping the day it actually served on
    (which may differ from day_key after a shift). Idempotent: only a still-queued
    row is touched."""
    with db._lock, _conn() as conn:
        row = conn.execute(
            "SELECT status FROM gym_calendar_queue WHERE gym_id=? AND account_key=? "
            "AND day_key=?", (gym_id, account_key, day_key)).fetchone()
        if row is not None and row["status"] == "queued":
            conn.execute(
                "UPDATE gym_calendar_queue SET status='served', served_day=? "
                "WHERE gym_id=? AND account_key=? AND day_key=?",
                (served_day, gym_id, account_key, day_key))
            conn.commit()


def queue_status(gym_id=None):
    with _conn() as conn:
        if gym_id is None:
            rows = conn.execute(
                "SELECT gym_id, account_key, num, day_key, pillar, status, served_day "
                "FROM gym_calendar_queue ORDER BY gym_id, account_key, day_key").fetchall()
        else:
            rows = conn.execute(
                "SELECT gym_id, account_key, num, day_key, pillar, status, served_day "
                "FROM gym_calendar_queue WHERE gym_id=? ORDER BY account_key, day_key",
                (gym_id,)).fetchall()
    return [dict(r) for r in rows]


# ---- runner hook -----------------------------------------------------------------------

def build_gym_calendar_draft(gym_id, account, day_key):
    """The gym's dated calendar post for day_key as a PENDING draft, or None (flag off,
    no seeded row, or the day is contested and no open day exists ahead).

    RULING 1 (collision-shift): the live book queue WINS. If day_key is contested for
    this account (the book queue owns it, or the served ledger already records a post
    that day), Echo SHIFTS to the NEXT open day in the pillar rotation and serves there
    instead. NEVER two posts on one served_day per account. The served ledger is then
    claimed for the day it actually served on, so a second queue that same day yields.

    Content generation is out of scope: the draft carries whatever caption/feed_url the
    row already holds (may be empty in Part A). force_approval is always True."""
    if not config.portal_social_enabled():
        return None
    row = _row_for(gym_id, account.key, day_key)
    if row is None:
        return None

    # Resolve the served day: honor the book-queue win and the one-per-day lock.
    serve_day = day_key
    if _day_contested(account.key, day_key):
        serve_day = _next_open_day(account.key, day_key)
        if serve_day is None:
            print(f"[gym-calendar] {account.key} gym {gym_id}: no open day within "
                  f"{_MAX_SHIFT_DAYS}d of {day_key}; skipped to avoid a double post")
            return None

    # Claim the slot (idempotent). The FIRST queue to serve this account today wins;
    # if another queue beat us to serve_day between the check and here, yield.
    mark_account_served(account.key, serve_day, source="gym_calendar")
    if serve_day != day_key and _book_queue_occupies(account.key, serve_day):
        # extraordinarily unlikely (we only shift to an OPEN day) but never risk a
        # double post: if the resolved day turns out book-owned, yield.
        return None

    _mark_row_served(gym_id, account.key, day_key, serve_day)

    platform = getattr(account, "platform", account.key)
    return Draft(
        draft_id=_draft_id(gym_id, account.key, serve_day, "feed"),
        account_key=account.key,
        platform=platform,
        caption=row.get("caption") or "",
        hashtags=[],
        creative_path=row.get("filename") or "",
        creative_public_url=row.get("feed_url") or "",
        scheduled_for=schedule.scheduled_for(serve_day),
        status=DraftStatus.PENDING,
        day_key=serve_day,
        draft_type="feed",
        # Client posts ALWAYS card (never auto-publish), same card-only gate the demo
        # calendar uses. This only STRENGTHENS the gate; it never bypasses one.
        force_approval=True,
    )
