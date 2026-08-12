"""
New-client CATCH-UP report: one Slack message a day until every recently signed-up
gym is caught up, per Blake's directive (2026-08-12: "echo stopped autoposting the
new clients... send one to my slack everyday till everyone is caught up that has
signed up in last 60 days").

Behind AGENT_CATCHUP_REPORT (default OFF). Read-only over Supabase: recent gyms come
from the portal gyms table (created_at within the window, active, not demo/load-test),
their coverage from content_calendar. HONEST numbers only: a gym with no calendar rows
reports zeros, never invented coverage.

Caught up (per gym) = at least CATCHUP_MIN_UPCOMING rows dated today or later.
The daily message lists every behind gym with exactly what it is missing; when ALL
are caught up it sends one final confirmation and goes quiet (kv marker) until a gym
falls behind again.
"""

from datetime import datetime, timedelta, timezone

from . import config, ops_alerts

CATCHUP_WINDOW_DAYS = 60
CATCHUP_MIN_UPCOMING = 7      # a week of runway = caught up


class CatchupReadError(Exception):
    """A Supabase read for the catch-up report failed — the truth is UNKNOWN. Never
    rendered as a zero (don't-guess rule); surfaced honestly in the report line."""


def _recent_gyms_default(now):
    """Portal gyms created inside the window: [{slug, name, created_at}]. Read-only
    Supabase REST via the calendar store's creds. Raises CatchupReadError on an HTTP
    error so a failed read is NEVER rendered as 'window empty'."""
    from .portal_calendar_store import SupabaseCalendarStore
    store = SupabaseCalendarStore()
    cutoff = (now - timedelta(days=CATCHUP_WINDOW_DAYS)).date().isoformat()
    r = store._client().get(
        store._rest("gyms"),
        params={
            "created_at": f"gte.{cutoff}",
            "status": "eq.active",
            "is_demo": "eq.false",
            "select": "slug,name,created_at",
            "order": "created_at",
        },
        headers=store._headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        raise CatchupReadError(f"gyms read {r.status_code}")
    return [g for g in (r.json() or []) if g.get("slug")]


def _coverage_default(slug, today):
    """One gym's calendar coverage: {upcoming, pending, approved, published_7d}.
    Read-only over content_calendar. A genuine no-rows gym returns real zeros; a READ
    FAILURE raises CatchupReadError so the report says 'coverage read failed' rather
    than a fabricated 0."""
    from .portal_calendar_store import SupabaseCalendarStore
    store = SupabaseCalendarStore()
    week_ago = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()
    r = store._client().get(
        store._rest("content_calendar"),
        params={
            "gym_id": f"eq.{slug}",
            "post_date": f"gte.{week_ago}",
            "select": "post_date,status,published_at",
        },
        headers=store._headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        raise CatchupReadError(f"coverage read {r.status_code} for {slug}")
    rows = r.json() or []
    upcoming = [x for x in rows if (x.get("post_date") or "") >= today
                and (x.get("status") or "") not in ("denied", "killed")]
    return {
        "upcoming": len(upcoming),
        "pending": len([x for x in upcoming if x.get("status") == "pending"]),
        "approved": len([x for x in upcoming if x.get("status") == "approved"]),
        "published_7d": len([x for x in rows if x.get("published_at")]),
    }


def build_report(now=None, recent_gyms=None, coverage=None):
    """PURE-ish builder: the day's catch-up snapshot. Returns
    {all_caught_up, gyms: [{slug, name, caught_up, ...counts}], text}."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    coverage = coverage or _coverage_default
    if recent_gyms is None:
        try:
            recent_gyms = _recent_gyms_default(now)
        except CatchupReadError as e:
            # UNKNOWN, not empty: say so, keep the report alive but never claim caught up.
            return {"all_caught_up": False, "gyms": [], "read_failed": True,
                    "text": f"*New-client catch-up* ({today}) — could not read the "
                            f"portal gyms right now ({e}). Numbers are UNKNOWN, not "
                            "zero; will retry."}

    gyms, unknown = [], []
    for g in recent_gyms:
        slug = g.get("slug")
        name = g.get("name") or slug
        try:
            cov = coverage(slug, today)
        except CatchupReadError:
            unknown.append(name)                       # read failed for THIS gym
            continue
        gyms.append({
            "slug": slug,
            "name": name,
            "caught_up": cov["upcoming"] >= CATCHUP_MIN_UPCOMING,
            **cov,
        })

    behind = [g for g in gyms if not g["caught_up"]]
    lines = [f"*New-client catch-up* ({today}) — gyms signed up in the last "
             f"{CATCHUP_WINDOW_DAYS} days: {len(gyms) + len(unknown)}"]
    if not gyms and not unknown:
        lines.append("No recent gyms found in the portal (window empty).")
    for g in gyms:
        mark = "✅" if g["caught_up"] else "🔴"
        lines.append(
            f"{mark} {g['name']}: {g['upcoming']} upcoming "
            f"({g['pending']} pending / {g['approved']} approved), "
            f"{g['published_7d']} published last 7d"
            + ("" if g["caught_up"]
               else f" — needs {CATCHUP_MIN_UPCOMING - g['upcoming']} more post(s)"))
    for name in unknown:
        lines.append(f"⚠️ {name}: coverage read failed (unknown, not zero)")
    # all_caught_up requires EVERY gym read cleanly AND be caught up — an unknown gym
    # blocks the "everyone caught up" state so a read failure never silences the report.
    all_caught_up = bool(gyms) and not behind and not unknown
    if all_caught_up:
        lines.append("Everyone is caught up. This report goes quiet until a gym "
                     "falls behind.")
    return {"all_caught_up": all_caught_up, "gyms": gyms, "unknown": unknown,
            "read_failed": bool(unknown), "text": "\n".join(lines)}


def run_daily(now=None, kv=None, alert=None, recent_gyms=None, coverage=None):
    """Send today's catch-up report to Slack, once per day, until all caught up.
    kv-deduped per day; after an all-caught-up day it goes quiet until a gym falls
    behind again (the quiet marker resets on any behind gym). Flag-gated."""
    if not config.catchup_report_enabled():
        return None
    from . import db as _db
    kv_get = (kv.get if kv is not None else _db.kv_get)
    kv_set = (kv.set if kv is not None else _db.kv_set)
    alert = alert or ops_alerts.alert

    now_dt = now or datetime.now(timezone.utc)
    today = now_dt.date().isoformat()
    if kv_get("catchup_report_sent", "") == today:
        return None                                      # already sent today
    report = build_report(now=now_dt, recent_gyms=recent_gyms, coverage=coverage)
    if report["all_caught_up"] and kv_get("catchup_report_quiet", "") == "1":
        return None                                      # stays quiet once confirmed
    # SEND FIRST, then record — so a failed Slack post never swallows the day or the
    # quiet confirmation. alert() swallows errors, so we can only best-effort here, but
    # the ordering guarantees the quiet marker is not set before the send is attempted.
    alert(report["text"], force=True)
    kv_set("catchup_report_sent", today)
    kv_set("catchup_report_quiet", "1" if report["all_caught_up"] else "0")
    return report
