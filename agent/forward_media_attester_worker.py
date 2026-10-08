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
from .forward_media_lane import unknown_environment_names

TENANTS_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_TENANTS'
WORKER_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_WORKER'
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_REVISION = re.compile(r'[0-9a-f]{32}\Z')
_ELIGIBLE_RPC = 'select public.forward_schedule_preparation_eligible_20261008(%s)'
_SNAPSHOT_RPC = 'select public.fixer_forward_media_attestation_request_20261006(%s)'
# Tenant-scoped staged discovery (DRAFT_fixer_forward_schedule_attester_
# discovery_20261008.sql): staged rows owing attestation and existing-lineage
# rows missing visual roles. Discovery replaces operator-supplied UUID lists;
# every candidate is re-gated through the exact SQL predicate before use.
_STAGED_PENDING_RPC = ('select public.fixer_forward_schedule_staged_attester'
                       '_pending_20261008(%s,%s,%s)')
_RECOVERY_PENDING_RPC = ('select public.fixer_forward_schedule_staged_visual'
                         '_recovery_pending_20261008(%s,%s,%s)')


class WorkerConfigurationHold(RuntimeError):
    """Static reason only; never include credential values or driver errors."""


@dataclass(frozen=True)
class Settings:
    tenants: tuple[str, ...]
    batch_size: int = 50
    interval_seconds: int = 60


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
    if unknown_environment_names(env, 'attester'):
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


def _advance_cursor(cursors, key, fetched, attempted):
    """Keyset cursor: only an ATTEMPTED valid persisted UUID advances discovery.

    Capacity-stopped lanes keep their cursor so unattempted fetched rows are
    revisited first on the next pass; a fetched-but-unattempted lane is never
    advanced past and never reset. Malformed trailing attempts leave the
    cursor unmoved (their holds stay visible); only a genuinely empty fetched
    tail resets to the beginning so new or revised rows behind the cursor are
    revisited on the next pass."""
    if attempted:
        last = attempted[-1]
        try:
            cursors[key] = guard._uuid(last[0]['calendar_row_id'])
        except (TypeError, KeyError, IndexError, ValueError,
                guard.ForwardMediaVerificationHold):
            pass
    elif not fetched:
        cursors.pop(key, None)


def _recovery_identity(snapshot, tenant):
    """Strictly parsed staged visual-recovery candidate identity.

    The persisted lineage identity is the ONLY recovery authority: the row and
    revision are re-gated through the eligibility predicate by _recover_visual,
    and SQL revalidates the lineage against the current revision."""
    row_id, revision = _row_identity(snapshot, tenant)
    lineage_id = guard._uuid(snapshot.get('lineage_receipt_id'))
    guard._uuid(snapshot.get('batch_id'))
    from . import forward_media_visual_index as visual_index
    missing = snapshot.get('missing_roles')
    if (not isinstance(missing, list) or not missing
            or len(set(missing)) != len(missing)
            or any(role not in visual_index.ROLES for role in missing)):
        raise ValueError('pending_identity_invalid')
    return row_id, revision, lineage_id


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
            _advance_cursor(cursors, tenant, pending, pending)
            # Tenant-scoped staged discovery (DRAFT_fixer_forward_schedule_
            # attester_discovery_20261008.sql): staged member rows still owing
            # attestation, and SAME-lineage rows missing one or more visual
            # roles. SQL discovery replaces operator UUID lists; every
            # candidate is re-gated through the exact eligibility predicate
            # before any preparation. A lane failure holds only this lane;
            # committed active results above are preserved.
            staged, recoveries = None, None
            if remaining > 0:
                try:
                    with conn.cursor() as cur:
                        cur.execute(_STAGED_PENDING_RPC,
                                    (tenant, limit, cursors.get((tenant, 'staged'))))
                        staged = cur.fetchmany(limit + 1)
                        if len(staged) > limit:
                            raise WorkerConfigurationHold('pending_batch_bound_exceeded')
                        cur.execute(_RECOVERY_PENDING_RPC,
                                    (tenant, limit, cursors.get((tenant, 'recovery'))))
                        recoveries = cur.fetchmany(limit + 1)
                        if len(recoveries) > limit:
                            raise WorkerConfigurationHold('pending_batch_bound_exceeded')
                    conn.commit()
                except WorkerConfigurationHold:
                    raise
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    report['tenants'].append({'tenant': tenant, 'status': 'hold',
                                              'reason': 'staged_discovery_unavailable'})
                    staged, recoveries = None, None
            staged_attempted = []
            for item in staged or []:
                if remaining <= 0:
                    break
                row_report = {'tenant': tenant, 'mode': 'staged', 'status': 'hold'}
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
                        row_report.update(_prepare_staged(conn, row_id, config, attest))
                except guard.ForwardMediaVerificationHold:
                    row_report['reason'] = 'verification_hold'
                except Exception:
                    row_report['reason'] = 'attestation_unavailable_or_invalid'
                remaining -= 1
                staged_attempted.append(item)
                report['rows'].append(row_report)
            if staged is not None:
                _advance_cursor(cursors, (tenant, 'staged'), staged, staged_attempted)
            recovery_attempted = []
            for item in recoveries or []:
                if remaining <= 0:
                    break
                row_report = {'tenant': tenant, 'mode': 'recovery', 'status': 'hold'}
                try:
                    if not isinstance(item, (tuple, list)) or len(item) != 1:
                        raise ValueError('invalid_pending_shape')
                    row_id, revision, lineage_id = _recovery_identity(item[0], tenant)
                    row_report['calendar_row_id'] = row_id
                    row_report['revision'] = revision
                    if (row_id, revision) in seen:
                        row_report.update(status='skipped', reason='duplicate_pending_revision')
                    else:
                        seen.add((row_id, revision))
                        row_report.update(_recover_visual(conn, row_id, lineage_id,
                                                          config, recover_fn))
                except guard.ForwardMediaVerificationHold:
                    row_report['reason'] = 'verification_hold'
                except Exception:
                    row_report['reason'] = 'attestation_unavailable_or_invalid'
                remaining -= 1
                recovery_attempted.append(item)
                report['rows'].append(row_report)
            if recoveries is not None:
                _advance_cursor(cursors, (tenant, 'recovery'), recoveries,
                                recovery_attempted)
        except WorkerConfigurationHold as exc:
            report['tenants'].append({'tenant': tenant, 'status': 'hold', 'reason': str(exc)})
        except Exception:
            report['tenants'].append({'tenant': tenant, 'status': 'hold', 'reason': 'discovery_unavailable'})
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
