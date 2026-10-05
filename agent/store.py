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
import sqlite3
import time
import uuid

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
        # This is raw-source provenance, not a fallback for the delivered URL.
        # Keep an absent legacy value empty rather than inventing one.
        "source_media_url": d.source_media_url,
        # Original Drive byte identity; never inferred from the delivered URL.
        "source_media_content_hash": getattr(d, "source_media_content_hash", "") or "",
        # A video poster and its immutable render proof must survive the store
        # round trip before real_calendar_mirror builds the prepared-writer side
        # channel.  The proof remains draft JSON only; it is never a calendar
        # column.
        "thumbnail_url": getattr(d, "thumbnail_url", ""),
        "poster_render_evidence": getattr(d, "poster_render_evidence", {}),
        "scheduled_for": d.scheduled_for,
        "status": d.status.value,
        "blocked_reason": d.blocked_reason,
        "source_fragments": d.source_fragments,
        "infographic_copy": d.infographic_copy,
        "slides": d.slides,
        "slide_urls": d.slide_urls,
        "is_story": d.is_story,
        "day_key": d.day_key,
        "draft_type": d.draft_type,
        "slack_channel": d.slack_channel,
        "slack_ts": d.slack_ts,
        "needs_media": d.needs_media,
        "force_approval": d.force_approval,
        "image_engine": d.image_engine,
        # Durable logical-post identity (flag-gated mirror feature). Persisted so a
        # rehydrated Draft keeps the SAME identity across the mirror's
        # delete/reinsert cycle; never inferred from date/photo/caption.
        "logical_post_id": getattr(d, "logical_post_id", "") or "",
    }


_SELECT = "draft_id, account_key, status, day_key, draft_type, data"


def _rescue_from_row(data, row):
    """Back-fill data with DB column values when the JSON blob is missing them.
    Applies to all five column-backed fields; setdefault never overwrites a key
    that the JSON already provides, so existing values are preserved."""
    data.setdefault("draft_id", row["draft_id"] or "")
    data.setdefault("account_key", row["account_key"] or "")
    data.setdefault("status", row["status"] or "pending")
    data.setdefault("day_key", row["day_key"] or "")
    data.setdefault("draft_type", row["draft_type"] or "")
    return data


def _row_to_draft(row):
    """Build a Draft from a stored row without ever crashing the caller.

    Legacy and partial rows are real in this store: `data` can be NULL,
    malformed JSON, or carry a status string the enum no longer knows. Any of
    those used to raise out of the read path and take down the whole daily run
    (and the Approve tap). Here they degrade instead: bad JSON falls back to
    the column values alone, an unknown status quarantines the draft as
    BLOCKED (a blocked draft can never publish), and a row that cannot produce
    a Draft at all returns None with a log line so the caller skips it.
    """
    try:
        data = json.loads(row["data"] or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data = _rescue_from_row(data, row)
    try:
        return _from_dict(data)
    except (ValueError, TypeError):
        data["status"] = DraftStatus.BLOCKED.value
        data["blocked_reason"] = data.get("blocked_reason") or (
            "legacy row: unreadable status, quarantined (will never publish)")
        try:
            return _from_dict(data)
        except Exception as e:
            print("[store] unreadable row skipped: "
                  f"{row['draft_id']!r} ({type(e).__name__})")
            return None


def _from_dict(r):
    draft = Draft(
        draft_id=r.get("draft_id", ""),
        account_key=r.get("account_key", ""),
        platform=r.get("platform", ""),
        caption=r.get("caption", ""),
        hashtags=r.get("hashtags", []),
        creative_path=r.get("creative_path", ""),
        creative_public_url=r.get("creative_public_url", ""),
        source_media_url=r.get("source_media_url", ""),
        scheduled_for=r.get("scheduled_for", ""),
        status=DraftStatus(r.get("status", "pending")),
        blocked_reason=r.get("blocked_reason", ""),
        source_fragments=r.get("source_fragments", []),
        infographic_copy=r.get("infographic_copy", {}),
        slides=r.get("slides", []),
        slide_urls=r.get("slide_urls", []),
        is_story=bool(r.get("is_story", False)),
        day_key=r.get("day_key", ""),
        draft_type=r.get("draft_type", ""),
        logical_post_id=r.get("logical_post_id", "") or "",
        source_media_content_hash=r.get("source_media_content_hash", "") or "",
        slack_channel=r.get("slack_channel", ""),
        slack_ts=r.get("slack_ts", ""),
        needs_media=bool(r.get("needs_media", False)),
        force_approval=bool(r.get("force_approval", False)),
        image_engine=r.get("image_engine", ""),
    )
    # These guarded-writer fields intentionally remain dynamic Draft attributes:
    # flag-off and legacy drafts preserve their historical object shape, while a
    # guarded video poster and its proof survive the persistence boundary used
    # by real_calendar_mirror.  The proof is a writer side channel, never a
    # content_calendar column.
    thumbnail_url = r.get("thumbnail_url", "")
    if isinstance(thumbnail_url, str) and thumbnail_url:
        draft.thumbnail_url = thumbnail_url
    poster_evidence = r.get("poster_render_evidence", {})
    if isinstance(poster_evidence, dict) and poster_evidence:
        draft.poster_render_evidence = poster_evidence
    return draft


class _RevisionConflict(Exception):
    """Internal: the row moved under us during a conditional stamp; retry."""


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
        """Insert or replace one draft.

        LOGICAL IDENTITY GUARD (the immutable logical_post_id ruling): inside ONE
        BEGIN IMMEDIATE transaction, a stored VALID logical_post_id can never be
        silently erased or replaced by a routine write. An incoming draft that omits
        the id inherits the stored one; an incoming draft carrying a DIFFERENT valid
        id is refused with ValueError (a conflicting identity is a bug, never a
        silent overwrite). An invalid incoming non-empty id is likewise refused.
        Fail closed before any write: a row owned by a different
        account_key is refused (no cross-tenant transfer or overwrite),
        and a stored non-empty invalid id is refused rather than silently
        erased by INSERT OR REPLACE.
        """
        try:
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rec = _to_dict(draft)
                incoming = (rec.get("logical_post_id") or "").strip()
                if incoming:
                    try:
                        incoming = str(uuid.UUID(incoming))
                    except (ValueError, AttributeError, TypeError):
                        raise ValueError(
                            f"put: draft {draft.draft_id!r} carries invalid "
                            f"logical_post_id {incoming!r}; refusing write")
                    rec["logical_post_id"] = incoming
                row = conn.execute(
                    "SELECT account_key, data FROM drafts WHERE draft_id=?",
                    (draft.draft_id,)).fetchone()
                if row is not None:
                    # Owner check BEFORE any write: a draft_id that collides
                    # across accounts is a different tenant's row. It is never
                    # read for identity and never overwritten by REPLACE.
                    if str(row["account_key"] or "") != str(draft.account_key or ""):
                        raise ValueError(
                            f"put: draft {draft.draft_id!r} is owned by a "
                            "different account_key; refusing cross-tenant write")
                    try:
                        existing = json.loads(row["data"] or "{}")
                    except Exception:
                        existing = {}
                    if not isinstance(existing, dict):
                        existing = {}
                    stored = (existing.get("logical_post_id") or "").strip()
                    if stored:
                        try:
                            stored = str(uuid.UUID(stored))
                        except (ValueError, AttributeError, TypeError):
                            raise ValueError(
                                f"put: draft {draft.draft_id!r} stores invalid "
                                f"logical_post_id {stored!r}; fail closed, "
                                "refusing to erase a corrupt identity")
                    if stored:
                        if incoming and incoming != stored:
                            raise ValueError(
                                f"put: draft {draft.draft_id!r} conflicts with "
                                "stored logical_post_id; refusing to replace "
                                "immutable identity")
                        if not incoming:
                            # Stale whole-draft write: preserve the durable id.
                            rec["logical_post_id"] = stored
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

    def ensure_logical_post_id(self, account_key, draft_id,
                               expected_updated_at=None, retries=3):
        """Durably stamp (or verify) one draft's immutable logical_post_id.

        Tenant-scoped and atomic: every attempt runs under SQLite BEGIN IMMEDIATE
        (the write lock is held for the whole read-check-write, so two stampers
        serialize and the loser reads back the winner's id). Behavior:

          * Row missing -> KeyError. Row owned by a DIFFERENT account_key ->
            ValueError (a cross-tenant stamp is never written).
          * Stored VALID UUID -> returned verbatim, zero writes (idempotent).
          * Stored INVALID non-empty value -> ValueError (fail closed; never
            silently remint over a corrupt identity).
          * Absent/empty -> a fresh uuid4 is written into the row's data JSON with
            a compare-and-swap on the row's updated_at revision; the new id is
            returned only after the write commits.
          * expected_updated_at, when given, must match the row's CURRENT
            revision before any write; a mismatch is a conflict.

        Conflicts and transient SQLITE_BUSY are retried with a fresh read-back,
        bounded by `retries`; an unresolved conflict raises RuntimeError rather
        than guessing. A draft_id that is empty raises ValueError immediately.
        """
        if not draft_id:
            raise ValueError("ensure_logical_post_id: empty draft_id")
        attempts = max(1, int(retries))
        last_conflict = None
        for _ in range(attempts):
            try:
                with self._conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute(
                        "SELECT account_key, data, updated_at FROM drafts "
                        "WHERE draft_id=?", (draft_id,)).fetchone()
                    if row is None:
                        raise KeyError(
                            f"ensure_logical_post_id: no draft row {draft_id!r}")
                    if str(row["account_key"] or "") != str(account_key or ""):
                        raise ValueError(
                            f"ensure_logical_post_id: draft {draft_id!r} is not "
                            f"owned by account {account_key!r}; refusing "
                            "cross-tenant stamp")
                    revision = row["updated_at"] or ""
                    if (expected_updated_at is not None
                            and revision != expected_updated_at):
                        raise _RevisionConflict(revision)
                    try:
                        data = json.loads(row["data"] or "{}")
                    except Exception:
                        data = {}
                    if not isinstance(data, dict):
                        data = {}
                    existing = (data.get("logical_post_id") or "").strip()
                    if existing:
                        try:
                            conn.commit()
                            return str(uuid.UUID(existing))
                        except (ValueError, AttributeError, TypeError):
                            raise ValueError(
                                f"ensure_logical_post_id: draft {draft_id!r} "
                                f"carries invalid logical_post_id {existing!r}; "
                                "fail closed, refusing to remint")
                    new_id = str(uuid.uuid4())
                    data["logical_post_id"] = new_id
                    cur = conn.execute(
                        "UPDATE drafts SET data=?, updated_at=datetime('now') "
                        "WHERE draft_id=? AND account_key=? AND updated_at=?",
                        (json.dumps(data), draft_id, account_key, revision))
                    if cur.rowcount != 1:
                        raise _RevisionConflict(revision)
                    conn.commit()
                    return new_id
            except _RevisionConflict as e:
                last_conflict = e  # fresh read-back on the next attempt
                continue
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                last_conflict = e
                time.sleep(0.05)
                continue
        raise RuntimeError(
            f"ensure_logical_post_id: could not durably stamp {draft_id!r} "
            f"after {attempts} attempts ({last_conflict!r})")

    def get(self, draft_id):
        with self._conn() as conn:
            row = conn.execute(f"SELECT {_SELECT} FROM drafts WHERE draft_id=?",
                               (draft_id,)).fetchone()
        if row is None:
            return None
        return _row_to_draft(row)

    def remove(self, draft_id):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM drafts WHERE draft_id=?", (draft_id,))
            conn.commit()
            return cur.rowcount > 0

    def list_pending(self):
        with self._conn() as conn:
            rows = conn.execute(f"SELECT {_SELECT} FROM drafts WHERE status=?",
                                (DraftStatus.PENDING.value,)).fetchall()
        drafts = (_row_to_draft(r) for r in rows)
        return [d for d in drafts if d is not None]

    def find_for_day(self, account_key, day_key, draft_type):
        """The most recent record for (account, day, type), ANY status. The
        blocked-draft dedupe uses this so a failing slot cards ONCE, not once
        per scheduler fire (the Jul 1 retry-storm class)."""
        if not day_key or not draft_type:
            return None
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {_SELECT} FROM drafts WHERE account_key=? AND day_key=? "
                "AND draft_type=? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (account_key, day_key, draft_type)).fetchone()
        if row is None:
            return None
        return _row_to_draft(row)

    def list_for_account(self, account_key):
        """Every draft for this account_key, ANY status, most recent first. Read
        only; scoped to the one account so a caller can never sweep another gym's
        drafts. Used by the real-calendar mirror to fold a gym's REAL drafts into
        the shared content_calendar. Unreadable rows are skipped (never crash)."""
        if not account_key:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT} FROM drafts WHERE account_key=? "
                "ORDER BY updated_at DESC, rowid DESC",
                (account_key,)).fetchall()
        drafts = (_row_to_draft(r) for r in rows)
        return [d for d in drafts if d is not None]

    def find_pending(self, account_key, day_key, draft_type):
        """The PENDING draft for (account, day, type), or None: the idempotency
        lookup, exactly as before. Older records without day_key never match."""
        if not day_key or not draft_type:
            return None
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {_SELECT} FROM drafts WHERE status=? AND account_key=? "
                "AND day_key=? AND draft_type=?",
                (DraftStatus.PENDING.value, account_key, day_key, draft_type)).fetchone()
        if row is None:
            return None
        return _row_to_draft(row)

    # Transient marker (NOT a DraftStatus enum member): a draft currently
    # owned by a claim_for_publish() winner, between the claim and the
    # publish() call resolving. Never written by put()/_to_dict(), and never
    # readable back out as a Draft (approvals.py always resolves it via
    # claim_for_publish/release_claim, by draft_id, before anything reads the
    # row again). Kept off DraftStatus so a stray read can't hand a caller a
    # Draft object claiming to be in an enum state that never existed.
    CLAIMING_STATUS = "publishing"

    def claim_for_publish(self, draft_id, from_status):
        """
        ATOMIC PUBLISH CLAIM (the fix for the Slack-lane double-publish
        incident): the Slack listener's _act() reads a draft via a plain
        store.get() -- no conditional update -- then calls
        approvals.handle_action("approve", ...), which only flipped
        draft.status to APPROVED *after* pub.publish() returned. Two
        concurrent approvals of the same draft_id (a Slack retry on a slow
        ack, a double tap by the approver, or two listener replicas) could
        both read the same row while it was still unclaimed and both reach
        publish() -- meta_publisher's 24h content-hash dedup was the only
        guard, and it is check-then-act (reads _recent_duplicate before the
        network call, stamps _stamp_published only after), so two callers
        that both checked before either stamped both published live.

        This is a compare-and-swap: flip this row's status from_status ->
        CLAIMING_STATUS, but ONLY if the row's CURRENTLY STORED status still
        equals from_status (the status the caller actually read before
        deciding this draft was approvable -- 'pending' the ordinary case,
        'expired' for the approve-on-expired path). Two callers racing on the
        same persisted row: only the first UPDATE can match (rowcount 1); the
        second sees the row already flipped away from from_status and gets
        rowcount 0, i.e. it lost the race and must not call publish().

        A draft with NO persisted row at all (runner.py's per-account
        autonomy lane calls approve() on a freshly drafted Draft *before* its
        first store.put(); tests routinely build a Draft in memory and call
        handle_action directly) has nothing to protect: there is only ever
        one caller who could possibly be racing against nothing, so the claim
        is granted by default rather than mistaken for "someone else already
        has it".

        Returns True iff this call may proceed to publish.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE drafts SET status=? WHERE draft_id=? AND status=?",
                (self.CLAIMING_STATUS, draft_id, from_status))
            conn.commit()
            if cur.rowcount > 0:
                return True
            exists = conn.execute(
                "SELECT 1 FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
            return exists is None

    def release_claim(self, draft_id, from_status):
        """
        Undo a claim after a publish FAILURE (MediaNotReady, or any other
        exception out of publisher.publish()), restoring the row to
        from_status so a human can retry -- exactly the state it was in
        before the claim. Without this, a claimed-then-failed draft would be
        stranded in CLAIMING_STATUS forever (invisible to list_pending(),
        unrecoverable), which would turn the rare double-post this fixes into
        a common never-post: worse than the bug it replaces.

        Only ever flips a row THIS store marked CLAIMING_STATUS; a row that
        moved on to something else in the meantime (there is no such caller
        today, since only the claim winner ever reaches publish()) is left
        alone rather than clobbered. A draft with no persisted row (the
        claim-granted-by-default case above) has nothing to release; this is
        then a harmless no-op.
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE drafts SET status=? WHERE draft_id=? AND status=?",
                (from_status, draft_id, self.CLAIMING_STATUS))
            conn.commit()


def _is_sqlite(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False
