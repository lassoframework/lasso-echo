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

LOCKED BASELINE (added): lock_pre_echo_baseline() writes one row per account into
the pre_echo_baselines DB table. That row can never be silently overwritten; re-runs
refuse unless --force is passed. baseline_report() reads the locked row and prints the
number, date range, and confidence note.

Confidence levels:
  clean                   first Echo post date found in posts table; pre-Echo window
                          is at least 4 weeks long; Graph read succeeded.
  partially contaminated  Echo's first published post is recent (window < 4 weeks),
                          OR the cutoff could not be found from confirmed posts and
                          falls back to an estimated date. Use the number with caution.
  no reliable pre-Echo data found
                          No post history available (no Graph read, no posts table
                          data, or no confirmed published posts and no fallback window).
                          The number is not reported; report honestly.

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
_CLEAN_MIN_PRE_ECHO_WEEKS = 4   # fewer weeks -> "partially contaminated"


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


# ---------------------------------------------------------------------------
# Locked pre-Echo baseline (write-once per account, read via baseline-report)
# ---------------------------------------------------------------------------

def _find_first_echo_post(account_key, db_conn=None):
    """Return (cutoff_dt, is_confirmed) for the first Echo-published post.

    is_confirmed=True  : the earliest confirmed mode='published' post was found.
    is_confirmed=False : no confirmed post; falls back to the earliest
                         mode='would_publish' timestamp as a hint, or None.
    """
    from . import db as _db
    conn, owned = (db_conn, False) if db_conn is not None else (_db.connect(), True)
    try:
        row = conn.execute(
            "SELECT MIN(published_at) AS ts FROM posts "
            "WHERE account_key=? AND mode='published'",
            (account_key,)).fetchone()
        ts = row["ts"] if row else None
        if ts:
            dt = _parse_graph_time(ts)
            if dt:
                return dt, True
        # fallback: would_publish (draft-only mode, not a confirmed Echo post)
        row2 = conn.execute(
            "SELECT MIN(published_at) AS ts FROM posts "
            "WHERE account_key=? AND mode='would_publish'",
            (account_key,)).fetchone()
        ts2 = row2["ts"] if row2 else None
        if ts2:
            dt2 = _parse_graph_time(ts2)
            if dt2:
                return dt2, False
        return None, False
    finally:
        if owned:
            conn.close()


def lock_pre_echo_baseline(account_key, http=None, db_conn=None,
                           force=False, now=None):
    """
    Compute and lock the pre-Echo posting-frequency baseline for one account.

    Finds the first Echo post date from the posts table, fetches pre-Echo
    history via the Graph API, and writes a single locked row to
    pre_echo_baselines. Refuses to overwrite an existing row unless force=True.

    Returns a dict with the locked row fields, or raises on Graph API failure.
    When no token is available, returns a dict with confidence="no reliable
    pre-Echo data found" and does NOT write to the DB (nothing to lock).
    """
    from . import db as _db
    from .accounts import active_accounts as _accounts

    now_dt = now or datetime.now(timezone.utc)
    client = http or _requests()

    # Find the account config
    account = next((a for a in _accounts() if a.key == account_key), None)
    if account is None:
        return {
            "account_key": account_key,
            "confidence": "no reliable pre-Echo data found",
            "confidence_note": f"account {account_key!r} not found in active accounts",
        }

    conn, owned = (db_conn, False) if db_conn is not None else (_db.connect(), True)
    try:
        # Refuse overwrite unless force
        existing = conn.execute(
            "SELECT * FROM pre_echo_baselines WHERE account_key=?",
            (account_key,)).fetchone()
        if existing and not force:
            row_dict = dict(existing)
            row_dict["_already_locked"] = True
            return row_dict

        # Determine the cutoff (first Echo post)
        cutoff_dt, confirmed_published = _find_first_echo_post(account_key, conn)

        token = account.get_token()
        if not token:
            rec = {
                "account_key": account_key,
                "locked_at": now_dt.isoformat(),
                "pre_echo_cutoff": cutoff_dt.isoformat() if cutoff_dt else None,
                "window_start": None,
                "window_end": None,
                "posts_count": None,
                "weeks_in_window": None,
                "avg_posts_per_week": None,
                "confidence": "no reliable pre-Echo data found",
                "confidence_note": "no API token available; Graph read skipped",
            }
            # Don't write to DB — nothing to lock
            return rec

        # Compute the window end (= cutoff, or now if no cutoff known)
        window_end = cutoff_dt if cutoff_dt else now_dt
        window_start = window_end - timedelta(weeks=WINDOW_WEEKS)

        # Fetch pre-Echo post times from the Graph API
        try:
            times = _fetch_post_times(client, account, token, window_start)
        except Exception as e:
            rec = {
                "account_key": account_key,
                "locked_at": now_dt.isoformat(),
                "pre_echo_cutoff": cutoff_dt.isoformat() if cutoff_dt else None,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "posts_count": None,
                "weeks_in_window": None,
                "avg_posts_per_week": None,
                "confidence": "no reliable pre-Echo data found",
                "confidence_note": f"Graph read failed: {type(e).__name__}: {e}",
            }
            return rec

        # Only count posts BEFORE the cutoff (if a cutoff is known)
        if cutoff_dt:
            times = [t for t in times if t < cutoff_dt]

        posts_count = len(times)
        actual_weeks = (window_end - window_start).days / 7
        avg = round(posts_count / actual_weeks, 2) if actual_weeks > 0 else 0.0

        # Confidence scoring
        if not cutoff_dt:
            confidence = "partially contaminated"
            confidence_note = (
                "no confirmed Echo post found in the posts table; the window "
                "ends at the current time, which may include recent Echo posts"
            )
        elif not confirmed_published:
            confidence = "partially contaminated"
            confidence_note = (
                "Echo cutoff derived from would_publish (draft-only) posts, "
                "not confirmed published posts; the pre-Echo window may overlap "
                "with early Echo activity"
            )
        elif actual_weeks < _CLEAN_MIN_PRE_ECHO_WEEKS:
            confidence = "partially contaminated"
            confidence_note = (
                f"pre-Echo window is {actual_weeks:.1f} weeks "
                f"(minimum for clean is {_CLEAN_MIN_PRE_ECHO_WEEKS} weeks); "
                "the number is available but the window is short"
            )
        else:
            confidence = "clean"
            confidence_note = (
                f"first confirmed Echo post on {cutoff_dt.date().isoformat()}; "
                f"pre-Echo window is {actual_weeks:.1f} weeks"
            )

        rec = {
            "account_key": account_key,
            "locked_at": now_dt.isoformat(),
            "pre_echo_cutoff": cutoff_dt.isoformat() if cutoff_dt else None,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "posts_count": posts_count,
            "weeks_in_window": round(actual_weeks, 2),
            "avg_posts_per_week": avg,
            "confidence": confidence,
            "confidence_note": confidence_note,
        }

        conn.execute(
            "INSERT OR REPLACE INTO pre_echo_baselines "
            "(account_key, locked_at, pre_echo_cutoff, window_start, window_end, "
            "posts_count, weeks_in_window, avg_posts_per_week, confidence, confidence_note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (account_key, rec["locked_at"], rec["pre_echo_cutoff"],
             rec["window_start"], rec["window_end"], rec["posts_count"],
             rec["weeks_in_window"], rec["avg_posts_per_week"],
             rec["confidence"], rec["confidence_note"]))
        conn.commit()
        return rec

    finally:
        if owned:
            conn.close()


def read_pre_echo_baseline(account_key, db_conn=None):
    """Return the locked baseline dict for account_key, or None if not locked."""
    from . import db as _db
    conn, owned = (db_conn, False) if db_conn is not None else (_db.connect(), True)
    try:
        row = conn.execute(
            "SELECT * FROM pre_echo_baselines WHERE account_key=?",
            (account_key,)).fetchone()
        return dict(row) if row else None
    finally:
        if owned:
            conn.close()


def baseline_report(account_key=None, db_conn=None):
    """
    Print the locked pre-Echo baseline for one account or all accounts that have
    a locked record. Returns a list of result dicts (one per account printed).

    No em dashes, no hyphens in output copy.
    """
    from . import db as _db
    conn, owned = (db_conn, False) if db_conn is not None else (_db.connect(), True)
    try:
        if account_key:
            rows = conn.execute(
                "SELECT * FROM pre_echo_baselines WHERE account_key=?",
                (account_key,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM pre_echo_baselines").fetchall()
    finally:
        if owned:
            conn.close()

    if not rows:
        target = account_key or "any account"
        print(f"No locked pre-Echo baseline found for {target}.")
        print("Run: python -m agent capture-baseline  (reads live Graph API data)")
        return []

    results = []
    for row in rows:
        rec = dict(row)
        key = rec["account_key"]
        avg = rec.get("avg_posts_per_week")
        count = rec.get("posts_count")
        weeks = rec.get("weeks_in_window")
        confidence = rec.get("confidence", "unknown")
        note = rec.get("confidence_note", "")
        start = rec.get("window_start", "")
        end = rec.get("window_end", "")
        cutoff = rec.get("pre_echo_cutoff")
        locked_at = rec.get("locked_at", "")

        print(f"=== {key} ===")
        if avg is None:
            print(f"  Pre-Echo avg posts per week : NOT AVAILABLE")
        else:
            print(f"  Pre-Echo avg posts per week : {avg}")
        if count is not None:
            print(f"  Total posts in window       : {count}")
        if weeks is not None:
            print(f"  Window length               : {weeks} weeks")
        if start and end:
            s = start[:10] if start else "unknown"
            e = end[:10] if end else "unknown"
            print(f"  Window                      : {s} to {e}")
        if cutoff:
            print(f"  First Echo post (cutoff)    : {cutoff[:10]}")
        else:
            print(f"  First Echo post (cutoff)    : not found in posts table")
        print(f"  Confidence                  : {confidence.upper()}")
        print(f"  Note                        : {note}")
        print(f"  Locked at                   : {locked_at[:10]}")
        print()
        results.append(rec)

    return results
