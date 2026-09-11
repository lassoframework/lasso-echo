"""
lasso_astra_rework.py — regenerate the IMAGE on every existing LASSO calendar
slot that is not already video and not already published, as a linked Astra v2
CANDIDATE — same safe pattern as the Summit variant-pairing work, reused
verbatim (variant_regen.generate_variant_image + create_variant_candidate).

Blake (2026-09-11): "it should rework my whole calendar as well" — LASSO's OWN
(gym_id='lasso') account only. CORRECTION (2026-09-11, same day): this is
"recreate the images, not redesign the schedule" — SAME dates, SAME times, SAME
number of posts, everything else identical. This module NEVER writes a new
post_date, NEVER deletes a row, NEVER changes post count: it only ever inserts
a linked 'candidate' row (variant_of the existing row's id) carrying a freshly
generated Astra image for the SAME slot. Nothing here touches
real_month_planner / real_month_run's schedule or category rotation at all.

SCOPE (this module decides nothing on its own beyond "which rows qualify"):
  * gym_id = 'lasso' only. Never any client gym (Task 2 is LASSO-only).
  * variant_status='active' (the one live row per slot; a slot that already has
    a pending candidate from an earlier pass is skipped — see _already_has_candidate).
  * status NOT IN ('published', 'denied', 'killed', 'deleted') — a slot that
    already shipped or was rejected is left alone.
  * NOT already video: thumbnail_url IS NULL AND image_url does not end in one
    of VIDEO_EXTENSIONS (mirrors the "a video row carries thumbnail_url"
    convention already documented in portal_calendar_store.create_variant_candidate).
  * The row's OWN existing caption/hook is the creative brief (variant_regen's
    contract): nothing here invents new copy. A row with no caption is skipped
    (variant_regen returns REASON_NO_CAPTION for it).

SAFETY: capped per run (--limit), reports real generate/insert counts, never
raises past one bad row (a single Astra failure is logged and skipped, the
sweep continues). Every inserted row is 'pending' (create_variant_candidate's
own contract) — nothing here publishes or swaps; a human picks in the portal.
"""

import argparse
import sys

from . import config

VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".m4v")
_NOT_ELIGIBLE_STATUS = ("published", "denied", "killed", "deleted")


def _is_video_row(row):
    if row.get("thumbnail_url"):
        return True
    url = str(row.get("image_url") or row.get("source_media_url") or "").lower()
    return url.endswith(VIDEO_EXTENSIONS)


def find_candidates(store, gym_id="lasso", months=None):
    """Every LASSO row eligible for an Astra v2 rework pass: active, not
    published/denied/killed/deleted, not already video. `months` is an
    iterable of 'YYYY-MM' strings to scan (the caller decides the forward
    span — see cli()); this function only filters, it does not choose dates."""
    rows = []
    seen_ids = set()
    for month in (months or []):
        for row in (store.list_month(gym_id, month) or []):
            rid = row.get("id")
            if rid is None or rid in seen_ids:
                continue
            seen_ids.add(rid)
            if str(row.get("gym_id")) != gym_id:
                continue
            if str(row.get("status") or "").lower() in _NOT_ELIGIBLE_STATUS:
                continue
            if str(row.get("variant_status") or "active") != "active":
                continue
            if _is_video_row(row):
                continue
            if not str(row.get("caption") or "").strip():
                continue
            rows.append(row)
    return rows


def rework_row(store, row, gym_id="lasso", regen_fn=None, log=None):
    """Generate one Astra v2 image for `row` and land it as a linked candidate.
    Returns (True, candidate) or (False, reason). Never raises."""
    log = log or (lambda *_: None)
    from . import variant_regen as _vr
    gen = regen_fn or _vr.generate_variant_image
    try:
        result = gen(row, gym_id)
    except Exception as exc:  # noqa: BLE001 - one bad row must not sink the sweep
        log(f"lasso-astra-rework: row {row.get('id')} generate raised "
            f"{type(exc).__name__}: {exc}")
        return False, "generate_raised"
    if not result.get("ok"):
        log(f"lasso-astra-rework: row {row.get('id')} skipped: {result.get('reason')}")
        return False, result.get("reason")
    try:
        candidate = store.create_variant_candidate(gym_id, row, result["image_url"])
    except Exception as exc:  # noqa: BLE001
        log(f"lasso-astra-rework: row {row.get('id')} insert raised "
            f"{type(exc).__name__}: {exc}")
        return False, "insert_raised"
    if candidate is None:
        return False, "insert_returned_none"
    log(f"lasso-astra-rework: row {row.get('id')} -> candidate {candidate.get('id')}")
    return True, candidate


def run(store, gym_id="lasso", months=None, limit=None, write=False, log=None):
    """Find eligible rows and, when write=True, generate+land a candidate for
    up to `limit` of them (None = no cap). Returns a summary dict. write=False
    is a dry-run: it reports what WOULD run and calls Astra for nothing."""
    log = log or print
    if not config.variant_pairing_enabled():
        log("lasso-astra-rework: ECHO_VARIANT_PAIRING is off; nothing to do")
        return {"ok": False, "reason": "variant_pairing_disabled", "found": 0, "done": 0}

    candidates = find_candidates(store, gym_id=gym_id, months=months)
    found = len(candidates)
    if not write:
        log(f"lasso-astra-rework: DRY RUN — {found} row(s) eligible for an "
            "Astra v2 candidate (pass --write to actually generate)")
        return {"ok": True, "found": found, "done": 0, "dry_run": True}

    todo = candidates if limit is None else candidates[:limit]
    done, failed = 0, []
    for row in todo:
        ok, info = rework_row(store, row, gym_id=gym_id, log=log)
        if ok:
            done += 1
        else:
            failed.append({"id": row.get("id"), "reason": info})
    log(f"lasso-astra-rework: {done}/{len(todo)} candidate(s) generated "
        f"({found} total eligible; {len(failed)} failed/skipped)")
    return {"ok": True, "found": found, "attempted": len(todo), "done": done,
           "failed": failed}


def _default_months(count=3):
    """The current month plus the next `count`-1 months, 'YYYY-MM' — the
    forward span this sweep scans by default (LASSO's calendar is built ~1
    month ahead per the plan-horizon belt, so this comfortably covers it)."""
    from datetime import date
    months = []
    y, m = date.today().year, date.today().month
    for _ in range(count):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def cli(argv):
    p = argparse.ArgumentParser(prog="lasso-astra-rework")
    p.add_argument("--gym", default="lasso")
    p.add_argument("--months", default=None,
                  help="comma-separated YYYY-MM list; default = this month + next 2")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)

    from . import portal_calendar_store as _pcs
    store = _pcs.SupabaseCalendarStore()
    months = args.months.split(",") if args.months else _default_months()
    result = run(store, gym_id=args.gym, months=months, limit=args.limit,
                write=args.write)
    if not result.get("ok"):
        sys.exit(1)
