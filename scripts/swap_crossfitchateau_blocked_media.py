#!/usr/bin/env python3
"""One-off repair for CrossFit Chateau calendar row 31af261c-e652-48cc-9f02-1a172c13f80b.

WHY THIS EXISTS
---------------
The publish boundary refused this approved row with
"media_asset_review_required". The row points at a Drive asset whose current
review evidence is no longer usable, so retrying the same row cannot succeed.

WHAT THIS SCRIPT DOES
---------------------
It selects a different, currently eligible and approved Drive asset for this
gym, materializes and hosts it through Echo's existing media-swap code, and
conditionally replaces the media on this exact row. The repaired row is moved
to pending and its reject reason is cleared so the gym must review the new
pixels before approving them. It never publishes or approves content.

The write is idempotent and race-safe: it runs only while the exact gym-owned
row is still approved with the original media-review reject reason. A second
run, or a row changed by another worker, performs no write.

This script NEVER touches billing, Stripe, pixel, CAPI, ad budgets, ad
targeting, published posts, or anything medical/legal.

USAGE
-----
    python3 scripts/swap_crossfitchateau_blocked_media.py --dry-run
    python3 scripts/swap_crossfitchateau_blocked_media.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_GYM = "crossfitchateau813e78"
_ROW_ID = "31af261c-e652-48cc-9f02-1a172c13f80b"
_REASON = "media_asset_review_required"
_FORBIDDEN_FLAGS = {
    "--billing", "--stripe", "--pixel", "--capi", "--ad-budget",
    "--targeting", "--publish", "--approve",
}


def _parse_args():
    args = sys.argv[1:]
    for flag in args:
        if flag.lower() in _FORBIDDEN_FLAGS:
            raise SystemExit(f"ERROR: flag '{flag}' is forbidden for this repair.")
        if flag != "--dry-run":
            raise SystemExit(
                f"ERROR: unknown argument '{flag}'. Only --dry-run is supported."
            )
    return "--dry-run" in args


def _load_target(store):
    row = store.get_row(_GYM, _ROW_ID)
    if row is None:
        raise SystemExit("ERROR: the exact gym-owned calendar row was not found.")
    if row.get("published_at") or row.get("late_post_id"):
        raise SystemExit("REFUSED: this row is already published.")
    return row


def _repairable(row):
    return (
        str(row.get("gym_id") or "") == _GYM
        and str(row.get("id") or "") == _ROW_ID
        and str(row.get("status") or "").lower() == "approved"
        and str(row.get("reject_reason") or "") == _REASON
    )


def _drive_candidates(store, row):
    from agent import media_swap

    library_path = media_swap.library_path_for(_GYM)
    candidates = media_swap.candidates_for(
        _GYM, row, store=store, lib=library_path
    )
    return [candidate for candidate in candidates
            if candidate.get("source") == "drive"]


def _conditional_replace(store, row, pick):
    """Replace media only if the blocked approved row is still in the observed state."""
    from agent import media_swap

    payload = {
        "image_url": pick["image_url"],
        "thumbnail_url": media_swap.swap_fields(pick)["thumbnail_url"],
        "source_media_asset_id": media_swap.swap_fields(pick)[
            "source_media_asset_id"
        ],
        "status": "pending",
        "reject_reason": "",
    }
    if pick.get("source_media_url") is not None:
        payload["source_media_url"] = pick.get("source_media_url")

    old_asset_id = str(row.get("source_media_asset_id") or "").strip()
    params = {
        "id": f"eq.{_ROW_ID}",
        "gym_id": f"eq.{_GYM}",
        "status": "eq.approved",
        "reject_reason": f"eq.{_REASON}",
        "source_media_asset_id": (
            f"eq.{old_asset_id}" if old_asset_id else "is.null"
        ),
    }
    response = store._client().patch(
        store._rest("content_calendar"),
        params=params,
        headers=store._headers({
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }),
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        from agent.portal_calendar_store import PortalStoreError, _scrub
        raise PortalStoreError(
            response.status_code, _scrub((response.text or "")[:200])
        )
    rows = [
        candidate for candidate in (response.json() or [])
        if str(candidate.get("gym_id") or "") == _GYM
        and str(candidate.get("id") or "") == _ROW_ID
    ]
    return rows[0] if rows else None


def main():
    dry_run = _parse_args()

    from agent import config, media_swap
    from agent.portal_calendar_store import SupabaseCalendarStore

    if not config.portal_calendar_supabase_enabled():
        raise SystemExit("ERROR: the shared calendar store is unavailable.")

    store = SupabaseCalendarStore()
    row = _load_target(store)
    if not _repairable(row):
        print(
            "No change: the row is no longer an approved "
            "media_asset_review_required block."
        )
        return

    candidates = _drive_candidates(store, row)
    print(f"Eligible fresh reviewed Drive assets: {len(candidates)}")
    if not candidates:
        raise SystemExit(
            "ERROR: no fresh, currently approved Drive asset is available."
        )
    print(f"Selected asset: {candidates[0].get('key')}")

    if dry_run:
        print("DRY RUN: no media was downloaded, hosted, or written.")
        return

    pick = media_swap.pick_replacement(
        _GYM,
        row,
        store=store,
        candidates_fn=lambda _base, _row: candidates,
    )
    if not pick.get("ok"):
        raise SystemExit(
            f"ERROR: replacement could not be prepared ({pick.get('reason')})."
        )

    updated = _conditional_replace(store, row, pick)
    if updated is None:
        raise SystemExit(
            "NO CHANGE: the row changed after inspection; refusing a stale write."
        )

    try:
        book_rows = store.list_month(
            _GYM, str(row.get("post_date") or "")[:7]
        )
    except Exception:
        # The media write already landed. Unknown book state must fail safe:
        # after_swap will keep the old asset stamped rather than re-pool media
        # another live row might still carry.
        book_rows = None
    media_swap.after_swap(
        _GYM,
        row,
        pick,
        book_rows=book_rows,
        swapped_ids=[_ROW_ID],
    )
    print(
        "Done. The reviewed replacement is saved and the row is pending "
        "client review; nothing was approved or published."
    )


if __name__ == "__main__":
    main()
