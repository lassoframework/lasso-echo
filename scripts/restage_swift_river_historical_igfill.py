#!/usr/bin/env python3
"""Plan or restage Swift River's Sep 2-Oct 2 2026 pending igfill cards.

This is an evidence-only dry run. Apply is disabled until a server-side operation
can atomically reserve a photo and compare-and-swap its complete day of rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "swiftrivercrossfite5c9db"
_IG_KEY = f"{_BASE}_ig"
_START = date(2026, 9, 2)
_END = date(2026, 10, 2)
_TARGET_ROWS = 24
_TARGET_DAYS = 17
_TARGET_HELD_ROWS = 1
_BOOK_PADDING_DAYS = 90
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _row_digest(rows):
    """Hash every field of each target row, so any row edit invalidates the plan."""
    canonical = sorted((dict(row) for row in rows), key=lambda row: str(row.get("id") or ""))
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode()).hexdigest()


def _is_target(row):
    if str(row.get("gym_id") or "") != _BASE:
        return False
    if str(row.get("status") or "").lower() != "pending":
        return False
    try:
        day = date.fromisoformat(str(row.get("post_date") or "")[:10])
    except ValueError:
        return False
    if not (_START <= day <= _END):
        return False
    return any("igfill_" in urlparse(str(value or "")).path.lower()
               for value in (row.get("image_url"), row.get("source_media_url")))


def _target_rows(rows):
    target = [row for row in rows if _is_target(row)]
    days = {str(row.get("post_date") or "")[:10] for row in target}
    ids = [str(row.get("id") or "") for row in target]
    if (len(target) != _TARGET_ROWS or len(days) != _TARGET_DAYS
            or sum(row.get("media_not_ready_reason") is not None for row in target)
            != _TARGET_HELD_ROWS
            or not all(ids) or len(set(ids)) != len(ids)):
        return None
    return sorted(target, key=lambda row: (str(row.get("post_date")), str(row.get("id"))))


def _months_for_book(rows):
    dates = [date.fromisoformat(str(row["post_date"])[:10]) for row in rows]
    first = min(dates) - timedelta(days=_BOOK_PADDING_DAYS)
    last = max(dates) + timedelta(days=_BOOK_PADDING_DAYS)
    months, cursor = set(), date(first.year, first.month, 1)
    while cursor <= last:
        months.add(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return sorted(months)


def _calendar_rows(store, months):
    rows = []
    for month in months:
        batch = store.list_month(_BASE, month)
        if batch is None:
            raise RuntimeError(f"calendar read returned no result for {month}")
        rows.extend(batch)
    return rows


def _consumed_asset_ids(*, ledger_rows=None):
    """Return every asset with an active gym_media_use record; malformed reads fail."""
    if ledger_rows is None:
        from agent import db
        path = db.db_path()
        if not os.path.isfile(path):
            raise RuntimeError("usage ledger database is unavailable")
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        prefix = f"gym_media_use:{_BASE}:"
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            ledger_rows = conn.execute(
                "SELECT key, value FROM kv WHERE key >= ? AND key < ?",
                (prefix, upper)).fetchall()
    prefix = f"gym_media_use:{_BASE}:"
    consumed = set()
    for item in ledger_rows:
        key = item["key"] if hasattr(item, "keys") else item[0]
        value = item["value"] if hasattr(item, "keys") else item[1]
        if not str(key).startswith(prefix):
            raise ValueError("usage ledger returned a foreign key")
        day = str(key)[len(prefix):]
        if not _DAY_RE.fullmatch(day) or date.fromisoformat(day).isoformat() != day:
            raise ValueError("usage ledger contains an invalid date key")
        try:
            records = json.loads(value or "[]")
        except Exception as exc:
            raise ValueError("usage ledger contains unreadable JSON") from exc
        if isinstance(records, dict):
            records = [records] if records else []
        if not isinstance(records, list):
            raise ValueError("usage ledger value is not a record list")
        for record in records:
            if (not isinstance(record, dict) or record.get("gym_id") != _BASE
                    or not isinstance(record.get("asset_id"), str)
                    or not record["asset_id"].strip()
                    or not isinstance(record.get("rolled_back"), bool)):
                raise ValueError("usage ledger contains a malformed record")
            if not record["rolled_back"]:
                consumed.add(record["asset_id"])
    return consumed


def _book_asset_ids(rows):
    return {str(row["source_media_asset_id"]) for row in rows
            if row.get("source_media_asset_id")}


def _ready_drive_sources(media_store):
    """Read source provenance; unknown or duplicate source identities cannot qualify."""
    sources = media_store.list_sources(_BASE, include_inactive=True)
    if not isinstance(sources, list):
        raise ValueError("source read did not return a list")
    by_id = {}
    for source in sources:
        if not isinstance(source, dict) or not str(source.get("id") or "").strip():
            raise ValueError("source read contains a malformed row")
        source_id = str(source["id"])
        if source_id in by_id:
            raise ValueError("source read contains duplicate identities")
        by_id[source_id] = source
    return {source_id for source_id, source in by_id.items()
            if source.get("gym_id") == _BASE
            and source.get("kind") == "gym_drive"
            and source.get("active") is True
            and source.get("revoked_externally") is False
            and source.get("sync_status") == "ready"}


def _fresh_photos(media_store, blocked_ids, consumed_ids, ready_sources):
    from agent import gym_media_selector

    assets = gym_media_selector.pickable(_BASE, "photo", store=media_store)
    result, seen = [], set()
    for asset in assets:
        asset_id = str(asset.get("id") or "")
        used_count = asset.get("used_count")
        if (not asset_id or asset_id in seen or asset_id in blocked_ids
                or asset_id in consumed_ids or asset.get("kind") != "photo"
                or asset.get("gym_id") != _BASE
                or str(asset.get("source_id") or "") not in ready_sources
                or not gym_media_selector.is_usable(asset)
                or isinstance(used_count, bool) or not isinstance(used_count, int)
                or used_count != 0 or "last_used_at" not in asset
                or asset.get("last_used_at") is not None):
            continue
        result.append(asset)
        seen.add(asset_id)
    return result


def _groups(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(str(row["post_date"])[:10], []).append(row)
    return {day: sorted(group, key=lambda row: str(row["id"]))
            for day, group in sorted(grouped.items())}


def _plan(rows, photos):
    days = _groups(rows)
    if len(photos) < len(days):
        return None
    return [{"day": day, "asset_id": str(photos[index]["id"]),
             "row_ids": [str(row["id"]) for row in group],
             "held_row_ids": [str(row["id"]) for row in group
                              if row.get("media_not_ready_reason") is not None]}
            for index, (day, group) in enumerate(days.items())]


def _resolve():
    from agent import accounts, config
    from agent.portal_calendar_store import SupabaseCalendarStore
    from agent.media_source_store import default_store

    account = accounts.get_account(_IG_KEY)
    calendar = SupabaseCalendarStore() if config.portal_calendar_supabase_enabled() else None
    return {"account": account, "calendar": calendar, "media": default_store()}


_APPLY_BLOCKER = (
    "apply disabled: no atomic per-day operation guards complete row identity, "
    "active source and asset/book/ledger eligibility, and usage reservation; "
    "a durable day receipt and partial-day resume protocol are also required"
)


def run(*, apply=False, expected_digest=None, ctx=None, ledger_rows=None):
    if apply:
        # Do not even resolve credentials or read mutable state for an unsafe apply.
        # A digest proves a snapshot, not write-time exclusivity or resumability.
        return {"ok": False, "dry_run": False, "reason": _APPLY_BLOCKER,
                "writes_attempted": 0}
    ctx = ctx or _resolve()
    calendar, media = ctx.get("calendar"), ctx.get("media")
    if ctx.get("account") is None or calendar is None or media is None:
        return {"ok": False, "reason": "missing account or calendar/media store"}
    if not media.available():
        return {"ok": False, "reason": "media store unavailable"}
    try:
        initial_months = ["2026-09", "2026-10"]
        initial_rows = _calendar_rows(calendar, initial_months)
        target = _target_rows(initial_rows)
        if target is None:
            return {"ok": False, "reason": "exact 24-row, 17-day historical target absent"}
        digest = _row_digest(target)
        book = _calendar_rows(calendar, _months_for_book(target))
        consumed = _consumed_asset_ids(ledger_rows=ledger_rows)
    except Exception as exc:
        return {"ok": False, "reason": f"read/ledger failure:{type(exc).__name__}"}
    try:
        ready_sources = _ready_drive_sources(media)
        photos = _fresh_photos(media, _book_asset_ids(book), consumed, ready_sources)
    except Exception as exc:
        return {"ok": False, "reason": f"photo eligibility read failed:{type(exc).__name__}",
                "target_digest": digest}
    plan = _plan(target, photos)
    if plan is None:
        return {"ok": False, "reason": "fewer than 17 approved never-used photos outside book/ledger",
                "target_digest": digest, "target_rows": len(target),
                "target_row_ids": [str(row["id"]) for row in target],
                "available_photos": len(photos)}
    report = {"ok": True, "dry_run": True, "plan_is_reservation": False,
              "apply_available": False,
              "apply_blocker": _APPLY_BLOCKER, "target_digest": digest,
              "target_rows": len(target), "target_days": len(plan),
              "target_row_ids": [str(row["id"]) for row in target], "plan": plan}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="report the blocked apply safety gate")
    parser.add_argument("--expected-digest", help="reserved for a future guarded apply path")
    args = parser.parse_args(argv)
    result = run(apply=args.apply, expected_digest=args.expected_digest)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
