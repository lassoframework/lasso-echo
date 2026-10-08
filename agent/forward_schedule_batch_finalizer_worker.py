"""Isolated, default-OFF service-role finalizer for staged forward schedule batches.

Two-phase schedule contract (DRAFT 2026-10-08): the planner stages one immutable
inactive batch; the isolated owner/attester lanes prepare trusted evidence; THIS
worker (service_role ONLY, never planner/owner/attester credentials) finalizes
exactly once per batch:

  1. Poll public.forward_schedule_batch_status_20261008 for the exact batch and
     bind tenant/digest/member_row_ids/old_row_ids from the persisted readback.
  2. Verify complete owner/visual/reservation proof for EVERY member from
     persisted trusted evidence only (eligibility predicate, snapshot revision,
     lineage row, three role attestations bound to the member's immutable
     content/media binding).
  3. Invoke public.finalize_forward_schedule_staged_batch_20261008 exactly once
     with candidates = full persisted membership and p_expected_old_rows = the
     PERSISTED old snapshots (never a refrozen live read; this module never
     reads content_calendar at all).
  4. Resolve ANY ambiguous/lost finalize response SOLELY by re-reading the
     persisted terminal receipt through the status RPC. Success is never
     inferred from mutable calendar rows.

The SQL authority re-checks everything (tenant, digest, membership equality,
old-row CAS, per-candidate proofs) in one transaction; a definite refusal is a
held batch, never a partial outcome. No flag activation, no provider send, no
client calendar mutation, no coach review happens here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import threading

from .portal_calendar_store import (
    ReservationArgumentError,
    ReservationGateError,
    ReservationHoldError,
    ReservationStoreError,
    SupabaseCalendarStore,
    _SHA256_RE,
    forward_reservation_flag,
)
from . import forward_media_visual_index as visual_index

WORKER_ENV = 'AGENT_FORWARD_SCHEDULE_FINALIZER'
TENANTS_ENV = 'AGENT_FORWARD_SCHEDULE_FINALIZER_TENANTS'
BATCHES_ENV = 'AGENT_FORWARD_SCHEDULE_FINALIZER_BATCHES'
_FORBIDDEN_CREDENTIALS = (
    'AGENT_FORWARD_MEDIA_ATTESTER_DSN', 'AGENT_FORWARD_MEDIA_OWNER_DSN',
    'AGENT_VISUAL_RECEIPT_OWNER_DSN', 'AGENT_SOCIALAPI_KEY',
    'AGENT_SOCIALAPI_ENC_KEY', 'ZERNIO_API_KEY', 'AGENT_GBP_ACCESS_TOKEN',
)
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_REVISION = re.compile(r'[0-9a-f]{32}\Z')

_STATUS_RPC = 'forward_schedule_batch_status_20261008'
_FINALIZE_RPC = 'finalize_forward_schedule_staged_batch_20261008'
_ELIGIBLE_RPC = 'forward_schedule_preparation_eligible_20261008'
_SNAPSHOT_RPC = 'fixer_forward_media_attestation_request_20261006'
_BATCH_TABLE = 'forward_schedule_stage_batch_20261008'
_MEMBER_TABLE = 'forward_schedule_stage_member_20261008'
_OLD_ROW_TABLE = 'forward_schedule_stage_old_row_20261008'
_LINEAGE_TABLE = 'fixer_forward_media_lineage_20261006'
_ATTESTATION_TABLE = 'forward_media_visual_attestation'


class FinalizerConfigurationHold(RuntimeError):
    """Static reason only; never include credential values or driver errors."""


class FinalizerBatchHold(RuntimeError):
    """Definite per-batch hold with a fixed reason code; nothing was written."""


@dataclass(frozen=True)
class Settings:
    tenants: tuple[str, ...]
    batches: tuple[str, ...] = ()
    batch_size: int = 5
    interval_seconds: int = 60


def _integer(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        raise FinalizerConfigurationHold('worker_bounds_invalid') from None
    if not minimum <= value <= maximum:
        raise FinalizerConfigurationHold('worker_bounds_invalid')
    return value


def _uuid(store, value, field='uuid'):
    return store._reservation_uuid(value, field)


def _uuid_list(raw, *, limit=10):
    values = []
    for token in raw.split(','):
        token = token.strip()
        if token:
            values.append(SupabaseCalendarStore._reservation_uuid(token, 'batch_id'))
    if len(values) > limit or len(set(values)) != len(values):
        raise FinalizerConfigurationHold('worker_bounds_invalid')
    return tuple(values)


def settings_from_environment(env=None):
    env = os.environ if env is None else env
    if any(env.get(name) for name in _FORBIDDEN_CREDENTIALS):
        raise FinalizerConfigurationHold('isolated_lane_credentials_present')
    tenants = tuple(dict.fromkeys(t.strip() for t in env.get(TENANTS_ENV, '').split(',') if t.strip()))
    if not tenants or len(tenants) > 32 or any(not _TENANT.fullmatch(t) for t in tenants):
        raise FinalizerConfigurationHold('explicit_tenant_allowlist_required')
    return Settings(
        tenants,
        _uuid_list(env.get(BATCHES_ENV, '')),
        _integer(env, 'AGENT_FORWARD_SCHEDULE_FINALIZER_BATCH_SIZE', 5, 1, 10),
        _integer(env, 'AGENT_FORWARD_SCHEDULE_FINALIZER_INTERVAL_SECONDS', 60, 5, 300),
    )


def worker_enabled():
    # Tri-state: only a cleanly armed reservation flag runs the finalizer; an
    # ambiguous or OFF flag never activates this lane.
    return (os.getenv(WORKER_ENV, '').lower() in ('1', 'true', 'yes', 'on')
            and forward_reservation_flag() is True)


def _rpc(store, fn, args, timeout=30):
    return store._reservation_rpc(fn, args, timeout=timeout)


def _get(store, table, params):
    """Strict bounded service-role table read. Any failure is UNKNOWN."""
    try:
        response = store._client().get(
            store._rest(table), params=params, headers=store._headers(), timeout=30)
        rows = response.json()
    except ReservationStoreError:
        raise
    except Exception as exc:
        raise ReservationStoreError(0, f'table {table} read transport: {type(exc).__name__}')
    if response.status_code >= 400 or not isinstance(rows, list):
        raise ReservationStoreError(response.status_code if response.status_code >= 400 else 502,
                                    f'persisted {table} readback unavailable')
    return rows


def batch_status(store, batch_id):
    """Strictly parsed persisted batch status; the ONLY outcome authority."""
    bid = _uuid(store, batch_id, 'batch_id')
    data = _rpc(store, _STATUS_RPC, {'p_batch_id': bid})
    if (not isinstance(data, dict) or data.get('batch_id') != bid
            or data.get('state') not in ('staged', 'finalized')
            or not str(data.get('tenant_id') or '').strip()
            or not isinstance(data.get('request_digest'), str)
            or not _SHA256_RE.fullmatch(data['request_digest'])
            or not isinstance(data.get('member_row_ids'), list)
            or not isinstance(data.get('observation_row_ids'), list)
            or not isinstance(data.get('old_row_ids'), list)):
        raise ReservationStoreError(502, 'batch status response malformed; outcome unknown')
    for key in ('member_row_ids', 'observation_row_ids', 'old_row_ids'):
        data[key] = [_uuid(store, item, key) for item in data[key]]
    if (not set(data['observation_row_ids']).issubset(set(data['member_row_ids']))
            or set(data['old_row_ids']) & set(data['member_row_ids'])
            or len(set(data['old_row_ids'])) != len(data['old_row_ids'])
            or len(set(data['member_row_ids'])) != len(data['member_row_ids'])
            or not data['member_row_ids']):
        raise ReservationStoreError(502, 'batch status membership malformed; outcome unknown')
    if data['state'] == 'finalized':
        validate_receipt(store, data)
    return data


def validate_receipt(store, status):
    """The persisted terminal receipt must bind the exact batch, member set and
    persisted old-row set completely; anything less is UNKNOWN."""
    receipt = status.get('finalize_receipt')
    if (not isinstance(receipt, dict)
            or receipt.get('batch_id') != status['batch_id']
            or receipt.get('state') != 'finalized'
            or receipt.get('tenant_id') != status['tenant_id']
            or receipt.get('request_digest') != status['request_digest']
            or not isinstance(receipt.get('row_ids'), list)
            or not isinstance(receipt.get('reservation_ids'), list)
            or len(receipt['row_ids']) != len(status['member_row_ids'])
            or len(set(map(str, receipt['row_ids']))) != len(receipt['row_ids'])
            or len(receipt['row_ids']) != len(receipt['reservation_ids'])
            or set(map(str, receipt['row_ids'])) != set(status['member_row_ids'])
            or not isinstance(receipt.get('archived_old_row_ids'), list)
            or len(receipt['archived_old_row_ids']) != len(status['old_row_ids'])
            or len(set(map(str, receipt['archived_old_row_ids'])))
                != len(receipt['archived_old_row_ids'])
            or set(map(str, receipt['archived_old_row_ids'])) != set(status['old_row_ids'])):
        raise ReservationStoreError(502, 'finalized batch lacks its complete terminal proof; outcome unknown')
    for item in receipt['row_ids'] + receipt['reservation_ids'] + receipt['archived_old_row_ids']:
        _uuid(store, item, 'receipt_id')
    return receipt


def _member_records(store, status):
    """Full persisted membership, in position order, bound to the status set."""
    rows = _get(store, _MEMBER_TABLE,
                {'batch_id': 'eq.' + status['batch_id'], 'order': 'position.asc',
                 'limit': '101'})
    if len(rows) != len(status['member_row_ids']) or len(rows) > 100:
        raise ReservationStoreError(502, 'persisted batch membership readback incomplete')
    if [_uuid(store, r.get('calendar_row_id'), 'calendar_row_id') for r in rows] \
            != status['member_row_ids']:
        raise ReservationStoreError(502, 'persisted batch membership differs from status; outcome unknown')
    for row in rows:
        if (not isinstance(row, dict) or row.get('tenant_id') != status['tenant_id']
                or not str(row.get('gym_id') or '').strip()
                or not str(row.get('post_date') or '').strip()
                or not str(row.get('source_media_url') or '').startswith('https://')
                or not str(row.get('image_url') or '').startswith('https://')
                or (row.get('thumbnail_url') is not None
                    and not str(row['thumbnail_url']).startswith('https://'))):
            raise ReservationStoreError(502, 'persisted member binding malformed')
        row['_row_id'] = _uuid(store, row['calendar_row_id'], 'calendar_row_id')
        row['_logical'] = _uuid(store, row.get('logical_post_id'), 'logical_post_id')
    return rows


def _old_snapshots(store, status):
    """The PERSISTED old-row snapshots, in position order. Never a live read."""
    rows = _get(store, _OLD_ROW_TABLE,
                {'batch_id': 'eq.' + status['batch_id'], 'order': 'position.asc',
                 'limit': '101', 'select': 'calendar_row_id,old_snapshot'})
    if len(rows) != len(status['old_row_ids']) or len(rows) > 100:
        raise ReservationStoreError(502, 'persisted old row readback incomplete')
    if [_uuid(store, r.get('calendar_row_id'), 'old_row_id') for r in rows] \
            != status['old_row_ids']:
        raise ReservationStoreError(502, 'persisted old row set differs from status; outcome unknown')
    snapshots = []
    for row in rows:
        snapshot = row.get('old_snapshot')
        if (not isinstance(snapshot, dict)
                or _uuid(store, snapshot.get('id'), 'old row id') != row['calendar_row_id']):
            raise ReservationStoreError(502, 'persisted old row snapshot malformed')
        snapshots.append(snapshot)
    return snapshots


def _candidate_proof(store, status, member):
    """Complete owner/visual/reservation proof for one member, from persisted
    trusted evidence only. Missing or contradictory proof holds the batch."""
    row_id = member['_row_id']
    tenant = status['tenant_id']
    eligibility = _rpc(store, _ELIGIBLE_RPC, {'p_calendar_row_id': row_id})
    if (not isinstance(eligibility, dict) or eligibility.get('eligible') is not True
            or eligibility.get('mode') != 'staged'
            or eligibility.get('tenant_id') != tenant
            or str(eligibility.get('batch_id') or '') != status['batch_id']):
        raise FinalizerBatchHold('member_not_preparable')
    snapshot = _rpc(store, _SNAPSHOT_RPC, {'p_calendar_row_id': row_id})
    revision = (snapshot or {}).get('revision') if isinstance(snapshot, dict) else None
    if (not isinstance(revision, str) or not _REVISION.fullmatch(revision)
            or snapshot.get('tenant_id') != tenant):
        raise FinalizerBatchHold('member_revision_unavailable')
    lineage = _get(store, _LINEAGE_TABLE,
                   {'calendar_row_id': 'eq.' + row_id, 'row_revision': 'eq.' + revision,
                    'select': 'evidence_id,tenant_id', 'limit': '2'})
    if len(lineage) != 1 or lineage[0].get('tenant_id') != tenant:
        raise FinalizerBatchHold('member_lineage_unavailable')
    evidence_id = _uuid(store, lineage[0].get('evidence_id'), 'evidence_id')
    attestations = _get(store, _ATTESTATION_TABLE,
                        {'lineage_receipt_id': 'eq.' + evidence_id,
                         'row_revision': 'eq.' + str(visual_index.row_revision_from(revision)),
                         'tenant_key': 'eq.' + tenant,
                         'select': 'attestation_id,role,media_url',
                         'order': 'role.asc,created_at.desc,attestation_id.desc',
                         'limit': '100'})
    expected_url = {'original': member['source_media_url'],
                    'delivered': member['image_url'],
                    'thumbnail': member.get('thumbnail_url') or member['image_url']}
    chosen = {}
    for att in attestations:
        role = att.get('role')
        if role in expected_url and role not in chosen:
            chosen[role] = att
    if (set(chosen) != set(visual_index.ROLES)
            or any(chosen[role].get('media_url') != expected_url[role] for role in chosen)):
        raise FinalizerBatchHold('member_visual_proof_incomplete')
    return {'calendar_row_id': row_id, 'logical_post_id': member['_logical'],
            'expected_revision': revision,
            'attestation_ids': [_uuid(store, chosen[role].get('attestation_id'), 'attestation_id')
                                for role in visual_index.ROLES]}


def finalize_batch(store, status):
    """Finalize ONE staged batch exactly once; resolve ambiguity by readback.

    candidates = the full persisted membership (position order);
    p_expected_old_rows = the persisted old snapshots verbatim. A definite SQL
    refusal holds the batch (its transaction rolled back). Any transport or
    parse failure is UNKNOWN and resolves ONLY through the persisted terminal
    receipt re-read via the status RPC -- never via mutable calendar rows and
    never via a second, changed finalize attempt."""
    members = _member_records(store, status)
    old_snapshots = _old_snapshots(store, status)
    candidates = [_candidate_proof(store, status, member) for member in members]
    args = {'p_tenant_id': status['tenant_id'], 'p_batch_id': status['batch_id'],
            'p_candidates': candidates, 'p_expected_old_rows': old_snapshots}
    try:
        receipt = _rpc(store, _FINALIZE_RPC, args, timeout=60)
    except (ReservationHoldError, ReservationArgumentError) as exc:
        # 23514/23505/22023/55000: definite no-write; the RPC rolled back.
        reason = ('finalize_gate_off' if isinstance(exc, ReservationGateError)
                  else 'finalize_arguments_invalid' if isinstance(exc, ReservationArgumentError)
                  else 'finalize_held')
        raise FinalizerBatchHold(reason) from None
    except ReservationStoreError:
        outcome = _resolve_ambiguous(store, status)
        if outcome is None:
            raise FinalizerBatchHold('finalize_outcome_unknown') from None
        return outcome, 'status_readback'
    if not isinstance(receipt, dict):
        outcome = _resolve_ambiguous(store, status)
        if outcome is None:
            raise FinalizerBatchHold('finalize_outcome_unknown') from None
        return outcome, 'status_readback'
    merged = dict(status)
    merged['state'] = 'finalized'
    merged['finalize_receipt'] = receipt
    try:
        return validate_receipt(store, merged), 'finalize_receipt'
    except ReservationStoreError:
        # A response that does not bind the exact batch is not an outcome;
        # the persisted terminal receipt is the only resolution.
        outcome = _resolve_ambiguous(store, status)
        if outcome is None:
            raise FinalizerBatchHold('finalize_outcome_unknown') from None
        return outcome, 'status_readback'


def _resolve_ambiguous(store, status):
    """Sole resolution of an ambiguous finalize outcome: re-read the persisted
    terminal receipt. Returns the validated receipt or None (still staged or
    readback itself unknown)."""
    try:
        readback = batch_status(store, status['batch_id'])
    except Exception:
        return None
    if (readback['state'] != 'finalized' or readback['tenant_id'] != status['tenant_id']
            or readback['request_digest'] != status['request_digest']
            or readback['member_row_ids'] != status['member_row_ids']
            or readback['old_row_ids'] != status['old_row_ids']):
        return None
    return readback['finalize_receipt']


def _discover_page(store, config, cursors):
    """Tenant-scoped UUID keyset pages, with round robin between tenants.

    Staged membership is immutable. Finalizing another row cannot shift this
    cursor as it would an offset. Held/unknown rows are revisited after wrap;
    a restart starts from the beginning safely because status is authoritative.
    Read at most one page per tenant, plus an empty-tail wrap query.
    """
    tenant_cursors = cursors.setdefault('batches', {})
    start = cursors.get('next_tenant', 0) % len(config.tenants)
    for offset in range(len(config.tenants)):
        index = (start + offset) % len(config.tenants)
        tenant = config.tenants[index]
        after = tenant_cursors.get(tenant)
        params = {'state': 'eq.staged', 'tenant_id': 'eq.' + tenant,
                  'select': 'batch_id,tenant_id', 'order': 'batch_id.asc',
                  'limit': str(config.batch_size)}
        if after:
            params['batch_id'] = 'gt.' + _uuid(store, after, 'batch_id')
        rows = _get(store, _BATCH_TABLE, params)
        if not rows and after:
            params.pop('batch_id')
            rows = _get(store, _BATCH_TABLE, params)
            after = None
        if any(not isinstance(row, dict) for row in rows):
            raise ReservationStoreError(502, 'batch discovery page malformed')
        ids = [_uuid(store, row.get('batch_id'), 'batch_id') for row in rows]
        if (len(ids) > config.batch_size or ids != sorted(set(ids))
                or any(row.get('tenant_id') != tenant for row in rows)
                or (after and any(bid <= after for bid in ids))):
            raise ReservationStoreError(502, 'batch discovery page malformed')
        if ids:
            # Advance tenant rotation only after a successful page read. The
            # batch cursor advances individually after processing, below.
            cursors['next_tenant'] = (index + 1) % len(config.tenants)
            return ids, tenant
    cursors['next_tenant'] = (start + 1) % len(config.tenants)
    return [], None


def run_once(*, settings=None, store=None, discovery_state=None):
    """One bounded pass over staged batches. Injection is for offline fixtures."""
    if not worker_enabled():
        return {'status': 'disabled', 'batches': []}
    try:
        configured = settings_from_environment()
        config = settings or configured
        if any(tenant not in configured.tenants for tenant in config.tenants):
            raise FinalizerConfigurationHold('tenant_outside_configured_allowlist')
        if (not config.tenants or len(config.tenants) > 32
                or any(not _TENANT.fullmatch(t) for t in config.tenants)
                or not 1 <= config.batch_size <= 10
                or len(config.batches) > 10
                or len(set(config.batches)) != len(config.batches)):
            raise FinalizerConfigurationHold('worker_bounds_invalid')
    except FinalizerConfigurationHold as exc:
        return {'status': 'hold', 'reason': str(exc), 'batches': []}
    store = store or SupabaseCalendarStore()
    report = {'status': 'complete', 'batches': []}
    cursors = {} if discovery_state is None else discovery_state
    discovered_tenant = None
    try:
        if config.batches:
            batch_ids = list(config.batches)
        else:
            batch_ids, discovered_tenant = _discover_page(store, config, cursors)
    except FinalizerConfigurationHold as exc:
        return {'status': 'hold', 'reason': str(exc), 'batches': []}
    except ReservationStoreError:
        return {'status': 'hold', 'reason': 'discovery_unavailable', 'batches': []}
    for batch_id in batch_ids:
        batch_report = {'batch_id': batch_id, 'status': 'hold'}
        try:
            status = batch_status(store, batch_id)
            if (status['tenant_id'] not in config.tenants
                    or (discovered_tenant is not None
                        and status['tenant_id'] != discovered_tenant)):
                raise FinalizerBatchHold('tenant_outside_configured_allowlist')
            if status['state'] == 'finalized':
                batch_report['status'] = 'already_finalized'
            else:
                receipt, resolved = finalize_batch(store, status)
                batch_report.update(status='finalized', resolved=resolved,
                                    row_ids=list(receipt['row_ids']))
        except FinalizerBatchHold as exc:
            batch_report['reason'] = str(exc)
        except ReservationStoreError:
            batch_report['reason'] = 'batch_outcome_unknown'
        except Exception:
            batch_report['reason'] = 'batch_processing_failed'
        report['batches'].append(batch_report)
        if discovered_tenant is not None:
            # This is discovery progress, never an outcome receipt. Even an
            # unknown outcome remains staged and is retried on the next wrap.
            cursors['batches'][discovered_tenant] = batch_id
    if any(b['status'] == 'hold' for b in report['batches']):
        report['status'] = 'partial_hold'
    return report


def run_forever(*, stop=None, logger=print):
    """Standalone service loop; no attester/owner DSN, provider key or planner lane."""
    stop = stop or threading.Event()
    discovery_state = {}
    while not stop.is_set():
        if not worker_enabled():
            logger(json.dumps({'status': 'disabled', 'batches': []}, sort_keys=True))
            return
        try:
            settings = settings_from_environment()
        except FinalizerConfigurationHold as exc:
            logger(json.dumps({'status': 'hold', 'reason': str(exc), 'batches': []}, sort_keys=True))
            return
        logger(json.dumps(run_once(settings=settings, discovery_state=discovery_state), sort_keys=True))
        stop.wait(settings.interval_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Isolated service-role forward schedule batch finalizer (default OFF)')
    parser.add_argument('--once', action='store_true', help='Run one bounded finalization pass')
    args = parser.parse_args(argv)
    try:
        if args.once:
            report = run_once()
            print(json.dumps(report, sort_keys=True))
            return 0 if report['status'] in ('complete', 'disabled') else 1
        run_forever()
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
