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
from .forward_media_owner import forbidden_credential_names

TENANTS_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_TENANTS'
WORKER_ENV = 'AGENT_FORWARD_MEDIA_ATTESTER_WORKER'
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_REVISION = re.compile(r'[0-9a-f]{32}\Z')


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
    if forbidden_credential_names(env):
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


def run_once(*, settings=None, tenant_offset=0, cursors=None,
             connection_factory=None, attest_fn=None):
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
