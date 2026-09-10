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
    # additive gyms migration (Blake 2026-08-25): per-gym posting timezone. Null =>
    # the global POSTING_TIMEZONE, so nothing changes until set by hand per gym.
    if "posting_timezone" not in gyms_have:
        try:
            conn.execute("ALTER TABLE gyms ADD COLUMN posting_timezone TEXT")
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
    # SCHEMA DRIFT REPAIR (2026-09-10). _SCHEMA is CREATE TABLE IF NOT EXISTS, so a
    # column ADDED to the gyms DDL after a database was first created never lands on
    # that database: only the additive ALTERs above ever reach an existing file. The
    # `echo` worker's volume was created before upload_link / gym_name / token_sha256 /
    # token_status / publish_creds were added to _SCHEMA, so its gyms table is missing
    # all five while echo-intake-web's (created later) has them. That is a latent crash,
    # not a cosmetic gap: db.gym_upsert(key, upload_link=...) raises "no such column"
    # on the worker, and any cross service reconcile would fail to round trip a row.
    # Additive and idempotent, exactly like every ALTER above; existing rows keep NULL.
    for _col, _type in (("gym_name", "TEXT"),
                        ("token_sha256", "TEXT"),
                        ("token_status", "TEXT"),
                        ("publish_creds", "TEXT"),
                        ("upload_link", "TEXT")):
        if _col not in gyms_have:
            try:
                conn.execute(f"ALTER TABLE gyms ADD COLUMN {_col} {_type}")
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


def kv_is_durable():
    """True when the kv store PERSISTS across process runs: an explicit AGENT_DB_PATH
    or the mounted data volume (AGENT_DATA_DIR, default /data). The CWD-fallback
    ./echo.db (a dev checkout, a worktree, a verify run, or a service deployed
    WITHOUT the volume) is EPHEMERAL: a dedup stamp written there dies with the
    process, so kv-deduped ops alerts fired from such a process re-fire on every
    run. Alert dedup callers use this to go DURABLE-OR-SILENT (the 2026-08-27 gritx
    needs-media storm: an out-of-band Echo process with a throwaway sqlite re-sent
    the same 11 day-alerts on every pass because its stamps never persisted)."""
    if os.environ.get("AGENT_DB_PATH"):
        return True
    return os.path.isdir(os.environ.get("AGENT_DATA_DIR", "/data"))


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


# ---- per-account posting cadence (posts_per_day) ----------------------------------
# A gym owner picks 1x or 2x per day in the portal. Stored in the shared kv table,
# keyed per tenant BASE (gritx, eng), mirroring the autonomy flag. The kv row is the
# LOCAL record; the shared plane (echo_gym_settings.posts_per_day via the portal
# store) is what the worker service reads — same dual-write shape as autonomy.
# Default (no row / bad value) = 1: today's cadence, always the safe fallback.

def _cadence_key(base_key):
    return f"portal_cadence_{base_key or ''}"


def set_posts_per_day(base_key, n):
    """Persist the cadence preference for one gym base. Only 1 or 2 is a valid
    cadence; anything else is refused (returns False, writes nothing). Null-safe:
    an empty base_key is a harmless no-op key."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return False
    if n not in (1, 2):
        return False
    kv_set(_cadence_key(base_key), str(n))
    return True


def posts_per_day(base_key):
    """The gym's LOCALLY stored cadence preference: 1, 2, or None when unset /
    unreadable. This is the FALLBACK: cadence.resolve_posts_per_day reads the
    shared plane first and only consults this when that says nothing."""
    if not base_key:
        return None
    try:
        raw = str(kv_get(_cadence_key(base_key), "")).strip()
    except Exception:
        return None
    return int(raw) if raw in ("1", "2") else None


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
        'posting_timezone',
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

    # SHARED RECORD (2026-09-10 split-brain fix). The local write above is the source
    # of truth for THIS service; the mirror below is what makes it visible to the OTHER
    # one. Deliberately OUTSIDE the `with _lock` block: the failure path calls
    # ops_alerts/alert_repeat, which call kv_get/kv_set, which take the same
    # NON-reentrant _lock. Mirroring inside the lock would deadlock the process on the
    # first failed write.
    mirror_fields = dict(fields)
    mirror_fields["display_name"] = display_name
    _mirror_gym_row(account_key, mirror_fields)


def _shared_store(store=None):
    """The shared echo_gyms store when it is usable, else None. Never raises."""
    if store is not None:
        return store
    try:
        from . import config
        if not config.gym_shared_store_enabled():
            return None
        from .gym_shared_store import SharedGymStore
        s = SharedGymStore()
        return s if s.available() else None
    except Exception:  # noqa: BLE001 - a config/import fault never breaks the local write
        return None


# ONE Slack line per incident, not one per gym. A Supabase outage during a fleet
# reconcile fails 100+ gym writes back to back, and alert_repeat fingerprints the
# MESSAGE while every message names its own account_key -- leaning on it alone would put
# 100+ lines in the channel, the exact alert-flood shape Blake killed on 2026-09-04.
# Every individual failure still gets its OWN append-only audit row (queryable with
# `python -m agent audit`), so no failure is lost; only the Slack fan-out is collapsed.
_MIRROR_ALERT_STAMP = "gym_shared_store_alerted_at"
_MIRROR_ALERT_WINDOW_SECONDS = 30 * 60


def _mirror_alert(message, account_key="", now=None):
    """Surface a shared-store failure LOUDLY, on the same ops surface as every other
    alert, without ever storming it.

    force=True on the Slack call is deliberate: AGENT_OPS_ALERTS_ENABLED is NOT set on
    echo-intake-web, so a plain alert() there is dormant, which would be exactly the
    silent-failure mode this whole fix exists to remove. The 30 minute kv stamp is this
    caller's OWN gate, the pattern ops_alerts documents for force callers. Never raises.
    """
    import time as _t
    now = now if now is not None else _t.time()
    # ALWAYS record this specific failure, whether or not Slack is told about it.
    try:
        # account_key goes in the ACCOUNT_KEY column, not just the subject, so
        # `python -m agent audit --account <key>` actually finds this row.
        audit("gym_shared_store", account_key or "-", message,
              account_key=account_key or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = kv_get(_MIRROR_ALERT_STAMP, "")
        last = float(raw) if raw else 0.0
    except Exception:  # noqa: BLE001 - an unreadable stamp must never eat the alert
        last = 0.0
    if last and (now - last) < _MIRROR_ALERT_WINDOW_SECONDS:
        return
    try:
        kv_set(_MIRROR_ALERT_STAMP, str(now))
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import ops_alerts
        ops_alerts.alert(message, force=True)
    except Exception as e:  # noqa: BLE001 - alerting must never take the caller down
        print(f"[gym-shared-store] alert failed: {type(e).__name__}: {e}")


def _mirror_gym_row(account_key, fields, store=None):
    """Best-effort dual write of one gym row to the shared echo_gyms table.

    Returns True when mirrored, False when there is no shared store (creds absent /
    kill switch off) or the write failed. A failure NEVER raises (the local write
    already succeeded and nothing is lost) but it is never silent either: it prints
    and fires a deduped ops alert. Token material and upload_link are filtered out by
    gym_shared_store.mirrorable and never leave this process."""
    s = _shared_store(store)
    if s is None:
        return False
    try:
        s.upsert(account_key, fields)
        return True
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        print(f"[gym-shared-store] mirror FAILED for {account_key}: {detail}")
        _mirror_alert(
            f"gym shared store: could not mirror the gyms row for {account_key} to "
            f"Supabase echo_gyms ({detail}). The local write succeeded, so nothing is "
            f"lost, but the other Echo service will not see this change until it is "
            f"retried (python -m agent gym-store-sync --apply). Further failures in "
            f"the next 30 minutes go to the audit table only.",
            account_key=account_key,
        )
        return False


# Negative-result cache for the read-through below. gym_get is called on the publish
# hot path and callers routinely probe two keys (zernio_publisher tries "<base>_ig"
# then "<base>"), so an uncached miss would pay a network round trip on EVERY post for
# a key that legitimately does not exist. 60s is short enough that a gym onboarded on
# the web service shows up on the worker within a minute, long enough to collapse a
# publish sweep's probes into one lookup.
_MISS_TTL_SECONDS = 60
_miss_cache = {}
_miss_lock = threading.Lock()


def _miss_cached(account_key, now=None):
    import time as _t
    now = now if now is not None else _t.time()
    with _miss_lock:
        stamp = _miss_cache.get(account_key)
        return stamp is not None and (now - stamp) < _MISS_TTL_SECONDS


def _remember_miss(account_key, now=None):
    import time as _t
    now = now if now is not None else _t.time()
    with _miss_lock:
        _miss_cache[account_key] = now


def _forget_miss(account_key):
    with _miss_lock:
        _miss_cache.pop(account_key, None)


def _hydrate_local(row):
    """Write a shared row into the LOCAL gyms table so subsequent reads are local.
    Only mirrored columns are written (never token material). Never raises."""
    from .gym_shared_store import MIRRORED_COLUMNS
    account_key = str((row or {}).get("account_key") or "").strip()
    if not account_key:
        return None
    fields = {c: row[c] for c in MIRRORED_COLUMNS
              if c in row and row[c] is not None and c != "display_name"}
    display_name = str(row.get("display_name") or "")
    try:
        # NB: _local_gym_upsert, not gym_upsert - hydrating must not bounce the row
        # straight back at the shared store (a pointless write, and an echo loop if
        # the shared store were ever slow).
        _local_gym_upsert(account_key, display_name, fields)
    except Exception as e:  # noqa: BLE001 - a cache fill failure is not a read failure
        print(f"[gym-shared-store] local hydrate failed for {account_key}: "
              f"{type(e).__name__}: {e}")
    _forget_miss(account_key)
    local = gym_get(account_key, _shared_read=False)
    # A failed cache fill must not turn a row we DID find into a None. Fall back to the
    # shared row itself: the caller asked "what is this gym's record", and we have it.
    if local is None:
        local = dict(row)
        local["account_key"] = account_key
        local.setdefault("display_name", display_name)
    return local


def gym_get(account_key, conn=None, _shared_read=True):
    """Returns the gyms row as a dict, or None. Accepts an optional open connection.

    READ THROUGH (2026-09-10 split-brain fix): when the row is absent from THIS
    service's SQLite and the shared echo_gyms store is available, the row is fetched
    from Supabase and hydrated into the local table, so the `echo` worker sees a gym
    that `echo-intake-web` onboarded (and vice versa) without waiting for a sync job.
    A caller that passed its own `conn` gets the pure local read it asked for, and a
    shared-store error falls back to the local answer (None) rather than raising:
    every caller of this function already treats None as "not set" and has its own
    safe default, so a Supabase outage degrades to exactly today's behaviour."""
    def _get(c):
        row = c.execute(
            "SELECT * FROM gyms WHERE account_key = ?", (account_key,)
        ).fetchone()
        return dict(row) if row else None

    if conn is not None:
        return _get(conn)
    with connect() as c:
        row = _get(c)
    if not _shared_read:
        return row
    if row is not None:
        # Local HIT. The row may still be STALE: the other service may have updated it
        # since. One throttled full-table pull (a no-op inside its window) keeps every
        # field fresh without a per-key network call. Re-read only if the pull wrote
        # something, so the common path costs nothing.
        if pull_shared_into_local():
            with connect() as c:
                return _get(c) or row
        return row
    key = str(account_key or "").strip()
    if not key or _miss_cached(key):
        return None
    s = _shared_store()
    if s is None:
        return None
    try:
        shared = s.get(key)
    except Exception as e:  # noqa: BLE001 - a read fault degrades to the local answer
        print(f"[gym-shared-store] read-through failed for {key}: "
              f"{type(e).__name__}: {e}")
        return None
    if not shared:
        _remember_miss(key)
        return None
    return _hydrate_local(shared)


def _local_gym_upsert(account_key, display_name, fields):
    """The LOCAL half of gym_upsert, with no shared mirror. Used by the read-through
    hydrate so a pull can never turn into a push."""
    allowed = {
        'display_name', 'gym_name', 'intake_token_hash', 'token_rotated_at',
        'token_revoked', 'intake_token_encrypted', 'upload_link', 'publish_flag',
        'publish_creds', 'publish_creds_status',
        'zernio_profile_id', 'zernio_default_fb_page_id',
        'posting_timezone',
        'baseline_posts_per_week', 'baseline_captured_at',
        'stripe_customer_id',
    }
    extra_cols, extra_vals = [], []
    for k, v in (fields or {}).items():
        if k in allowed and k != 'display_name':
            extra_cols.append(k)
            extra_vals.append(v)
    all_cols = ['account_key', 'display_name'] + extra_cols
    all_vals = [account_key, display_name or ''] + extra_vals
    placeholders = ', '.join(['?'] * len(all_cols)) + ", datetime('now')"
    col_str = ', '.join(all_cols) + ', updated_at'
    update_parts = [f"{c} = excluded.{c}" for c in all_cols
                    if c != 'account_key'
                    and not (c == 'display_name' and not (display_name or '').strip())]
    update_parts.append("updated_at = datetime('now')")
    sql = (
        f"INSERT INTO gyms ({col_str}) VALUES ({placeholders}) "
        f"ON CONFLICT(account_key) DO UPDATE SET {', '.join(update_parts)}"
    )
    with _lock, connect() as conn:
        conn.execute(sql, all_vals)
        conn.commit()


def gym_key_for_zernio_profile(zernio_profile_id, conn=None):
    """The account_key currently bound to `zernio_profile_id`, or None. Used by the
    cross-tenant bind guard (account_key_guard) to detect a profile already owned by a
    DIFFERENT gym before a second bind wires one gym's posts onto another gym's socials.
    When more than one row somehow holds the same id, returns the lowest account_key
    (deterministic). Read-only; accepts an optional open connection."""
    pid = (str(zernio_profile_id) if zernio_profile_id is not None else "").strip()
    if not pid:
        return None

    def _get(c):
        row = c.execute(
            "SELECT account_key FROM gyms WHERE zernio_profile_id = ? "
            "ORDER BY account_key LIMIT 1", (pid,)
        ).fetchone()
        return (dict(row)["account_key"] if row else None)

    if conn is not None:
        return _get(conn)
    with connect() as c:
        return _get(c)


# FULL-TABLE REFRESH THROTTLE.
#
# gym_get's read-through covers a local MISS, which is a NEW gym. It does NOT cover an
# UPDATE to a row this service already has -- caught by the live end-to-end on
# 2026-09-10: the worker wrote stripe_customer_id for a gym, the mirror reached
# Supabase, and echo-intake-web kept answering from its own stale local row because the
# row was present, so no read-through fired. Polling per key would be the obvious fix
# and the wrong one (a network round trip per gym per publish sweep); ONE PostgREST
# request refreshes the whole table instead, so both gym_get and gym_list drive this
# throttled full pull and cross-service staleness is bounded at _REFRESH_SECONDS for
# EVERY field, not just for new rows.
#
# 60s: one request per minute per process is negligible next to what the worker already
# does per tick, and it converges to ZERO writes once both sides agree (a row whose
# shared copy is not newer and has nothing to fill is skipped entirely). In-process
# only, so a restart refreshes immediately.
_LIST_REFRESH_SECONDS = 60
_last_list_refresh = [0.0]


def _parse_ts(value):
    """A timestamp from either store as an aware UTC datetime, or None.

    The two sides write different shapes: SQLite stamps `datetime('now')` (naive UTC,
    'YYYY-MM-DD HH:MM:SS') and the shared store an explicit ISO string with an offset.
    A naive value is UTC by construction here, so it is read as UTC rather than local
    time -- reading it as local would make every comparison wrong by the host offset."""
    if not value:
        return None
    from datetime import datetime, timezone
    text = str(value).strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def pull_shared_into_local(store=None, force=False, now=None):
    """Refresh the LOCAL gyms table from the shared echo_gyms record.

    Rows missing locally are inserted; rows both stores have are updated when the
    SHARED copy is newer (last write wins by updated_at) and otherwise only have their
    still-empty fields filled. ONE PostgREST request covers the whole table, which is
    why both gym_get and gym_list can afford to drive it.

    Returns the number of local rows written. Never raises: on any shared-store
    error it prints, fires the deduped ops alert, and returns 0, leaving the caller
    with exactly today's local-only behaviour."""
    import time as _t
    now = now if now is not None else _t.time()
    if not force and (now - _last_list_refresh[0]) < _LIST_REFRESH_SECONDS:
        return 0
    s = _shared_store(store)
    if s is None:
        return 0
    _last_list_refresh[0] = now
    try:
        rows = s.list_all()
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        print(f"[gym-shared-store] pull failed: {detail}")
        _mirror_alert(
            f"gym shared store: could not read Supabase echo_gyms ({detail}). This "
            f"service is answering gym lookups from its LOCAL SQLite only, so a gym "
            f"onboarded on the other Echo service may be invisible here until this "
            f"clears.",
            account_key="-",
        )
        return 0
    with connect() as c:
        local = {r["account_key"]: dict(r) for r in c.execute(
            "SELECT * FROM gyms").fetchall()}
    from .gym_shared_store import MIRRORED_COLUMNS
    written = 0
    for row in rows:
        key = str(row.get("account_key") or "").strip()
        if not key:
            continue
        have = local.get(key)
        # LAST WRITE WINS, BY TIMESTAMP -- the same semantics the ONE table this
        # replaces always had. Both services write, so "newest wins" is the only rule
        # that makes an UPDATE propagate at all; refusing to overwrite a non-empty local
        # value (the first cut of this function) left a changed field stale forever on
        # the other service and called it a "disagreement".
        #
        # When the shared row is NOT newer, the old rule still applies and only fills
        # values this service is missing. That is what makes a service whose mirror
        # write failed keep its own newer local value: the failure alerted, the local
        # write is not lost, and a stale shared copy can never roll it back.
        shared_newer = False
        if have is not None:
            st, lt = _parse_ts(row.get("updated_at")), _parse_ts(have.get("updated_at"))
            shared_newer = bool(st and lt and st > lt)
        fields = {}
        for col in MIRRORED_COLUMNS:
            val = row.get(col)
            if val is None or str(val).strip() == "":
                continue
            if (have is not None and not shared_newer
                    and str(have.get(col) or "").strip()):
                continue
            fields[col] = val
        if have is not None and not fields:
            continue
        display_name = fields.pop("display_name", "") if have is not None \
            else str(row.get("display_name") or "")
        try:
            _local_gym_upsert(key, display_name, fields)
            _forget_miss(key)
            written += 1
        except Exception as e:  # noqa: BLE001
            print(f"[gym-shared-store] local write failed for {key}: "
                  f"{type(e).__name__}: {e}")
    return written


def gym_list(conn=None, _shared_read=True):
    """Returns all gyms rows as list of dicts, ordered by account_key.
    Accepts an optional open connection.

    When the shared echo_gyms store is available, the same THROTTLED full-table pull
    gym_get uses runs first (see pull_shared_into_local), so an enumeration on the
    `echo` worker includes gyms `echo-intake-web` onboarded, with current values. A
    caller that passed its own `conn` gets the pure local read it asked for."""
    def _list(c):
        return [dict(r) for r in c.execute(
            "SELECT * FROM gyms ORDER BY account_key"
        ).fetchall()]

    if conn is not None:
        return _list(conn)
    if _shared_read:
        pull_shared_into_local()
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
