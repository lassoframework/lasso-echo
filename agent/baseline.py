"""
Pre-Echo baseline capture: the "before" number for the before/after proof metric.

MANUAL, READ-ONLY, RUN-ONCE-BY-HAND: `python -m agent capture-baseline`. This is
deliberately NOT flag-armed and NOT scheduled anywhere; nothing in the runner,
listener, or scheduler ever calls it. Blake runs it once, by hand, before Echo
starts publishing, so the posting-frequency baseline is captured while it still
IS the baseline.

What it does: for each ACTIVE account it reads recent posting history from the
Graph API (a READ: /media for IG, /posts for a Page), counts posts per week over
the trailing 8 weeks, writes a dated JSON to /data (baseline_YYYY-MM.json), and
prints a short human summary.

NO SECRETS: tokens are read at call time for the GET only; they never appear in
the JSON, the summary, or any log line. An account with no token is recorded as
a gap, never guessed.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

from . import config
from .accounts import Platform, active_accounts

WINDOW_WEEKS = 8


def _requests():
    import requests
    return requests


def _baseline_dir():
    return os.environ.get("AGENT_BASELINE_DIR", "/data")


def _parse_graph_time(value):
    """Meta returns ISO stamps like 2026-06-30T12:00:00+0000; normalize and parse.
    Returns an aware datetime, or None for anything unparseable (never guessed)."""
    if not value:
        return None
    s = str(value).strip()
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)  # +0000 -> +00:00
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_post_times(client, account, token, since_dt):
    """All post timestamps for one account since `since_dt`, following paging.
    IG exposes /media (field timestamp); a Page exposes /posts (field created_time)."""
    target = account.get_target_id()
    if not target:
        raise RuntimeError("no target id set")
    if account.platform == Platform.INSTAGRAM:
        edge, field = "media", "timestamp"
    else:
        edge, field = "posts", "created_time"

    url = f"{config.GRAPH_API_BASE}/{target}/{edge}"
    params = {"fields": field, "limit": 100,
              "since": int(since_dt.timestamp()), "access_token": token}
    times = []
    while url:
        r = client.get(url, params=params, timeout=30)
        if getattr(r, "status_code", 200) >= 400:
            raise RuntimeError(f"Graph read returned HTTP {r.status_code}")
        body = r.json() or {}
        for item in body.get("data", []):
            dt = _parse_graph_time(item.get(field))
            if dt is not None:
                times.append(dt)
        url = (body.get("paging") or {}).get("next")
        params = None  # a paging.next url already carries its own query string
    return times


def _weekly_counts(times, now):
    """posts per week over the trailing WINDOW_WEEKS; index 0 = the most recent
    week (the 7 days ending now), index 7 = the oldest week in the window."""
    counts = [0] * WINDOW_WEEKS
    for t in times:
        age_days = (now - t).days
        if age_days < 0:
            continue
        week = age_days // 7
        if week < WINDOW_WEEKS:
            counts[week] += 1
    return counts


def capture_baseline(http=None, accounts=None, now=None, out_dir=None):
    """
    Capture the pre-Echo posting-frequency baseline. Returns (path, summary dict).
    Writes <out_dir>/baseline_YYYY-MM.json and prints a short human summary.
    """
    client = http or _requests()
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(weeks=WINDOW_WEEKS)
    out_dir = out_dir or _baseline_dir()

    summary = {
        "captured_at": now.isoformat(),
        "window_weeks": WINDOW_WEEKS,
        "window_start": since.isoformat(),
        "accounts": {},
    }

    for account in (accounts if accounts is not None else active_accounts()):
        token = account.get_token()
        if not token:
            summary["accounts"][account.key] = {
                "platform": account.platform, "error": "no token set"}
            continue
        try:
            times = _fetch_post_times(client, account, token, since)
        except Exception as e:
            summary["accounts"][account.key] = {
                "platform": account.platform,
                "error": f"read failed: {type(e).__name__}: {e}"}
            continue
        weekly = _weekly_counts(times, now)
        total = sum(weekly)
        summary["accounts"][account.key] = {
            "platform": account.platform,
            "posts_total": total,
            "posts_per_week": weekly,   # index 0 = most recent week
            "avg_posts_per_week": round(total / WINDOW_WEEKS, 2),
        }

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"baseline_{now.strftime('%Y-%m')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"\nPre Echo posting baseline (trailing {WINDOW_WEEKS} weeks)")
    if not summary["accounts"]:
        print("  capture-baseline: 0 accounts produced a baseline (none "
              "active, or none resolved). The file below is an empty shell.")
    for key, rec in summary["accounts"].items():
        if "error" in rec:
            print(f"  {key}: SKIPPED ({rec['error']})")
        else:
            print(f"  {key}: {rec['posts_total']} post(s), "
                  f"avg {rec['avg_posts_per_week']} per week")
    print(f"Written to {path}")
    return path, summary
