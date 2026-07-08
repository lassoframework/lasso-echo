"""
Weekly / monthly category plan and quotas (category rotation Parts 4-6).

This is the CATEGORY axis of the plan: which bucket each posting day belongs to,
which platform sub-topic a platform day carries, and how the week's mix honors
the standing quotas. It is separate from plan_month.py (which chooses WHICH
creative fills a day); this module decides the bucket, that module fills it.

Everything here rides AGENT_CATEGORY_ROTATION (the callers gate on it); the pure
planning functions are deterministic and side-effect free so a month can be
previewed and its balance checked before a single draft is built.

Weekly quotas (maxima unless noted):
  podcast   3   (Mon, Thu, Sun: the three touches, fixed)
  b2b       1   (Wed, fixed)
  platform  2   (the default Tue/Sat slots)
  summit    per the ramp (summit_quota_for_week; near zero in July, heaviest the
                two weeks before Nov 7)
  book      1   (max; present every other week)
  doctrine  fills every remaining gap (uncapped)

Book and doctrine cycle into the flex slots on ALTERNATING ISO weeks: book on
even weeks, doctrine on odd weeks, so book stays present but never exceeds one
per week.

Platform sub-topics rotate SEQUENTIALLY across platform posts (not by calendar
day), so with two platform posts a week no sub-topic repeats until ten platform
posts have run (roughly five weeks) -- comfortably past the no-repeat-in-10-days
rule.
"""

from datetime import date, timedelta

from .content_categories import PLATFORM_SUBTOPICS
from .schedule import weekday_abbr

# Maxima. summit is governed by the ramp; doctrine is uncapped (it fills gaps).
WEEKLY_CAPS = {"podcast": 3, "b2b": 1, "platform": 2, "book": 1}

# Summit auto-stops after this date (mirrors summit.SUMMIT_END_DATE).
_SUMMIT_DAY = date(2026, 11, 7)
_SUMMIT_END = date(2026, 11, 8)


def summit_quota_for_week(week_monday):
    """
    How many summit posts the week of `week_monday` should carry, per the ramp:
    near zero in July, rising through September and October, heaviest (2) the two
    weeks before Nov 7, then zero once the summit is past. Clamped to 0..2.

    (Part 6 owns the ramp; it is defined here because the Part 4 quotas depend on
    it. The three-date ramp lock lives in test_summit_ramp.py.)
    """
    d = date.fromisoformat(_monday_of(week_monday))
    if d > _SUMMIT_END:
        return 0
    days_until = (_SUMMIT_DAY - d).days
    if days_until < 0:
        return 0
    if days_until <= 14:      # the two weeks before Nov 7: heaviest
        return 2
    if days_until <= 63:      # roughly early September through late October: rising
        return 1
    return 0                   # July / August and earlier: near zero


def _monday_of(day_key):
    """The ISO Monday of the week containing day_key (accepts any weekday)."""
    d = date.fromisoformat(str(day_key)[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def _week_counts(week_monday):
    """
    The bucket counts for the three flex slots (Tue/Fri/Sat) this week. The four
    fixed slots (3 podcast + 1 b2b) are not included. Always sums to 3 and never
    breaches a cap: platform<=2, book<=1, summit<=2.
    """
    wk = date.fromisoformat(_monday_of(week_monday)).isocalendar().week
    summit = max(0, min(2, summit_quota_for_week(week_monday)))
    even = wk % 2 == 0
    book = 1 if even else 0
    doctrine_alt = 0 if even else 1          # the alternating book/doctrine slot
    remaining = 3 - summit - book - doctrine_alt   # always 0..2
    platform = max(0, min(2, remaining))
    doctrine = doctrine_alt + (remaining - platform)
    return {"summit": summit, "platform": platform, "book": book, "doctrine": doctrine}


def week_plan(week_monday, platform_seq=0):
    """
    The week's category plan as (entries, next_platform_seq).

    entries: 7 dicts in day order, each
      {"day": YYYY-MM-DD, "weekday": abbr, "category": one of CATEGORIES,
       "sub_topic": platform sub-topic or ""}.
    platform_seq: the running count of platform posts BEFORE this week, so
    sub-topics keep advancing across week boundaries (never repeating within 10).

    Fixed: Mon/Thu/Sun podcast, Wed b2b. Flex: Tue/Fri/Sat per _week_counts,
    placed deterministically (summit prefers Fri, platform prefers Tue/Sat).
    """
    mon = date.fromisoformat(_monday_of(week_monday))
    days = [(mon + timedelta(days=i)).isoformat() for i in range(7)]
    tue, fri, sat = days[1], days[4], days[5]

    plan = {days[0]: "podcast", days[2]: "b2b", days[3]: "podcast",
            days[6]: "podcast"}

    counts = _week_counts(week_monday)
    prefs = {
        "summit":   [fri, sat, tue],
        "platform": [tue, sat, fri],
        "book":     [sat, fri, tue],
        "doctrine": [sat, fri, tue],
    }
    open_days = {tue, fri, sat}
    for cat in ("summit", "platform", "book", "doctrine"):
        for _ in range(counts[cat]):
            for d in prefs[cat]:
                if d in open_days:
                    plan[d] = cat
                    open_days.discard(d)
                    break

    entries = []
    for d in days:
        cat = plan[d]
        sub_topic = ""
        if cat == "platform":
            sub_topic = PLATFORM_SUBTOPICS[platform_seq % len(PLATFORM_SUBTOPICS)]
            platform_seq += 1
        entries.append({"day": d, "weekday": weekday_abbr(d),
                        "category": cat, "sub_topic": sub_topic})
    return entries, platform_seq


def _mondays_touching_month(month):
    """Every ISO Monday whose week overlaps the given YYYY-MM month, in order."""
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    # last day of month
    nxt = date(year + (mon == 12), (mon % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    monday = first - timedelta(days=first.weekday())
    out = []
    while monday <= last:
        out.append(monday.isoformat())
        monday += timedelta(days=7)
    return out


def month_plan(month, platform_seq=0):
    """
    The category plan for a whole month (YYYY-MM). Returns:
      {"entries": [ ... only days inside the month ... ],
       "weeks":   [ {"monday":..., "counts": {cat: n}}, ... ],
       "summary": {category: count},           # the category mix
       "subtopic_spread": {sub_topic: count}}  # platform sub-topic balance
    Deterministic and side-effect free: a month can be previewed and its balance
    checked before any draft is built. platform_seq threads across weeks so the
    sub-topic rotation never repeats within ten platform posts.
    """
    entries = []
    weeks = []
    for monday in _mondays_touching_month(month):
        wk_entries, platform_seq = week_plan(monday, platform_seq)
        counts = {}
        for e in wk_entries:
            counts[e["category"]] = counts.get(e["category"], 0) + 1
        weeks.append({"monday": monday, "counts": counts})
        entries.extend(e for e in wk_entries if e["day"][:7] == month)

    summary = {}
    subtopic_spread = {}
    for e in entries:
        summary[e["category"]] = summary.get(e["category"], 0) + 1
        if e["sub_topic"]:
            subtopic_spread[e["sub_topic"]] = subtopic_spread.get(e["sub_topic"], 0) + 1
    return {"entries": entries, "weeks": weeks,
            "summary": summary, "subtopic_spread": subtopic_spread}


def format_summary(month_result):
    """A one-block, human-readable category mix + sub-topic spread for the month
    plan output (so balance is visible before approval). Pure string builder."""
    lines = ["Category mix:"]
    for cat in ("podcast", "platform", "b2b", "summit", "book", "doctrine"):
        n = month_result["summary"].get(cat, 0)
        lines.append(f"  {cat:9s} {n}")
    spread = month_result["subtopic_spread"]
    if spread:
        lines.append("Platform subtopics:")
        for st in PLATFORM_SUBTOPICS:
            if st in spread:
                lines.append(f"  {st:15s} {spread[st]}")
    return "\n".join(lines)
