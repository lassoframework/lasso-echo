"""
Sunday operator report (readiness Part D), flag AGENT_WEEKLY_REPORT_ENABLED
(default OFF = zero behavior anywhere: no build, no post, no kv stamp).

Armed, ONE Slack card lands in the approval channel Sundays at 6:00 PM ET:
the week in one read: posts published per account, approvals pending, the
engagement rollup from stored insights (VIEWS based; IG framed on engagement
only, reusing the Day 30 framing rules, so an IG frequency comparison can
NEVER appear here), runway days per account, the flags snapshot delta vs last
week, and the single most important by hand item inferred from probe states.

HONEST: missing data reads "no data", never a fabricated number. Dash free.
Debounced: one card per Sunday, stamped in kv, restart safe.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, runway
from .accounts import active_accounts
from .day30 import framing_for

_ET = ZoneInfo("America/New_York")
SEND_HOUR_ET = 18

# the flags whose weekly drift is worth a delta line
_WATCHED_FLAGS = (
    ("master", lambda: config.master_enabled()),
    ("publish", lambda: config.publish_enabled()),
    ("podcast", lambda: config.podcast_enabled()),
    ("nano", lambda: config.creative_studio_enabled()),
    ("hosting", lambda: config.hosting_enabled()),
    ("reporting", lambda: config.reporting_enabled()),
    ("knowledge", lambda: config.knowledge_enabled()),
    ("runway", lambda: config.runway_enabled()),
)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fmt(v):
    return "no data" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def _week_posts(account_key, since_iso):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM posts WHERE account_key=? AND mode='published' "
            "AND published_at >= ? ORDER BY published_at",
            (account_key, since_iso)).fetchall()]


def _engagement_rollup(posts):
    """(engagements, views, rate) from stored insights; None where absent."""
    eng, views, any_eng, any_views = 0, 0, False, False
    for p in posts:
        parts = [_num(p.get(k)) for k in ("likes", "comments", "saves", "shares")]
        parts = [v for v in parts if v is not None]
        if parts:
            eng += sum(parts)
            any_eng = True
        v = _num(p.get("views"))
        if v is not None:
            views += v
            any_views = True
    engagements = eng if any_eng else None
    total_views = views if any_views else None
    rate = (round(engagements / total_views, 4)
            if engagements is not None and total_views else None)
    return engagements, total_views, rate


def _flags_snapshot():
    return {name: bool(fn()) for name, fn in _WATCHED_FLAGS}


def _flags_delta(current):
    import json
    try:
        prior = json.loads(db.kv_get("weekly_flags_snapshot", "") or "{}")
    except Exception:
        prior = {}
    if not prior:
        return "first snapshot, no prior week to compare"
    changes = [f"{name} {'off to on' if current[name] else 'on to off'}"
               for name in current
               if name in prior and prior[name] != current[name]]
    return "; ".join(changes) if changes else "no change vs last week"


def _by_hand_item(runway_by_account, pending):
    """The single most important by hand item, by severity, from local probe
    state (no network from here; honest about what it can see)."""
    import os
    for acct in active_accounts():
        if not os.environ.get(acct.token_env):
            return (f"set the {acct.key} token by hand ({acct.token_env} is "
                    "empty; publishing and reads are blocked without it)")
    low = [(k, d) for k, d in runway_by_account.items() if d is not None and d < 7]
    if low:
        k, d = min(low, key=lambda x: x[1])
        return f"restock the {k} library (runway {d} day(s), under the 7 day bar)"
    if pending and pending > 5:
        return f"clear the approval queue ({pending} cards waiting)"
    if not config.gbp_enabled():
        return "GBP is still pending (AGENT_GBP_ENABLED off); arm it when ready"
    return "none inferred; the probes look clean"


def build_report(now=None):
    """The week in one card. Pure read; the kv snapshot writes in maybe_send."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat()
    lines = [f"ECHO WEEK ending {now.astimezone(_ET).strftime('%d %b %Y')}"]
    runway_by_account = {}
    for acct in active_accounts():
        posts = _week_posts(acct.key, since)
        engagements, views, rate = _engagement_rollup(posts)
        framing = framing_for(acct)
        bits = [f"{acct.key}: {len(posts)} post(s) published"]
        # the Day 30 framing law rides here too: an engagement framed account
        # (IG) reports engagement only; a frequency comparison NEVER appears
        bits.append(f"engagement rate on views {_fmt(rate)} "
                    f"({_fmt(engagements)} engagements, {_fmt(views)} views)")
        if framing == "frequency" and posts:
            bits.append(f"cadence: {len(posts)} post(s) in 7 days")
        lines.append("; ".join(bits))
        try:
            days = runway.runway_days(acct.key, acct.library_path())
        except Exception:
            days = None
        runway_by_account[acct.key] = days
        lines.append(f"runway {acct.key}: {_fmt(days)} day(s)")
    try:
        from .store import PendingStore
        pending = len(PendingStore().list_pending())
    except Exception:
        pending = None
    lines.append(f"approvals pending: {_fmt(pending)}")
    current = _flags_snapshot()
    lines.append(f"flags vs last week: {_flags_delta(current)}")
    lines.append(f"by hand this week: {_by_hand_item(runway_by_account, pending)}")
    text = "\n".join(lines)
    assert "—" not in text and "–" not in text, "weekly report carries a dash"
    return text, current


def maybe_send(poster, now=None):
    """
    Fire once per Sunday at SEND_HOUR_ET when armed. Returns the text when
    sent, None otherwise. Flag OFF = None immediately: no build, no read, no
    kv write, zero behavior change.
    """
    if not config.weekly_report_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(_ET)
    if now_et.weekday() != 6 or now_et.hour < SEND_HOUR_ET:
        return None
    week_key = now_et.date().isoformat()
    if db.kv_get("weekly_report_sent") == week_key:
        return None
    text, flags = build_report(now=now)
    if poster is not None:
        poster.post_notice(text)
    import json
    db.kv_set("weekly_flags_snapshot", json.dumps(flags))
    db.kv_set("weekly_report_sent", week_key)
    return text
