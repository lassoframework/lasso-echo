"""Durable local journal for a future GBP remote Drive use caller. No callers wired yet.

This module is ONLY local bookkeeping in the canonical Echo SQLite DB
(`agent.db.db_path()`, overridable per call for tests). It makes no provider,
HTTP, PG, or remote calls of any kind. The separate GBP caller owns all remote
work and must call this journal at the exact integration points documented in
`INTEGRATION` below. This is not a second remote-use authority: it never
decides landing from counters or ambiguity, it only freezes identity, records
write intent before send, and accepts exact evidence afterwards.

States (forward-only except the prewrite cancel):

  prepared            identity frozen, stable use UUID minted; nothing sent
  abandoned           prewrite cancellation from `prepared` only; no remote use
  write_intent        recorded BEFORE the calendar POST is attempted
  unknown_result      POST outcome missing/ambiguous; zero-row readback and
                      timeout NEVER settle this; only exact evidence can
  confirmed_landed    exact same logical row + asset + payload evidence read
                      back and the landed proof persisted durably
  consumption_pending landed proof persisted; remote consumption invoked by the
                      caller but its receipt is not yet recorded; durable
                      through crash so settlement can resume
  receipt_confirmed   consumption receipt recorded; claim completion pending
  claim_done          exact completed claim verified; entry settled

Conflict rules:
  - prepare() for an existing (gym_id, logical_post_id) returns the SAME use
    UUID and frozen snapshots when the full identity digest matches (restart /
    retry safety), and raises JournalHold('identity_conflict') when payload,
    tenant, asset or source differ under the same logical id.
  - An `abandoned` entry releases the logical id for a genuinely new placement.
  - Every transition is a guarded UPDATE checked by rowcount; a local write or
    ack failure requires exact durable after-image readback; otherwise it holds
    with an unknown outcome and recovery must inspect the durable entry.

INTEGRATION (required caller points, caller not yet modified):
  1. After claim and BEFORE building/sending the calendar POST: prepare().
  2. Immediately BEFORE the POST: record_write_intent(); if it holds, do not send.
  3. On missing/ambiguous POST response or timeout: mark_unknown(); a zero-row
     readback may call record_zero_readback() which KEEPS unknown_result.
  4. On exact readback of the same logical row + asset + payload: confirm_landed()
     with the persisted row; settle each row individually (partial batches only
     ever settle exact landed rows).
  5. Only after confirm_landed succeeds, invoke remote consumption, then
     begin_consumption() before the call and confirm_receipt() after it; on
     crash, recover with unsettled() and resume the same UUID.
  6. Prewrite cancellation: abandon() while still `prepared`.
"""
from __future__ import annotations

from datetime import date
import json
import re
import sqlite3
import uuid

from . import remote_drive_use
from .db import db_path as _canonical_db_path
from .local_inventory_mutation import canonical_json, digest

STATE_PREPARED = 'prepared'
STATE_ABANDONED = 'abandoned'
STATE_WRITE_INTENT = 'write_intent'
STATE_UNKNOWN = 'unknown_result'
STATE_LANDED = 'confirmed_landed'
STATE_CONSUMPTION_PENDING = 'consumption_pending'
STATE_RECEIPT_CONFIRMED = 'receipt_confirmed'
STATE_CLAIM_DONE = 'claim_done'

_ACTIVE = (STATE_PREPARED, STATE_WRITE_INTENT, STATE_UNKNOWN,
           STATE_LANDED, STATE_CONSUMPTION_PENDING, STATE_RECEIPT_CONFIRMED, STATE_CLAIM_DONE)

_GYM = re.compile(r'[a-z0-9][a-z0-9_-]{0,127}\Z')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gbp_drive_use_journal (
  use_id TEXT PRIMARY KEY,
  gym_id TEXT NOT NULL,
  logical_post_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  epoch_id TEXT NOT NULL,
  post_date TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  asset_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  calendar_row TEXT NOT NULL,
  payload TEXT NOT NULL,
  asset_before TEXT NOT NULL,
  source_before TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  state TEXT NOT NULL,
  landed_proof TEXT,
  receipt TEXT,
  zero_readbacks INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS gbp_drive_use_journal_logical
  ON gbp_drive_use_journal (gym_id, logical_post_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS gbp_drive_use_journal_active_identity
  ON gbp_drive_use_journal (gym_id, logical_post_id) WHERE state != 'abandoned';
"""


class JournalHold(RuntimeError):
    """Static error codes only; never exposes database errors or credentials."""


def _ensure_schema(conn):
    for statement in _SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)


def _connect(path):
    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        conn.execute('PRAGMA busy_timeout=30000')
        conn.execute('PRAGMA synchronous=FULL')
        return conn
    except Exception:
        if conn is not None:
            _close(conn)
        raise JournalHold('journal_local_db_unavailable') from None


def _resolve_path(path):
    return str(path) if path is not None else _canonical_db_path()


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


def _rollback(conn):
    # A lost COMMIT acknowledgment may leave no transaction to roll back.
    try:
        conn.execute('ROLLBACK')
    except Exception:
        pass


def _remote_request(entry, use_id):
    return {**{k: entry[k] for k in ('gym_id', 'epoch_id', 'asset_id',
            'source_id', 'content_hash', 'post_date', 'asset_before', 'source_before')},
            'use_id': use_id}


def _commit_entry(conn, path, entry):
    try:
        conn.execute('COMMIT')
        return entry
    except Exception:
        _rollback(conn)
        # Close before reopening: uncommitted state is never durable evidence.
        _close(conn)
        try:
            recovered = get(entry['use_id'], path=path)
            if recovered == entry:
                return recovered
        except Exception:
            pass
        raise JournalHold('journal_commit_outcome_unknown') from None


def _frozen(request):
    """The immutable identity subset used for the request digest and conflicts."""
    return {k: request[k] for k in
            ('gym_id', 'logical_post_id', 'claim_id', 'post_date', 'content_hash',
             'asset_id', 'source_id', 'calendar_row', 'payload',
             'asset_before', 'source_before', 'epoch_id')}


def _validate(request):
    try:
        if type(request) is not dict:
            raise ValueError
        gym = request['gym_id']
        if not isinstance(gym, str) or not _GYM.fullmatch(gym):
            raise ValueError
        for key in ('logical_post_id', 'claim_id', 'content_hash', 'asset_id', 'source_id'):
            if (not isinstance(request[key], str) or not request[key].strip()
                    or len(request[key]) > 512):
                raise ValueError
        if date.fromisoformat(request['post_date']).isoformat() != request['post_date']:
            raise ValueError
        for key in ('calendar_row', 'payload', 'asset_before', 'source_before'):
            if type(request[key]) is not dict:
                raise ValueError
        row = request['calendar_row']
        if (row.get('logical_post_id') != request['logical_post_id']
                or row.get('gym_id') != gym
                or row.get('post_date') != request['post_date']
                or row.get('source_media_asset_id') != request['asset_id']
                or canonical_json(request['payload']) != canonical_json(row)):
            raise ValueError
        for key in ('asset_id', 'source_id', 'content_hash', 'epoch_id'):
            if key in row and row[key] != request[key]:
                raise ValueError
        remote_drive_use.validate(_remote_request(request, str(uuid.uuid4())))
        canonical_json(_frozen(request))
    except Exception:
        raise JournalHold('journal_request_invalid') from None


def _row_to_entry(row):
    (use_id, gym_id, logical_post_id, claim_id, post_date, content_hash,
     asset_id, source_id, calendar_row, payload, asset_before, source_before,
     request_digest, state, landed_proof, receipt, zero_readbacks,
     created_at, updated_at, epoch_id) = row
    return dict(
        use_id=use_id, epoch_id=epoch_id, gym_id=gym_id, logical_post_id=logical_post_id,
        claim_id=claim_id, post_date=post_date, content_hash=content_hash,
        asset_id=asset_id, source_id=source_id,
        calendar_row=json.loads(calendar_row), payload=json.loads(payload),
        asset_before=json.loads(asset_before), source_before=json.loads(source_before),
        request_digest=request_digest, state=state,
        landed_proof=json.loads(landed_proof) if landed_proof else None,
        receipt=json.loads(receipt) if receipt else None,
        zero_readbacks=zero_readbacks, created_at=created_at, updated_at=updated_at)


_SELECT = ('use_id,gym_id,logical_post_id,claim_id,post_date,content_hash,asset_id,'
           'source_id,calendar_row,payload,asset_before,source_before,request_digest,'
           'state,landed_proof,receipt,zero_readbacks,created_at,updated_at,epoch_id')


def _get_row(conn, use_id):
    return conn.execute(
        f'SELECT {_SELECT} FROM gbp_drive_use_journal WHERE use_id=?',
        (use_id,)).fetchone()


def _transition(conn, use_id, expected, code, **sets):
    """Guarded single-row transition; any local write/ack failure holds."""
    placeholders = ','.join(f'{k}=?' for k in sets)
    try:
        cur = conn.execute(
            f'UPDATE gbp_drive_use_journal SET {placeholders},'
            " updated_at=datetime('now') WHERE use_id=? AND state=?",
            (*sets.values(), use_id, expected))
        if cur.rowcount != 1:
            raise JournalHold(code)
        row = _get_row(conn, use_id)
        if row is None or row[13] != sets.get('state', row[13]):
            raise JournalHold(code)
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_write_failed') from None
    return _row_to_entry(row)


def prepare(request, *, path=None, use_id=None):
    """Freeze identity and return the durable entry, minting one stable use UUID.

    Retry/restart with the identical frozen identity under the same
    (gym_id, logical_post_id) returns the EXISTING entry and UUID unchanged.
    Any payload/asset/source/epoch change under that tenant's logical id holds.
    A different tenant has an independent key. Required epoch_id binds the
    future remote consumption receipt; no remote request is sent here.
    """
    _validate(request)
    new_id = use_id or str(uuid.uuid4())
    try:
        if str(uuid.UUID(new_id)) != new_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise JournalHold('journal_request_invalid') from None
    frozen = _frozen(request)
    request_digest = digest(frozen)
    conn = _connect(_resolve_path(path))
    try:
        conn.execute('BEGIN IMMEDIATE')
        _ensure_schema(conn)
        existing = conn.execute(
            f'SELECT {_SELECT} FROM gbp_drive_use_journal'
            ' WHERE gym_id=? AND logical_post_id=? AND state!=?'
            ' ORDER BY created_at DESC LIMIT 1',
            (request['gym_id'], request['logical_post_id'], STATE_ABANDONED)).fetchone()
        if existing:
            entry = _row_to_entry(existing)
            if entry['request_digest'] != request_digest:
                raise JournalHold('journal_identity_conflict')
            return _commit_entry(conn, path, entry)
        conn.execute(
            'INSERT INTO gbp_drive_use_journal'
            ' (use_id,gym_id,logical_post_id,claim_id,post_date,content_hash,'
            ' asset_id,source_id,calendar_row,payload,asset_before,source_before,'
            ' request_digest,state,epoch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (new_id, request['gym_id'], request['logical_post_id'], request['claim_id'],
             request['post_date'], request['content_hash'], request['asset_id'],
             request['source_id'], canonical_json(request['calendar_row']),
             canonical_json(request['payload']), canonical_json(request['asset_before']),
             canonical_json(request['source_before']), request_digest, STATE_PREPARED, request['epoch_id']))
        entry = _row_to_entry(_get_row(conn, new_id))
        return _commit_entry(conn, path, entry)
    except JournalHold:
        _rollback(conn)
        raise
    except Exception:
        _rollback(conn)
        raise JournalHold('journal_local_write_failed') from None
    finally:
        _close(conn)


def _with_entry(path, use_id, fn):
    conn = _connect(_resolve_path(path))
    try:
        conn.execute('BEGIN IMMEDIATE')
        _ensure_schema(conn)
        row = _get_row(conn, use_id)
        if row is None:
            raise JournalHold('journal_entry_missing')
        entry = fn(conn, row)
        return _commit_entry(conn, path, entry)
    except JournalHold:
        _rollback(conn)
        raise
    except Exception:
        _rollback(conn)
        raise JournalHold('journal_local_write_failed') from None
    finally:
        _close(conn)


def abandon(use_id, *, path=None):
    """Prewrite cancellation. Legal only from `prepared`; records no remote use."""
    return _with_entry(path, use_id, lambda conn, row: _transition(
        conn, use_id, STATE_PREPARED, 'journal_abandon_requires_prepared',
        state=STATE_ABANDONED))


def record_write_intent(use_id, *, path=None):
    """MUST be recorded before the calendar POST. prepared -> write_intent."""
    return _with_entry(path, use_id, lambda conn, row: _transition(
        conn, use_id, STATE_PREPARED, 'journal_intent_requires_prepared',
        state=STATE_WRITE_INTENT))


def mark_unknown(use_id, *, path=None):
    """POST response missing/ambiguous. write_intent -> unknown_result."""
    return _with_entry(path, use_id, lambda conn, row: _transition(
        conn, use_id, STATE_WRITE_INTENT, 'journal_unknown_requires_intent',
        state=STATE_UNKNOWN))


def record_zero_readback(use_id, *, path=None):
    """A zero-row readback after timeout is NOT non-landing proof; stays unknown."""
    def fn(conn, row):
        if row[13] != STATE_UNKNOWN:
            raise JournalHold('journal_zero_readback_requires_unknown')
        return _transition(conn, use_id, STATE_UNKNOWN, 'journal_local_write_failed',
                           zero_readbacks=row[16] + 1, state=STATE_UNKNOWN)
    return _with_entry(path, use_id, fn)


# Omitted fields accepted ONLY at these safe values. Sources are repository
# contracts, not inferred arbitrary select=* keys:
# - portal_calendar_store._CORE_VISUAL_MEDIA_CAS_COLUMNS: mirrored nullable
#   media/scheduling/claim fields, plus active variant visibility contract.
# - calendar_publish_claim_ownership_20260918.sql / publish_day_capacity:
#   nullable ownership fields; content_calendar_media_not_ready_20260828.sql.
# - calendar_lever_columns_20260826.sql, experiment_column_20260826.sql,
#   cadence_20260827.sql, content_calendar_event_id_20260828.sql.
# - calendar_approval_provenance_20261005.sql: nullable approval provenance.
# - GBP_BUILD_SPEC.md section 4: nullable GBP columns.
# variant_of null names the anchor itself in frozen production
# tests/fixtures/forward_lock_entry/portal-function-definitions-20261008.sql.
# The baseline CREATE TABLE is not present in this repository. New/unknown
# columns remain held until a source-confirmed contract extends this mapping.
_LANDED_OMITTED_DEFAULTS = dict.fromkeys((
    'thumbnail_url', 'source_media_url', 'time_slot', 'slot_index',
    'media_not_ready_reason', 'scheduled_at', 'published_at', 'late_post_id',
    'publish_claim_token', 'publish_reservation_day', 'variant_of',
    'hook_family', 'ask_type', 'caption_len_band', 'has_member_face',
    'experiment_label', 'event_id', 'approval_kind', 'approved_by',
    'approved_at', 'approval_digest', 'gbp_topic_type', 'gbp_cta_type',
    'gbp_cta_url', 'gbp_event', 'gbp_offer', 'gbp_location_id', 'reject_reason',
    # generated_approval_pins_20261007 clears pins for non-generated media.
    'generated_authority_pins', 'render_manifest_digest',
), None)
_LANDED_OMITTED_DEFAULTS['variant_status'] = 'active'
# Production content_calendar readback at 2026-10-09T00:25Z omitted mentions
# as an empty array on both rows; accept only that exact server-added value.
_LANDED_OMITTED_DEFAULTS['mentions'] = []


def _landed_row_matches(persisted, proposed, *, manifest_digest=None):
    # Explicit server additions only. Unknown columns need contract review.
    if type(persisted) is not dict:
        return False
    # The trusted staged binder may fill this initially-null field. Callers
    # must separately verify its exact current reservation/manifest evidence;
    # this argument is an exact value, never permission to ignore the column.
    if manifest_digest is not None:
        if (not isinstance(manifest_digest, str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_digest)
                or persisted.get('render_manifest_digest') != manifest_digest
                or proposed.get('render_manifest_digest') not in (None, manifest_digest)):
            return False
        proposed = dict(proposed, render_manifest_digest=manifest_digest)
    if not set(proposed) <= set(persisted):
        return False
    try:
        if canonical_json({k: persisted[k] for k in proposed}) != canonical_json(proposed):
            return False
    except Exception:
        return False
    extras = set(persisted) - set(proposed)
    if extras - ({'id', 'created_at', 'updated_at'} | set(_LANDED_OMITTED_DEFAULTS)):
        return False
    for k in extras:
        v = persisted[k]
        if k in _LANDED_OMITTED_DEFAULTS:
            if canonical_json(v) != canonical_json(_LANDED_OMITTED_DEFAULTS[k]):
                return False
        elif k == 'id':
            if not isinstance(v, (str, int)) or isinstance(v, bool) or not v:
                return False
        elif not isinstance(v, str) or not v:
            return False
    return True


def confirm_landed(use_id, evidence, *, path=None):
    """Persist the exact landed proof. Evidence must match the frozen identity.

    The frozen payload is the exact single proposed calendar row (the planner
    hands a list of these rows to insert_rows). Batch/envelope payloads hold.
    Persisted rows may add id/created_at/updated_at and source-confirmed safe
    omitted defaults in _LANDED_OMITTED_DEFAULTS. This also accepts nulls added
    by the store's heterogeneous row key-union normalization; every proposed
    field remains exact. Unknown schema fields hold until contract review.

    `evidence` is {'calendar_row': <persisted row read back>, 'asset_id': ...,
    'content_hash': ...}. A manifest added by staged preparation additionally
    requires `forward_manifest_evidence` from the trusted PG snapshot and
    active reservation RPCs, bound to the finalized local batch. That evidence
    is persisted in the landed proof. Only the same logical row + asset + payload
    settles the entry; anything less holds. Settles one row at a time so a
    partial batch only ever confirms its exact landed rows. Lands from
    write_intent or unknown_result; proof is durable before any consumption.
    """
    def fn(conn, row):
        if row[13] not in (STATE_WRITE_INTENT, STATE_UNKNOWN):
            raise JournalHold('journal_landed_requires_intent_or_unknown')
        entry = _row_to_entry(row)
        manifest_digest = None
        if type(evidence) is dict and evidence.get('forward_manifest_evidence') is not None:
            persisted = evidence.get('calendar_row')
            bound = forward_stage_for_member(str((persisted or {}).get('id') or ''), path=path)
            if not forward_manifest_evidence_matches(
                    entry, persisted, bound, evidence['forward_manifest_evidence']):
                raise JournalHold('journal_landed_evidence_mismatch')
            manifest_digest = evidence['forward_manifest_evidence']['snapshot']['render_manifest_digest']
        if (type(evidence) is not dict
                or not _landed_row_matches(evidence.get('calendar_row'), entry['calendar_row'],
                                           manifest_digest=manifest_digest)
                or evidence.get('asset_id') != entry['asset_id']
                or evidence.get('content_hash') != entry['content_hash']
                or ('use_id' in evidence and evidence['use_id'] != use_id)
                or any(k in evidence and canonical_json(evidence[k]) != canonical_json(entry[k]) for k in
                       ('gym_id', 'source_id', 'logical_post_id', 'payload',
                        'asset_before', 'source_before', 'epoch_id'))):
            raise JournalHold('journal_landed_evidence_mismatch')
        proof = dict(evidence, use_id=use_id, logical_post_id=entry['logical_post_id'])
        return _transition(conn, use_id, row[13], 'journal_local_write_failed',
                           state=STATE_LANDED, landed_proof=canonical_json(proof))
    return _with_entry(path, use_id, fn)


def begin_consumption(use_id, *, path=None):
    """Caller is about to invoke remote consumption; landed proof is already durable."""
    return _with_entry(path, use_id, lambda conn, row: _transition(
        conn, use_id, STATE_LANDED, 'journal_consumption_requires_landed',
        state=STATE_CONSUMPTION_PENDING))


def confirm_receipt(use_id, receipt, *, path=None):
    """Settle only an authoritative exact receipt verified by remote_drive_use.

    This checks evidence structure and frozen binding locally. The caller must
    obtain the receipt from the authoritative remote API, never fabricate it.
    """
    def fn(conn, row):
        if row[13] != STATE_CONSUMPTION_PENDING:
            raise JournalHold('journal_receipt_requires_pending')
        entry = _row_to_entry(row)
        try:
            request = _remote_request(entry, use_id)
            remote_drive_use.validate(request)
            remote_drive_use._verify(receipt, request)
            if (canonical_json(receipt['request']) != canonical_json(request)
                    or canonical_json(receipt['source_after']) != canonical_json(request['source_before'])):
                raise ValueError
            stable = set(request['asset_before']) - {'used_count', 'last_used_at', 'drive_use_version'}
            if canonical_json({k: receipt['asset_after'][k] for k in stable}) != canonical_json(
                    {k: request['asset_before'][k] for k in stable}):
                raise ValueError
            serialized = canonical_json(receipt)
        except Exception:
            raise JournalHold('journal_receipt_invalid') from None
        return _transition(conn, use_id, STATE_CONSUMPTION_PENDING,
                           'journal_receipt_requires_pending',
                           state=STATE_RECEIPT_CONFIRMED, receipt=serialized)
    return _with_entry(path, use_id, fn)


def confirm_claim_done(use_id, *, path=None):
    """Mark terminal only after exact claim status and asset binding readback."""
    def fn(conn, row):
        entry = _row_to_entry(row)
        if entry['state'] not in (STATE_RECEIPT_CONFIRMED, STATE_CLAIM_DONE):
            raise JournalHold('journal_claim_done_requires_receipt')
        try:
            claim = conn.execute(
                'SELECT status,post_id FROM socialapi_claims WHERE draft_id=? AND account_key=?',
                (entry['claim_id'], entry['gym_id'] + '_gbp')).fetchone()
        except Exception:
            raise JournalHold('journal_claim_readback_unavailable') from None
        if claim != ('done', entry['asset_id']):
            raise JournalHold('journal_claim_done_unverified')
        if entry['state'] == STATE_CLAIM_DONE:
            return entry
        return _transition(conn, use_id, STATE_RECEIPT_CONFIRMED,
                           'journal_claim_done_requires_receipt', state=STATE_CLAIM_DONE)
    return _with_entry(path, use_id, fn)


def get(use_id, *, path=None):
    conn = _connect(_resolve_path(path))
    try:
        _ensure_schema(conn)
        row = _get_row(conn, use_id)
        return _row_to_entry(row) if row else None
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_db_unavailable') from None
    finally:
        _close(conn)


def get_by_logical_post(gym_id, logical_post_id, *, path=None):
    conn = _connect(_resolve_path(path))
    try:
        _ensure_schema(conn)
        row = conn.execute(
            f'SELECT {_SELECT} FROM gbp_drive_use_journal'
            ' WHERE gym_id=? AND logical_post_id=? AND state!=?'
            ' ORDER BY created_at DESC LIMIT 1',
            (gym_id, logical_post_id, STATE_ABANDONED)).fetchone()
        return _row_to_entry(row) if row else None
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_db_unavailable') from None
    finally:
        _close(conn)


def unsettled(*, path=None):
    """Active entries not yet claim-completed; crash-recovery work list."""
    conn = _connect(_resolve_path(path))
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            f'SELECT {_SELECT} FROM gbp_drive_use_journal WHERE state IN (?,?,?,?,?,?)'
            ' ORDER BY created_at',
            (STATE_PREPARED, STATE_WRITE_INTENT, STATE_UNKNOWN,
             STATE_LANDED, STATE_CONSUMPTION_PENDING, STATE_RECEIPT_CONFIRMED)).fetchall()
        return [_row_to_entry(r) for r in rows]
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_db_unavailable') from None
    finally:
        _close(conn)


# ---- GBP forward staged-batch binding (OFF-by-default caller, 2026-10-09) ----
#
# Durable binding for the two-phase forward-schedule batch contract. The
# calendar store's in-memory last_forward_stage_attempt survives only one
# process; this journal persists the EXACT stage attempt identity (batch id,
# canonical tenant, request digest, exact request text, member and old-row
# sets) BEFORE the stage RPC so a lost stage/finalizer acknowledgment or a
# restart recovers through the bound batch-status RPC -- never through
# calendar row presence or a regenerated request.
#
# States (forward-only):
#   stage_intent    attempt identity frozen durably BEFORE the stage RPC;
#                   the stage outcome is unknown until an exact receipt binds
#   staged_pending  exact `staged` receipt bound; the batch is INACTIVE
#                   candidate rows only -- never a landed placement, never
#                   remote Drive use
#   finalized       exact terminal finalize receipt from the batch-status RPC
#                   bound against the frozen identity; only now may the GBP
#                   caller settle remote use for an active member readback
#
# A candidate row is not consumption evidence, and the current use counter is
# never consulted here. Nothing in this section decides landing from
# ambiguity; every transition is a guarded single-row UPDATE.

STAGE_STATE_INTENT = 'stage_intent'
STAGE_STATE_STAGED = 'staged_pending'
STAGE_STATE_FINALIZED = 'finalized'

_STAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS gbp_forward_stage_journal (
  batch_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  request_text TEXT NOT NULL,
  member_row_ids TEXT NOT NULL,
  old_row_ids TEXT NOT NULL,
  state TEXT NOT NULL,
  stage_receipt TEXT,
  finalize_receipt TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_STAGE_SELECT = ('batch_id,tenant_id,request_digest,request_text,member_row_ids,'
                 'old_row_ids,state,stage_receipt,finalize_receipt,created_at,updated_at')

_UUID_RE = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                      r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')
_SHA256_RE = re.compile(r'[0-9a-f]{64}\Z')


def _ensure_stage_schema(conn):
    conn.execute(_STAGE_SCHEMA)


def _stage_row_to_entry(row):
    (batch_id, tenant_id, request_digest, request_text, member_row_ids,
     old_row_ids, state, stage_receipt, finalize_receipt,
     created_at, updated_at) = row
    return dict(
        batch_id=batch_id, tenant_id=tenant_id, request_digest=request_digest,
        request_text=request_text,
        member_row_ids=json.loads(member_row_ids),
        old_row_ids=json.loads(old_row_ids), state=state,
        stage_receipt=json.loads(stage_receipt) if stage_receipt else None,
        finalize_receipt=json.loads(finalize_receipt) if finalize_receipt else None,
        created_at=created_at, updated_at=updated_at)


def _get_stage_row(conn, batch_id):
    return conn.execute(
        f'SELECT {_STAGE_SELECT} FROM gbp_forward_stage_journal WHERE batch_id=?',
        (batch_id,)).fetchone()


def _validate_stage_attempt(attempt):
    """Strict local shape/consistency check of one frozen stage attempt."""
    import hashlib
    try:
        if type(attempt) is not dict:
            raise ValueError
        batch_id = attempt['batch_id']
        tenant_id = attempt['tenant_id']
        request_digest = attempt['request_digest']
        request_text = attempt['request_text']
        if (not isinstance(batch_id, str) or not _UUID_RE.fullmatch(batch_id)
                or not isinstance(tenant_id, str) or not tenant_id.strip()
                or len(tenant_id) > 512
                or not isinstance(request_digest, str)
                or not _SHA256_RE.fullmatch(request_digest)
                or not isinstance(request_text, str) or not request_text.strip()):
            raise ValueError
        if hashlib.sha256(request_text.encode('utf-8')).hexdigest() != request_digest:
            raise ValueError
        request = json.loads(request_text)
        if (not isinstance(request, dict) or request.get('tenant_id') != tenant_id
                or not isinstance(request.get('members'), list)
                or not request['members'] or len(request['members']) > 100
                or not isinstance(request.get('old_rows'), list)):
            raise ValueError
        for key in ('member_row_ids', 'old_row_ids'):
            ids = attempt[key]
            if (not isinstance(ids, list)
                    or any(not isinstance(i, str) or not _UUID_RE.fullmatch(i)
                           for i in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError
        members = request['members']
        member_ids = [str((m.get('row') or {}).get('id')) for m in members
                      if isinstance(m, dict) and isinstance(m.get('row'), dict)]
        if (len(member_ids) != len(members)
                or sorted(member_ids) != sorted(attempt['member_row_ids'])):
            raise ValueError
        old_ids = [str(o.get('id')) for o in request['old_rows']
                   if isinstance(o, dict)]
        if (len(old_ids) != len(request['old_rows'])
                or sorted(old_ids) != sorted(attempt['old_row_ids'])):
            raise ValueError
        if set(attempt['member_row_ids']) & set(attempt['old_row_ids']):
            raise ValueError
    except Exception:
        raise JournalHold('stage_attempt_invalid') from None


def _receipt_matches_attempt(receipt, entry, state):
    """True only when an RPC payload exactly matches the bound identity."""
    try:
        if (type(receipt) is not dict
                or receipt.get('batch_id') != entry['batch_id']
                or receipt.get('tenant_id') != entry['tenant_id']
                or receipt.get('request_digest') != entry['request_digest']
                or receipt.get('state') != state
                or receipt.get('member_row_ids') != entry['member_row_ids']
                or receipt.get('old_row_ids') != entry['old_row_ids']):
            return False
        observed = receipt.get('observation_row_ids')
        if observed is not None:
            if (not isinstance(observed, list)
                    or any(i not in set(entry['member_row_ids']) for i in observed)):
                return False
        return True
    except Exception:
        return False


def _terminal_receipt_matches(entry, receipt):
    """True only for a complete exact terminal finalize receipt bound to the
    frozen identity: same batch/tenant/digest, exactly the member set as
    activated row ids, one reservation id per row, and exactly the frozen
    old-row set archived. Same-day logical siblings may share a reservation;
    group lineage is validated by the finalizer authority. Anything less is
    unknown, never finalized."""
    try:
        if (type(receipt) is not dict
                or receipt.get('batch_id') != entry['batch_id']
                or receipt.get('state') != STAGE_STATE_FINALIZED
                or receipt.get('tenant_id') != entry['tenant_id']
                or receipt.get('request_digest') != entry['request_digest']):
            return False
        row_ids = receipt.get('row_ids')
        reservation_ids = receipt.get('reservation_ids')
        archived = receipt.get('archived_old_row_ids')
        if (not isinstance(row_ids, list) or not isinstance(reservation_ids, list)
                or not isinstance(archived, list)
                or len(row_ids) != len(reservation_ids)
                or len(row_ids) != len(entry['member_row_ids'])
                or len(set(map(str, row_ids))) != len(row_ids)
                or set(map(str, row_ids)) != set(entry['member_row_ids'])
                or set(map(str, archived)) != set(entry['old_row_ids'])
                or len(set(map(str, archived))) != len(archived)):
            return False
        for value in list(row_ids) + list(reservation_ids) + list(archived):
            if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
                return False
        return True
    except Exception:
        return False


def _with_stage_entry(path, batch_id, fn):
    conn = _connect(_resolve_path(path))
    try:
        conn.execute('BEGIN IMMEDIATE')
        _ensure_stage_schema(conn)
        row = _get_stage_row(conn, batch_id)
        if row is None:
            raise JournalHold('stage_entry_missing')
        entry = fn(conn, row)
        try:
            conn.execute('COMMIT')
            return entry
        except Exception:
            _rollback(conn)
            _close(conn)
            recovered = get_forward_stage(batch_id, path=path)
            if recovered == entry:
                return recovered
            raise JournalHold('journal_commit_outcome_unknown') from None
    except JournalHold:
        _rollback(conn)
        raise
    except Exception:
        _rollback(conn)
        raise JournalHold('journal_local_write_failed') from None
    finally:
        _close(conn)


def _stage_transition(conn, batch_id, expected, code, **sets):
    placeholders = ','.join(f'{k}=?' for k in sets)
    try:
        cur = conn.execute(
            f'UPDATE gbp_forward_stage_journal SET {placeholders},'
            " updated_at=datetime('now') WHERE batch_id=? AND state=?",
            (*sets.values(), batch_id, expected))
        if cur.rowcount != 1:
            raise JournalHold(code)
        row = _get_stage_row(conn, batch_id)
        if row is None or row[6] != sets.get('state', row[6]):
            raise JournalHold(code)
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_write_failed') from None
    return _stage_row_to_entry(row)


def record_forward_stage_intent(attempt, *, path=None):
    """Freeze the exact stage attempt identity durably BEFORE the stage RPC.

    An exact retry (same batch id, tenant, digest, request text and member /
    old-row sets) returns the existing entry unchanged -- restart and replay
    safety. Any identity difference under the same batch id holds as
    'stage_identity_conflict': the same batch id must never carry two
    different requests."""
    _validate_stage_attempt(attempt)
    conn = _connect(_resolve_path(path))
    try:
        conn.execute('BEGIN IMMEDIATE')
        _ensure_stage_schema(conn)
        row = _get_stage_row(conn, attempt['batch_id'])
        if row is not None:
            entry = _stage_row_to_entry(row)
            bound = {k: entry[k] for k in
                     ('batch_id', 'tenant_id', 'request_digest', 'request_text',
                      'member_row_ids', 'old_row_ids')}
            if canonical_json(bound) != canonical_json({k: attempt[k] for k in bound}):
                raise JournalHold('stage_identity_conflict')
            _rollback(conn)
            return entry
        # Conflicting member ownership is rejected BEFORE the stage RPC: a
        # calendar row id already bound to a different durable batch must
        # never be claimed by a second attempt (cross-tenant or otherwise).
        claimed = set()
        for other in conn.execute(
                f'SELECT member_row_ids FROM gbp_forward_stage_journal').fetchall():
            claimed.update(json.loads(other[0]))
        if claimed & set(attempt['member_row_ids']):
            raise JournalHold('stage_member_conflict')
        conn.execute(
            'INSERT INTO gbp_forward_stage_journal'
            ' (batch_id,tenant_id,request_digest,request_text,member_row_ids,'
            ' old_row_ids,state) VALUES (?,?,?,?,?,?,?)',
            (attempt['batch_id'], attempt['tenant_id'], attempt['request_digest'],
             attempt['request_text'], canonical_json(attempt['member_row_ids']),
             canonical_json(attempt['old_row_ids']), STAGE_STATE_INTENT))
        entry = _stage_row_to_entry(_get_stage_row(conn, attempt['batch_id']))
        try:
            conn.execute('COMMIT')
            return entry
        except Exception:
            _rollback(conn)
            _close(conn)
            recovered = get_forward_stage(attempt['batch_id'], path=path)
            if recovered == entry:
                return recovered
            raise JournalHold('journal_commit_outcome_unknown') from None
    except JournalHold:
        _rollback(conn)
        raise
    except Exception:
        _rollback(conn)
        raise JournalHold('journal_local_write_failed') from None
    finally:
        _close(conn)


def record_forward_stage_receipt(batch_id, receipt, *, path=None):
    """Bind an exact validated `staged` receipt. The batch remains INACTIVE
    candidates: this state never authorizes remote consumption."""
    def fn(conn, row):
        entry = _stage_row_to_entry(row)
        if not _receipt_matches_attempt(receipt, entry, 'staged'):
            raise JournalHold('stage_receipt_mismatch')
        if entry['state'] == STAGE_STATE_STAGED:
            if canonical_json(entry['stage_receipt']) != canonical_json(receipt):
                raise JournalHold('stage_receipt_mismatch')
            return entry
        if entry['state'] != STAGE_STATE_INTENT:
            raise JournalHold('stage_receipt_requires_intent')
        return _stage_transition(conn, batch_id, STAGE_STATE_INTENT,
                                 'stage_receipt_requires_intent',
                                 state=STAGE_STATE_STAGED,
                                 stage_receipt=canonical_json(receipt))
    return _with_stage_entry(path, batch_id, fn)


def record_forward_finalized(batch_id, status, *, path=None):
    """Bind an exact terminal finalize receipt from the batch-status RPC.

    `status` must be the strictly parsed batch-status payload (state
    `finalized`) whose identity fields exactly match the frozen attempt and
    whose finalize receipt carries the complete terminal proof. Any mismatch
    holds: the entry stays staged_pending and remote use stays forbidden."""
    def fn(conn, row):
        entry = _stage_row_to_entry(row)
        if entry['state'] == STAGE_STATE_FINALIZED:
            if (isinstance(status, dict)
                    and _terminal_receipt_matches(entry, status.get('finalize_receipt'))
                    and _receipt_matches_attempt(status, entry, STAGE_STATE_FINALIZED)
                    and canonical_json(entry['finalize_receipt'])
                    == canonical_json(status['finalize_receipt'])):
                return entry
            raise JournalHold('stage_finalize_mismatch')
        # Recovery: a lost stage acknowledgment followed by a lost finalizer
        # acknowledgment leaves stage_intent; the exact terminal proof settles
        # it directly. Anything less stays put.
        if entry['state'] not in (STAGE_STATE_INTENT, STAGE_STATE_STAGED):
            raise JournalHold('stage_finalize_requires_staged')
        if (not _receipt_matches_attempt(status, entry, STAGE_STATE_FINALIZED)
                or not _terminal_receipt_matches(entry, status.get('finalize_receipt'))):
            raise JournalHold('stage_finalize_mismatch')
        return _stage_transition(conn, batch_id, entry['state'],
                                 'stage_finalize_requires_staged',
                                 state=STAGE_STATE_FINALIZED,
                                 finalize_receipt=canonical_json(status['finalize_receipt']))
    return _with_stage_entry(path, batch_id, fn)


def get_forward_stage(batch_id, *, path=None):
    conn = _connect(_resolve_path(path))
    try:
        _ensure_stage_schema(conn)
        row = _get_stage_row(conn, batch_id)
        return _stage_row_to_entry(row) if row else None
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_db_unavailable') from None
    finally:
        _close(conn)


def pending_forward_stages(tenant_id=None, *, path=None):
    """Durable restart recovery list: non-finalized stage attempts. A lost
    stage/finalizer acknowledgment is resolved ONLY by binding the batch
    status RPC to these frozen identities."""
    conn = _connect(_resolve_path(path))
    try:
        _ensure_stage_schema(conn)
        if tenant_id is None:
            rows = conn.execute(
                f'SELECT {_STAGE_SELECT} FROM gbp_forward_stage_journal'
                ' WHERE state IN (?,?) ORDER BY created_at',
                (STAGE_STATE_INTENT, STAGE_STATE_STAGED)).fetchall()
        else:
            rows = conn.execute(
                f'SELECT {_STAGE_SELECT} FROM gbp_forward_stage_journal'
                ' WHERE state IN (?,?) AND tenant_id=? ORDER BY created_at',
                (STAGE_STATE_INTENT, STAGE_STATE_STAGED, tenant_id)).fetchall()
        return [_stage_row_to_entry(r) for r in rows]
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_db_unavailable') from None
    finally:
        _close(conn)


def forward_remote_use_allowed(batch_id, *, path=None):
    """True ONLY when the bound batch reached `finalized` with an exact
    terminal receipt. A staged candidate, an unknown stage outcome, a missing
    entry, or an unreadable journal is never consumption evidence."""
    try:
        entry = get_forward_stage(batch_id, path=path)
    except JournalHold:
        return False
    return forward_finalized_proof_valid(entry)


def forward_finalized_proof_valid(entry):
    """Revalidate frozen identity and terminal proof at the consumption boundary.

    A persisted finalized label alone is insufficient: corrupted or incomplete
    stored JSON must never authorize remote use, even after a prior transition.
    """
    try:
        _validate_stage_attempt(entry)
        return (entry['state'] == STAGE_STATE_FINALIZED
                and _terminal_receipt_matches(entry, entry.get('finalize_receipt')))
    except Exception:
        return False


def forward_stage_for_member(member_row_id, *, path=None):
    """The durable stage entry whose bound member set contains this exact
    calendar row id, or None. Never trusts the row's own marker text."""
    if not isinstance(member_row_id, str) or not _UUID_RE.fullmatch(member_row_id):
        return None
    conn = _connect(_resolve_path(path))
    try:
        _ensure_stage_schema(conn)
        rows = conn.execute(
            f'SELECT {_STAGE_SELECT} FROM gbp_forward_stage_journal'
            ' ORDER BY created_at').fetchall()
        matches = [_stage_row_to_entry(row) for row in rows
                   if member_row_id
                   in _stage_row_to_entry(row)['member_row_ids']]
        if len(matches) > 1:
            # Conflicting cross-batch ownership: never settle from the first
            # match; the member binding is ambiguous and remote use holds.
            raise JournalHold('stage_member_ambiguous')
        return matches[0] if matches else None
    except JournalHold:
        raise
    except Exception:
        raise JournalHold('journal_local_db_unavailable') from None
    finally:
        _close(conn)


def forward_stage_member_row(entry, member_row_id, *, path=None):
    """The exact FROZEN member row for one bound calendar row id, from the
    durable request text, or None. Settlement compares this frozen row --
    never the live row's mutable marker fields -- against the readback."""
    try:
        if not isinstance(entry, dict):
            return None
        request = json.loads(entry['request_text'])
        if not isinstance(request, dict):
            return None
        for member in request.get('members') or []:
            row = member.get('row') if isinstance(member, dict) else None
            if isinstance(row, dict) and str(row.get('id')) == str(member_row_id):
                return row
    except Exception:
        return None
    return None


def forward_stage_source_sha256(bound, row_id):
    """Frozen producer byte identity, used only to query trusted PG authority.

    An observation is not source eligibility or manifest authority. Its exact
    SHA is a request binding which the reservation RPC independently verifies.
    Missing observations cannot bootstrap a manifest from a live row.
    """
    import hashlib
    try:
        _validate_stage_attempt(bound)
        request = json.loads(bound['request_text'])
        members = [m for m in request['members'] if m['row']['id'] == row_id]
        if len(members) != 1:
            return None
        member = members[0]
        packet = member['observation']
        observation = json.loads(packet['observation_json'])
        raw = packet['digest_input']
        sha = observation['source_sha256']
        row = member['row']
        if (json.loads(raw) != {k: v for k, v in observation.items() if k != 'observation_digest'}
                or hashlib.sha256(raw.encode()).hexdigest() != observation['observation_digest']
                or observation['tenant'] != bound['tenant_id']
                or observation['source_asset_id'] != row['source_media_asset_id']
                or observation['source_exact_url'] != row['source_media_url']
                or observation['delivered_exact_url'] != row['image_url']
                or not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha)):
            return None
        return sha
    except Exception:
        return None


def forward_manifest_evidence_matches(entry, persisted, bound, evidence):
    """Bind trusted snapshot + active reservation proof to the frozen use.

    The planner obtains both objects through the existing service-only PG
    RPCs. The journal rechecks and persists them with the landing before CAS.
    The proof's revision must be the current snapshot revision, and its
    reservation must be the exact member's terminal batch reservation.
    """
    try:
        if (type(persisted) is not dict or type(evidence) is not dict
                or not forward_finalized_proof_valid(bound)
                or bound['tenant_id'] != entry['gym_id']
                or persisted.get('variant_status') != 'active'
                or persisted.get('media_not_ready_reason') is not None):
            return False
        snapshot, proof = evidence['snapshot'], evidence['reservation_proof']
        row_id = persisted['id']
        member = forward_stage_member_row(bound, row_id)
        sha = forward_stage_source_sha256(bound, row_id)
        if (type(snapshot) is not dict or type(proof) is not dict or sha is None
                or not isinstance(member, dict)
                or member.get('logical_post_id') != entry['logical_post_id']
                or persisted.get('source_media_asset_id') != entry['asset_id']):
            return False
        fields = {'calendar_row_id': 'id', 'gym_id': 'gym_id', 'account': 'account',
                  'format': 'format', 'gbp_location_id': 'gbp_location_id',
                  'post_date': 'post_date', 'group_key': 'visual_group_key',
                  'source_asset_id': 'source_media_asset_id', 'source_url': 'source_media_url',
                  'image_url': 'image_url', 'thumbnail_url': 'thumbnail_url',
                  'render_manifest_digest': 'render_manifest_digest'}
        if (any(k not in snapshot or snapshot[k] != persisted.get(v) for k, v in fields.items())
                or snapshot.get('tenant_id') != entry['gym_id']
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', snapshot['render_manifest_digest'])
                or not isinstance(snapshot.get('revision'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', snapshot['revision'])
                or proof.get('row_revision') != snapshot['revision']
                or proof.get('tenant_id') != entry['gym_id']
                or proof.get('post_date') != entry['post_date']
                or proof.get('logical_post_id') != entry['logical_post_id']
                or proof.get('source_sha256') != sha
                or not _UUID_RE.fullmatch(str(proof.get('lineage_evidence_id') or ''))
                or not isinstance(proof.get('attestation_ids'), list)
                or len(proof['attestation_ids']) != 3
                or len(set(proof['attestation_ids'])) != 3
                or any(not _UUID_RE.fullmatch(str(a)) for a in proof['attestation_ids'])):
            return False
        terminal = bound['finalize_receipt']
        index = terminal['row_ids'].index(row_id)
        return proof.get('reservation_id') == terminal['reservation_ids'][index]
    except Exception:
        return False
