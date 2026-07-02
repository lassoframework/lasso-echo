"""
Store backup + restore (ops safety). Flag: AGENT_BACKUP_ENABLED (default OFF).

Nightly (AGENT_BACKUP_HOUR_UTC, default 2): a CONSISTENT snapshot of
/data/echo.db via the sqlite backup API (safe alongside WAL writers) lands in
R2 under echo/backups/echo_<UTC stamp>.db, then a retention sweep deletes
backups older than AGENT_BACKUP_RETENTION_DAYS (default 14). ONE ops alert on
failure only; success is silent.

Restore is MANUAL and guarded:
    /opt/venv/bin/python -m agent restore-store --from <r2 key> [--confirm]
downloads to a STAGING path and prints per-table verification counts. It NEVER
overwrites the live db without --confirm (and even then the live db is kept as
.pre_restore.bak first).
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config, db, ops_alerts

BACKUP_PREFIX = "echo/backups/"


def _r2():
    from .intake_ingest import _default_r2
    return _default_r2()


def _snapshot_bytes():
    """A consistent point-in-time copy of the live db (sqlite backup API)."""
    staging = db.db_path() + ".backup_snapshot"
    src = sqlite3.connect(db.db_path())
    dst = sqlite3.connect(staging)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(staging, "rb") as fh:
        data = fh.read()
    os.remove(staging)
    return data


def _stamp_of(key):
    """Parse the UTC stamp out of echo/backups/echo_<stamp>.db, or None."""
    base = os.path.basename(key)
    if not (base.startswith("echo_") and base.endswith(".db")):
        return None
    try:
        return datetime.strptime(base[5:-3], "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def backup_now(r2=None, now=None):
    """One backup + retention sweep. Returns the new key, or None on failure
    (with ONE ops alert). Success is silent by design."""
    now = now or datetime.now(timezone.utc)
    try:
        r2 = r2 or _r2()
        if r2 is None:
            raise RuntimeError("R2 credentials not configured")
        key = f"{BACKUP_PREFIX}echo_{now.strftime('%Y%m%dT%H%M%S')}.db"
        r2.put_bytes(key, _snapshot_bytes(), content_type="application/octet-stream")
        retention = int(os.environ.get("AGENT_BACKUP_RETENTION_DAYS", "14"))
        cutoff = now - timedelta(days=retention)
        for old_key in r2.list_keys(BACKUP_PREFIX):
            stamp = _stamp_of(old_key)
            if stamp is not None and stamp < cutoff:
                r2.delete(old_key)
        return key
    except Exception as e:
        ops_alerts.alert(f"store backup failed: {type(e).__name__}: {e}")
        return None


def maybe_backup(now=None, r2=None):
    """The nightly trigger: once per day at the backup hour, persisted mark.
    Fully inert while AGENT_BACKUP_ENABLED is OFF."""
    if not config.backup_enabled():
        return None
    now = now or datetime.now(timezone.utc)
    hour = int(os.environ.get("AGENT_BACKUP_HOUR_UTC", "2"))
    if now.hour != hour:
        return None
    today = now.date().isoformat()
    if db.kv_get("backup_done_date") == today:
        return None
    key = backup_now(r2=r2, now=now)
    if key:
        db.kv_set("backup_done_date", today)
    return key


def restore_store(from_key, r2=None, confirm=False):
    """
    Restore to STAGING, verify, and only replace the live db with --confirm
    (the live db is kept as .pre_restore.bak). Returns the verification counts.
    """
    r2 = r2 or _r2()
    if r2 is None:
        print("restore-store: R2 credentials not configured.")
        return None
    data = r2.get_bytes(from_key)
    staging = db.db_path() + ".restore_staging"
    with open(staging, "wb") as fh:
        fh.write(data)

    counts = {}
    conn = sqlite3.connect(staging)
    try:
        for table in ("drafts", "posts", "served", "snapshots", "audit"):
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = "missing"
    finally:
        conn.close()
    print(f"restore-store: verified staging copy at {staging}")
    for table, n in counts.items():
        print(f"  {table}: {n}")

    if not confirm:
        print("restore-store: live db UNCHANGED (pass --confirm to replace it; "
              "the current live db would be kept as .pre_restore.bak).")
        return counts

    live = db.db_path()
    if os.path.exists(live):
        os.replace(live, live + ".pre_restore.bak")
    os.replace(staging, live)
    print(f"restore-store: live db replaced from {from_key} "
          "(previous db kept as .pre_restore.bak)")
    return counts
