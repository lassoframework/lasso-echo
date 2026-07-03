"""
Insights backfill for the Day 30 report. RUN BY HAND:

    /opt/venv/bin/python -m agent backfill-insights --account <key>
        --since YYYY-MM-DD [--dry]

For every post the account PUBLISHED since the date (publish records from the
store ONLY, post ids never guessed), pull per-post metrics from the Meta Graph
API (VIEWS, never impressions; the same metric set as the daily reporting
path) and UPSERT them onto the same posts rows daily reporting fills.
Idempotent: a re-run updates in place, never duplicates. Rate-limit aware:
HTTP 429 backs off exponentially and completes. --dry lists the work and
touches neither Graph nor the store.
"""

import time
from datetime import datetime, timezone

from . import config, db
from .accounts import get_account
from .reporting_live import IG_POST_METRICS

MAX_TRIES = 5


def _fetch_with_backoff(media_id, token, http, sleeper=time.sleep):
    """One per-post insights read; 429 backs off 1, 2, 4, 8 seconds and retries."""
    for attempt in range(MAX_TRIES):
        r = http.get(f"{config.GRAPH_API_BASE}/{media_id}/insights",
                     params={"metric": IG_POST_METRICS, "access_token": token},
                     timeout=30)
        status = getattr(r, "status_code", 200)
        if status == 429:
            if attempt == MAX_TRIES - 1:
                raise RuntimeError(f"rate limited on {media_id} after {MAX_TRIES} tries")
            sleeper(2 ** attempt)
            continue
        if status >= 400:
            from .reporting_live import graph_error_detail
            raise RuntimeError(f"reading {media_id}: {graph_error_detail(r)}")
        out = {}
        for item in (r.json() or {}).get("data", []) or []:
            name = item.get("name")
            value = (item.get("values") or [{}])[-1].get("value")
            if name:
                out[name] = value
        return out
    return {}


def backfill_insights(account_key, since, dry=False, http=None, sleeper=time.sleep):
    """The backfill pass. Returns {"posts": n, "updated": n, "skipped": n}."""
    acct = get_account(account_key)
    if acct is None:
        print(f"backfill-insights: unknown account {account_key!r}")
        return None
    try:
        datetime.strptime(since, "%Y-%m-%d")
    except ValueError:
        print(f"backfill-insights: --since must be YYYY-MM-DD, got {since!r}")
        return None

    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, media_id, published_at FROM posts WHERE account_key=? "
            "AND mode='published' AND media_id != '' AND published_at >= ? "
            "ORDER BY published_at", (account_key, since)).fetchall()]
    summary = {"posts": len(rows), "updated": 0, "skipped": 0}
    print(f"backfill-insights: {len(rows)} published post(s) for {account_key} "
          f"since {since}")

    if dry:
        for r in rows:
            print(f"  would backfill {r['media_id']} ({r['published_at'][:10]})")
        print("backfill-insights: DRY. No Graph call made, nothing written.")
        return summary

    token = acct.get_token()
    if not token:
        print("backfill-insights: no token available for this account.")
        return None
    if http is None:
        import requests  # lazy
        http = requests

    for row in rows:
        try:
            pm = _fetch_with_backoff(row["media_id"], token, http, sleeper=sleeper)
        except Exception as e:
            summary["skipped"] += 1
            from . import ops_alerts
            reason = ops_alerts.scrub(str(e))
            print(f"  skipped {row['media_id']}: {reason}")
            db.audit("insights_skip", row["media_id"], reason, account_key)
            continue
        with db.connect() as conn:
            # UPSERT IN PLACE on the existing publish row: idempotent by design,
            # a re-run rewrites the same columns and never adds a row.
            conn.execute(
                "UPDATE posts SET likes=?, comments=?, saves=?, shares=?, "
                "views=?, reach=? WHERE id=?",
                (pm.get("likes"), pm.get("comments"), pm.get("saves"),
                 pm.get("shares"), pm.get("views"), pm.get("reach"), row["id"]))
            conn.commit()
        summary["updated"] += 1
        print(f"  backfilled {row['media_id']}")
    return summary
