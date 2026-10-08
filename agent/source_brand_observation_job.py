"""source_brand_observation_job.py — finite, default-OFF single-run observation cron lane.

WHY THIS EXISTS: `TrustedSourceObservationProducer.observe(gym_id)` is the
reviewed trusted semantic observation path (source_brand_observation.py), but
nothing scheduled it. This module is a thin, testable single-run CLI wrapper
around exactly that composition — no new observation logic, no scheduler, no
long-running loop. One invocation observes the bounded allowlisted set and
exits, so it is safe as a Railway cron `startCommand`. It mirrors
source_brand_capture_job.py.

GATES (all fail-closed; everything defaults OFF):
  - ECHO_SOURCE_OBSERVATION_JOB_ENABLED must equal 'true' or the run exits
    cleanly with NO I/O at all (no env reads beyond the flag, no journals,
    no network, no files) and exit code 0.
  - ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST is a REQUIRED comma-separated list
    of exact canonical gym UUIDs. Missing, empty, malformed, or duplicate
    entries hold the whole run (tenant_allowlist_required) — there is never
    a fallback to "all mapped gyms" or any untrusted discovery. Only
    allowlisted gyms are observed (no cross-tenant work).
  - ECHO_SOURCE_OBSERVATION_JOB_MAX_GYMS bounds per-run work (default 50,
    hard-clamped 1..50). A selected target set larger than the limit HOLDS
    the whole run (allowlist_exceeds_limit): the bound is a fail-closed
    capacity gate, never a silent truncation lane.
  - The startup composition's own gates then apply (see
    source_brand_startup.py): runner/collector/ingest enable flags, the
    private operator-owned approved-mappings file, the private journal dir,
    the collector Supabase config and the Zernio credential. A
    CaptureIngestError from startup holds the whole run with that code.
  - The producer's own gate ECHO_SOURCE_OBSERVATION_ENABLED must also equal
    'true' or every observe() call holds with source_observation_disabled —
    counted as an armed hold, so the process exits nonzero.

RECEIVER API:
  run_job(environ=None, *, logger=None, _producer=None, _approved_gyms=None)
  returns a sanitized receipt dict. `_producer`/`_approved_gyms` are the
  injectable test seams; when absent, production composition is used:
      runner = initialize_source_capture(environ=env)
      producer = TrustedSourceObservationProducer(
          resolve_mapping=runner.collector._resolve,
          authenticate_capture=runner.collector.authenticate_capture,
          environ=env)
      approved = tuple(runner.collector._resolve._approved.keys())
  This reuses the operator-approved mapping startup and the collector's
  durable capture journal — no fresh in-memory capture, no edits to
  startup/collector.

FAILURE ISOLATION: one gym's hold/error never sinks the run; independent
gyms are still observed. Every gym gets its own receipt entry. Any armed
hold or error makes the process exit nonzero.

RECEIPT SCHEMA (sanitized — counts, gym UUIDs, statuses, hold codes,
observation_id and content_sha256 only; never report bodies, raw source
bytes, credentials, or mapping contents):
  OFF:        {'state': 'off'}
  HELD RUN:   {'state': 'held', 'hold': <code>, 'gyms': [],
               'observed': 0, 'held': 1, 'errors': 0}
  COMPLETE:   {'state': 'complete', 'limit': N, 'observed': n, 'held': n,
               'errors': n, 'gyms': [entry, ...]}
  Per-gym entries:
    observed: {'gym_id', 'status': 'observed', 'state': 'verified',
               'observation_id', 'content_sha256'}
    held:     {'gym_id', 'status': 'held', 'hold': <fixed ObservationHold
               code, 'gym_not_in_approved_mapping', or
               'observation_state_held'>, optionally with the sanitized
               observation_id and content_sha256 for a completed held result}
    error:    {'gym_id', 'status': 'error', 'error': <exception type name>}

CLI: `python -m agent.source_brand_observation_job [--gym UUID ...]`.
  --gym is repeatable and may ONLY narrow the configured allowlist: invalid
  UUIDs, a missing configured allowlist, or any gym outside the configured
  allowlist exits 2 before the job runs. Exit codes: OFF or a complete run
  with zero holds and zero errors -> 0; any armed hold or error -> 1;
  bad CLI args -> 2.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from .source_brand_ingest import CaptureIngestError
from .source_brand_observation import ObservationHold, TrustedSourceObservationProducer
from .source_brand_startup import initialize_source_capture

_JOB_FLAG = 'ECHO_SOURCE_OBSERVATION_JOB_ENABLED'
_ALLOWLIST_ENV = 'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST'
_MAX_GYMS_ENV = 'ECHO_SOURCE_OBSERVATION_JOB_MAX_GYMS'
_DEFAULT_MAX_GYMS = 50
_HARD_MAX_GYMS = 50


def parse_allowlist(raw):
    """Exact canonical gym UUIDs, or None (hold) when missing/empty/invalid/duplicate."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    entries = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            return None
        try:
            value = str(uuid.UUID(part))
        except (ValueError, AttributeError, TypeError):
            return None
        if value in entries:
            return None
        entries.append(value)
    return tuple(entries) or None


def parse_max_gyms(raw):
    if raw is None:
        return _DEFAULT_MAX_GYMS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(_HARD_MAX_GYMS, value))


def _hold(code):
    return {'state': 'held', 'hold': code, 'gyms': [], 'observed': 0,
            'held': 1, 'errors': 0}


def run_job(environ=None, *, logger=None, _producer=None, _approved_gyms=None):
    """One bounded observation pass over the allowlist. Returns a receipt dict.

    `_producer`/`_approved_gyms` are test seams; production always builds the
    producer through initialize_source_capture + TrustedSourceObservationProducer
    (the sole trusted composition).
    """
    env = dict(os.environ if environ is None else environ)
    log = logger or (lambda m: print(f'[source-observation-job] {m}'))
    if env.get(_JOB_FLAG) != 'true':
        return {'state': 'off'}
    allowlist = parse_allowlist(env.get(_ALLOWLIST_ENV))
    if allowlist is None:
        log('held: tenant allowlist missing or invalid — no fallback')
        return _hold('tenant_allowlist_required')
    limit = parse_max_gyms(env.get(_MAX_GYMS_ENV))
    if limit is None:
        log('held: max gyms must be an integer')
        return _hold('max_gyms_invalid')

    if _producer is None:
        try:
            runner = initialize_source_capture(environ=env)
        except CaptureIngestError as exc:
            log(f'held: {exc} — no fallback to untrusted data')
            return _hold(str(exc))
        if runner is None:
            log('held: source capture startup returned no runner')
            return _hold('source_capture_runner_disabled')
        producer = TrustedSourceObservationProducer(
            resolve_mapping=runner.collector._resolve,
            authenticate_capture=runner.collector.authenticate_capture,
            environ=env)
        approved = tuple(runner.collector._resolve._approved.keys())
    else:
        producer, approved = _producer, _approved_gyms

    unknown = [g for g in allowlist if g not in approved]
    targets = [g for g in allowlist if g in approved]
    if len(targets) > limit:
        # Fail-closed: the per-run bound is a capacity gate, not a starvation
        # lane. Rejecting the oversized selection is safer than observing a
        # permanent prefix while the tail defers forever.
        log(f'held: {len(targets)} selected gym(s) exceed per-run limit {limit} '
            f'— no silent deferral')
        return _hold('allowlist_exceeds_limit')

    entries = []
    observed = held = errors = 0
    for gym_id in targets:
        try:
            result = producer.observe(gym_id)
            if result.get('state') == 'held':
                held += 1
                entries.append({'gym_id': gym_id, 'status': 'held',
                                'hold': 'observation_state_held',
                                'observation_id': result.get('observation_id'),
                                'content_sha256': result.get('content_sha256')})
                log(f"{gym_id}: held (observation_id={result.get('observation_id')}, "
                    f"content_sha256={result.get('content_sha256')})")
                continue
            observed += 1
            entries.append({'gym_id': gym_id, 'status': 'observed',
                            'state': result.get('state'),
                            'observation_id': result.get('observation_id'),
                            'content_sha256': result.get('content_sha256')})
            log(f"{gym_id}: observed ({result.get('state')}, "
                f"observation_id={result.get('observation_id')})")
        except ObservationHold as exc:
            held += 1
            entries.append({'gym_id': gym_id, 'status': 'held', 'hold': str(exc)})
            log(f'{gym_id}: held ({exc})')
        except Exception as exc:  # noqa: BLE001 - one gym never sinks the run
            errors += 1
            entries.append({'gym_id': gym_id, 'status': 'error',
                            'error': type(exc).__name__})
            log(f'{gym_id}: error ({type(exc).__name__})')
    for gym_id in unknown:
        held += 1
        entries.append({'gym_id': gym_id, 'status': 'held',
                        'hold': 'gym_not_in_approved_mapping'})
        log(f'{gym_id}: held (gym_not_in_approved_mapping)')
    log(f'done: {observed} observed, {held} held, {errors} error(s) '
        f'(limit {limit})')
    return {'state': 'complete', 'limit': limit, 'observed': observed,
            'held': held, 'errors': errors, 'gyms': entries}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument('--gym', action='append', default=None,
                   help='restrict to this gym UUID (repeatable); smoke-test only')
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    if args.gym:
        # --gym may only NARROW the configured tenant allowlist; it never
        # replaces or widens it. Anything outside the configured allowlist is
        # rejected before the job runs.
        narrowed = parse_allowlist(','.join(args.gym))
        if narrowed is None:
            print('[source-observation-job] error: --gym requires valid, unique '
                  'canonical gym UUIDs')
            return 2
        configured = parse_allowlist(os.environ.get(_ALLOWLIST_ENV))
        if configured is None:
            print('[source-observation-job] error: --gym only narrows an existing '
                  f'{_ALLOWLIST_ENV}; configure the tenant allowlist first')
            return 2
        outside = [g for g in narrowed if g not in configured]
        if outside:
            print('[source-observation-job] error: --gym outside the configured '
                  f'tenant allowlist (rejected, no observation): {outside}')
            return 2
        os.environ[_ALLOWLIST_ENV] = ','.join(narrowed)
    receipt = run_job()
    # Concise receipt only: counts, statuses and hold codes. Never report
    # bodies, raw source bytes, credentials, or private mapping contents.
    summary = {k: v for k, v in receipt.items() if k != 'gyms'}
    print(f'[source-observation-job] result: {json.dumps(summary, sort_keys=True)}')
    for entry in receipt.get('gyms') or []:
        detail = (entry.get('hold') or entry.get('error')
                  or f"{entry.get('state')} (observation_id={entry.get('observation_id')})")
        print(f"  {entry['gym_id']}  {entry['status']}: {detail}")
    # Exit code contract: OFF and a genuinely complete run return 0. An armed
    # hold (startup gate, invalid allowlist, oversize selection, any per-gym
    # hold) or any observation error exits nonzero — after every selected gym
    # has been reported above.
    if receipt.get('state') == 'off':
        return 0
    if (receipt.get('state') == 'complete' and receipt.get('held') == 0
            and receipt.get('errors') == 0):
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
