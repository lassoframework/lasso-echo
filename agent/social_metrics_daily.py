"""
social_metrics_daily.py — the DAILY follower series pull into gym_social_metrics_daily.

Fixes AUD-007 / D1: that table has 0 rows because nothing has ever written to it, so every
"is my Instagram growing" question has no series to answer from.

WHERE THE NUMBERS COME FROM. Zernio's GET /v1/accounts/follower-stats returns, in ONE
request for the whole org, `stats` = {accountId: [{date, followers}]}: a per account DAILY
series. That is exactly the granularity gym_social_metrics_daily is shaped for. Verified
live 2026-09-05: 46 accounts, 31 of them carrying a series, ENG instagram 27 days from
2026-08-10.

WHICH GYM A NUMBER BELONGS TO. echo_social_connections is the source of truth for who is
connected on what (AUD-005), and after migration social_metrics_daily_provenance_20260905
it carries the Zernio account id too. So the join is: connection row -> late_account_id ->
follower series. Nothing here reads gym_social_accounts, the legacy table that disagrees.

NULL MEANS NULL, in three places, all of them code and none of them a comment:
  - zernio.follower_series drops a point with a missing or non numeric follower count
    rather than emitting 0.
  - NO_FOLLOWER_PLATFORMS accounts (googlebusiness, openaiads) report currentFollowers 0
    with 0 data points. That 0 is ABSENCE, not a measurement, and is never written.
  - the store writer removes a None metric from the payload rather than sending it.

Read-only against Zernio. The only write is gym_social_metrics_daily. OFF by default
(AGENT_SOCIAL_METRICS_DAILY); nothing here arms itself.

    railway run /opt/venv/bin/python -m agent social-metrics-daily --dry-run
    railway run /opt/venv/bin/python -m agent social-metrics-daily
"""

from datetime import date, datetime, timezone

from . import config
from . import zernio as _z

#: The lane that produced a row. Mirrors METRICS_DATA_CONTRACT.md s3.
SOURCE = "zernio"

#: The endpoint recorded in every row's provenance block.
ENDPOINT = "/v1/accounts/follower-stats"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _cutoff(days, today=None):
    """The earliest metric_date a run will write, or None for no limit."""
    if not days or int(days) <= 0:
        return None
    today = today or date.today()
    return date.fromordinal(today.toordinal() - int(days) + 1)


def build_rows(connections, stats_json, backfill_days=90, pulled_at=None, today=None):
    """PURE. [(row dicts)] ready for upsert_social_metric_days, from the connection rows
    and one follower-stats payload.

    `connections` is echo_social_connections rows: [{gym_id, platform, late_account_id}].
    A connection with no late_account_id is SKIPPED, not guessed at by handle: a metric
    written against the wrong account is worse than a missing one.

    A googlebusiness or openaiads connection is skipped entirely. Zernio reports 0
    followers and 0 data points for those, and writing that 0 would be a fabricated
    measurement rather than the absence it actually is.
    """
    pulled_at = pulled_at or _now_iso()
    cut = _cutoff(backfill_days, today=today)
    rows = []
    seen = set()
    for conn in connections or []:
        if not isinstance(conn, dict):
            continue
        gym_id = str(conn.get("gym_id") or "").strip()
        platform = str(conn.get("platform") or "").strip().lower()
        account_id = str(conn.get("late_account_id") or "").strip()
        if not gym_id or not account_id:
            continue
        if platform in _z.NO_FOLLOWER_PLATFORMS:
            continue
        key = (gym_id, account_id)
        if key in seen:
            continue
        seen.add(key)
        for day, followers in _z.follower_series(stats_json, account_id):
            if cut is not None and day < cut.isoformat():
                continue
            rows.append({
                "gym_id": gym_id,
                "late_account_id": account_id,
                "metric_date": day,
                "platform": platform or None,
                "source": SOURCE,
                "followers": followers,
                # reach / impressions / engagement / profile_views are deliberately
                # ABSENT, not 0: this endpoint does not report them, and the writer
                # drops a None rather than sending one.
                "pulled_at": pulled_at,
                "raw": {"_provenance": {
                    "source": SOURCE,
                    "endpoint": ENDPOINT,
                    "account_id": account_id,
                    "fetched_at": pulled_at,
                }},
            })
    return rows


def run(client=None, store=None, logger=None, dry_run=False, backfill_days=None):
    """Pull the fleet's daily follower series and upsert it. Never raises out.

    Returns {ok, reason?, accounts, rows, written, dry_run}.
    """
    log = logger or (lambda m: print(f"[social-metrics-daily] {m}"))
    if not dry_run and not config.social_metrics_daily_enabled():
        return {"ok": False, "reason": "AGENT_SOCIAL_METRICS_DAILY is off", "written": 0}
    if not config.zernio_enabled():
        return {"ok": False, "reason": "zernio disabled (no key on this host)", "written": 0}
    if store is None:
        from . import zernio_routes as _zr
        store = _zr._shared_store()
    if store is None:
        return {"ok": False, "reason": "supabase creds absent on this host", "written": 0}

    c = client if client is not None else _z.ZernioClient()
    try:
        stats = c.follower_stats()
    except Exception as exc:  # noqa: BLE001 - a read failure must not poison the table
        log(f"follower_stats failed ({type(exc).__name__}); wrote nothing")
        return {"ok": False, "reason": f"follower_stats failed: {type(exc).__name__}",
                "written": 0}

    try:
        conns = store.social_connection_rows(state="connected")
    except Exception as exc:  # noqa: BLE001
        log(f"connection read failed ({type(exc).__name__}); wrote nothing")
        return {"ok": False, "reason": f"connection read failed: {type(exc).__name__}",
                "written": 0}

    days = backfill_days if backfill_days is not None else config.social_metrics_backfill_days()
    rows = build_rows(conns, stats, backfill_days=days)
    if dry_run:
        log(f"DRY RUN: {len(rows)} row(s) from {len(conns)} connection(s); wrote nothing")
        return {"ok": True, "accounts": len(conns), "rows": len(rows),
                "written": 0, "dry_run": True}
    try:
        written = store.upsert_social_metric_days(rows)
    except Exception as exc:  # noqa: BLE001
        log(f"upsert failed ({type(exc).__name__})")
        return {"ok": False, "reason": f"upsert failed: {type(exc).__name__}",
                "rows": len(rows), "written": 0}
    log(f"wrote {written} daily row(s) across {len(conns)} connection(s)")
    return {"ok": True, "accounts": len(conns), "rows": len(rows),
            "written": written, "dry_run": False}


def health_for(store_rows, window_days=28):
    """PURE. {(gym_id, platform): (read, basis)} where read is
    'growing' | 'flat' | 'declining' | None, per METRICS_DATA_CONTRACT.md s6.

    `store_rows` is gym_social_metrics_daily rows. A row whose `followers` is None is
    NOT counted as a measured point, so a gym with rows but no follower numbers reads
    None (unknown) rather than 'flat'.
    """
    series = {}
    for r in store_rows or []:
        f = r.get("followers")
        if f is None or isinstance(f, bool) or not isinstance(f, (int, float)):
            continue
        d = str(r.get("metric_date") or "")[:10]
        if len(d) != 10:
            continue
        series.setdefault((str(r.get("gym_id")), r.get("platform")), []).append((d, int(f)))
    return {k: _z.health_read(v, window_days=window_days) for k, v in series.items()}
