"""
Evening digest (Tier 3 polish), gated by AGENT_DIGEST_ENABLED (default OFF).

One Slack line per day at AGENT_DIGEST_HOUR_UTC (default 23): drafted /
approved / published / blocked / runway days. A ten second read of the day.
Assembled entirely from the /data store; posts nothing else, changes nothing.
"""

import os
from datetime import datetime, timezone

from . import config, db


def build_digest(day_key, library_path=None):
    """The one line for the day, from the store."""
    with db.connect() as conn:
        drafted = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE day_key=?", (day_key,)).fetchone()[0]
        approved = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE day_key=? AND status='approved'",
            (day_key,)).fetchone()[0]
        blocked = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE day_key=? AND status='blocked'",
            (day_key,)).fetchone()[0]
        published = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE mode='published' "
            "AND substr(published_at, 1, 10)=?", (day_key,)).fetchone()[0]

    runway_bit = ""
    if config.runway_enabled() and library_path:
        try:
            from .runway import runway_days
            from .accounts import active_accounts
            days = min(runway_days(a.key, a.library_prefix or library_path)
                       for a in active_accounts())
            runway_bit = f", runway {days}d"
        except Exception:
            pass

    return (f"ECHO DAY {day_key}: drafted {drafted}, approved {approved}, "
            f"published {published}, blocked {blocked}{runway_bit}.")


def maybe_send(poster, now=None, library_path=None):
    """
    Fire the digest once per day at the digest hour. Returns the line when sent,
    None otherwise. Fully inert while AGENT_DIGEST_ENABLED is OFF. The sent mark
    persists in the store so a restart never double-sends.
    """
    if not config.digest_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    hour = int(os.environ.get("AGENT_DIGEST_HOUR_UTC", "23"))
    today = now.date().isoformat()
    if now.hour != hour:
        return None
    if db.kv_get("digest_sent_date") == today:
        return None
    line = build_digest(today, library_path=library_path)
    if poster is not None:
        poster.post_notice(line)
    db.kv_set("digest_sent_date", today)
    return line
