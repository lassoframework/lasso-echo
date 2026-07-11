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
from .reporting_live import fetch_post_metrics, media_kind_for_post

MAX_TRIES = 5


class _RateLimited(Exception):
    pass


def _fetch_with_backoff(row, token, http, platform, sleeper=time.sleep):
    """ONE metric builder, both callers: this delegates to the same media-type
    aware fetch the daily snapshot uses; 429 backs off 1, 2, 4, 8 and retries."""
    media_id = row["media_id"]
    for attempt in range(MAX_TRIES):
        try:
            return fetch_post_metrics(
                media_id, token, http=http, platform=platform,
                kind=media_kind_for_post(dict(row)),
                published_at=row.get("published_at") or "")
        except RuntimeError as e:
            if "HTTP 429" in str(e):
                if attempt == MAX_TRIES - 1:
                    raise RuntimeError(
                        f"rate limited on {media_id} after {MAX_TRIES} tries")
                sleeper(2 ** attempt)
                continue
            raise
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
            "SELECT id, media_id, published_at, draft_id, creative_key "
            "FROM posts WHERE account_key=? "
            "AND mode='published' AND media_id != '' AND published_at >= ? "
            "ORDER BY published_at", (account_key, since)).fetchall()]
    summary = {"posts": len(rows), "updated": 0, "skipped": 0}
    print(f"backfill-insights: {len(rows)} published post(s) for {account_key} "
          f"since {since}")
    if not rows:
        print("backfill-insights: nothing to backfill in this window (no "
              f"published posts for {account_key} since {since}). Widen --since "
              "or check the account has published.")
        return summary

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
            pm = _fetch_with_backoff(row, token, http, acct.platform,
                                     sleeper=sleeper)
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
