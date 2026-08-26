"""
backfill_caption_ledger.py — seed the caption ledger from historical
content_calendar rows (published + staged).

Reads all existing content_calendar rows from Supabase, hashes each caption,
and calls caption_ledger.record_staged() so future cooldown checks include
the full history. Without this seeding, the ledger only knows about rows that
went through the new code path (insert_rows / mark_published after Wave 3 was
deployed). Running this once after deployment gives the cooldown its full
historical context.

Flag: AGENT_CAPTION_COOLDOWN (default OFF).
  When OFF the job exits immediately as a no-op (the ledger is inactive).
  When ON the job reads all rows and stamps them.

Usage (from repo root):
  python -m agent.jobs.backfill_caption_ledger [--dry-run]

--dry-run  Hash and count rows but do NOT write to the kv ledger. Useful for
           verifying Supabase connectivity and counts before a real backfill.

Outputs: per-gym counts are printed to stdout. No Slack alert (this is a
one-shot operational job, not a recurring alerting job).
"""
from __future__ import annotations

import argparse
import os
import sys

# ---------------------------------------------------------------------------
# run() — the callable entry point (also invoked by __main__)
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, http=None) -> dict:
    """Seed the caption ledger from all historical content_calendar rows.

    Returns a summary dict::

        {
          "ok": bool,
          "reason": str,          # only on ok=False
          "rows_read": int,
          "rows_stamped": int,
          "gyms": {gym_id: count, ...}
        }

    `http` is an injectable HTTP client (same pattern as SupabaseCalendarStore)
    for unit testing without a real network call.
    """
    from .. import config

    if not config.caption_cooldown_enabled():
        return {
            "ok": False,
            "reason": "AGENT_CAPTION_COOLDOWN is OFF; backfill is a no-op",
            "rows_read": 0,
            "rows_stamped": 0,
            "gyms": {},
        }

    from .. import caption_ledger as _ledger
    from ..portal_calendar_store import SupabaseCalendarStore, PortalStoreError

    store = SupabaseCalendarStore(http=http)

    # Fetch all content_calendar rows in pages (PostgREST default limit is 1 000).
    # We read until we get fewer than PAGE_SIZE rows.
    PAGE_SIZE = 1000
    all_rows = []
    try:
        offset = 0
        while True:
            page = _fetch_page(store, offset, PAGE_SIZE)
            all_rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"Supabase read failed: {type(exc).__name__}: {exc}",
            "rows_read": len(all_rows),
            "rows_stamped": 0,
            "gyms": {},
        }

    rows_read = len(all_rows)
    rows_stamped = 0
    gyms: dict[str, int] = {}

    for row in all_rows:
        gym_id = str(row.get("gym_id") or "")
        caption = row.get("caption") or ""
        post_date = row.get("post_date") or ""
        if not gym_id or not caption or not post_date:
            continue
        if not dry_run:
            try:
                _ledger.record_staged(gym_id, caption, post_date)
                rows_stamped += 1
                gyms[gym_id] = gyms.get(gym_id, 0) + 1
            except Exception:
                pass  # stamp failure is non-fatal; keep going
        else:
            # Dry run: count without writing
            rows_stamped += 1
            gyms[gym_id] = gyms.get(gym_id, 0) + 1

    return {
        "ok": True,
        "dry_run": dry_run,
        "rows_read": rows_read,
        "rows_stamped": rows_stamped,
        "gyms": gyms,
    }


def _fetch_page(store, offset: int, limit: int) -> list:
    """Fetch one page of all content_calendar rows (any gym, any status)."""
    r = store._client().get(
        store._rest("content_calendar"),
        params={
            "select": "gym_id,caption,post_date,status",
            "order": "post_date",
            "limit": str(limit),
            "offset": str(offset),
        },
        headers=store._headers(),
        timeout=60,
    )
    if r.status_code >= 400:
        from ..portal_calendar_store import PortalStoreError, _scrub
        raise PortalStoreError(r.status_code, _scrub((r.text or "")[:200]))
    return r.json() or []


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _main():
    parser = argparse.ArgumentParser(
        description="Backfill caption ledger from historical content_calendar rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Hash and count without writing to kv ledger.",
    )
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"[backfill_caption_ledger] {mode}")
    if not result.get("ok"):
        print(f"  SKIPPED: {result.get('reason')}")
        sys.exit(0)
    print(f"  rows_read   : {result['rows_read']}")
    print(f"  rows_stamped: {result['rows_stamped']}")
    gyms = result.get("gyms") or {}
    if gyms:
        print("  per gym:")
        for gid, count in sorted(gyms.items(), key=lambda x: -x[1]):
            print(f"    {gid}: {count}")
    else:
        print("  (no rows stamped)")


if __name__ == "__main__":
    _main()
