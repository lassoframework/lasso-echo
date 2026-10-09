"""Listener-owned recovery of frozen GBP uses. No planning or finalizer writes.

AGENT_GBP_DRIVE_USE_RECOVERY defaults OFF. The listener must retain its original
durable database; a finalizer has a separate database and only owns PG batches.
"""
from pathlib import Path
import os
import sqlite3
import uuid

FLAG = "AGENT_GBP_DRIVE_USE_RECOVERY"
JOURNAL_ID_ENV = "AGENT_GBP_DRIVE_USE_RECOVERY_JOURNAL_ID"
MAX_ROWS = 32
MAX_TENANTS = 8
MAX_BATCHES = 16
EPHEMERAL_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp",
                   "/private/var/folders", "/dev/shm")


def enabled():
    value = os.environ.get(FLAG, "false").strip().lower()
    if value in ("", "false", "0", "no", "off"):
        return False
    if value in ("true", "1", "yes", "on"):
        return True
    return None


def _journal_path():
    from .. import db, gbp_drive_use_journal as journal
    path = Path(db.db_path())
    if not path.is_absolute() or not path.is_file():
        raise ValueError("original durable journal missing")
    path = path.resolve()
    if path != Path(journal._canonical_db_path()).resolve():
        raise ValueError("journal path mismatch")
    if any(path == Path(root) or Path(root) in path.parents for root in EPHEMERAL_ROOTS):
        raise ValueError("ephemeral journal")
    return path


def _expected_identity():
    value = os.environ.get(JOURNAL_ID_ENV, "")
    if str(uuid.UUID(value)) != value:
        raise ValueError("original journal instance pin missing or invalid")
    return value


def _verify_identity(conn):
    expected = _expected_identity()
    identity = conn.execute(
        "SELECT journal_instance_id FROM gbp_drive_use_recovery_owner WHERE singleton=1"
    ).fetchone()
    if identity != (expected,):
        raise ValueError("original journal instance pin mismatch")
    return expected


def _durable_path():
    path = _journal_path()
    conn = _reader(path)
    try:
        _verify_identity(conn)
    finally:
        conn.close()
    return path


def pin_original_journal(instance_id):
    """Explicit operator step AFTER original volume/journal verification.

    Never called by recovery. Cannot enroll a missing file or silently replace
    an existing identity. Pin this UUID independently in deployment config.
    """
    if str(uuid.UUID(instance_id)) != instance_id:
        raise ValueError("journal instance UUID must be canonical")
    path = _journal_path()
    conn = _reader(path, write=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # An arbitrary empty SQLite DB is not an original planner journal.
        conn.execute("SELECT use_id FROM gbp_drive_use_journal LIMIT 1")
        conn.execute("SELECT batch_id FROM gbp_forward_stage_journal LIMIT 1")
        conn.execute("""CREATE TABLE IF NOT EXISTS gbp_drive_use_recovery_owner (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            journal_instance_id TEXT NOT NULL,
            cursor_created_at TEXT, cursor_use_id TEXT)""")
        existing = conn.execute(
            "SELECT journal_instance_id FROM gbp_drive_use_recovery_owner WHERE singleton=1"
        ).fetchone()
        if existing is not None and existing != (instance_id,):
            raise ValueError("original journal already pinned to another instance")
        conn.execute(
            "INSERT OR IGNORE INTO gbp_drive_use_recovery_owner(singleton,journal_instance_id) "
            "VALUES(1,?)", (instance_id,))
        conn.execute("CREATE INDEX IF NOT EXISTS gbp_drive_use_recovery_order "
                     "ON gbp_drive_use_journal(created_at,use_id)")
        conn.commit()
    finally:
        conn.close()
    return instance_id


def _reader(path, *, write=False):
    # Never create an empty replacement journal on a missing mount.
    mode = "rw" if write else "ro"
    conn = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=2,
                           isolation_level=None)
    if not write:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA synchronous=FULL")
    # Bound even a corrupt or very large journal scan to 100k VM instructions.
    ticks = 0
    def budget():
        nonlocal ticks
        ticks += 1
        return ticks >= 100
    conn.set_progress_handler(budget, 1000)
    return conn


def _take_fair_ids(path, row_limit, tenant_id=None):
    """Enumerate a bounded circular slice; admission alone advances cursor."""
    conn = _reader(path)
    try:
        _verify_identity(conn)
        cursor = conn.execute(
            "SELECT cursor_created_at,cursor_use_id FROM gbp_drive_use_recovery_owner "
            "WHERE singleton=1").fetchone()
        if (cursor[0] is None) != (cursor[1] is None):
            raise ValueError("recovery cursor malformed")
        sql = ("SELECT use_id,created_at FROM gbp_drive_use_journal "
               "WHERE state IN ('write_intent','unknown_result','confirmed_landed',"
               "'consumption_pending','receipt_confirmed') ")
        scope_args = ()
        if tenant_id is not None:
            if (not isinstance(tenant_id, str) or not tenant_id
                    or len(tenant_id) > 128):
                raise ValueError("invalid recovery tenant")
            sql += "AND gym_id=? "
            scope_args = (tenant_id,)
        if cursor[0] is None:
            rows = conn.execute(sql + "ORDER BY created_at,use_id LIMIT ?",
                                (*scope_args, row_limit)).fetchall()
        else:
            rows = conn.execute(sql + "AND (created_at,use_id)>(?,?) "
                                "ORDER BY created_at,use_id LIMIT ?",
                                (*scope_args, *cursor, row_limit)).fetchall()
            if len(rows) < row_limit:
                rows += conn.execute(sql + "AND (created_at,use_id)<=(?,?) "
                                     "ORDER BY created_at,use_id LIMIT ?",
                                     (*scope_args, *cursor, row_limit - len(rows))).fetchall()
        return rows
    finally:
        conn.close()


def _advance_cursor(path, use_id, created_at):
    """Persist one admitted attempt BEFORE processing it, never a deferred row."""
    conn = _reader(path, write=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _verify_identity(conn)
        conn.execute("UPDATE gbp_drive_use_recovery_owner "
                     "SET cursor_created_at=?,cursor_use_id=? WHERE singleton=1",
                     (created_at, use_id))
        conn.commit()
    finally:
        conn.close()


def _batch_for_use(path, entry):
    """Discover only frozen local batch identity for admission, never PG state."""
    conn = _reader(path)
    try:
        _verify_identity(conn)
        matches = conn.execute(
            "SELECT batch_id FROM gbp_forward_stage_journal WHERE EXISTS ("
            "SELECT 1 FROM json_each(request_text,'$.members') "
            "WHERE json_extract(value,'$.row.logical_post_id')=? "
            "AND json_extract(value,'$.row.gym_id')=?) LIMIT 2",
            (entry["logical_post_id"], entry["gym_id"])).fetchall()
    finally:
        conn.close()
    if len(matches) != 1:
        raise ValueError("missing or ambiguous frozen use batch")
    return matches[0][0]


def _bound_member(path, row_id):
    from .. import gbp_drive_use_journal as journal
    conn = _reader(path)
    try:
        _verify_identity(conn)
        matches = conn.execute(
            "SELECT batch_id FROM gbp_forward_stage_journal "
            "WHERE EXISTS (SELECT 1 FROM json_each(member_row_ids) WHERE value=?) LIMIT 2",
            (row_id,)).fetchall()
    finally:
        conn.close()
    if len(matches) != 1:
        raise ValueError("missing or ambiguous batch binding")
    return journal.get_forward_stage(matches[0][0])


def _claim_matches(path, entry):
    conn = _reader(path)
    try:
        _verify_identity(conn)
        claim = conn.execute(
            "SELECT status,post_id FROM socialapi_claims WHERE draft_id=? AND account_key=?",
            (entry["claim_id"], entry["gym_id"] + "_gbp")).fetchone()
    finally:
        conn.close()
    return (claim is not None and (
        (claim[0] == "in_flight" and claim[1] in (None, "", entry["asset_id"]))
        or claim == ("done", entry["asset_id"])))


def run(*, store=None, media_store=None, logger=None, row_limit=MAX_ROWS, tenant_id=None):
    log = logger or (lambda message: print(f"[gbp-drive-recovery] {message}"))
    summary = dict(ok=True, recovered=0, held=0, examined=0, batches=0, tenants=0)
    if enabled() is False:
        return dict(summary, reason="disabled")
    from .. import gbp_drive_use_journal as journal, gbp_planner
    from ..portal_calendar_store import gbp_staged_journal_flag
    if (enabled() is not True or gbp_staged_journal_flag() is not True
            or not gbp_planner._remote_drive_cas_enabled()):
        return dict(summary, ok=False, reason="recovery flags hold")
    try:
        if type(row_limit) is not int or not 1 <= row_limit <= MAX_ROWS:
            raise ValueError("invalid recovery bound")
        path = _durable_path()
        ids = _take_fair_ids(path, row_limit, tenant_id=tenant_id)
    except Exception:
        return dict(summary, ok=False, reason="original durable journal unavailable")
    if store is None:
        from ..portal_calendar_store import SupabaseCalendarStore
        store = SupabaseCalendarStore()
    authority = getattr(store, "_s", store)
    tenants, batches, attempted_batches, verified_batches = set(), set(), set(), set()
    for use_id, created_at in ids:
        admission_error = None
        entry = None
        try:
            entry = journal.get(use_id)
            tenant = entry["gym_id"]
            if tenant not in tenants and len(tenants) >= MAX_TENANTS:
                summary["reason"] = "tenant work bound"
                break
            batch_id = _batch_for_use(path, entry)
            if batch_id not in batches and len(batches) >= MAX_BATCHES:
                summary["reason"] = "batch work bound"
                break
            tenants.add(tenant)
            batches.add(batch_id)
        except Exception as exc:
            # A malformed/missing frozen binding is an attempted hold, not
            # a quota deferral: advance it fairly without any remote read.
            admission_error = exc
            if isinstance(entry, dict):
                tenants.add(entry["gym_id"])
        try:
            _advance_cursor(path, use_id, created_at)
        except Exception:
            summary.update(ok=False, reason="recovery cursor unavailable")
            return summary
        summary["examined"] += 1
        try:
            if admission_error is not None:
                raise admission_error
            if not _claim_matches(path, entry):
                raise ValueError("original claim unavailable or mismatched")
            found = gbp_planner._readback_inserted_rows(
                store, tenant, [entry["calendar_row"]], max_rows=2)
            matches = [row for row in (found or []) if
                       row.get("logical_post_id") == entry["logical_post_id"]]
            if len(matches) != 1:
                raise ValueError("exact active row unavailable")
            persisted = matches[0]
            bound = _bound_member(path, str(persisted.get("id") or ""))
            if not gbp_planner._forward_recovery_member_matches(entry, persisted, bound, provisional=True):
                raise ValueError("frozen member mismatch")
            if bound["batch_id"] != batch_id:
                raise ValueError("live member differs from admitted frozen batch")
            if batch_id not in attempted_batches:
                attempted_batches.add(batch_id)
                authority.bind_forward_finalization(batch_id)
                verified_batches.add(batch_id)
            if batch_id not in verified_batches:
                raise ValueError("batch binding failed this wave")
            if authority.forward_remote_use_settlement_allowed(batch_id) is not True:
                raise ValueError("terminal settlement proof unavailable")
            bound = _bound_member(path, str(persisted.get("id") or ""))
            manifest_evidence = gbp_planner._forward_recovery_manifest_evidence(
                store, entry, persisted, bound)
            # Re-read after the PG receipt lookup; its acknowledgment is not
            # evidence that the member is still ACTIVE with these exact bytes.
            fresh = gbp_planner._readback_inserted_rows(
                store, tenant, [entry["calendar_row"]], max_rows=2)
            if (fresh is None or len(fresh) != 1
                    or not gbp_planner._forward_recovery_member_matches(
                        entry, fresh[0], bound, manifest_evidence=manifest_evidence)):
                raise ValueError("fresh active member unavailable")
            persisted = fresh[0]
            if (enabled() is not True or gbp_staged_journal_flag() is not True
                    or not gbp_planner._remote_drive_cas_enabled()
                    or not _claim_matches(path, entry)):
                raise ValueError("fresh recovery authority hold")
            if media_store is None:
                from .. import gym_media_index
                media_store = gym_media_index.default_store()
            pick = dict(kind="drive", base=tenant, asset=entry["asset_before"],
                        store=media_store, day_key=entry["post_date"],
                        claim_id=entry["claim_id"], claim_account=tenant + "_gbp",
                        journal_entry=entry)
            if not gbp_planner._settle_armed_drive_landing(
                    tenant, entry["calendar_row"], pick, persisted, log,
                    forward_binding_reader=lambda row_id: _bound_member(path, row_id),
                    manifest_evidence=manifest_evidence):
                raise ValueError("settlement held")
            summary["recovered"] += 1
        except Exception:
            summary["held"] += 1
            log(f"use {use_id}: recovery held; frozen use retained")
    summary.update(ok=not summary["held"], batches=len(batches), tenants=len(tenants))
    return summary
