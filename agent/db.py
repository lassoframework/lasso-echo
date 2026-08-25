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
CREATE TABLE IF NOT EXISTS pre_echo_baselines (
  account_key TEXT PRIMARY KEY,
  locked_at TEXT NOT NULL,
  pre_echo_cutoff TEXT,
  window_start TEXT,
  window_end TEXT,
  posts_count INTEGER,
  weeks_in_window REAL,
  avg_posts_per_week REAL,
  confidence TEXT,
  confidence_note TEXT
);
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
CREATE TABLE IF NOT EXISTS consent_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_path TEXT NOT NULL,
  action TEXT NOT NULL,
  member_ref TEXT DEFAULT '',
  granted_by TEXT DEFAULT '',
  note TEXT DEFAULT '',
  recorded_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS socialapi_claims (
  draft_id TEXT, account_key TEXT, status TEXT DEFAULT 'in_flight',
  post_id TEXT DEFAULT '', claimed_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (draft_id, account_key));
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
    # additive gyms migration: Zernio per-gym profile binding + chosen default Facebook Page.
    # zernio_profile_id maps a gym to its Zernio profile (tenant boundary); the FB page id is the
    # gym's chosen Page, which Echo owns and injects per post. Existing rows stay null.
    if "zernio_profile_id" not in gyms_have:
        try:
            conn.execute("ALTER TABLE gyms ADD COLUMN zernio_profile_id TEXT")
        except Exception:
            pass
    if "zernio_default_fb_page_id" not in gyms_have:
        try:
            conn.execute("ALTER TABLE gyms ADD COLUMN zernio_default_fb_page_id TEXT")
        except Exception:
            pass
    # additive gyms migration (Part A): trailing-90d posting baseline captured at
    # onboarding. baseline_posts_per_week is the number Echo's before/after story
    # reads from; baseline_captured_at timestamps when it was set. Existing rows
    # stay null (no baseline) until a setter fills them.
    if "baseline_posts_per_week" not in gyms_have:
        try:
            conn.execute("ALTER TABLE gyms ADD COLUMN baseline_posts_per_week REAL")
        except Exception:
            pass
    if "baseline_captured_at" not in gyms_have:
        try:
            conn.execute("ALTER TABLE gyms ADD COLUMN baseline_captured_at TEXT")
        except Exception:
            pass
    # additive gyms migration (Part B): the gym's Stripe customer id, used to check
    # whether the gym's SOCIAL product subscription is ACTIVE before the portal
    # serves a live calendar. Set by hand / by onboarding; existing rows stay null
    # (a null customer id reads as not-active, fail closed). Never a token or secret.
    if "stripe_customer_id" not in gyms_have:
        try:
            conn.execute("ALTER TABLE gyms ADD COLUMN stripe_customer_id TEXT")
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


# ---- per-account autonomy flag ----------------------------------------------------
# A gym owner flips "Autonomous" in the portal. When ON, Echo stops requiring a
# per-post approval for THAT account: currently-pending posts are auto-approved and
# future generated posts land as approved (still gated by AGENT_PUBLISH_ENABLED for
# the actual publish). Stored in the shared kv table, keyed per account_key, so the
# flag is durable across restarts and scoped to one gym (gym A's flag never touches
# gym B's). NEVER a token or secret. Default (no row) = manual/approve-each.

def _autonomy_key(account_key):
    return f"portal_autonomy_{account_key or ''}"


def set_autonomy(account_key, on):
    """Persist the autonomy flag for one account. on truthy -> "1" (autonomous),
    falsy -> "0" (manual). Null-safe: an empty account_key is a harmless no-op key."""
    kv_set(_autonomy_key(account_key), "1" if on else "0")


def is_autonomous(account_key):
    """True iff the account's autonomy flag is ON. Null-safe: a missing row, an empty
    account_key, or an unreadable value reads as False (manual, the safe default)."""
    if not account_key:
        return False
    try:
        return str(kv_get(_autonomy_key(account_key), "0")).strip() == "1"
    except Exception:
        return False


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


def socialapi_claim(draft_id, account_key):
    """Atomically claim the right to publish this draft on the SocialAPI lane.

    Returns one of:
      ("won", "")           caller owns the claim; proceed to publish
      ("in_flight", pid)    another publish holds the claim (pid may be "" if it
                            has not yet reached the vendor, or the vendor post id
                            if a prior attempt got that far but did not finish)
      ("done", pid)         already published; caller must NOT publish again

    The PRIMARY KEY on (draft_id, account_key) makes the INSERT the atomic
    single-winner across threads AND processes: a concurrent second caller hits
    IntegrityError and reads back the existing row. This closes the double-post
    race where the posts row is only written later by approvals. Raises on a real
    DB error so the caller can fail SAFE (hold, never publish blind)."""
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT status, post_id FROM socialapi_claims "
            "WHERE draft_id=? AND account_key=?",
            (draft_id, account_key)).fetchone()
        if row is not None:
            if row["status"] == "done":
                return ("done", row["post_id"] or "")
            return ("in_flight", row["post_id"] or "")
        try:
            conn.execute(
                "INSERT INTO socialapi_claims (draft_id, account_key, status) "
                "VALUES (?,?, 'in_flight')", (draft_id, account_key))
            conn.commit()
            return ("won", "")
        except sqlite3.IntegrityError:
            # A concurrent caller won the race between our SELECT and INSERT.
            row = conn.execute(
                "SELECT status, post_id FROM socialapi_claims "
                "WHERE draft_id=? AND account_key=?",
                (draft_id, account_key)).fetchone()
            if row is not None and row["status"] == "done":
                return ("done", row["post_id"] or "")
            return ("in_flight", (row["post_id"] if row else "") or "")


def socialapi_claim_set_post(draft_id, account_key, post_id):
    """Record the vendor post id on an in-flight claim (the vendor accepted the
    post but it is still processing). Keeps the claim so a retry POLLS this post
    instead of re-POSTing it."""
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE socialapi_claims SET post_id=? "
            "WHERE draft_id=? AND account_key=?", (post_id, draft_id, account_key))
        conn.commit()


def socialapi_claim_done(draft_id, account_key, post_id):
    """Mark a claim published. A later re-approve returns an idempotent no-op."""
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE socialapi_claims SET status='done', post_id=? "
            "WHERE draft_id=? AND account_key=?", (post_id, draft_id, account_key))
        conn.commit()


def socialapi_claim_release(draft_id, account_key):
    """Release a claim so a genuine retry can proceed. Called ONLY when nothing
    was posted to the vendor (a pre-network failure), never after the vendor
    accepted the post."""
    with _lock, connect() as conn:
        conn.execute(
            "DELETE FROM socialapi_claims WHERE draft_id=? AND account_key=?",
            (draft_id, account_key))
        conn.commit()


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
        'zernio_profile_id', 'zernio_default_fb_page_id',
        'baseline_posts_per_week', 'baseline_captured_at',
        'stripe_customer_id',
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

    # PRESERVE display_name when not passed (audit 2026-08-25 CRITICAL): almost every
    # caller upserts a single field (upload_link, zernio_profile_id, baseline...) and
    # omits display_name — the old unconditional `display_name = excluded.display_name`
    # then ERASED the stored name to ''. onboard.run even wiped its own write within one
    # call (writes the name, then gym_upsert(key, upload_link=...) blanks it), which
    # emptied the gyms-table name for every portal gym and killed the zernio-profile-link
    # display-name fallback (a UUID-keyed gym then silently never publishes). An empty
    # display_name arg now means "leave the stored name alone"; pass a non-empty name to
    # change it.
    update_parts = [f"{c} = excluded.{c}" for c in all_cols
                    if c != 'account_key'
                    and not (c == 'display_name' and not (display_name or '').strip())]
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


# ---- Part A: per-gym posting baseline (trailing-90d, captured at onboarding) ----

def set_baseline_posts_per_week(account_key, posts_per_week, captured_at=None):
    """Store a gym's trailing-90d posting baseline (posts/week) and stamp when it
    was captured. Part D's before/after story reads this. The value is a manual /
    explicit number now (the Zernio history source is Part C); accept it as given.
    Creates the gyms row if it does not exist. Timestamped on the gym record."""
    from datetime import datetime, timezone
    ts = captured_at or datetime.now(timezone.utc).isoformat()
    gym_upsert(account_key,
               baseline_posts_per_week=float(posts_per_week),
               baseline_captured_at=ts)
    return ts


def get_baseline_posts_per_week(account_key, conn=None):
    """The gym's stored baseline as (posts_per_week, captured_at), or (None, None)
    when no baseline has been captured for this gym yet."""
    row = gym_get(account_key, conn=conn)
    if not row:
        return None, None
    return row.get("baseline_posts_per_week"), row.get("baseline_captured_at")


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
