"""
Month planner: fill open posting days from the eligible creative pool.

    python -m agent plan-month --account <key> --month YYYY-MM [--write]
    python -m agent approve-month --account <key> --month YYYY-MM [--through YYYY-MM-DD]

Gated by AGENT_PLAN_MONTH_ENABLED (default OFF). Draft-only: plan-month writes
pending drafts; every draft still cards for the tap before publish.

Rules honored:
  - 14-day rotation window: no concept repeated within config.ROTATION_WINDOW_DAYS
    across the served log AND within the current planning pass.
  - Canvas guard: no same canvas on two consecutive posting days when alternatives
    exist. Reads the last DB stamp for the first open day; tracks forward within
    the pass.
  - Scheduling: schedule.should_post_on gates every day.
  - No double-booking: days with an existing approved/pending/published entry skip.
  - First post per account never auto-swept: approve-month holds the first draft
    when the account has no published posts.
"""

import json
import os
from calendar import monthrange
from datetime import date as _date
from datetime import timedelta

from . import config, db, rotation, schedule
from . import runway

_SET_MAP = {"brand": "house", "service": "house",
            "b2b": "b2b", "platform": "platform", "platform_ads": "platform_ads"}


def _creative_set(c):
    """Set label: house / b2b / platform / platform_ads for v2 concepts, library for old."""
    base = os.path.basename(c.path)
    if base.startswith("lasso_v2_"):
        name = os.path.splitext(base)[0][len("lasso_v2_"):]
        from .regen_library import CONCEPTS
        return _SET_MAP.get(CONCEPTS.get(name, {}).get("set", ""), "other")
    return "library"


def _creative_canvas(c):
    """Canvas token for one creative: CONCEPTS lookup for v2, sidecar for old format."""
    base = os.path.basename(c.path)
    if base.startswith("lasso_v2_"):
        name = os.path.splitext(base)[0][len("lasso_v2_"):]
        from .regen_library import canvas_for
        return canvas_for(name)
    return rotation.sidecar_canvas(c.path)


def plan_month(account_key, month, library_path=None, write=False, from_day=None):
    """
    Fill open posting days for one account from the eligible creative pool.
    Returns {"planned": [(day_key, creative_key)], "skipped": [day_key], "wrote": int}
    or None when the flag is OFF. from_day (YYYY-MM-DD, optional) plans only
    days >= it, so a mid-month replan never touches the days before it.
    """
    if not config.plan_month_enabled():
        return None

    library_path = library_path or config.LIBRARY_PATH
    year, mon = int(month[:4]), int(month[5:7])
    n_days = monthrange(year, mon)[1]

    # Days that already have a post or a planned/approved draft
    existing = set()
    with db.connect() as conn:
        for r in conn.execute(
                "SELECT published_at FROM posts WHERE account_key=? AND "
                "substr(published_at, 1, 7)=?", (account_key, month)).fetchall():
            existing.add(r["published_at"][:10])
        for r in conn.execute(
                "SELECT day_key FROM drafts WHERE account_key=? AND "
                "substr(day_key, 1, 7)=? AND status IN ('approved', 'pending')",
                (account_key, month)).fetchall():
            existing.add(r["day_key"])

    open_days = [
        f"{month}-{n:02d}"
        for n in range(1, n_days + 1)
        if schedule.should_post_on(f"{month}-{n:02d}")
        and f"{month}-{n:02d}" not in existing
        and (not from_day or f"{month}-{n:02d}" >= from_day)
    ]

    if not open_days:
        return {"planned": [], "skipped": [], "wrote": 0}

    eligibles = runway.eligible_creatives(account_key, library_path)
    if not eligibles:
        return {"planned": [], "skipped": list(open_days), "wrote": 0}

    served_log = rotation.load_served().get(account_key, [])
    window = config.ROTATION_WINDOW_DAYS

    # planned entries: (day_key, creative_key, creative_path, canvas) or (day_key, None,…)
    planned_entries: list = []

    def _in_window(creative_key, as_of_day):
        cutoff = (_date.fromisoformat(as_of_day) - timedelta(days=window)).isoformat()
        for e in served_log:
            if e.get("key") == creative_key and e.get("date", "") > cutoff:
                return True
        for p_day, p_key, _pp, _pc in planned_entries:
            if p_key == creative_key:
                delta = (_date.fromisoformat(as_of_day) - _date.fromisoformat(p_day)).days
                if delta < window:
                    return True
        return False

    def _last_served_date(creative_key):
        dates = [e.get("date", "") for e in served_log if e.get("key") == creative_key]
        planned_dates = [d for d, k, _p, _c in planned_entries if k == creative_key]
        all_dates = dates + planned_dates
        return max(all_dates) if all_dates else ""

    prev_canvas = rotation.last_canvas(account_key, open_days[0])
    # Tracks how many times each set label has been chosen this pass — used to
    # round-robin across house/b2b/platform/platform_ads when recency is tied.
    plan_set_counts: dict = {}

    for day_key in open_days:
        candidates = [
            c for c in eligibles
            if not _in_window(os.path.basename(c.path), day_key)
        ]
        if not candidates:
            planned_entries.append((day_key, None, None, None))
            continue

        # Canvas guard: prefer candidates with a different canvas where possible
        if prev_canvas:
            alt = [c for c in candidates if _creative_canvas(c) != prev_canvas]
            if alt:
                candidates = alt

        # Primary: least recently served (never-served "" sorts before any real date)
        candidates.sort(key=lambda c: _last_served_date(os.path.basename(c.path)))

        # Tiebreaker within equal-recency tier: prefer the set least represented so
        # far in this pass. Prevents stable-sort insertion order from exhausting
        # house+b2b before platform/platform_ads are ever reached on cold-start runs.
        top_date = _last_served_date(os.path.basename(candidates[0].path))
        top_tier = [c for c in candidates
                    if _last_served_date(os.path.basename(c.path)) == top_date]
        if len(top_tier) > 1:
            min_count = min(plan_set_counts.get(_creative_set(c), 0) for c in top_tier)
            top_tier = [c for c in top_tier
                        if plan_set_counts.get(_creative_set(c), 0) == min_count]

        chosen = top_tier[0]
        plan_set_counts[_creative_set(chosen)] = plan_set_counts.get(
            _creative_set(chosen), 0) + 1
        c_key = os.path.basename(chosen.path)
        c_canvas = _creative_canvas(chosen)

        planned_entries.append((day_key, c_key, chosen.path, c_canvas))
        prev_canvas = c_canvas

    assignments = [(d, k, p, cv) for d, k, p, cv in planned_entries if k is not None]
    skipped = [d for d, k, _p, _cv in planned_entries if k is None]

    wrote = 0
    if write:
        with db.connect() as conn:
            for day_key, creative_key, creative_path, canvas in assignments:
                draft_id = f"plan_{account_key}_{day_key}"
                conn.execute(
                    "INSERT OR IGNORE INTO drafts (draft_id, account_key, status, "
                    "day_key, draft_type, data) VALUES (?,?,?,?,?,?)",
                    (draft_id, account_key, "pending", day_key, "plan",
                     json.dumps({"creative_path": creative_path or creative_key,
                                 "creative_key": creative_key, "canvas": canvas})))
            conn.commit()
            wrote = len(assignments)

    return {
        "planned": [(d, k) for d, k, _p, _cv in planned_entries if k is not None],
        "skipped": skipped,
        "wrote": wrote,
    }


def approve_month(account_key, month, through=None):
    """
    Bulk-approve pending plan drafts for one month (or through a given day).
    First post guard: if the account has no published posts, the earliest
    pending plan draft is held for the tap.
    Returns {"approved": [day_key], "held": [day_key]}.
    """
    query = (
        "SELECT draft_id, day_key FROM drafts WHERE account_key=? AND "
        "substr(day_key, 1, 7)=? AND status='pending' AND draft_type='plan'"
    )
    params: list = [account_key, month]
    if through:
        query += " AND day_key <= ?"
        params.append(through)
    query += " ORDER BY day_key"

    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()
        published = conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE account_key=? AND mode='published'",
            (account_key,)).fetchone()["n"]

    if not rows:
        return {"approved": [], "held": []}

    hold_first = (published == 0)
    approved = []
    held = []

    with db.connect() as conn:
        for i, row in enumerate(rows):
            if hold_first and i == 0:
                held.append(row["day_key"])
                continue
            conn.execute(
                "UPDATE drafts SET status='approved' WHERE draft_id=?",
                (row["draft_id"],))
            approved.append(row["day_key"])
        conn.commit()

    return {"approved": approved, "held": held}


def plan_cli(args):
    account, month, write, from_day = None, None, False, None
    i = 0
    while i < len(args):
        if args[i] == "--account" and i + 1 < len(args):
            account = args[i + 1]; i += 2; continue
        if args[i] == "--month" and i + 1 < len(args):
            month = args[i + 1]; i += 2; continue
        if args[i] == "--from" and i + 1 < len(args):
            from_day = args[i + 1]; i += 2; continue
        if args[i] == "--write":
            write = True; i += 1; continue
        print(f"unrecognized: {args[i]}\n"
              "usage: python -m agent plan-month --account <key> "
              "--month YYYY-MM [--from YYYY-MM-DD] [--write]")
        return
    if not account or not month:
        print("usage: python -m agent plan-month --account <key> --month YYYY-MM "
              "[--from YYYY-MM-DD] [--write]")
        return
    out = plan_month(account, month, write=write, from_day=from_day)
    if out is None:
        print("plan-month: OFF (set AGENT_PLAN_MONTH_ENABLED=true). Nothing done.")
        return
    print(f"plan-month: {len(out['planned'])} day(s) planned, "
          f"{len(out['skipped'])} skipped, {out['wrote']} written")
    for day_key, creative_key in out["planned"]:
        print(f"  {day_key}  {creative_key}")
    for day_key in out["skipped"]:
        print(f"  {day_key}  (no eligible candidate)")
    # Category rotation ON: show the category mix + platform sub-topic spread for
    # the NEWLY PLANNED days so the summary always matches the day list shown above.
    # (month_plan covers all 31 days; this filters to only the days we just planned
    # so the count in the summary equals the count in the day list — no discrepancy.)
    if config.category_rotation_enabled():
        from . import category_plan
        full = category_plan.month_plan(month)
        planned_set = {d for d, _ in out["planned"]}
        if planned_set:
            _entries = [e for e in full["entries"] if e["day"] in planned_set]
            _summary = {}
            _spread = {}
            for e in _entries:
                _summary[e["category"]] = _summary.get(e["category"], 0) + 1
                if e["sub_topic"]:
                    _spread[e["sub_topic"]] = _spread.get(e["sub_topic"], 0) + 1
            filtered = {**full, "summary": _summary, "subtopic_spread": _spread}
        else:
            filtered = full
        print()
        print(category_plan.format_summary(filtered))


def approve_cli(args):
    account, month, through = None, None, None
    i = 0
    while i < len(args):
        if args[i] == "--account" and i + 1 < len(args):
            account = args[i + 1]; i += 2; continue
        if args[i] == "--month" and i + 1 < len(args):
            month = args[i + 1]; i += 2; continue
        if args[i] == "--through" and i + 1 < len(args):
            through = args[i + 1]; i += 2; continue
        print(f"unrecognized: {args[i]}\n"
              "usage: python -m agent approve-month --account <key> "
              "--month YYYY-MM [--through YYYY-MM-DD]")
        return
    if not account or not month:
        print("usage: python -m agent approve-month --account <key> --month YYYY-MM "
              "[--through YYYY-MM-DD]")
        return
    out = approve_month(account, month, through=through)
    print(f"approve-month: {len(out['approved'])} approved, {len(out['held'])} held "
          f"(first post guard)")
    for day_key in out["approved"]:
        print(f"  approved {day_key}")
    for day_key in out["held"]:
        print(f"  held {day_key} (first post for this account, tap required)")
