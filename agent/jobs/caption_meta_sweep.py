"""caption_meta_sweep.py — clean the book of edit-rationale meta blocks.

Born from the CrossFit ENG live leak (FB post published 2026-08-23 00:02 ET whose
caption ended with "[why] Removed word parents and added people, added pets after
kids. Made the wording more inclusive." — internal edit reasoning shipped to a real
gym's audience). The source and the gates are fixed (drafter parser, portal edit
split, post_quality/gbp caption_issues, the calendar_autopublish final gate); this
sweep repairs what is ALREADY sitting in content_calendar for EVERY gym:

  * WAITING rows (pending / approved / draft / queued / failed): a caption whose
    meta block is a clean SUFFIX is stripped through the STATUS-PRESERVING caption
    patch (patch_caption_preserve_status). patch_caption is deliberately NOT used —
    it resets status to 'pending', which would silently UN-APPROVE a row the client
    already approved; this cleanup is Echo hygiene, not a human edit needing
    re-approval. An ALL-meta caption is left untouched and reported as held (a human
    must rewrite it; wiping it would strand an empty caption).
  * PUBLISHED / PUBLISHING rows: reported ONLY. They are live (or mid-flight) on a
    real platform, so cleaning them needs a live platform edit by a human — this
    sweep never touches live platforms and never rewrites publish history.

Usage (from repo root):
  python -m agent.jobs.caption_meta_sweep [--dry-run] [gym_ids ...]

With no gym_ids: every client gym base plus 'lasso'. --dry-run computes and prints
what would change but writes nothing. Returns a per-gym summary; published carriers
are also surfaced once through ops_alerts so a human sees which live posts need a
manual platform edit.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from agent import post_quality

# The book window swept: far enough back to catch every published carrier still
# worth a live edit, far enough forward to cover the whole staged runway (the
# planner's hard horizon is ~today+31; +45 leaves margin for legacy long books).
BACK_DAYS = 45
FORWARD_DAYS = 45

# Statuses that are live (or mid-flight) on a real platform: report, never write.
_LIVE_STATUSES = ("published", "publishing")


def _default_gyms():
    """Every client gym base plus LASSO's own account — the whole book."""
    from agent.calendar_autopublish import client_gym_bases
    bases = list(client_gym_bases() or [])
    if "lasso" not in bases:
        bases.append("lasso")
    return bases


def sweep_gym(gym_id, store, *, dry_run=False, today_iso=None, log=None):
    """Sweep ONE gym's calendar rows for meta-block captions.

    Returns {gym, scanned, cleaned, held, published, errors} where cleaned/held/
    published are lists of {id, post_date, account, status} descriptors (published
    rows are the ones needing a live platform edit)."""
    log = log or (lambda m: print(f"[caption-meta-sweep] {m}"))
    today = date.fromisoformat(today_iso) if today_iso else date.today()
    start = (today - timedelta(days=BACK_DAYS)).isoformat()
    end = (today + timedelta(days=FORWARD_DAYS)).isoformat()

    out = {"gym": gym_id, "scanned": 0, "cleaned": [], "held": [],
           "published": [], "errors": 0}
    try:
        rows = store.rows_in_range(gym_id, start, end) or []
    except Exception as exc:  # noqa: BLE001 - one gym's read never stops the sweep
        log(f"{gym_id}: rows_in_range failed: {type(exc).__name__}: {exc}")
        out["errors"] += 1
        return out

    for row in rows:
        out["scanned"] += 1
        body, meta = post_quality.split_meta_suffix(row.get("caption") or "")
        if not meta:
            continue
        desc = {"id": row.get("id"),
                "post_date": str(row.get("post_date") or "")[:10],
                "account": row.get("account") or "",
                "status": (row.get("status") or "").strip().lower()}
        if desc["status"] in _LIVE_STATUSES:
            # live on a real platform: a human edits it there; never from here.
            out["published"].append(desc)
            continue
        if not (body or "").strip():
            # all-meta: stripping would leave an empty caption — hold for a human.
            out["held"].append(desc)
            continue
        if dry_run:
            out["cleaned"].append(desc)
            continue
        try:
            updated = store.patch_caption_preserve_status(
                gym_id, row.get("id"), body.strip())
        except Exception as exc:  # noqa: BLE001 - one row never stops the sweep
            log(f"{gym_id} {desc['id']}: caption clean failed: "
                f"{type(exc).__name__}: {exc}")
            out["errors"] += 1
            continue
        if updated is None:
            # the race guard refused (row went publishing/published between the
            # read and the write): the publish-lane final gate owns it now.
            out["held"].append(desc)
            continue
        out["cleaned"].append(desc)
    return out


def run(gym_ids=None, store=None, *, dry_run=False, today_iso=None, log=None,
        alert=None):
    """Sweep every gym. Returns the list of per-gym summaries and fires ONE ops
    alert naming the published carriers (they need live platform edits)."""
    log = log or (lambda m: print(f"[caption-meta-sweep] {m}"))
    if store is None:
        from agent.portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    gyms = list(gym_ids) if gym_ids else _default_gyms()

    results = [sweep_gym(g, store, dry_run=dry_run, today_iso=today_iso, log=log)
               for g in gyms]

    for r in results:
        log(f"{r['gym']}: scanned {r['scanned']}, cleaned {len(r['cleaned'])}, "
            f"held {len(r['held'])}, published-carriers {len(r['published'])}, "
            f"errors {r['errors']}" + (" (dry run)" if dry_run else ""))

    live = [(r["gym"], d) for r in results for d in r["published"]]
    if live and not dry_run:
        # a human must edit these on the live platform; say exactly which ones, once.
        lines = [f"{g}: {d['account']} {d['post_date']} row {d['id']}"
                 for g, d in live]
        try:
            if alert is None:
                from agent.ops_alerts import alert as alert
            alert("caption meta sweep: " + str(len(live)) + " LIVE post(s) carry an "
                  "internal edit-rationale block ([why]/[reason]) and need a manual "
                  "edit on the platform itself (Echo never rewrites live posts): "
                  + "; ".join(lines[:20]))
        except Exception as exc:  # noqa: BLE001 - reporting never fails the sweep
            log(f"live-carrier alert failed: {type(exc).__name__}: {exc}")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Strip edit-rationale meta blocks ([why]/[reason]) from waiting "
                    "calendar captions; report published carriers.")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Compute and print changes; write nothing.")
    parser.add_argument("gym_ids", nargs="*",
                        help="Gym bases to sweep. Default: every client gym + lasso.")
    args = parser.parse_args(argv)
    results = run(gym_ids=args.gym_ids or None, dry_run=args.dry_run)
    if any(r["errors"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
