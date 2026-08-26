"""
dedupe_forward_book.py
======================
Wave 0 preflight job: deduplicate the forward content calendar.

For every gym, groups future 'pending' rows in content_calendar by caption_hash
(SHA256 of normalized caption, first 16 hex chars). Keeps the earliest row per hash;
sets the rest to status='denied' with reject_reason='duplicate_purge_2026_08'.

All writes go through SupabaseCalendarStore (portal_calendar_store). Never direct SQL.

Usage:
    python3 -m agent.jobs.dedupe_forward_book          # live run
    python3 -m agent.jobs.dedupe_forward_book --dry-run  # inspect only, no writes

Importable:
    from agent.jobs.dedupe_forward_book import run
    run(dry_run=True)

Flag:
    AGENT_DEDUPE_FORWARD_BOOK (default OFF). Job is a no-op unless this flag is 'true'.
"""

import argparse
import hashlib
import json
import logging
import os
import re
from datetime import date, timezone, datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    v = os.environ.get("AGENT_DEDUPE_FORWARD_BOOK", "false").strip().lower()
    return v in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Caption hash
# ---------------------------------------------------------------------------

def caption_hash(text: str) -> str:
    """
    Stable 16-char SHA256 hash of a normalized caption.

    Normalization:
      1. Lowercase the whole string.
      2. Strip hashtags (#word) and @-mentions (@word).
      3. Remove all non-alphanumeric, non-space characters.
      4. Collapse whitespace, strip edges.
      5. Truncate to first 200 chars.
      6. SHA256, return first 16 hex chars.
    """
    t = str(text).lower()
    t = re.sub(r"[#@]\S+", "", t)
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()[:200]
    return hashlib.sha256(t.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Gym list helpers
# ---------------------------------------------------------------------------

def _load_gym_ids(registry_path: str) -> list:
    """
    Load gym IDs from the registry JSON.  The registry is a list of dicts with
    at minimum an 'account_key' or 'gym_id' field.  If the file is absent or
    malformed, returns an empty list (job is a no-op for that run).
    """
    if not os.path.isfile(registry_path):
        logger.warning("dedupe_forward_book: registry not found at %s", registry_path)
        return []
    try:
        with open(registry_path) as f:
            data = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.error("dedupe_forward_book: could not read registry: %s", exc)
        return []
    ids = []
    for entry in (data if isinstance(data, list) else []):
        gid = entry.get("account_key") or entry.get("gym_id") or entry.get("id")
        if gid:
            ids.append(str(gid))
    return ids


# ---------------------------------------------------------------------------
# Future-pending row query (direct PostgREST, store has no cross-gym query)
# ---------------------------------------------------------------------------

def _future_pending_rows(store, gym_id: str, today_iso: str) -> list:
    """
    Return all 'pending' content_calendar rows for gym_id whose post_date is
    >= today_iso.  Retrieves id, gym_id, post_date, caption, status columns.

    Uses the store's internal _client() / _headers() / _rest() helpers so we
    never bypass the store's auth layer.
    """
    params = {
        "gym_id": f"eq.{gym_id}",
        "status": "eq.pending",
        "post_date": f"gte.{today_iso}",
        "select": "id,gym_id,post_date,caption,status",
        "order": "post_date.asc",
        "limit": "1000",
    }
    r = store._client().get(
        store._rest("content_calendar"),
        params=params,
        headers=store._headers(),
        timeout=30,
    )
    if r.status_code >= 400:
        logger.error(
            "dedupe_forward_book: query failed for gym %s: HTTP %s",
            gym_id, r.status_code,
        )
        return []
    return r.json() or []


# ---------------------------------------------------------------------------
# Core deduplication logic
# ---------------------------------------------------------------------------

DENY_STATUS = "denied"
DENY_REASON = "duplicate_purge_2026_08"


def _dedupe_gym(store, gym_id: str, today_iso: str, dry_run: bool) -> dict:
    """
    Deduplicate forward pending rows for one gym.

    Returns a summary dict:
        {gym_id, total_pending, duplicate_groups, rows_denied}
    """
    rows = _future_pending_rows(store, gym_id, today_iso)
    if not rows:
        return {"gym_id": gym_id, "total_pending": 0,
                "duplicate_groups": 0, "rows_denied": 0}

    # Group by caption_hash.  Within each group, sort by post_date asc so
    # the EARLIEST row survives (index 0).
    groups: dict[str, list] = {}
    for row in rows:
        h = caption_hash(row.get("caption") or "")
        groups.setdefault(h, []).append(row)

    # Sort each group by post_date so index 0 is the keeper.
    for h in groups:
        groups[h].sort(key=lambda r: str(r.get("post_date") or ""))

    rows_denied = 0
    dup_groups = 0

    for h, group in groups.items():
        if len(group) < 2:
            continue  # no duplicates in this group
        dup_groups += 1
        to_deny = group[1:]  # keep group[0], deny the rest
        for row in to_deny:
            row_id = row.get("id")
            if not row_id:
                logger.warning(
                    "dedupe_forward_book: row missing id for gym %s, skipping",
                    gym_id,
                )
                continue
            if dry_run:
                logger.info(
                    "[DRY-RUN] would deny row %s (gym=%s, date=%s, hash=%s)",
                    row_id, gym_id, row.get("post_date"), h,
                )
            else:
                try:
                    # set_status uses the store's isolation (gym_id filter + race guard)
                    store.set_status(gym_id, row_id, DENY_STATUS)
                    # Patch the reject_reason field via a separate update call.
                    _patch_reject_reason(store, gym_id, row_id, DENY_REASON)
                    logger.info(
                        "denied row %s (gym=%s, date=%s, hash=%s)",
                        row_id, gym_id, row.get("post_date"), h,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "dedupe_forward_book: failed to deny row %s (gym=%s): %s",
                        row_id, gym_id, exc,
                    )
                    continue
            rows_denied += 1

    return {
        "gym_id": gym_id,
        "total_pending": len(rows),
        "duplicate_groups": dup_groups,
        "rows_denied": rows_denied,
    }


def _patch_reject_reason(store, gym_id: str, row_id: str, reason: str) -> None:
    """
    PATCH reject_reason on the row, scoped by gym_id.
    A missing reject_reason column is tolerated silently (the deny itself already landed).
    """
    params = {
        "id": f"eq.{row_id}",
        "gym_id": f"eq.{gym_id}",
    }
    try:
        r = store._client().patch(
            store._rest("content_calendar"),
            params=params,
            headers=store._headers({
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }),
            json={"reject_reason": reason},
            timeout=30,
        )
        if r.status_code >= 400:
            logger.warning(
                "dedupe_forward_book: could not write reject_reason for row %s: HTTP %s",
                row_id, r.status_code,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dedupe_forward_book: reject_reason patch raised for row %s: %s",
            row_id, exc,
        )


# ---------------------------------------------------------------------------
# Logging / reporting
# ---------------------------------------------------------------------------

def _log_results(results: list, dry_run: bool) -> None:
    """
    Log per-gym counts and a total summary.  Called for both live and dry-run.
    """
    prefix = "[DRY-RUN] " if dry_run else ""
    total_denied = sum(r["rows_denied"] for r in results)
    total_groups = sum(r["duplicate_groups"] for r in results)
    gyms_with_dupes = [r for r in results if r["duplicate_groups"] > 0]

    logger.info(
        "%sdedupe_forward_book: scanned %d gym(s), %d duplicate group(s), %d row(s) %s",
        prefix,
        len(results),
        total_groups,
        total_denied,
        "would be denied" if dry_run else "denied",
    )
    for r in gyms_with_dupes:
        logger.info(
            "%s  gym=%s  pending=%d  dup_groups=%d  %s=%d",
            prefix,
            r["gym_id"],
            r["total_pending"],
            r["duplicate_groups"],
            "would_deny" if dry_run else "denied",
            r["rows_denied"],
        )

    # Slack / ops channel message (best-effort; failure does not abort the job)
    _try_slack_log(results, dry_run)


def _try_slack_log(results: list, dry_run: bool) -> None:
    """
    Post a summary to #echoclaude via the Slack bot token in env.
    Falls back silently if the token or channel is absent.
    """
    token = os.environ.get("AGENT_SLACK_BOT_TOKEN", "")
    channel = os.environ.get("AGENT_SLACK_CHANNEL_ID", "")
    if not token or not channel:
        return

    total_denied = sum(r["rows_denied"] for r in results)
    total_groups = sum(r["duplicate_groups"] for r in results)
    prefix = "[DRY-RUN] " if dry_run else ""
    lines = [
        f"{prefix}*dedupe_forward_book* complete: "
        f"{len(results)} gym(s) scanned, "
        f"{total_groups} duplicate group(s), "
        f"{total_denied} row(s) {'would be ' if dry_run else ''}denied",
    ]
    for r in results:
        if r["duplicate_groups"] > 0:
            verb = "would deny" if dry_run else "denied"
            lines.append(
                f"  gym={r['gym_id']} "
                f"pending={r['total_pending']} "
                f"dup_groups={r['duplicate_groups']} "
                f"{verb}={r['rows_denied']}"
            )

    text = "\n".join(lines)
    try:
        import urllib.request
        import urllib.error
        body = json.dumps({"channel": channel, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
    except Exception as exc:  # noqa: BLE001
        logger.warning("dedupe_forward_book: slack post failed: %s", exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> list:
    """
    Deduplicate future pending rows across all gyms in the registry.

    Returns a list of per-gym result dicts:
        [{gym_id, total_pending, duplicate_groups, rows_denied}, ...]

    Guarded by AGENT_DEDUPE_FORWARD_BOOK (default OFF). When the flag is off the
    function logs a warning and returns [] without touching anything.
    """
    if not _enabled():
        logger.warning(
            "dedupe_forward_book: AGENT_DEDUPE_FORWARD_BOOK is OFF — set to 'true' to arm"
        )
        return []

    from agent import portal_calendar_store as pcs  # noqa: PLC0415
    from agent import config as _cfg  # noqa: PLC0415

    store = pcs.SupabaseCalendarStore()
    today_iso = date.today().isoformat()

    registry_path = _cfg.gym_registry_path()
    gym_ids = _load_gym_ids(registry_path)

    if not gym_ids:
        logger.warning(
            "dedupe_forward_book: no gym IDs found in registry at %s", registry_path
        )
        return []

    results = []
    for gym_id in gym_ids:
        result = _dedupe_gym(store, gym_id, today_iso, dry_run)
        results.append(result)

    _log_results(results, dry_run)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Deduplicate the forward content_calendar for all gyms."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log what WOULD be changed without writing anything.",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    _main()
