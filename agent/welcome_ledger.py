"""
Welcome-post ledger (Part D guards): dedupe by GYM (not by Stripe customer or
contact), and never welcome the same gym twice. Backed by the welcome_ledger
table in agent/db.py (echo.db, WAL, same store as everything else).

Also holds the "welcome bundle" for the one-card-both-go-out publish flow: the
primary (Slack card) draft id and the four real per-target drafts (IG feed, FB
feed, IG story, FB story) it fans out to on Approve. See
agent/welcome_new_clients.py for the fan-out itself.

Two contacts at one gym collapse to one gym_key (see gym_key()); the caller
(welcome_new_clients) is responsible for doing that collapse across a batch of
Stripe customers BEFORE calling already_posted, so the collapse itself is
reportable.
"""

import re

from . import db

BUNDLE_FIELDS = ("primary_draft_id", "ig_feed_draft_id", "fb_feed_draft_id",
                 "ig_story_draft_id", "fb_story_draft_id")


def gym_key(gym_name, account_key=""):
    """A stable dedupe key: the account_key (a portal tenant key) always wins
    when known, since it is the real per-gym identity; otherwise a slug of the
    gym name. Same gym, same key, regardless of which contact triggered it."""
    if account_key:
        return f"acct:{account_key}"
    slug = re.sub(r"[^a-z0-9]+", "-", str(gym_name or "").strip().lower()).strip("-")
    return f"name:{slug}" if slug else ""


def already_posted(key):
    if not key:
        return False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM welcome_ledger WHERE gym_key=?", (key,)).fetchone()
        return row is not None


def record_posted(key, gym_name, owner_name, account_key, confidence, source,
                  template_id, feed_url="", story_url="", logo_source="",
                  stripe_customer_id="", primary_draft_id="",
                  ig_feed_draft_id="", fb_feed_draft_id="",
                  ig_story_draft_id="", fb_story_draft_id=""):
    """Idempotent: INSERT OR REPLACE so a re-run of the same gym never double
    counts or double posts once the caller has checked already_posted first."""
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO welcome_ledger "
            "(gym_key, gym_name, owner_name, account_key, confidence, source, "
            "template_id, feed_url, story_url, logo_source, status, "
            "stripe_customer_id, primary_draft_id, ig_feed_draft_id, "
            "fb_feed_draft_id, ig_story_draft_id, fb_story_draft_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'posted_for_review', ?,?,?,?,?,?)",
            (key, gym_name, owner_name, account_key, confidence, source,
             template_id, feed_url, story_url, logo_source, stripe_customer_id,
             primary_draft_id, ig_feed_draft_id, fb_feed_draft_id,
             ig_story_draft_id, fb_story_draft_id))
        conn.commit()


def mark_status(key, status):
    """Record the outcome: 'posted_for_review' -> 'published' | 'skipped' |
    'blocked'. Purely informational except that 'published' additionally gates
    already_posted-style re-publish attempts via get_entry()."""
    with db.connect() as conn:
        conn.execute("UPDATE welcome_ledger SET status=? WHERE gym_key=?",
                     (status, key))
        conn.commit()


def set_primary_draft_id(key, draft_id):
    """Repoint the Slack-card draft id after an Edit re-draft (which mints a
    new draft_id). Keeps find_by_primary_draft_id() working post-edit."""
    with db.connect() as conn:
        conn.execute("UPDATE welcome_ledger SET primary_draft_id=? WHERE gym_key=?",
                     (draft_id, key))
        conn.commit()


def get_entry(key):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM welcome_ledger WHERE gym_key=?", (key,)).fetchone()
        return dict(row) if row else None


def find_by_primary_draft_id(draft_id):
    """The ledger row whose Slack approval card is `draft_id`, or None."""
    if not draft_id:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM welcome_ledger WHERE primary_draft_id=?",
            (draft_id,)).fetchone()
        return dict(row) if row else None


def all_entries():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM welcome_ledger ORDER BY posted_at DESC").fetchall()
        return [dict(r) for r in rows]
