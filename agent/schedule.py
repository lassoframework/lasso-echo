"""
Posting schedule (2026 cadence). PURE timing logic: it decides WHICH days a post
runs and WHAT time it is scheduled for. It never publishes and never touches the
approval gate — it only computes a scheduled_for timestamp and skip/priority calls.

All settings are read from config at call time, so an env/config override retunes the
behavior without a code change.
"""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import config

# date.weekday(): Monday=0 .. Sunday=6
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_day(day_key):
    """A 'YYYY-MM-DD' key (or an ISO datetime beginning with one) -> a date."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(day_key))
    if not m:
        raise ValueError(f"day_key must start YYYY-MM-DD, got {day_key!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def weekday_abbr(day_key):
    """Lowercase 3-letter weekday for a day_key: one of mon..sun."""
    return _WEEKDAYS[_parse_day(day_key).weekday()]


def should_post_on(day_key):
    """False on a configured skip day (default Saturday); True otherwise.
    When AGENT_CATEGORY_ROTATION is ON, Saturday is always a posting day
    (it is the platform video slot in the seven-day schedule)."""
    if config.category_rotation_enabled() and weekday_abbr(day_key) == "sat":
        return True
    skip = [d.lower() for d in config.POSTING_SKIP_DAYS]
    return weekday_abbr(day_key) not in skip


def is_priority_day(day_key):
    """True on a configured priority day (default Tue/Wed/Thu)."""
    prio = [d.lower() for d in config.POSTING_PRIORITY_DAYS]
    return weekday_abbr(day_key) in prio


def slot_time(slot):
    """'HH:MM' for the slot: 'morning' (default 07:30) else 'primary' (default 18:30)."""
    if slot == "morning":
        return config.POSTING_MORNING_TIME
    return config.POSTING_PRIMARY_TIME


def scheduled_for(day_key, slot="primary"):
    """
    ISO datetime for the post in POSTING_TIMEZONE, e.g. 2026-07-01T18:30:00-04:00.
    The zoneinfo tz carries the correct DST offset (EDT -04:00 / EST -05:00).
    """
    d = _parse_day(day_key)
    hh, mm = slot_time(slot).split(":")
    tz = ZoneInfo(config.POSTING_TIMEZONE)
    return datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=tz).isoformat()
