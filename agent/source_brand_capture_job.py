"""source_brand_capture_job.py — finite, default-OFF single-run capture cron lane.

WHY THIS EXISTS: `initialize_source_capture` + `SourceBrandCaptureRunner.capture`
are the reviewed owner path, but nothing scheduled them. This module is a thin,
testable single-run CLI wrapper around exactly that composition — no new capture
logic, no scheduler, no long-running loop. One invocation captures the bounded
allowlisted set and exits, so it is safe as a Railway cron `startCommand`.

GATES (all fail-closed; everything defaults OFF):
  - ECHO_SOURCE_CAPTURE_JOB_ENABLED must equal 'true' or the run exits cleanly
    with no I/O at all (no journals, no network, no files).
  - The runner's own startup gates then apply: ECHO_SOURCE_CAPTURE_RUNNER_ENABLED,
    ECHO_SOURCE_COLLECTOR_ENABLED, ECHO_SOURCE_CAPTURE_INGEST_ENABLED, the private
    operator-owned approved-mappings file, the private journal dir, the collector
    Supabase config and the Zernio credential (see source_brand_startup.py).
  - ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST is a REQUIRED comma-separated list of exact
    canonical gym UUIDs. Missing, empty, malformed, or non-UUID entries hold the
    whole run — there is never a fallback to "all mapped gyms" or any untrusted
    discovery. Only allowlisted gyms are captured (no cross-tenant work).
  - ECHO_SOURCE_CAPTURE_JOB_MAX_GYMS bounds per-run work (default 50, clamped
    1..50 — sufficient for the current 14-gym global rollout). An allowlist
    larger than the limit HOLDS the whole run (allowlist_exceeds_limit): the
    bound is a fail-closed capacity gate, never a silent starvation lane where
    targets[:limit] would permanently defer the tail of the allowlist.
  - ECHO_SOURCE_CAPTURE_JOB_DAY (YYYY-MM-DD, UTC) pins the scheduled day for a
    deterministic durable request ID; when unset the current UTC date is used.

DURABLE REQUEST ID: 'source-capture:{gym_uuid}:{scheduled_utc_day}' — stable per
(gym, scheduled day), so a retried cron invocation reuses the same receipt
binding in the durable journal instead of claiming a new request.

FAILURE ISOLATION: one gym's hold/error never sinks the run; every gym gets its
own receipt entry. Receipts contain counts, request IDs and hold codes only —
never raw source bytes, credentials, or private mapping contents.

REMAINING CONTRACT GAP (held, not bypassed): real operator-installed approved
mappings, dedicated collector credentials/volume, and portal #793's SQL/RPC
contract are NOT provisioned by this file. Until they exist, every armed run
holds with the startup hold code. See docs/SOURCE_CAPTURE_JOB.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

from .source_brand_ingest import CaptureIngestError
from .source_brand_startup import initialize_source_capture

_JOB_FLAG = 'ECHO_SOURCE_CAPTURE_JOB_ENABLED'
_ALLOWLIST_ENV = 'ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST'
_MAX_GYMS_ENV = 'ECHO_SOURCE_CAPTURE_JOB_MAX_GYMS'
_DAY_ENV = 'ECHO_SOURCE_CAPTURE_JOB_DAY'
_DEFAULT_MAX_GYMS = 50
_HARD_MAX_GYMS = 50


def durable_request_id(gym_id, scheduled_day):
    """Deterministic orchestration request ID per approved gym/scheduled UTC day."""
    return f'source-capture:{gym_id}:{scheduled_day}'


def parse_allowlist(raw):
    """Exact canonical gym UUIDs, or None (hold) when missing/empty/invalid."""
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


def scheduled_day(env):
    raw = env.get(_DAY_ENV)
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.strptime(raw.strip(), '%Y-%m-%d').date().isoformat()
        except ValueError:
            return None
    return datetime.now(timezone.utc).date().isoformat()


def _hold(code):
    return {'state': 'held', 'hold': code, 'gyms': [], 'captured': 0,
            'held': 1, 'errors': 0}


def run_job(environ=None, *, http=None, identity_http=None, apify_client=None,
            logger=None, _runner=None, _approved_gyms=None):
    """One bounded capture pass over the allowlist. Returns a receipt dict.

    `_runner`/`_approved_gyms` are test seams; production always builds the
    runner through initialize_source_capture (the sole trusted composition).
    """
    env = dict(os.environ if environ is None else environ)
    log = logger or (lambda m: print(f'[source-capture-job] {m}'))
    if env.get(_JOB_FLAG) != 'true':
        return {'state': 'off'}
    allowlist = parse_allowlist(env.get(_ALLOWLIST_ENV))
    if allowlist is None:
        log('held: tenant allowlist missing or invalid — no fallback')
        return _hold('tenant_allowlist_required')
    day = scheduled_day(env)
    if day is None:
        log('held: scheduled day must be YYYY-MM-DD')
        return _hold('scheduled_day_invalid')
    limit = parse_max_gyms(env.get(_MAX_GYMS_ENV))
    if limit is None:
        log('held: max gyms must be an integer')
        return _hold('max_gyms_invalid')

    if _runner is None:
        try:
            runner = initialize_source_capture(environ=env, http=http,
                identity_http=identity_http, apify_client=apify_client)
        except CaptureIngestError as exc:
            log(f'held: {exc} — no fallback to untrusted data')
            return _hold(str(exc))
        if runner is None:
            return {'state': 'off'}
        approved = tuple(runner.collector._resolve._approved.keys())
    else:
        runner, approved = _runner, _approved_gyms

    unknown = [g for g in allowlist if g not in approved]
    targets = [g for g in allowlist if g in approved]
    if len(targets) > limit:
        # Fail-closed: the per-run bound is a capacity gate, not a starvation
        # lane. Rejecting the oversized selection is safer than capturing a
        # permanent prefix while the tail defers forever.
        log(f'held: {len(targets)} selected gym(s) exceed per-run limit {limit} '
            f'— no silent deferral')
        return _hold('allowlist_exceeds_limit')

    entries = []
    captured = held = errors = 0
    for gym_id in targets:
        request_id = durable_request_id(gym_id, day)
        try:
            result = runner.capture(gym_id, request_id)
            captured += 1
            entries.append({'gym_id': gym_id, 'request_id': request_id,
                            'status': 'captured',
                            'captures': len(result.get('captures') or [])})
            log(f'{gym_id}: captured {len(result.get("captures") or [])} source(s) '
                f'({request_id})')
        except CaptureIngestError as exc:
            held += 1
            entries.append({'gym_id': gym_id, 'request_id': request_id,
                            'status': 'held', 'hold': str(exc)})
            log(f'{gym_id}: held ({exc})')
        except Exception as exc:  # noqa: BLE001 - one gym never sinks the run
            errors += 1
            entries.append({'gym_id': gym_id, 'request_id': request_id,
                            'status': 'error', 'error': type(exc).__name__})
            log(f'{gym_id}: error ({type(exc).__name__})')
    for gym_id in unknown:
        held += 1
        entries.append({'gym_id': gym_id, 'status': 'held',
                        'hold': 'gym_not_in_approved_mapping'})
        log(f'{gym_id}: held (gym_not_in_approved_mapping)')
    log(f'done: {captured} captured, {held} held, {errors} error(s) '
        f'(day {day}, limit {limit})')
    return {'state': 'complete', 'day': day, 'limit': limit,
            'captured': captured, 'held': held, 'errors': errors,
            'gyms': entries}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument('--day', default=None,
                   help='scheduled UTC day YYYY-MM-DD (overrides env; deterministic request IDs)')
    p.add_argument('--gym', action='append', default=None,
                   help='restrict to this gym UUID (repeatable); smoke-test only')
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    if args.day:
        os.environ[_DAY_ENV] = args.day
    if args.gym:
        # --gym may only NARROW the configured tenant allowlist; it never
        # replaces or widens it. Anything outside the configured allowlist is
        # rejected before the job runs.
        narrowed = parse_allowlist(','.join(args.gym))
        if narrowed is None:
            print('[source-capture-job] error: --gym requires valid, unique '
                  'canonical gym UUIDs')
            return 2
        configured = parse_allowlist(os.environ.get(_ALLOWLIST_ENV))
        if configured is None:
            print('[source-capture-job] error: --gym only narrows an existing '
                  f'{_ALLOWLIST_ENV}; configure the tenant allowlist first')
            return 2
        outside = [g for g in narrowed if g not in configured]
        if outside:
            print('[source-capture-job] error: --gym outside the configured '
                  f'tenant allowlist (rejected, no capture): {outside}')
            return 2
        os.environ[_ALLOWLIST_ENV] = ','.join(narrowed)
    receipt = run_job()
    # Concise receipt only: counts, statuses and hold codes. Never raw source
    # bytes, credentials, or private mapping contents.
    summary = {k: v for k, v in receipt.items() if k != 'gyms'}
    print(f'[source-capture-job] result: {json.dumps(summary, sort_keys=True)}')
    for entry in receipt.get('gyms') or []:
        detail = entry.get('hold') or entry.get('error') or f"{entry.get('captures', 0)} capture(s)"
        print(f"  {entry['gym_id']}  {entry['status']}: {detail}")
    # Exit code contract: OFF and a genuinely complete run return 0. An armed
    # hold (startup gate, invalid allowlist, oversize selection) or any capture
    # error exits nonzero — after every selected gym has been reported above.
    if receipt.get('state') == 'off':
        return 0
    if (receipt.get('state') == 'complete' and receipt.get('held') == 0
            and receipt.get('errors') == 0):
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
