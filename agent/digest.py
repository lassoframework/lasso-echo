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


def build_account_digest(account_key, day_key, library_path=None):
    """One summary line for a single account on this day.

    Format: ECHO {account_key} {day_key}: drafted {N}, approved {N},
    published {N}, failed {N}[, runway {D}d]

    'failed' is drafts with status 'blocked' for this account on this day.
    The runway bit is included only when runway_enabled() is True and
    library_path is set; a missing/broken library silently omits the bit.
    """
    with db.connect() as conn:
        drafted = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE account_key=? AND day_key=?",
            (account_key, day_key)).fetchone()[0]
        approved = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE account_key=? AND day_key=?"
            " AND status='approved'",
            (account_key, day_key)).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE account_key=? AND day_key=?"
            " AND status='blocked'",
            (account_key, day_key)).fetchone()[0]
        published = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE account_key=? AND mode='published'"
            " AND substr(published_at, 1, 10)=?",
            (account_key, day_key)).fetchone()[0]

    runway_bit = ""
    if config.runway_enabled() and library_path:
        try:
            from .runway import runway_days
            days = runway_days(account_key, library_path)
            runway_bit = f", runway {days}d"
        except Exception:
            pass

    return (f"ECHO {account_key} {day_key}: drafted {drafted}, approved {approved},"
            f" published {published}, failed {failed}{runway_bit}")


def runway_nudge(account_key, library_prefix, poster, threshold=None):
    """
    Send a low-runway nudge for one account when the runway is below threshold
    and no nudge has been sent today for this account. Returns True when a nudge
    fires, False otherwise.

    Gated by AGENT_RUNWAY_ENABLED (default OFF): if runway is disabled the call
    is a no-op and returns False.
    """
    if not config.runway_enabled():
        return False
    if threshold is None:
        threshold = int(os.environ.get("AGENT_RUNWAY_ALERT_DAYS", "7"))
    today = datetime.now(timezone.utc).date().isoformat()
    nudge_key = "runway_nudge_" + account_key + "_" + today
    if db.kv_get(nudge_key) is not None and db.kv_get(nudge_key) != "":
        return False
    try:
        from .runway import runway_days
        days = runway_days(account_key, library_prefix)
    except Exception:
        return False
    if days >= threshold:
        return False
    poster.post_notice(
        "LASSO OPS: " + account_key + " runway is " + str(days)
        + " days. Please add fresh material.")
    db.kv_set(nudge_key, "sent")
    return True


def maybe_send(poster, now=None, library_path=None):
    """
    Fire the digest once per day at the digest hour. Returns the combined
    per-account lines when sent, None otherwise. Fully inert while
    AGENT_DIGEST_ENABLED is OFF. The sent mark persists in the store so a
    restart never double-sends.

    Sends one build_account_digest line per active account, joined by newlines,
    as a single post_notice call. After sending, calls runway_nudge for each
    active account (also gated by AGENT_RUNWAY_ENABLED, default OFF).
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

    from .accounts import active_accounts
    accounts = active_accounts()
    lines = []
    for account in accounts:
        lib = account.library_prefix or library_path
        lines.append(build_account_digest(account.key, today, library_path=lib))

    combined = "\n".join(lines)
    if poster is not None:
        poster.post_notice(combined)
    db.kv_set("digest_sent_date", today)

    for account in accounts:
        lib = account.library_prefix or library_path
        if lib:
            try:
                runway_nudge(account.key, lib, poster)
            except Exception:
                pass

    return combined
