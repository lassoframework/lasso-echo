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
  client_sources - per-account approved/pending source docs (AGENT_CLIENT_SOURCES)

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
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now')),
  day TEXT, account_key TEXT, kind TEXT, subject TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS client_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_key TEXT, category TEXT,
  text TEXT, citation TEXT, status TEXT DEFAULT 'approved',
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS gyms (
  account_key TEXT PRIMARY KEY,
  display_name TEXT DEFAULT '',
  gym_name TEXT,
  intake_token_hash TEXT,
  token_sha256 TEXT,
  token_rotated_at TEXT,
  token_revoked INTEGER DEFAULT 0,
  token_status TEXT DEFAULT 'NOT_SET',
  intake_token_encrypted TEXT,
  upload_link TEXT,
  publish_flag TEXT DEFAULT 'OFF',
  publish_creds TEXT DEFAULT 'NOT SET (by hand)',
  publish_creds_status TEXT DEFAULT 'NOT SET (by hand)',
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
"""


def db_path():
    p = os.environ.get("AGENT_DB_PATH")
    if p:
        return p
    data_dir = os.environ.get("AGENT_DATA_DIR", "/data")
    if os.path.isdir(data_dir):
        return os.path.join(data_dir, "echo.db")
    return "echo.db"  # local dev fallback; production has the volume


_POST_METRIC_COLUMNS = ("likes", "comments", "saves", "shares", "views", "reach")


def connect(path=None):
    """A WAL-mode connection with the schema ensured. Callers close it."""
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    # additive column migration: per-post metrics for reporting (VIEWS, never an
    # impressions column, by design)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
    for col in _POST_METRIC_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {col} INTEGER")
    # additive gyms migration: intake_token_encrypted added for reversible
    # encryption at rest (AGENT_INTAKE_ENC_KEY); existing rows stay as-is.
    gyms_have = {r["name"] for r in conn.execute("PRAGMA table_info(gyms)")}
    if "intake_token_encrypted" not in gyms_have:
        try:
            conn.execute("ALTER TABLE gyms ADD COLUMN intake_token_encrypted TEXT")
        except Exception:
            pass
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


def audit(kind, subject, reason, account_key="", day=""):
    """APPEND-ONLY decision trail: why the agent did what it did. Always on (no
    flag: logging truth is not optional). Reasons pass through the secret scrub
    so tokens and key material can never land in the table. Never raises."""
    try:
        from . import ops_alerts
        with _lock, connect() as conn:
            conn.execute(
                "INSERT INTO audit (day, account_key, kind, subject, reason) "
                "VALUES (?,?,?,?,?)",
                (day, account_key, str(kind)[:40], str(subject)[:200],
                 ops_alerts.scrub(str(reason))[:500]))
            conn.commit()
    except Exception as e:
        print(f"[audit] write failed: {type(e).__name__}: {e}")


def gym_upsert(account_key, display_name='', **fields):
    """INSERT OR REPLACE into gyms with the given fields. Never stores a raw token.
    fields: any subset of the gyms columns except account_key and created_at."""
    allowed = {
        'display_name', 'gym_name', 'intake_token_hash', 'token_rotated_at',
        'token_revoked', 'intake_token_encrypted', 'upload_link', 'publish_flag',
        'publish_creds', 'publish_creds_status',
    }
    extra_cols = []
    extra_vals = []
    for k, v in fields.items():
        if k in allowed and k != 'display_name':
            extra_cols.append(k)
            extra_vals.append(v)

    all_cols = ['account_key', 'display_name'] + extra_cols
    all_vals = [account_key, display_name] + extra_vals

    placeholders = ', '.join(['?'] * len(all_cols)) + ", datetime('now')"
    col_str = ', '.join(all_cols) + ', updated_at'

    update_parts = [f"{c} = excluded.{c}" for c in all_cols if c != 'account_key']
    update_parts.append("updated_at = datetime('now')")
    update_str = ', '.join(update_parts)

    sql = (
        f"INSERT INTO gyms ({col_str}) VALUES ({placeholders}) "
        f"ON CONFLICT(account_key) DO UPDATE SET {update_str}"
    )
    with _lock, connect() as conn:
        conn.execute(sql, all_vals)
        conn.commit()


def gym_get(account_key, conn=None):
    """Returns the gyms row as a dict, or None. Accepts an optional open connection."""
    def _get(c):
        row = c.execute(
            "SELECT * FROM gyms WHERE account_key = ?", (account_key,)
        ).fetchone()
        return dict(row) if row else None

    if conn is not None:
        return _get(conn)
    with connect() as c:
        return _get(c)


def gym_list(conn=None):
    """Returns all gyms rows as list of dicts, ordered by account_key.
    Accepts an optional open connection."""
    def _list(c):
        return [dict(r) for r in c.execute(
            "SELECT * FROM gyms ORDER BY account_key"
        ).fetchall()]

    if conn is not None:
        return _list(conn)
    with connect() as c:
        return _list(c)


def audit_rows(day=None, account_key=None, limit=500):
    q = "SELECT ts, day, account_key, kind, subject, reason FROM audit WHERE 1=1"
    params = []
    if day:
        q += " AND day=?"
        params.append(day)
    if account_key:
        q += " AND account_key=?"
        params.append(account_key)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]
