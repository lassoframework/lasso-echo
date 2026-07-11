"""
seed-calendar: build the human-approved monthly calendar the trust ladder
compares against. RUN BY HAND:

    /opt/venv/bin/python -m agent seed-calendar --account <key>
        --month YYYY-MM [--write]

Sourced ONLY from approval evidence in the store: posts that actually went
through Blake's approve tap (the posts table) plus currently queued drafts in
APPROVED status. Pending, blocked, skipped, superseded drafts NEVER enter.
Days with no approved material stay EMPTY and are listed as gaps; a slot is
never invented. Prints the calendar; --write stores the creative-key list at
approved_calendar_<key>_<month> in kv (exactly what trust.auto_eligibility
reads).
"""

import json
import os
from calendar import monthrange

from . import db, schedule


def build_calendar(account_key, month):
    """
    {"by_day": {day: [keys]}, "keys": sorted unique keys, "gaps": [days]}.
    Approval evidence only; nothing invented.
    """
    by_day = {}
    with db.connect() as conn:
        # posts = drafts Blake approved (the tap is the only way into this table)
        for r in conn.execute(
                "SELECT published_at, creative_key FROM posts WHERE account_key=? "
                "AND substr(published_at, 1, 7)=? AND creative_key != ''",
                (account_key, month)).fetchall():
            by_day.setdefault(r["published_at"][:10], []).append(r["creative_key"])
        # plus currently queued drafts ALREADY in approved status, same month
        for r in conn.execute(
                "SELECT day_key, data FROM drafts WHERE account_key=? "
                "AND status='approved' AND substr(day_key, 1, 7)=?",
                (account_key, month)).fetchall():
            try:
                rec = json.loads(r["data"] or "{}")
            except Exception:
                continue
            key = os.path.basename(rec.get("creative_path") or "")
            if key:
                by_day.setdefault(r["day_key"], []).append(key)

    year, mon = int(month[:4]), int(month[5:7])
    gaps = []
    for day_num in range(1, monthrange(year, mon)[1] + 1):
        day = f"{month}-{day_num:02d}"
        if schedule.should_post_on(day) and day not in by_day:
            gaps.append(day)

    keys = sorted({k for ks in by_day.values() for k in ks})
    return {"by_day": by_day, "keys": keys, "gaps": gaps}


def run(account_key, month, write=False):
    cal = build_calendar(account_key, month)
    print(f"seed-calendar {account_key} {month}: {len(cal['keys'])} approved "
          f"creative(s) across {len(cal['by_day'])} day(s)")
    if not cal["keys"]:
        print(f"seed-calendar: no approval evidence for {account_key} in {month} "
              "— the calendar is built only from approved cards, and none exist "
              "for this month yet.")
    for day in sorted(cal["by_day"]):
        for key in cal["by_day"][day]:
            print(f"  {day}  {key}")
    if cal["gaps"]:
        print(f"gaps (posting days with NO approved material, left empty, "
              f"never invented): {len(cal['gaps'])}")
        for day in cal["gaps"]:
            print(f"  {day}")
    if write:
        db.kv_set(f"approved_calendar_{account_key}_{month}",
                  json.dumps(cal["keys"]))
        print(f"written: approved_calendar_{account_key}_{month} "
              f"({len(cal['keys'])} key(s))")
    else:
        print("dry print only; pass --write to store it for the trust ladder")
    return cal
