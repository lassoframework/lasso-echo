#!/usr/bin/env python3
"""One-time, Pierce-only Sep 19-Oct 18 reset and first weekly stage.

Run on the Echo worker with its /data volume after AGENT_PIERCE_WEEKLY=true is
deployed. The JSON backup contains the complete rows and is mode 0600.
"""
import hashlib
import json
import os
import sys
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "piercefitness"
FIRST = "2026-09-19"
LAST = "2026-10-18"
BACKUP = Path("/data/pierce_calendar_reset_20260918.json")
WIPEABLE = {"approved", "denied", "pending", "draft", "queued"}


def target_rows(store):
    rows = (store.list_month(BASE, "2026-09") or []) + (store.list_month(BASE, "2026-10") or [])
    return sorted((r for r in rows if FIRST <= str(r.get("post_date", ""))[:10] <= LAST
                   and str(r.get("status") or "").lower() in WIPEABLE
                   and r.get("variant_status", "active") == "active"),
                  key=lambda r: str(r["id"]))


def main():
    from agent import config
    from agent.portal_calendar_store import SupabaseCalendarStore

    pierce_today = datetime.now(ZoneInfo("America/Toronto")).date()
    if pierce_today > date(2026, 9, 19):
        raise SystemExit("Date guard: first weekly block has already started")
    if not config.portal_calendar_supabase_enabled():
        raise SystemExit("No live calendar store")
    if os.getenv("AGENT_PIERCE_WEEKLY", "").lower() not in ("1", "true", "yes", "on"):
        raise SystemExit("Weekly Pierce gate is not armed on the worker")
    store = SupabaseCalendarStore()
    if pierce_today == date(2026, 9, 19):
        today_rows = [r for r in store.list_month(BASE, "2026-09")
                      if str(r.get("post_date", ""))[:10] == FIRST]
        if any(r.get("status") in ("published", "publishing", "failed")
               for r in today_rows):
            raise SystemExit("Sep 19 publishing has begun; refusing the timed reset")
    stage_only = "--stage-only" in sys.argv
    if stage_only:
        if not BACKUP.exists():
            raise SystemExit("No prior backup for stage-only recovery")
        saved = json.loads(BACKUP.read_text())
        if saved.get("gym_id") != BASE or saved.get("first") != FIRST or saved.get("last") != LAST:
            raise SystemExit("Backup scope mismatch")
        before = saved["rows"]
        if target_rows(store):
            raise SystemExit("Future rows already exist; refusing stage-only recovery")
        print(f"Recovery from {BACKUP}, {len(before)} original rows")
    else:
        before = target_rows(store)
    print(f"Pierce {FIRST}..{LAST}: {len(before)} removable rows; "
          f"statuses={{{', '.join(sorted(set(str(r.get('status')) for r in before)))}}}")
    if "--dry-run" in sys.argv:
        return
    if stage_only:
        print("Existing backup retained; staging the first week")
    else:
        _clear_future(store, before)

    from agent.client_media_sync import scan_and_generate
    result = scan_and_generate(clients=[BASE], store=store, now=date(2026, 9, 18),
                               days=7, logger=lambda m: print(m))
    print(f"Weekly scan: {result}")
    if not result.get("ok") or not result.get("generated"):
        raise SystemExit("First weekly build did not stage; backup retained for recovery")
    audit_first_week(store, before)


def _clear_future(store, before):
    if not before:
        raise SystemExit("No rows matched; refusing an ambiguous reset")
    if BACKUP.exists():
        raise SystemExit(f"Backup already exists at {BACKUP}; refusing repeat")
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(BACKUP, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"gym_id": BASE, "first": FIRST, "last": LAST,
                   "rows": before}, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    digest = hashlib.sha256(BACKUP.read_bytes()).hexdigest()
    print(f"Backup: {BACKUP}; sha256={digest}; rows={len(before)}")

    # Delete by the exact backed-up IDs, scoped again by gym and future date.
    # If a publisher/coach changes a row after backup, its status no longer
    # matches and this script stops before staging anything new.
    ids = [r["id"] for r in before]
    r = store._client().delete(
        store._rest("content_calendar"),
        params={"gym_id": f"eq.{BASE}",
                "id": f"in.({','.join(ids)})",
                "post_date": [f"gte.{FIRST}", f"lte.{LAST}"],
                "status": "in.(approved,denied,pending,draft,queued)",
                "variant_status": "eq.active"},
        headers=store._headers({"Prefer": "return=representation"}),
        timeout=30)
    if r.status_code >= 400:
        raise SystemExit(f"Delete failed with HTTP {r.status_code}; backup retained")
    deleted = r.json() or []
    if {str(row["id"]) for row in deleted} != {str(i) for i in ids}:
        raise SystemExit(f"Partial delete: {len(deleted)}/{len(ids)}; backup retained")
    if target_rows(store):
        raise SystemExit("Future rows remain after delete; backup retained")
    print(f"Deleted {len(deleted)} backed-up Pierce rows")

def audit_first_week(store, before):
    rows = target_rows(store)
    feeds = [r for r in rows if r.get("format") in ("feed", "reel")
             and r.get("account") == "instagram"
             and FIRST <= str(r.get("post_date", ""))[:10] <= "2026-09-25"]
    if not feeds or any(r.get("status") != "pending" for r in feeds):
        raise SystemExit("Persisted first-week feed rows are absent or not pending")
    # Platform mirrors share copy by design. Compare one canonical IG feed per
    # concept/date against other dates and the removed month's old feed copy.
    captions = [(str(r.get("post_date")), " ".join((r.get("caption") or "").split()))
                for r in feeds]
    if any(not copy for _, copy in captions):
        raise SystemExit("A persisted first-week caption is blank")
    stale = {" ".join((r.get("caption") or "").split())
             for r in before if r.get("format") in ("feed", "reel")}
    repeated_old = [(day, copy[:60]) for day, copy in captions if copy in stale]
    similar = [(captions[i][0], captions[j][0])
               for i in range(len(captions)) for j in range(i + 1, len(captions))
               if captions[i][0] != captions[j][0]
               and SequenceMatcher(None, captions[i][1].lower(),
                                   captions[j][1].lower()).ratio() >= .88]
    print(f"Persisted first-week IG feeds: {len(feeds)}; distinct captions: "
          f"{len({copy for _, copy in captions})}; stale repeats: {len(repeated_old)}; "
          f"near-duplicates across dates: {len(similar)}")
    if repeated_old or similar or len({copy for _, copy in captions}) < len(captions):
        raise SystemExit("Caption diversity check failed; do not notify Bryan")


if __name__ == "__main__":
    main()
