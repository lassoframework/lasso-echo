"""
Creative runway card (Stage 3), gated by AGENT_RUNWAY_ENABLED (default OFF).

Runway = unused approved gate-clean creatives divided by posts per day, per
account. Only assets that could actually ship count: moderation-clean and
consent-clear by construction (they are in the approved library), unposted
(never served and never logged as posted), in-style (not on the off-style
exclusion list, not a story variant), and fabrication-gate clean.

Armed, one line lands with the day's Slack card: the big number, a green /
amber / red read, and the projected zero date. Below AGENT_RUNWAY_ALERT_DAYS
(default 7) ONE debounced ops alert asks for specific raw material (the ask is
drafted from the approved source doc's own pillars, nothing invented).
"""

import os
from datetime import date, timedelta

from . import config, content_planner, db, ops_alerts, rotation
from .library import list_creatives


def _posts_per_day():
    posting_days = 7 - len(config.POSTING_SKIP_DAYS)
    return max(posting_days, 1) / 7.0


def _used_keys(account_key):
    used = set()
    for e in rotation.load_served().get(account_key, []):
        used.add(e.get("key", ""))
    try:
        with db.connect() as conn:
            for r in conn.execute(
                    "SELECT creative_key FROM posts WHERE account_key=?",
                    (account_key,)).fetchall():
                if r["creative_key"]:
                    used.add(r["creative_key"])
    except Exception:
        pass
    return used


def eligible_creatives(account_key, library_path):
    """The assets runway may count: in-style, gate-clean, unposted."""
    off_style = rotation.style_exclusions(library_path)
    used = _used_keys(account_key)
    approved_claims = rotation._approved_claims()
    out = []
    for c in list_creatives(library_path):
        base = os.path.basename(c.path)
        if base in off_style or base in used:
            continue
        if base.startswith("lasso_v2_") and os.path.splitext(base)[0].endswith("_story"):
            continue
        if not rotation.is_gate_clean(getattr(c, "client_note", ""), approved_claims):
            continue
        from . import dam
        if dam.consent_blocked(c.path):
            continue  # consent guard counts against runway too
        out.append(c)
    return out


def runway_days(account_key, library_path):
    return round(len(eligible_creatives(account_key, library_path)) / _posts_per_day(), 1)


def _color(days, threshold):
    if days < threshold:
        return "RED"
    if days < threshold * 2:
        return "AMBER"
    return "GREEN"


def status_line(account_key, library_path, day_key):
    """The one-line runway status for the day's card thread."""
    threshold = int(os.environ.get("AGENT_RUNWAY_ALERT_DAYS", "7"))
    days = runway_days(account_key, library_path)
    zero = (date.fromisoformat(day_key) + timedelta(days=int(days))).isoformat()
    return (f"RUNWAY {account_key}: {days} days of approved content left "
            f"({_color(days, threshold)}). Projected zero: {zero}."), days


def _ask_text(account_key):
    """The specific ask, drafted from the approved source doc's own pillars."""
    doc = content_planner.load_source_doc()
    pillars = doc.pillars_with_copy() if doc is not None else []
    focus = f" for {pillars[0]}" if pillars else ""
    return (f"Runway is low for {account_key}. Please send raw material{focus}: "
            "three recent member photos or short clips with permission, and one "
            "member win in the member's own words with permission on record.")


def daily_runway(account_key, library_path, day_key, poster=None):
    """
    The daily runway pass for one account: post the status line, and below the
    threshold send ONE debounced ops alert (at most one per 7 days per account).
    Returns the line, or None while AGENT_RUNWAY_ENABLED is OFF.
    """
    if not config.runway_enabled():
        return None
    line, days = status_line(account_key, library_path, day_key)
    if poster is not None:
        poster.post_notice(line)
    threshold = int(os.environ.get("AGENT_RUNWAY_ALERT_DAYS", "7"))
    if days < threshold:
        last = db.kv_get(f"runway_alert_{account_key}", "")
        cutoff = (date.fromisoformat(day_key) - timedelta(days=7)).isoformat()
        if not last or last <= cutoff:
            ops_alerts.alert(_ask_text(account_key))
            db.kv_set(f"runway_alert_{account_key}", day_key)
    return line
