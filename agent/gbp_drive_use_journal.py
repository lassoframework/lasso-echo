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
), None)
_LANDED_OMITTED_DEFAULTS['variant_status'] = 'active'
# Production content_calendar readback at 2026-10-09T00:25Z omitted mentions
# as an empty array on both rows; accept only that exact server-added value.
_LANDED_OMITTED_DEFAULTS['mentions'] = []


def _landed_row_matches(persisted, proposed):
    # Explicit server additions only. Unknown columns need contract review.
    if type(persisted) is not dict:
        return False
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
    'content_hash': ...}. Only an exact same logical row + asset + payload
    settles the entry; anything less holds. Settles one row at a time so a
    partial batch only ever confirms its exact landed rows. Lands from
    write_intent or unknown_result; proof is durable before any consumption.
    """
    def fn(conn, row):
        if row[13] not in (STATE_WRITE_INTENT, STATE_UNKNOWN):
            raise JournalHold('journal_landed_requires_intent_or_unknown')
        entry = _row_to_entry(row)
        if (type(evidence) is not dict
                or not _landed_row_matches(evidence.get('calendar_row'), entry['calendar_row'])
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
