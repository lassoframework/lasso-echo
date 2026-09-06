"""
day_shape_block_alarm.py — the day-shape assertion already stops a bad write.
This module makes a BLOCKED gym visible and escalating, not just logged.

WHY THIS EXISTS
---------------
Blake, 2026-09-06, queue item 4: "Day-shape alert: on assertion failure alert
#echoclaude the same hour naming gym, date and which field collided; include
that gym's remaining runway; RE-FIRE daily while blocked; escalate to SOCIAL
at 3 consecutive days as a content defect."

day_shape.assert_day_distinct() (this same track) already fires an ops_alerts
alert every time a plan pass hits a violation -- that half was already true
by construction, since client_month_run._apply calls it on every plan pass
and a plan pass runs on a schedule. What was missing:
  1. The alert did not name the gym's remaining RUNWAY (how many days of
     already-good calendar are left before it runs dry) -- a human reading
     the alert had no way to judge urgency without a second lookup.
  2. Nothing counted CONSECUTIVE blocked days per gym, so there was no way
     to tell "blocked once, fixed itself" from "blocked for a week and
     nobody has looked" -- exactly the shape this incident has already
     found three times today in other subsystems (stuck_publishing, the
     held-grade dedupe, the stale escalation).
  3. Nothing escalated harder when a block persisted. A gym stuck 3+ days
     is a content defect, not a transient collision, and needs a SOCIAL
     content review, not just another identical alert.

THE MECHANISM
-------------
Each day a plan pass hits a day-shape violation for a gym, `record_block`
stamps `day_shape_blocked_<gym>_<YYYY-MM-DD>` in kv (idempotent: re-running
the same day is a no-op). `consecutive_blocked_days` walks backward from
today counting an unbroken run of stamped days. `run` (called from the
day-shape except-branch) posts the standard alert with the gym's runway
added, and ADDITIONALLY posts a SOCIAL escalation the day the streak first
reaches `DAY_SHAPE_ESCALATE_DAYS` (3) -- not every day after, so hitting 3
does not spam once past it; the run itself continuing to alert daily (the
existing day_shape.py behavior) is the re-fire.

FLAGS
-----
  AGENT_DAY_SHAPE_BLOCK_ALARM (config.day_shape_block_alarm_enabled, default
  ON): purely additive -- can only add runway context and an escalation
  alert, never block or change a write. Same "can only add a signal"
  doctrine as the other two alarms shipped tonight.

  DAY_SHAPE_ESCALATE_DAYS (config.day_shape_escalate_days, default 3):
  consecutive blocked days that trigger the SOCIAL escalation.

Pure core (`consecutive_blocked_days`, `runway_days`), thin I/O wrapper
(`record_and_maybe_escalate`).
"""
from __future__ import annotations

from datetime import date, timedelta

from agent import config, db

_BLOCK_STAMP_PREFIX = "day_shape_blocked_"
_ESCALATED_STAMP_PREFIX = "day_shape_escalated_"

_LIVE_STATUSES = frozenset({"pending", "approved", "published"})


def _log(msg):
    print(f"[day-shape-block-alarm] {msg}")


def _block_key(gym_id, day):
    return f"{_BLOCK_STAMP_PREFIX}{gym_id}_{day.isoformat()}"


def _escalated_key(gym_id):
    return f"{_ESCALATED_STAMP_PREFIX}{gym_id}"


def record_block(gym_id, *, today=None):
    """Stamp today as a blocked day for `gym_id`. Idempotent per calendar day."""
    today = today or date.today()
    db.kv_set(_block_key(gym_id, today), "1")


def consecutive_blocked_days(gym_id, *, asof=None, kv_get=None, max_lookback=30):
    """How many consecutive calendar days, ending at `asof` (default today) and
    walking backward, have a recorded block for `gym_id`. Stops at the first
    day with no stamp -- a single good day resets the count to 0 from there,
    which is the correct read: the gym is unblocked as of that day even if it
    was blocked before.

    `kv_get` is injected for pure testing; production default reads real kv.
    Bounded by `max_lookback` so a gym blocked for months cannot make this
    scan unbounded.
    """
    asof = asof or date.today()
    kv_get = kv_get or db.kv_get
    count = 0
    for i in range(max_lookback):
        day = asof - timedelta(days=i)
        if kv_get(_block_key(gym_id, day), ""):
            count += 1
        else:
            break
    return count


def runway_days(rows, *, today=None):
    """How many days of already-good (pending/approved/published) calendar
    remain past `today` for one gym's rows. The last live post_date minus
    today; 0 if nothing live is scheduled past today, meaning the gym goes
    dark the moment today's slot is used. Pure -- `rows` is the caller's
    already-fetched content_calendar batch for ONE gym."""
    today = today or date.today()
    live_dates = [
        r.get("post_date") for r in (rows or [])
        if isinstance(r, dict)
        and str(r.get("status") or "").strip().lower() in _LIVE_STATUSES
        and r.get("post_date")
    ]
    if not live_dates:
        return 0
    try:
        last = max(date.fromisoformat(d) for d in live_dates)
    except (TypeError, ValueError):
        return 0
    return max(0, (last - today).days)


def record_and_maybe_escalate(gym_id, violations, gym_rows, *, today=None,
                              enabled=None, alert=None, log=_log):
    """Called from the day_shape except-branch, alongside the existing
    ops_alerts.alert() call. Records today's block, computes runway, and -- the
    day the consecutive-blocked streak FIRST reaches
    config.day_shape_escalate_days() -- posts one additional SOCIAL escalation
    naming the gym, the streak length, and the runway. Does not re-escalate
    every day past the threshold (the existing day_shape alert already re-fires
    daily; this is the ONE extra "this is now a content defect" signal).

    Returns {"streak": int, "escalated": bool}."""
    if enabled is None:
        enabled = config.day_shape_block_alarm_enabled()
    if not enabled:
        return {"streak": 0, "escalated": False}

    today = today or date.today()
    alert = alert or _ops_alert

    record_block(gym_id, today=today)
    streak = consecutive_blocked_days(gym_id, asof=today)
    runway = runway_days(gym_rows, today=today)

    threshold = config.day_shape_escalate_days()
    esc_key = _escalated_key(gym_id)
    escalated = False

    if streak < threshold:
        # No active streak past threshold. Clear any old escalation stamp so
        # a FUTURE streak reaching threshold again escalates again, rather
        # than staying silenced forever by a stamp from a streak that ended.
        db.kv_set(esc_key, "")
    elif not db.kv_get(esc_key, ""):
        # First day THIS streak reached (or already exceeded, e.g. the alarm
        # was disabled and re-enabled mid-streak) the threshold. Escalate
        # once, then stamp so later days in the SAME streak do not re-escalate
        # -- the existing day_shape alert already re-fires daily on its own.
        first_field = violations[0].message() if violations else "unknown"
        try:
            alert(
                f"DAY SHAPE CONTENT DEFECT: {gym_id} has been blocked "
                f"{streak} consecutive days (threshold {threshold}). "
                f"Remaining runway: {runway} day(s) of good calendar left. "
                f"First collision: {first_field}. Escalating to SOCIAL for "
                f"a content review — this gym's plan keeps colliding, not "
                f"just once.")
            escalated = True
        except Exception as e:  # noqa: BLE001 - never sinks the plan-pass report
            log(f"could not escalate {gym_id}: {type(e).__name__}: {e}")
        db.kv_set(esc_key, today.isoformat())

    return {"streak": streak, "escalated": escalated}


def _ops_alert(msg):
    from agent import ops_alerts
    ops_alerts.alert(msg)
