"""
Wave 0.2 — Dedupe the forward book through Echo, never through SQL.

content_calendar is a read-side mirror; approvals and status changes flow
through Echo's store. This one-shot job groups future pending rows per gym
by caption_hash, keeps the earliest occurrence (lowest post_date, then
lowest row id as a tiebreak), and moves duplicates to 'denied' with
reject_reason='duplicate_purge_2026_08' via portal_calendar_store.

Flag: AGENT_DEDUPE_FORWARD_BOOK (default OFF).
  - When OFF (default), the job runs in --dry-run mode regardless of the
    --dry-run flag: it computes and logs the would-be changes but writes nothing.
  - When ON, real writes are made unless --dry-run is passed explicitly.

--dry-run  Compute and log changes; make no writes.

Usage (from repo root):
  python -m agent.jobs.dedupe_forward_book [--dry-run]

Expected outcome for LASSO: 125 IG slots collapse to ~43. The freed slots
are refilled by the Wave 6 planner after this job is verified.

Log format: counts per gym are posted to #ops via ops_alerts.alert().
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import re
import sys
from datetime import date

from agent import config
from agent import ops_alerts
from agent.portal_calendar_store import PortalStoreError, SupabaseCalendarStore


# ---------------------------------------------------------------------------
# caption_hash — mirrors the Wave 3 spec definition so deduplication is
# consistent with the ledger that Wave 3 introduces.  Defined here as a local
# copy so Wave 0 ships independently; once Wave 3 lands, import from there.
# ---------------------------------------------------------------------------

def caption_hash(text: str) -> str:
    """Normalize and hash a caption for deduplication.

    Strips @handles and #hashtags (they do not differentiate the caption body),
    lower-cases, removes non-alphanumeric characters, collapses whitespace, and
    takes the first 200 characters before hashing.  Returns a 16-hex-char
    prefix of the SHA-256 digest — identical to the Wave 3 spec definition.
    """
    t = re.sub(r"[#@]\S+", "", str(text).lower())
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()[:200]
    return hashlib.sha256(t.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Core deduplication logic (pure — no I/O, injectable for tests)
# ---------------------------------------------------------------------------

def find_duplicates(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition rows into (keepers, duplicates).

    Groups by caption_hash.  Within each group, keeps the row with the
    earliest post_date; ties are broken by the row's 'id' string (lexicographic
    ascending) so the result is deterministic.  All other rows in the group are
    duplicates and must be denied.

    Args:
        rows: list of content_calendar row dicts with at least 'id',
              'post_date', and 'caption' keys.

    Returns:
        (keepers, duplicates) — two non-overlapping lists; keepers + duplicates
        equals the original list in some order.
    """
    by_hash: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        h = caption_hash(row.get("caption") or "")
        by_hash[h].append(row)

    keepers: list[dict] = []
    duplicates: list[dict] = []

    for _hash, group in by_hash.items():
        # Sort: earliest post_date first, then smallest id as tiebreak.
        sorted_group = sorted(group, key=lambda r: (r.get("post_date") or "", str(r.get("id") or "")))
        keepers.append(sorted_group[0])
        duplicates.extend(sorted_group[1:])

    return keepers, duplicates


# ---------------------------------------------------------------------------
# Per-gym dedupe — contacts the store, applies deny writes
# ---------------------------------------------------------------------------

def dedupe_gym(
    gym_id: str,
    store: SupabaseCalendarStore,
    today_iso: str,
    dry_run: bool,
) -> dict:
    """Run deduplication for one gym.

    Returns a result dict:
      {
        "gym_id": str,
        "total_pending": int,
        "duplicates_found": int,
        "duplicates_denied": int,  # 0 in dry-run
        "errors": int,
      }
    """
    result = {
        "gym_id": gym_id,
        "total_pending": 0,
        "duplicates_found": 0,
        "duplicates_denied": 0,
        "errors": 0,
    }

    rows = store.list_pending_future(gym_id, today_iso)
    result["total_pending"] = len(rows)

    _keepers, duplicates = find_duplicates(rows)
    result["duplicates_found"] = len(duplicates)

    if dry_run:
        for dup in duplicates:
            print(
                f"[dry-run] would deny gym={gym_id} id={dup.get('id')} "
                f"post_date={dup.get('post_date')} caption[:60]="
                f"{str(dup.get('caption') or '')[:60]!r}"
            )
        return result

    for dup in duplicates:
        row_id = dup.get("id")
        try:
            updated = store.deny_with_reason(gym_id, row_id, "duplicate_purge_2026_08")
            if updated:
                result["duplicates_denied"] += 1
            else:
                # Row was already gone or gym_id mismatch — not an error.
                result["duplicates_denied"] += 1
        except PortalStoreError as exc:
            print(
                f"[dedupe] ERROR denying gym={gym_id} id={row_id}: {exc}",
                file=sys.stderr,
            )
            result["errors"] += 1

    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(gym_ids: list[str], store: SupabaseCalendarStore | None = None, dry_run: bool = False) -> list[dict]:
    """Run the forward-book deduplication for every gym in gym_ids.

    Args:
        gym_ids:  List of gym_id strings to process.
        store:    Injectable SupabaseCalendarStore; defaults to a real one from
                  env creds (for production use).
        dry_run:  When True, compute and log changes but make no writes.

    Returns:
        List of per-gym result dicts (see dedupe_gym).
    """
    flag_on = config.dedupe_forward_book_enabled()
    if not flag_on and not dry_run:
        print(
            "[dedupe] AGENT_DEDUPE_FORWARD_BOOK is OFF — running in dry-run mode. "
            "Set AGENT_DEDUPE_FORWARD_BOOK=true to enable real writes.",
            file=sys.stderr,
        )
        dry_run = True

    if store is None:
        store = SupabaseCalendarStore()

    today_iso = date.today().isoformat()
    results = []

    for gym_id in gym_ids:
        print(f"[dedupe] processing gym={gym_id} today={today_iso} dry_run={dry_run}")
        result = dedupe_gym(gym_id, store, today_iso, dry_run)
        results.append(result)
        print(
            f"[dedupe] gym={gym_id}: total_pending={result['total_pending']} "
            f"duplicates_found={result['duplicates_found']} "
            f"duplicates_denied={result['duplicates_denied']} "
            f"errors={result['errors']}"
        )

    # Post a summary to #ops.
    total_dupes = sum(r["duplicates_found"] for r in results)
    total_denied = sum(r["duplicates_denied"] for r in results)
    total_errors = sum(r["errors"] for r in results)
    mode = "DRY RUN" if dry_run else "LIVE"

    lines = [f"dedupe_forward_book [{mode}] — {today_iso}"]
    for r in results:
        lines.append(
            f"  {r['gym_id']}: {r['total_pending']} pending, "
            f"{r['duplicates_found']} dupes found, "
            f"{r['duplicates_denied']} denied, "
            f"{r['errors']} errors"
        )
    lines.append(
        f"  TOTAL: {total_dupes} duplicates found, {total_denied} denied, {total_errors} errors"
    )
    summary = "\n".join(lines)
    print(summary)
    ops_alerts.alert(summary, force=True)

    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Wave 0.2 — deduplicate the Echo forward book (future pending rows)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Compute changes and log them; make no writes to content_calendar.",
    )
    parser.add_argument(
        "gym_ids",
        nargs="*",
        help="Gym IDs to process. Defaults to ['lasso'] when omitted.",
    )
    args = parser.parse_args(argv)

    gym_ids = args.gym_ids if args.gym_ids else ["lasso"]
    results = run(gym_ids=gym_ids, dry_run=args.dry_run)

    # Exit non-zero when any gym had errors (real run only).
    if any(r["errors"] > 0 for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
