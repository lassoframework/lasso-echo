"""
SQLite store on the /data volume (Tier 2 foundation).

One database, /data/echo.db (env AGENT_DB_PATH overrides; falls back to ./echo.db
when the volume is absent, e.g. local dev), holding:

  drafts     - the pending-draft store (was pending_drafts.json)
  posts      - everything published or would-published (was post_log.jsonl),
               with creative key / archetype / set / permalink columns reporting reads
  served     - the rotation served log (was rotation_served.json)
  snapshots  - daily per-account metric snapshots (filled by the reporting job)
  counters   - per-day counters (generation spend cap etc.)
  kv         - small key/value state (debounce stamps, digest marks)

WAL journal mode so the listener's threads (scheduler, ingest, approvals) write
concurrently without corruption; every write is idempotent (INSERT OR REPLACE /
dedupe keys). This is a STORAGE SWAP: no behavior change to any flow. On first
run each legacy json file is migrated in and renamed to <name>.migrated.bak.
NOTHING here ever stores a token.
"""

import json
import os
import sqlite3
import threading

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
  draft_id TEXT PRIMARY KEY, account_key TEXT, status TEXT,
  day_key TEXT, draft_type TEXT, data TEXT, updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT, account_key TEXT,
  platform TEXT, caption TEXT, media_id TEXT, permalink TEXT, mode TEXT,
  creative_key TEXT, archetype TEXT, set_name TEXT, published_at TEXT);
CREATE TABLE IF NOT EXISTS served (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_key TEXT, key TEXT,
  pillar TEXT, date TEXT, archetype TEXT, set_name TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_key TEXT, date TEXT,
  metrics TEXT, UNIQUE(account_key, date));
CREATE TABLE IF NOT EXISTS counters (
  name TEXT, day TEXT, count INTEGER DEFAULT 0, PRIMARY KEY (name, day));
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path():
    p = os.environ.get("AGENT_DB_PATH")
    if p:
        return p
    data_dir = os.environ.get("AGENT_DATA_DIR", "/data")
    if os.path.isdir(data_dir):
        return os.path.join(data_dir, "echo.db")
    return "echo.db"  # local dev fallback; production has the volume


def connect(path=None):
    """A WAL-mode connection with the schema ensured. Callers close it."""
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _backup(path):
    try:
        os.replace(path, path + ".migrated.bak")
    except OSError:
        pass


def migrate_legacy(conn, pending_json=None, served_json=None, postlog_jsonl=None):
    """One-time import of the legacy json state files (each only when its table is
    still empty and the file exists); the originals are kept as .migrated.bak."""
    cur = conn.cursor()

    if pending_json and os.path.exists(pending_json):
        if cur.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0:
            try:
                with open(pending_json, encoding="utf-8") as fh:
                    data = json.load(fh) or {}
                for draft_id, rec in data.items():
                    cur.execute(
                        "INSERT OR REPLACE INTO drafts "
                        "(draft_id, account_key, status, day_key, draft_type, data) "
                        "VALUES (?,?,?,?,?,?)",
                        (draft_id, rec.get("account_key", ""), rec.get("status", ""),
                         rec.get("day_key", ""), rec.get("draft_type", ""),
                         json.dumps(rec)))
                conn.commit()
                _backup(pending_json)
            except Exception as e:
                print(f"[db] pending migration skipped: {type(e).__name__}: {e}")

    if served_json and os.path.exists(served_json):
        if cur.execute("SELECT COUNT(*) FROM served").fetchone()[0] == 0:
            try:
                with open(served_json, encoding="utf-8") as fh:
                    served = json.load(fh) or {}
                for account_key, entries in served.items():
                    for e in entries:
                        cur.execute(
                            "INSERT INTO served (account_key, key, pillar, date, "
                            "archetype, set_name) VALUES (?,?,?,?,?,?)",
                            (account_key, e.get("key", ""), e.get("pillar", ""),
                             e.get("date", ""), e.get("archetype", ""),
                             e.get("set", "")))
                conn.commit()
                _backup(served_json)
            except Exception as e:
                print(f"[db] served migration skipped: {type(e).__name__}: {e}")

    if postlog_jsonl and os.path.exists(postlog_jsonl):
        if cur.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
            try:
                with open(postlog_jsonl, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        r = json.loads(line)
                        cur.execute(
                            "INSERT INTO posts (draft_id, account_key, platform, "
                            "caption, media_id, mode, published_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (r.get("draft_id", ""), r.get("account_key", ""),
                             r.get("platform", ""), r.get("caption", ""),
                             r.get("media_id", ""), r.get("mode", ""),
                             r.get("published_at", "")))
                conn.commit()
                _backup(postlog_jsonl)
            except Exception as e:
                print(f"[db] postlog migration skipped: {type(e).__name__}: {e}")


# ---- tiny helpers the modules share ------------------------------------------------
def kv_get(key, default=""):
    with _lock, connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key, value):
    with _lock, connect() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                     (key, str(value)))
        conn.commit()


def counter_bump(name, day):
    """Increment and return the (name, day) counter. Idempotent schema, atomic."""
    with _lock, connect() as conn:
        conn.execute(
            "INSERT INTO counters (name, day, count) VALUES (?,?,1) "
            "ON CONFLICT(name, day) DO UPDATE SET count = count + 1", (name, day))
        conn.commit()
        return conn.execute("SELECT count FROM counters WHERE name=? AND day=?",
                            (name, day)).fetchone()["count"]


def counter_get(name, day):
    with _lock, connect() as conn:
        row = conn.execute("SELECT count FROM counters WHERE name=? AND day=?",
                           (name, day)).fetchone()
        return row["count"] if row else 0
