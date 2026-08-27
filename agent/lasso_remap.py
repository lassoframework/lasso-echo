"""lasso_remap.py — rebuild the LASSO forward calendar with the new VIDEO MIX.

Blake's ruling 2026-08-27: weave the podcast VIDEO clips into the LASSO calendar for
a better content mix (the grid is 79% text cards / 0 humans; podcast clips are real
video of real people), and rebuild the forward calendar with the new mix while KEEPING
the summit 10-day sprint cadence intact.

This is the thin REMAP seam. It does NOT re-implement planning: it reuses the EXISTING
replan path exactly as the nightly refill (agent/jobs/grade_fix._lasso_refill) does —

    real_month_run.plan_and_build(acct, start, days)   # builds the real month drafts
      -> plan_month applies the video mix when AGENT_LASSO_VIDEO_MIX is armed (thu/sun
         prefer video + a cap-safe Wed video slot), sprint days untouched
    real_month_planner.plan_span_months(start, days)   # the full span to reconcile
    real_month_planner.apply_month_plan(gym, drafts, store, span_months=span)
      -> DELETE-then-INSERT, GYM SCOPED, and preserve_and_prune keeps every APPROVED /
         PUBLISHED slot exactly as it is (only UNAPPROVED future days are replaced).

GATES (all inherited from the reused path, none weakened here):
  * Behind AGENT_REAL_MONTH_PLAN: OFF -> plan_and_build returns [] and this remap is a
    no-op that stages nothing (byte-for-byte today).
  * The VIDEO MIX itself is behind AGENT_LASSO_VIDEO_MIX (OFF -> the pre-video rotation).
  * apply_month_plan runs the calendar A-grade gate (AGENT_CALENDAR_GRADE) and the 25%
    podcast cap over the staged rows; approvals are preserved; nothing publishes; every
    staged row lands 'pending' for the human tap.
  * --write is required to touch the store; without it the run is a DRY plan+grade print
    (no delete, no insert).
"""
from __future__ import annotations

from datetime import date

from . import config


def _month_start(month: str | None):
    """Resolve the remap start date. `month` is 'YYYY-MM' (start of that month) or None
    (today). Only UNAPPROVED FUTURE days are ever touched downstream, so a start in the
    past still cannot rewrite an approved/published slot."""
    if month:
        y, m = month.split("-")
        return date(int(y), int(m), 1).isoformat()
    return date.today().isoformat()


def _days_for(start_iso: str, month: str | None) -> int:
    """Span length: a whole named month gets its exact day count; a today-anchored run
    gets 30 (the same window the nightly refill uses)."""
    if not month:
        return 30
    from calendar import monthrange
    d = date.fromisoformat(start_iso)
    return monthrange(d.year, d.month)[1]


def remap(gym_id="lasso", *, month=None, write=False, store=None, logger=None) -> dict:
    """Rebuild the LASSO forward calendar with the video mix for one month (or the next
    30 days). Reuses plan_and_build + apply_month_plan; approved/published days are
    preserved, only unapproved future days are replaced. `write` False -> dry run
    (plan + grade preview, no store writes). Returns a summary dict; never raises out."""
    log = logger or (lambda m: print(f"[lasso-remap] {m}"))
    if not config.real_month_plan_enabled():
        log("AGENT_REAL_MONTH_PLAN is OFF; nothing to remap (no-op).")
        return {"ok": False, "reason": "AGENT_REAL_MONTH_PLAN off",
                "video_mix": config.lasso_video_mix_enabled(),
                "built": 0, "upserted": 0, "deleted": 0}

    from . import real_month_planner as _rmp, real_month_run as _rmr

    start = _month_start(month)
    days = _days_for(start, month)
    acct_key = gym_id if str(gym_id).endswith(("_ig", "_fb")) else f"{gym_id}_ig"

    drafts = _rmr.plan_and_build(acct_key, start, days)
    built = len(drafts)
    video_feeds = sum(1 for d in drafts
                      if not getattr(d, "is_story", False)
                      and (getattr(d, "draft_type", "") == "podcast"
                           or getattr(d, "category", "") == "podcast"))
    log(f"planned {built} draft(s) for {gym_id} over {start} +{days}d "
        f"(video_mix={config.lasso_video_mix_enabled()}; "
        f"podcast/video feeds ~{video_feeds}).")

    if not write:
        log("dry run (no --write): nothing staged. Re-run with --write to apply.")
        return {"ok": True, "dry_run": True, "start": start, "days": days,
                "video_mix": config.lasso_video_mix_enabled(),
                "built": built, "podcast_video_feeds": video_feeds,
                "upserted": 0, "deleted": 0}

    if store is None:
        from .portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()

    span = _rmp.plan_span_months(start, days)
    res = _rmp.apply_month_plan(gym_id, drafts, store, span_months=span)
    res.setdefault("built", built)
    res["start"] = start
    res["days"] = days
    res["video_mix"] = config.lasso_video_mix_enabled()
    res["podcast_video_feeds"] = video_feeds
    if res.get("ok"):
        log(f"applied: upserted {res.get('upserted', 0)}, deleted "
            f"{res.get('deleted', 0)} across {res.get('months')}. "
            f"{res.get('grade', '')}".rstrip())
    else:
        log(f"NOT applied: {res.get('reason')}")
    return res


def cli(argv):
    """CLI: python -m agent lasso-remap [--month YYYY-MM] [--gym lasso] [--write]."""
    month = None
    gym = "lasso"
    write = False
    i = 0
    while i < len(argv):
        if argv[i] == "--month" and i + 1 < len(argv):
            month = argv[i + 1]; i += 2; continue
        if argv[i] in ("--gym", "--account") and i + 1 < len(argv):
            gym = argv[i + 1]; i += 2; continue
        if argv[i] == "--write":
            write = True; i += 1; continue
        i += 1
    out = remap(gym, month=month, write=write)
    if not out.get("ok") and not out.get("dry_run"):
        raise SystemExit(1)
    return out
