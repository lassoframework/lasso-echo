"""plan_horizon.py — the HARD one-month planning-horizon cap (Blake, 2026-08-28).

Blake's rule, verbatim intent: "Echo actually only builds a full month out because the
goal of Echo is to go through the posts and relearn. I don't want it building out more
than a full month because it's going to just have to be recreating all the time and
cost me way too many tokens."

Echo's monthly retro/relearn loop rebuilds every gym's forward plan, so a calendar row
drafted MORE than one month past today is guaranteed churn: it gets recreated before it
ever posts, burning tokens for nothing. Live evidence (2026-08-28): LASSO carried 212
content_calendar rows beyond today+31 (out to 2026-12-04), almost all denied/stale
relearn churn from earlier long-span builds.

ONE CLAMP, ONE PLACE:
  * horizon_clamp(start_date, days, ...) — the span clamp every plan / backfill /
    remap path calls so no lane can BUILD a row past today + config.plan_horizon_days()
    (default 31, AGENT_PLAN_HORIZON_DAYS). Wired into build_client_month,
    backfill_denied_slots, scan_and_generate, real_month_run.plan_and_build, and
    lasso_remap.remap. Clamping only ever SHORTENS the tail of the span — cadence
    never gaps INSIDE the month.
  * belt_filter(account_key, rows, ...) — the row-level INSERT BELT
    portal_calendar_store.insert_rows applies so no lane can STAGE a beyond-horizon
    row even if a caller forgot the clamp. Drops-with-log, one summary line per batch
    (the needs-media digest pattern), never per-row spam, never silent.

DELIBERATE EXEMPTIONS (row-level, narrow — never a loophole every row can ride):
  * a row tied to a dated real-world EVENT (content_calendar.event_id set): gym event
    arcs stage near the EVENT window on purpose (a Black Friday planned in August is
    anchored to a date, not relearn churn).
  * the LASSO dated lanes (gym_id 'lasso', pillar summit/book/welcome): the Summit
    sprint cycles and the dated book/welcome offers are anchored to real dates
    (Summit is Nov 7-8).

RAILS: already-approved/published rows are untouched everywhere — the cap governs what
gets BUILT/STAGED, it is never a retroactive sweep (nothing here deletes or trims an
existing row). AGENT_PLAN_HORIZON_DAYS=0 disables the cap entirely (emergency escape
hatch: the flag exists to raise/lower the number, not to disable the principle).
Nothing here publishes."""

from datetime import date, timedelta

from . import config


def horizon_end(now=None):
    """The LAST post_date a build may plan/stage (now + plan_horizon_days), or None
    when the cap is disabled (AGENT_PLAN_HORIZON_DAYS=0). `now` is injectable for
    determinism; production callers default to today."""
    days = config.plan_horizon_days()
    if days <= 0:
        return None
    base = now if isinstance(now, date) else date.today()
    return base + timedelta(days=days)


def horizon_clamp(start_date, days, *, now=None, logger=None, label=""):
    """Clamp a build span's `days` so the whole span stays inside the horizon.

    Returns the number of days the caller may actually plan: unchanged when the span
    already fits (the default 30-day builds never clamp at the default 31-day
    horizon), else the honest maximum, with ONE log line stating requested vs clamped
    (never silent). A start_date entirely beyond the horizon clamps to 0 — the caller
    must treat that as "nothing to build".

    Pure over (start_date, days, now, the env flag): no I/O, no writes. `now` is
    injectable; an unparseable start_date fails OPEN (returns `days` unchanged — the
    insert belt still backstops the write). days <= 0 passes through untouched (the
    callers' own days-must-be-positive guards keep their behavior)."""
    if days is None or days <= 0:
        return days
    end = horizon_end(now)
    if end is None:
        return days
    try:
        start = start_date if isinstance(start_date, date) \
            else date.fromisoformat(str(start_date)[:10])
    except (TypeError, ValueError):
        return days
    allowed = (end - start).days
    if allowed >= days:
        return days
    allowed = max(0, allowed)
    log = logger or (lambda m: print(f"[plan-horizon] {m}"))
    who = f"{label}: " if label else ""
    log(f"plan horizon: {who}requested {days} day(s) from {start.isoformat()} but "
        f"Echo builds at most one month out (today+{config.plan_horizon_days()}, "
        f"last allowed day {end.isoformat()}); clamped to {allowed} day(s) — the "
        "monthly relearn rebuilds anything further out anyway")
    return allowed


# The LASSO dated-offer lanes allowed past the horizon: real DATED campaigns (the
# Summit sprint cycles run to the Nov 7-8 event; book/welcome posts are pre-written
# dated content), not relearn churn. LASSO only — a client gym's 'summit' pillar (none
# exists today) would NOT ride this.
_LASSO_DATED_PILLARS = ("summit", "book", "welcome")


def is_horizon_exempt(row):
    """True when a row is anchored to a DATED real-world thing and may sit beyond the
    horizon: it carries an event_id (a gym event arc stages near the EVENT window), or
    it is one of LASSO's own dated lanes (pillar summit/book/welcome on gym_id
    'lasso'). NARROW by design: everything else beyond the horizon is relearn churn
    and is dropped by the belt. Pure."""
    r = row or {}
    if r.get("event_id"):
        return True
    gym = str(r.get("gym_id") or "").strip().lower()
    pillar = str(r.get("pillar") or r.get("category") or "").strip().lower()
    return gym == "lasso" and pillar in _LASSO_DATED_PILLARS


def belt_filter(account_key, rows, *, now=None, alert=None):
    """The row-level INSERT BELT: drop any row whose post_date is beyond the horizon,
    unless the row is exempt (is_horizon_exempt). Returns (kept_rows, dropped_dates).

    When anything is dropped, exactly ONE summary alert/log line fires for the batch
    (the flush_needs_media_alerts digest posture: a count + a date span, never a line
    per row). A row with no/unparseable post_date passes through (the planners' own
    no-post_date drop handles it). Cap disabled (horizon 0) -> everything passes,
    byte-for-byte. Never raises out: an alert failure must not block staging."""
    rows = list(rows or [])
    end = horizon_end(now)
    if end is None or not rows:
        return rows, []
    kept, dropped = [], []
    for row in rows:
        pd = str((row or {}).get("post_date") or "")[:10]
        try:
            d = date.fromisoformat(pd) if pd else None
        except ValueError:
            d = None
        if d is not None and d > end and not is_horizon_exempt(row):
            dropped.append(pd)
            continue
        kept.append(row)
    if dropped:
        ds = sorted(dropped)
        span = ds[0] if len(ds) == 1 else f"{ds[0]} to {ds[-1]}"
        msg = (f"plan horizon belt: dropped {len(dropped)} {account_key} row(s) "
               f"beyond today+{config.plan_horizon_days()} ({span}) at stage time. "
               "Echo relearns monthly, so far-future drafts are rebuilt anyway; the "
               "next month build refills these days when they come into the window.")
        try:
            (alert or _default_alert)(msg)
        except Exception:  # noqa: BLE001 - alerting never blocks staging
            pass
    return kept, dropped


def _default_alert(msg):
    """One loud line: local log always, ops alert best effort (digest pattern)."""
    print(f"[plan-horizon] {msg}")
    try:
        from . import ops_alerts
        ops_alerts.alert(msg)
    except Exception:  # noqa: BLE001 - alerting never blocks staging
        pass
