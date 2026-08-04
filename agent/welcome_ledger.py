"""
Welcome-post ledger (Part D guards): dedupe by GYM (not by Stripe customer or
contact), and never welcome the same gym twice. Backed by the welcome_ledger
table in agent/db.py (echo.db, WAL, same store as everything else).

Two contacts at one gym collapse to one gym_key (see gym_key()); the caller
(welcome_new_clients) is responsible for doing that collapse across a batch of
Stripe customers BEFORE calling already_posted, so the collapse itself is
reportable.
"""

import re

from . import db


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
                  template_id, feed_url="", story_url="", logo_source=""):
    """Idempotent: INSERT OR REPLACE so a re-run of the same gym never double
    counts or double posts once the caller has checked already_posted first."""
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO welcome_ledger "
            "(gym_key, gym_name, owner_name, account_key, confidence, source, "
            "template_id, feed_url, story_url, logo_source, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'posted_for_review')",
            (key, gym_name, owner_name, account_key, confidence, source,
             template_id, feed_url, story_url, logo_source))
        conn.commit()


def mark_status(key, status):
    """Record Blake's tap: 'approved' | 'edited' | 'skipped'. Informational
    only -- nothing in this system publishes to Meta on any status."""
    with db.connect() as conn:
        conn.execute("UPDATE welcome_ledger SET status=? WHERE gym_key=?",
                     (status, key))
        conn.commit()


def all_entries():
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM welcome_ledger ORDER BY posted_at DESC").fetchall()
        return [dict(r) for r in rows]
