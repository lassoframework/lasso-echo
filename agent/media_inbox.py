"""
Media inbox core (Stage 2 Part 5): the provider-agnostic ingest queue.

Dormant behind AGENT_MEDIA_INBOX_ENABLED (default OFF: receive() returns None,
nothing is staged, no table is touched). Armed, any adapter (GHL, WhatsApp, the
upload endpoint) hands one normalized payload to receive():

    {"provider": "ghl", "sender": "+13175550101", "text": "one sentence",
     "media": [{"name": "IMG_1.jpg", "mime": "image/jpeg", "data": b"..."}]}

and the inbox:
  1. resolves the sender phone to a tenant via tenants.tenant_for_sender —
     NEVER guessed: an unknown sender's media is staged as HELD with one ops
     alert per sender per day, and nothing downstream may draft from it,
  2. stages the bytes to the inbox staging dir (AGENT_MEDIA_INBOX_DIR),
  3. records one row per media item, IDEMPOTENT BY CONTENT HASH: the same
     bytes received twice (webhook retries, double sends) insert nothing new,
  4. stores the texted sentence as the caption note on every item in the batch.

Nothing here publishes, drafts, or hosts; the ingest worker (Part 6) processes
STAGED rows onward. Secrets: none read here; sender phones are data, not
credentials, but they never land in ops alerts (only a masked suffix does).
"""

import hashlib
import os
from datetime import datetime, timezone

from . import config, db, ops_alerts, tenants

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT,
  sender TEXT,
  tenant_key TEXT DEFAULT '',
  name TEXT,
  mime TEXT,
  sha256 TEXT UNIQUE,
  caption_note TEXT DEFAULT '',
  status TEXT DEFAULT 'staged',
  staged_path TEXT DEFAULT '',
  received_at TEXT DEFAULT (datetime('now')));
"""

# statuses: staged (routed, waiting for the worker) | held (unknown sender,
# never drafted from) | processed | rejected (worker outcomes, Part 6)


def _conn():
    conn = db.connect()
    conn.executescript(_SCHEMA)
    return conn


def staging_dir():
    return os.environ.get("AGENT_MEDIA_INBOX_DIR", "media_inbox_staging")


def _mask(phone):
    """A sender phone never lands in an alert whole; the last 4 digits do."""
    s = str(phone or "")
    return f"...{s[-4:]}" if len(s) >= 4 else "(unknown)"


def _alert_unknown_sender(sender):
    """ONE ops alert per unknown sender per day (kv stamped AFTER the alert so a
    failed post retries; the silent-miss law)."""
    day = datetime.now(timezone.utc).date().isoformat()
    key = f"inbox_unknown_alerted_{hashlib.sha1(str(sender).encode()).hexdigest()[:12]}_{day}"
    if db.kv_get(key):
        return
    ops_alerts.alert(
        f"media inbox: media from an UNKNOWN sender ({_mask(sender)}) is HELD, "
        "not routed. Map the phone to a tenant (tenant.json sender_phones) or "
        "discard by hand. Never guessed.", force=True)
    db.kv_set(key, "1")


def receive(payload, base_dir=None):
    """
    Ingest one normalized adapter payload. Returns a summary dict
    {"tenant": key|"", "staged": n, "held": n, "duplicates": n, "ids": [...]}
    or None while AGENT_MEDIA_INBOX_ENABLED is OFF. Never raises for a single
    bad media item.
    """
    if not config.media_inbox_enabled():
        return None
    provider = str((payload or {}).get("provider", "") or "unknown")
    sender = str((payload or {}).get("sender", "") or "")
    note = str((payload or {}).get("text", "") or "").strip()
    media = (payload or {}).get("media") or []

    tenant_key = tenants.tenant_for_sender(sender, base_dir=base_dir) or ""
    held = not tenant_key
    if held and media:
        _alert_unknown_sender(sender)

    out = {"tenant": tenant_key, "staged": 0, "held": 0, "duplicates": 0, "ids": []}
    os.makedirs(staging_dir(), exist_ok=True)
    for item in media:
        name = os.path.basename(str(item.get("name", "") or "media.bin"))
        data = item.get("data") or b""
        if not data:
            continue
        sha = hashlib.sha256(data).hexdigest()
        with db._lock, _conn() as conn:
            dupe = conn.execute("SELECT id FROM media_inbox WHERE sha256=?",
                                (sha,)).fetchone()
            if dupe:
                out["duplicates"] += 1
                continue
            path = os.path.join(staging_dir(), f"{sha[:16]}_{name}")
            with open(path, "wb") as fh:
                fh.write(data)
            status = "held" if held else "staged"
            cur = conn.execute(
                "INSERT INTO media_inbox (provider, sender, tenant_key, name, "
                "mime, sha256, caption_note, status, staged_path) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (provider, sender, tenant_key, name,
                 str(item.get("mime", "") or ""), sha, note, status, path))
            conn.commit()
            out["ids"].append(cur.lastrowid)
        out["held" if held else "staged"] += 1
    if out["staged"] or out["held"] or out["duplicates"]:
        db.audit("media_inbox", tenant_key or "(held)",
                 f"{provider}: {out['staged']} staged, {out['held']} held, "
                 f"{out['duplicates']} duplicate(s) from {_mask(sender)}")
    return out


def rows(status=None, tenant_key=None):
    """Inbox rows, optionally filtered. Read path the worker and tests share."""
    q, args = "SELECT * FROM media_inbox", []
    conds = []
    if status:
        conds.append("status=?"); args.append(status)
    if tenant_key:
        conds.append("tenant_key=?"); args.append(tenant_key)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY id", args).fetchall()]


def set_status(row_id, status, note=None):
    with db._lock, _conn() as conn:
        if note is None:
            conn.execute("UPDATE media_inbox SET status=? WHERE id=?",
                         (status, row_id))
        else:
            conn.execute("UPDATE media_inbox SET status=?, caption_note=? "
                         "WHERE id=?", (status, note, row_id))
        conn.commit()
