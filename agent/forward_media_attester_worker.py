"""Isolated, default-OFF background discovery for the trusted media attester.

Run ONLY in a dedicated process/service with the narrow attester DSN and no
publisher or Supabase service credentials. No job is started on import. The
production guard owns callbacks and exact-byte verification; this runner sends
only persisted row UUID and revision, never caller provenance.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import threading

from . import forward_media_guard as guard

TENANTS_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_TENANTS'
WORKER_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_WORKER'
# Explicit staged-preparation lane (two-phase schedule contract, DRAFT 20261008).
# A staged row is NEVER discoverable through the pending RPC (active rows only),
# so the operator supplies exact staged row UUIDs; each must independently pass
# the SQL predicate public.forward_schedule_preparation_eligible_20261008 with
# mode='staged'. The Python marker is never consulted as authority.
STAGED_ROWS_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_STAGED_ROWS'
# Missing-visual-role recovery lane: exact "row_uuid:lineage_evidence_uuid"
# pairs for rows whose lineage committed but whose visual role attestations are
# incomplete after a crash. The SAME lineage is reused; no new lineage, claim,
# publish or approval is ever created here.
RECOVER_ROWS_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_RECOVER_ROWS'
_FORBIDDEN_CREDENTIALS = (
    'SUPABASE_SERVICE_ROLE_KEY', 'AGENT_SOCIALAPI_KEY', 'AGENT_SOCIALAPI_ENC_KEY',
    'ZERNIO_API_KEY', 'AGENT_GBP_ACCESS_TOKEN',
)
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_REVISION = re.compile(r'[0-9a-f]{32}\Z')
_ELIGIBLE_RPC = 'select public.forward_schedule_preparation_eligible_20261008(%s)'
_SNAPSHOT_RPC = 'select public.fixer_forward_media_attestation_request_20261006(%s)'


class WorkerConfigurationHold(RuntimeError):
    """Static reason only; never include credential values or driver errors."""


@dataclass(frozen=True)
class Settings:
    tenants: tuple[str, ...]
    batch_size: int = 50
    interval_seconds: int = 60
    staged_rows: tuple[str, ...] = ()
    recoveries: tuple[tuple[str, str], ...] = ()


def _uuid_list(raw, *, limit=100):
    values = []
    for token in raw.split(','):
        token = token.strip()
        if token:
            try:
                values.append(guard._uuid(token))
            except guard.ForwardMediaVerificationHold:
                raise WorkerConfigurationHold('worker_bounds_invalid') from None
    if len(values) > limit or len(set(values)) != len(values):
        raise WorkerConfigurationHold('worker_bounds_invalid')
    return tuple(values)


def _recovery_list(raw, *, limit=100):
    pairs = []
    for token in raw.split(','):
        token = token.strip()
        if token:
            parts = token.split(':')
            if len(parts) != 2:
                raise WorkerConfigurationHold('worker_bounds_invalid')
            try:
                pairs.append((guard._uuid(parts[0]), guard._uuid(parts[1])))
            except guard.ForwardMediaVerificationHold:
                raise WorkerConfigurationHold('worker_bounds_invalid') from None
    if len(pairs) > limit or len({row for row, _ in pairs}) != len(pairs):
        raise WorkerConfigurationHold('worker_bounds_invalid')
    return tuple(pairs)


def _integer(env, name, default, minimum, maximum):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        raise WorkerConfigurationHold('worker_bounds_invalid') from None
    if not minimum <= value <= maximum:
        raise WorkerConfigurationHold('worker_bounds_invalid')
    return value


def settings_from_environment(env=None):
    env = os.environ if env is None else env
    if any(env.get(name) for name in _FORBIDDEN_CREDENTIALS):
        raise WorkerConfigurationHold('publisher_or_service_credentials_present')
    if (not env.get('AGENT_FORWARD_MEDIA_ATTESTER_DSN')
            or env.get('AGENT_FORWARD_MEDIA_ATTESTER_ROLE') != guard.ROLE):
        raise WorkerConfigurationHold('dedicated_attester_configuration_required')
    tenants = tuple(dict.fromkeys(t.strip() for t in env.get(TENANTS_ENV, '').split(',') if t.strip()))
    if not tenants or len(tenants) > 32 or any(not _TENANT.fullmatch(t) for t in tenants):
        raise WorkerConfigurationHold('explicit_tenant_allowlist_required')
    return Settings(
        tenants,
        _integer(env, 'AGENT_FORWARD_MEDIA_ATTESTER_BATCH_SIZE', 50, 1, 100),
        _integer(env, 'AGENT_FORWARD_MEDIA_ATTESTER_INTERVAL_SECONDS', 60, 5, 60),
        _uuid_list(env.get(STAGED_ROWS_ENV, '')),
        _recovery_list(env.get(RECOVER_ROWS_ENV, '')),
    )


def worker_enabled():
    return (os.getenv(WORKER_ENV, '').lower() in ('1', 'true', 'yes', 'on')
            and guard.enabled())


def _close(conn):
    try:
        conn.close()  # Also releases the tenant's session advisory lock.
    except Exception:
        pass


def _row_identity(snapshot, tenant):
    if not isinstance(snapshot, dict) or snapshot.get('tenant_id') != tenant:
        raise ValueError('pending_identity_invalid')
    row_id = guard._uuid(snapshot.get('calendar_row_id'))
    revision = snapshot.get('revision')
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ValueError('pending_revision_invalid')
    return row_id, revision


def _eligibility(cur, row_id):
    """Strictly parsed SQL predicate result; the ONLY preparation authority."""
    cur.execute(_ELIGIBLE_RPC, (row_id,))
    result = cur.fetchone()
    data = result[0] if result else None
    if (not isinstance(data, dict) or type(data.get('eligible')) is not bool
            or data.get('mode') not in ('active', 'staged', None)
            or (data['eligible'] and data['mode'] is None)
            or (not data['eligible'] and data['mode'] is not None)
            or (data.get('tenant_id') is not None and not isinstance(data['tenant_id'], str))):
        raise ValueError('preparation_eligibility_invalid')
    return data


def _lock_tenant(cur, tenant):
    cur.execute("select pg_try_advisory_lock(hashtextextended(%s,0))",
                ('fixer_attester_worker_20261006:' + tenant,))
    return cur.fetchone() == (True,)


def _snapshot_revision(cur, row_id, tenant):
    cur.execute(_SNAPSHOT_RPC, (row_id,))
    result = cur.fetchone()
    snapshot = result[0] if result else None
    return _row_identity(snapshot, tenant)[1]


def _prepare_staged(conn, row_id, config, attest):
    """Isolated ATTESTATION PREPARATION for one exact staged member row.

    Authorization comes from the SQL predicate alone (mode='staged', tenant
    inside the configured allowlist, batch identity bound); the calendar row
    is never read directly and the stage marker is never trusted by itself.
    Ends the read transaction before attest's network/object work."""
    with conn.cursor() as cur:
        data = _eligibility(cur, row_id)
        sql_reason = data.get('reason')
        if (not data['eligible'] or data['mode'] != 'staged'
                or data.get('tenant_id') not in config.tenants):
            return {'status': 'hold', 'reason': 'preparation_ineligible',
                    'sql_reason': sql_reason}
        tenant = data['tenant_id']
        batch_id = guard._uuid(data.get('batch_id'))
        if not _lock_tenant(cur, tenant):
            return {'status': 'hold', 'reason': 'tenant_busy'}
        revision = _snapshot_revision(cur, row_id, tenant)
    conn.commit()
    result = attest(row_id, revision)
    if not isinstance(result, dict) or result.get('revision') != revision:
        raise ValueError('attester_response_invalid')
    return {'status': 'attested', 'mode': 'staged', 'tenant': tenant,
            'batch_id': batch_id, 'evidence_id': guard._uuid(result.get('evidence_id'))}


def _recover_visual(conn, row_id, evidence_id, config, recover):
    """Resume the missing visual roles of one exact lineage after a crash.

    The row must still pass the SQL preparation predicate (either lane); the
    SAME persisted lineage is reused and only missing roles are appended. No
    claim, publish, approval or second lineage is ever created here."""
    from . import forward_media_visual_index as visual_index
    if not visual_index.enabled():
        return {'status': 'hold', 'reason': 'visual_index_disabled'}
    with conn.cursor() as cur:
        data = _eligibility(cur, row_id)
        sql_reason = data.get('reason')
        if (not data['eligible'] or data.get('tenant_id') not in config.tenants):
            return {'status': 'hold', 'reason': 'preparation_ineligible',
                    'sql_reason': sql_reason}
        tenant = data['tenant_id']
        if not _lock_tenant(cur, tenant):
            return {'status': 'hold', 'reason': 'tenant_busy'}
        revision = _snapshot_revision(cur, row_id, tenant)
    conn.commit()
    result = recover(row_id, revision, evidence_id, tenant_key=tenant)
    ids = (result or {}).get('attestation_ids') if isinstance(result, dict) else None
    if (not isinstance(ids, dict) or set(ids) != set(visual_index.ROLES)
            or result.get('tenant_key') != tenant):
        raise ValueError('visual_recovery_response_invalid')
    for role in visual_index.ROLES:
        guard._uuid(ids[role])
    return {'status': 'recovered' if result.get('recovered') else 'complete',
            'mode': data['mode'], 'tenant': tenant,
            'appended': list(result.get('appended') or [])}


def run_once(*, settings=None, tenant_offset=0, cursors=None,
             connection_factory=None, attest_fn=None, recover_fn=None):
    """Bound one pass; connection/attest injection is for offline fixtures only.

    No immediate retry queue: each pass discovers persisted pending revisions
    anew. Committed proofs disappear from discovery, and SQL's unique row/revision
    constraint prevents concurrent or uncertain-commit retries duplicating proof.
    Holds return only fixed reason codes, never raw exception messages or URLs.
    """
    if not worker_enabled():
        return {'status': 'disabled', 'rows': []}
    try:
        configured = settings_from_environment()
        config = settings or configured
        if any(tenant not in configured.tenants for tenant in config.tenants):
            raise WorkerConfigurationHold("tenant_outside_configured_allowlist")
        if (not config.tenants or len(config.tenants) > 32
                or any(not _TENANT.fullmatch(t) for t in config.tenants)
                or not 1 <= config.batch_size <= 100):
            raise WorkerConfigurationHold('worker_bounds_invalid')
    except WorkerConfigurationHold as exc:
        return {'status': 'hold', 'reason': str(exc), 'rows': []}
    connect = connection_factory or guard._connect
    attest = attest_fn or guard.attest
    if recover_fn is None:
        from . import forward_media_visual_index as visual_index
        recover_fn = visual_index.recover
    tenants = config.tenants[tenant_offset % len(config.tenants):] + config.tenants[:tenant_offset % len(config.tenants)]
    report = {'status': 'complete', 'rows': [], 'tenants': []}
    remaining = config.batch_size
    seen = set()
    cursors = {} if cursors is None else cursors
    for tenant_index, tenant in enumerate(tenants):
        if remaining <= 0:
            break
        # Reserve capacity for later tenants. Rotate between passes when the
        # allowlist exceeds the batch bound so a held tenant cannot starve others.
        limit = max(1, remaining // (len(tenants) - tenant_index))
        conn = None
        try:
            conn = connect()
            with conn.cursor() as cur:
                cur.execute('select current_user')
                if cur.fetchone() != (guard.ROLE,):
                    raise WorkerConfigurationHold('attester_role_mismatch')
                cur.execute("select pg_try_advisory_lock(hashtextextended(%s,0))",
                            ('fixer_attester_worker_20261006:' + tenant,))
                if cur.fetchone() != (True,):
                    report['tenants'].append({'tenant': tenant, 'status': 'busy'})
                    continue
                cur.execute('select public.fixer_forward_media_pending_attestations_20261006(%s,%s,%s)',
                            (tenant, limit, cursors.get(tenant)))
                pending = cur.fetchmany(limit + 1)
                if len(pending) > limit:
                    raise WorkerConfigurationHold('pending_batch_bound_exceeded')
            # End the read transaction before network/render work. The session
            # lock remains held until connection close; attest uses its own lane.
            conn.commit()
            # Keyset pagination prevents permanently held earlier rows from
            # occupying every pass. Empty tail resets to the beginning, so
            # new/revised rows behind the cursor are revisited on the next pass.
            report['tenants'].append({'tenant': tenant, 'status': 'discovered', 'count': len(pending)})
            remaining -= len(pending)
            for item in pending:
                row_report = {'tenant': tenant, 'status': 'hold'}
                try:
                    if not isinstance(item, (tuple, list)) or len(item) != 1:
                        raise ValueError('invalid_pending_shape')
                    row_id, revision = _row_identity(item[0], tenant)
                    row_report['calendar_row_id'] = row_id
                    row_report['revision'] = revision
                    if (row_id, revision) in seen:
                        row_report.update(status='skipped', reason='duplicate_pending_revision')
                    else:
                        seen.add((row_id, revision))
                        # Production call deliberately has no callbacks, bytes,
                        # or connection override. guard.attest loads production
                        # callbacks from its dedicated-role provenance RPC.
                        result = attest(row_id, revision)
                        if (not isinstance(result, dict) or result.get('revision') != revision):
                            raise ValueError('attester_response_invalid')
                        evidence_id = guard._uuid(result.get('evidence_id'))
                        row_report.update(status='attested', evidence_id=evidence_id)
                except guard.ForwardMediaVerificationHold:
                    row_report['reason'] = 'verification_hold'
                except Exception:
                    row_report['reason'] = 'attestation_unavailable_or_invalid'
                report['rows'].append(row_report)
            if pending:
                # The attester result itself is validated per row above. Only
                # a valid persisted UUID may advance discovery; malformed
                # responses still produce visible holds without leaking data.
                last = pending[-1]
                try:
                    cursors[tenant] = guard._uuid(last[0]['calendar_row_id'])
                except (TypeError, KeyError, IndexError, ValueError,
                        guard.ForwardMediaVerificationHold):
                    pass
            elif cursors.get(tenant):
                cursors.pop(tenant, None)
        except WorkerConfigurationHold as exc:
            report['tenants'].append({'tenant': tenant, 'status': 'hold', 'reason': str(exc)})
        except Exception:
            report['tenants'].append({'tenant': tenant, 'status': 'hold', 'reason': 'discovery_unavailable'})
        finally:
            if conn is not None:
                _close(conn)
    # Explicit staged-preparation and missing-visual-role recovery lanes. These
    # rows are never discoverable through the active-only pending RPC; the
    # operator supplies exact identities, and each row must independently pass
    # the SQL eligibility predicate before any preparation is attempted.
    if config.staged_rows or config.recoveries:
        conn = None
        try:
            conn = connect()
            with conn.cursor() as cur:
                cur.execute('select current_user')
                if cur.fetchone() != (guard.ROLE,):
                    raise WorkerConfigurationHold('attester_role_mismatch')
            for row_id in config.staged_rows:
                row_report = {'calendar_row_id': row_id, 'mode': 'staged', 'status': 'hold'}
                try:
                    row_report.update(_prepare_staged(conn, row_id, config, attest))
                except guard.ForwardMediaVerificationHold:
                    row_report['reason'] = 'verification_hold'
                except Exception:
                    row_report['reason'] = 'attestation_unavailable_or_invalid'
                report['rows'].append(row_report)
            for row_id, evidence_id in config.recoveries:
                row_report = {'calendar_row_id': row_id, 'mode': 'recovery',
                              'status': 'hold'}
                try:
                    row_report.update(_recover_visual(conn, row_id, evidence_id,
                                                      config, recover_fn))
                except guard.ForwardMediaVerificationHold:
                    row_report['reason'] = 'verification_hold'
                except Exception:
                    row_report['reason'] = 'attestation_unavailable_or_invalid'
                report['rows'].append(row_report)
        except WorkerConfigurationHold as exc:
            report['tenants'].append({'tenant': None, 'status': 'hold', 'reason': str(exc)})
        except Exception:
            report['tenants'].append({'tenant': None, 'status': 'hold', 'reason': 'discovery_unavailable'})
        finally:
            if conn is not None:
                _close(conn)
    if any(r['status'] == 'hold' for r in report['rows'] + report['tenants']):
        report['status'] = 'partial_hold'
    return report


def run_forever(*, stop=None, logger=print):
    """Standalone service loop; no sender, scheduler, service-role client or retry queue."""
    stop = stop or threading.Event()
    offset = 0
    cursors = {}
    while not stop.is_set():
        if not worker_enabled():
            logger(json.dumps({'status': 'disabled', 'rows': []}, sort_keys=True))
            return
        try:
            settings = settings_from_environment()
        except WorkerConfigurationHold as exc:
            logger(json.dumps({'status': 'hold', 'reason': str(exc), 'rows': []}, sort_keys=True))
            return
        logger(json.dumps(run_once(settings=settings, tenant_offset=offset,
                                   cursors=cursors), sort_keys=True))
        offset += max(1, min(settings.batch_size, len(settings.tenants)))
        stop.wait(settings.interval_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Isolated trusted media attester worker (default OFF)')
    parser.add_argument('--once', action='store_true', help='Run one bounded discovery pass')
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
