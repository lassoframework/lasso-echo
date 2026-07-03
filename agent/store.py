"""
Pending-draft store (runtime memory).

When Echo drafts a post and posts the Slack card, the draft has to live somewhere
until Blake taps Approve / Edit / Skip. This is that store. JSON-backed, simple,
and it never holds a token (drafts carry captions and a creative reference, not
credentials).

This is "runtime memory" in the storage split: it lives in a data file the host
backs up, separate from the git-tracked voice doc and config.
"""

import json
import os

from . import config, ops_alerts
from .drafter import Draft, DraftStatus

STORE_PATH_DEFAULT = os.environ.get("AGENT_PENDING_PATH", "pending_drafts.json")


def _to_dict(d: Draft):
    return {
        "draft_id": d.draft_id,
        "account_key": d.account_key,
        "platform": d.platform,
        "caption": d.caption,
        "hashtags": d.hashtags,
        "creative_path": d.creative_path,
        "creative_public_url": d.creative_public_url,
        "scheduled_for": d.scheduled_for,
        "status": d.status.value,
        "blocked_reason": d.blocked_reason,
        "source_fragments": d.source_fragments,
        "slides": d.slides,
        "slide_urls": d.slide_urls,
        "is_story": d.is_story,
        "day_key": d.day_key,
        "draft_type": d.draft_type,
        "slack_channel": d.slack_channel,
        "slack_ts": d.slack_ts,
    }


def _from_dict(r):
    return Draft(
        draft_id=r["draft_id"],
        account_key=r["account_key"],
        platform=r["platform"],
        caption=r["caption"],
        hashtags=r.get("hashtags", []),
        creative_path=r.get("creative_path", ""),
        creative_public_url=r.get("creative_public_url", ""),
        scheduled_for=r.get("scheduled_for", ""),
        status=DraftStatus(r.get("status", "pending")),
        blocked_reason=r.get("blocked_reason", ""),
        source_fragments=r.get("source_fragments", []),
        slides=r.get("slides", []),
        slide_urls=r.get("slide_urls", []),
        is_story=bool(r.get("is_story", False)),
        day_key=r.get("day_key", ""),
        draft_type=r.get("draft_type", ""),
        slack_channel=r.get("slack_channel", ""),
        slack_ts=r.get("slack_ts", ""),
    )


class PendingStore:
    """Same API as the json store it replaces; SQLite-backed (agent/db.py, WAL on
    /data). `path` still accepted: it becomes this store's own sqlite file (tests
    pass tmp paths). A legacy pending_drafts.json at the default location migrates
    in once and is kept as .migrated.bak. STORAGE SWAP ONLY: behavior unchanged.
    """

    def __init__(self, path=None):
        from . import db as _db
        self._db = _db
        # a caller-provided path is that store's own sqlite file; default = the
        # shared /data db. Legacy json migrates on first open either way.
        self.path = path or None
        legacy = path if (path and path.endswith(".json") and os.path.exists(path)
                          and not _is_sqlite(path)) else STORE_PATH_DEFAULT
        try:
            with self._conn() as conn:
                self._db.migrate_legacy(conn, pending_json=legacy)
        except Exception:
            pass  # an unopenable db fails LOUDLY on the first write (put), not here

    def _conn(self):
        if self.path and not (self.path.endswith(".json") and os.path.exists(self.path)
                              and not _is_sqlite(self.path)):
            return self._db.connect(self.path)
        if self.path:
            # a legacy json path was passed: use a sibling sqlite file
            return self._db.connect(self.path + ".db")
        return self._db.connect()

    def put(self, draft: Draft):
        try:
            with self._conn() as conn:
                rec = _to_dict(draft)
                conn.execute(
                    "INSERT OR REPLACE INTO drafts "
                    "(draft_id, account_key, status, day_key, draft_type, data) "
                    "VALUES (?,?,?,?,?,?)",
                    (draft.draft_id, draft.account_key, draft.status.value,
                     draft.day_key, draft.draft_type, json.dumps(rec)))
                conn.commit()
        except Exception as e:
            msg = f"store write failed: {type(e).__name__}: {e}"
            print(f"[store] {ops_alerts.scrub(msg)}")
            ops_alerts.alert(msg)
            raise
        return draft

    def get(self, draft_id):
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM drafts WHERE draft_id=?",
                               (draft_id,)).fetchone()
        return _from_dict(json.loads(row["data"])) if row else None

    def remove(self, draft_id):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM drafts WHERE draft_id=?", (draft_id,))
            conn.commit()
            return cur.rowcount > 0

    def list_pending(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT data FROM drafts WHERE status=?",
                                (DraftStatus.PENDING.value,)).fetchall()
        return [_from_dict(json.loads(r["data"])) for r in rows]

    def find_for_day(self, account_key, day_key, draft_type):
        """The most recent record for (account, day, type), ANY status. The
        blocked-draft dedupe uses this so a failing slot cards ONCE, not once
        per scheduler fire (the Jul 1 retry-storm class)."""
        if not day_key or not draft_type:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM drafts WHERE account_key=? AND day_key=? "
                "AND draft_type=? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (account_key, day_key, draft_type)).fetchone()
        return _from_dict(json.loads(row["data"])) if row else None

    def find_pending(self, account_key, day_key, draft_type):
        """The PENDING draft for (account, day, type), or None: the idempotency
        lookup, exactly as before. Older records without day_key never match."""
        if not day_key or not draft_type:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM drafts WHERE status=? AND account_key=? "
                "AND day_key=? AND draft_type=?",
                (DraftStatus.PENDING.value, account_key, day_key, draft_type)).fetchone()
        return _from_dict(json.loads(row["data"])) if row else None


def _is_sqlite(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False
