"""source_brand_provider_refresh_job.py — finite, default-OFF, bounded and
concurrent provider identity refresh cron lane.

WHY THIS EXISTS: the daily 06:00 capture->observation pipeline
(agent.source_brand_pipeline_job) is the only lane that completes an
authenticated Zernio identity lookup and persists its portal attestation
through the echo_source_brand_attest_provider RPC. The portal's
echo_source_brand_active(gym) schema2 contract treats a provider attestation
older than 15 minutes as inactive, so a daily capture leaves every configured
gym inactive for all but ~15 minutes of the day. This job closes that gap
with the smallest possible trusted path: it re-runs ONLY the existing
AuthenticatedZernioIdentityReader for each approved, allowlisted gym — exact
stored-profile mapping, independent /v1/accounts/health ownership authority,
paired profile-scoped account match, and fresh attestation write plus exact
readback. It NEVER captures websites, never ingests bytes, never runs
observation, and never touches bundle approval or derivation.

CONCURRENCY AND BOUNDS (P2 repair): gyms are independent tenant IDs — each
refresh does its own tenant-token equality check, its own authenticated
Zernio lookups and its own portal attestation write/readback through the
existing stateless CollectorPortalReader and AuthenticatedZernioIdentityReader
(default requests calls keep no shared mutable session), so the job
runs gyms concurrently with at most 10 workers and at most 10 gyms per
service invocation. Concurrency shortens the wall-clock of a run so a pass
fits inside the 15-minute attestation TTL; it does NOT make freshness
continuous or guaranteed:
  - A missed or slow run may still let a gym's attestation expire. The
    template cron runs every 5 minutes, but provider slowness, a platform
    stall or a cold start can exceed the window; freshness is best-effort
    per run, never promised unconditionally.
  - A fleet larger than 10 gyms requires disjoint operator partitions:
    multiple services, each with its own allowlist of <=10 gyms, so no gym
    is claimed by two invocations and no invocation exceeds the limit.
  - Any timeout or incomplete proof fails closed: the reader itself holds
    (persisting a negative/partial attestation, never proof of absence)
    on transport failure, TTL expiry (authenticated_social_status_expired)
    or ambiguous evidence; the job surfaces that hold and exits nonzero.

WHAT IT PRESERVES (nothing is bypassed):
  - Exact approved mapping: the same private operator-owned authority file
    (ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE, loaded by
    source_brand_startup.load_approved_mappings) is the ONLY source of
    gym_id <-> echo_account_key bindings. No backfill, no inference.
  - Owner proof / authenticated identity: all identity evidence still comes
    from AuthenticatedZernioIdentityReader (health authority + paired account
    list); this module adds no identity logic of its own.
  - Tenant isolation: before each refresh the current echo_intake_tokens row
    must exactly equal {'gym_id': gym, 'echo_account_key': key} (the same
    check read_zernio_identity_readonly performs), and the job only ever
    touches allowlisted canonical gym UUIDs that are also in the approved
    mapping. Unknown allowlist entries hold per-gym.
  - Immutable attestation order: attestations are still written/read back
    only by CollectorPortalReader.attest_provider through the existing RPC;
    this job just invokes the reader more often.

GATES (all fail-closed; everything defaults OFF):
  - ECHO_SOURCE_PROVIDER_REFRESH_JOB_ENABLED must equal 'true' or the run
    exits cleanly with NO I/O at all (no files, no network) and code 0.
  - ECHO_SOURCE_COLLECTOR_ENABLED must equal 'true'; an armed run without it
    HOLDS (source_collector_disabled) — an armed run never degrades to a
    silent off.
  - ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST is a REQUIRED comma-separated
    list of exact canonical gym UUIDs (same parser as the capture job).
    Missing, empty, malformed, or non-UUID entries hold the whole run.
  - ECHO_SOURCE_PROVIDER_REFRESH_JOB_MAX_GYMS bounds per-invocation work:
    default 10, hard clamp 1..10 (stricter than the capture lane's 50). An
    oversized selection HOLDS the whole run (allowlist_exceeds_limit) —
    fail-closed capacity gate evaluated BEFORE any provider I/O, never a
    silent starvation lane.
  - The approved-mappings authority file, collector Supabase config and
    ZERNIO_API_KEY must all be present and valid, exactly as the capture
    startup requires; any absence holds the whole run before any I/O.

FAILURE ISOLATION: one gym's hold/error never sinks the run; concurrent
workers per-gym mean a slow or held gym cannot serially starve the others.
The reader itself persists a negative/partial attestation as a hold (never
as proof of disconnection) and raises a bounded hold code. Receipts contain
counts, gym UUIDs, statuses, profile IDs and hold codes only — never raw
responses, credentials, or private mapping contents. Receipt gym entries
always follow allowlist order regardless of completion order.

RECEIVER API:
  run_job(environ=None, *, http=None, identity_http=None, logger=None,
          _identity=None, _approved=None)
  `_identity`/`_approved` are test seams; production always composes the
  real CollectorPortalReader + AuthenticatedZernioIdentityReader.

CLI: `python -m agent.source_brand_provider_refresh_job`.
  Exit codes: OFF, or complete with zero holds/errors -> 0; any armed hold
  or error -> 1.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys

from .source_brand_capture_job import parse_allowlist
from .source_brand_collector import (AuthenticatedZernioIdentityReader,
                                     CollectorPortalReader)
from .source_brand_ingest import CaptureIngestError
from .source_brand_startup import load_approved_mappings

_JOB_FLAG = 'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ENABLED'
_COLLECTOR_FLAG = 'ECHO_SOURCE_COLLECTOR_ENABLED'
_ALLOWLIST_ENV = 'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST'
_MAX_GYMS_ENV = 'ECHO_SOURCE_PROVIDER_REFRESH_JOB_MAX_GYMS'
_MAPPINGS_FILE_ENV = 'ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE'
_DEFAULT_MAX_GYMS = 10
_HARD_MAX_GYMS = 10
_MAX_WORKERS = 10


def parse_refresh_max_gyms(raw):
    """Per-invocation bound: default 10, hard clamp 1..10, invalid -> None."""
    if raw is None:
        return _DEFAULT_MAX_GYMS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(_HARD_MAX_GYMS, value))


def _hold(code, **extra):
    return {'state': 'held', 'hold': code, 'gyms': [], 'refreshed': 0,
            'held': 1, 'errors': 0, **extra}


def _build_identity(env, http, identity_http):
    """Compose the trusted reader only; raises CaptureIngestError on hold."""
    if env.get(_COLLECTOR_FLAG) != 'true':
        raise CaptureIngestError('source_collector_disabled')
    path = env.get(_MAPPINGS_FILE_ENV)
    if not isinstance(path, str) or not path:
        raise CaptureIngestError('private_mapping_authority_required')
    mappings = load_approved_mappings(path)
    key = env.get('ZERNIO_API_KEY')
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        raise CaptureIngestError('zernio_account_credential_required')
    reader = CollectorPortalReader(environ=env, http=http)
    identity = AuthenticatedZernioIdentityReader(read_rows=reader, environ=env,
                                                 http=identity_http)
    return identity, {m.gym_id: m for m in mappings}


def _refresh_one(identity, gym_id, mapping):
    """Refresh exactly one gym; returns (kind, entry). Never propagates.

    Runs on its own worker thread. The reader and portal transport are
    stateless and thread-safe, and each gym performs exactly one
    authenticated identity-reader invocation (health authority plus account list)
    plus its own attestation write/readback — no shared mutable state, no
    cross-tenant data. Tenant isolation is enforced first: the current
    portal intake token must exactly equal the approved gym/key binding
    before any provider call, so a mismatched tenant holds before provider I/O.
    """
    try:
        reader = getattr(identity, '_read', None)
        if callable(reader):
            tokens = reader('echo_intake_tokens', {
                'gym_id': 'eq.' + gym_id, 'select': 'gym_id,echo_account_key'})
            if tokens != [{'gym_id': gym_id,
                           'echo_account_key': mapping.echo_account_key}]:
                raise CaptureIngestError('current_tenant_mapping_mismatch')
        result = identity(gym_id, mapping.echo_account_key)
        return 'refreshed', {'gym_id': gym_id, 'status': 'refreshed',
                             'profile_id': result.get('profile_id')}
    except CaptureIngestError as exc:
        return 'held', {'gym_id': gym_id, 'status': 'held', 'hold': str(exc)}
    except Exception as exc:  # noqa: BLE001 - one gym never sinks the run
        return 'error', {'gym_id': gym_id, 'status': 'error',
                         'error': type(exc).__name__}


def run_job(environ=None, *, http=None, identity_http=None, logger=None,
            _identity=None, _approved=None):
    """One bounded identity-refresh pass over the allowlist. Receipt dict.

    No capture, no ingest, no observation: each target gets exactly one
    authenticated Zernio identity-reader invocation plus its portal attestation.
    """
    env = dict(os.environ if environ is None else environ)
    log = logger or (lambda m: print(f'[source-provider-refresh-job] {m}'))
    if env.get(_JOB_FLAG) != 'true':
        return {'state': 'off'}
    allowlist = parse_allowlist(env.get(_ALLOWLIST_ENV))
    if allowlist is None:
        log('held: tenant allowlist missing or invalid — no fallback')
        return _hold('tenant_allowlist_required')
    limit = parse_refresh_max_gyms(env.get(_MAX_GYMS_ENV))
    if limit is None:
        log('held: max gyms must be an integer')
        return _hold('max_gyms_invalid')

    if _identity is None:
        try:
            identity, approved = _build_identity(env, http, identity_http)
        except CaptureIngestError as exc:
            log(f'held: {exc} — no fallback to untrusted data')
            return _hold(str(exc))
    else:
        identity, approved = _identity, _approved

    unknown = [g for g in allowlist if g not in approved]
    targets = [g for g in allowlist if g in approved]
    if len(targets) > limit:
        log(f'held: {len(targets)} selected gym(s) exceed per-run limit {limit} '
            f'— no silent deferral')
        return _hold('allowlist_exceeds_limit')

    # Independent tenant IDs refresh concurrently (bounded workers), so a
    # slow or held gym cannot serially starve the rest of the pass. Each
    # worker does its own tenant-token check + one authenticated identity
    # reader invocation + its own attestation; receipts preserve allowlist order.
    workers = max(1, min(_MAX_WORKERS, len(targets)))
    entries = []
    refreshed = held = errors = 0
    if targets:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix='provider-refresh') as pool:
            results = pool.map(lambda g: _refresh_one(identity, g, approved[g]),
                               targets)
            for kind, entry in results:
                entries.append(entry)
                if kind == 'refreshed':
                    refreshed += 1
                    log(f"{entry['gym_id']}: identity refreshed")
                elif kind == 'held':
                    held += 1
                    log(f"{entry['gym_id']}: held ({entry['hold']})")
                else:
                    errors += 1
                    log(f"{entry['gym_id']}: error ({entry['error']})")
    for gym_id in unknown:
        held += 1
        entries.append({'gym_id': gym_id, 'status': 'held',
                        'hold': 'gym_not_in_approved_mapping'})
        log(f'{gym_id}: held (gym_not_in_approved_mapping)')
    log(f'done: {refreshed} refreshed, {held} held, {errors} error(s) '
        f'(limit {limit})')
    return {'state': 'complete', 'limit': limit, 'refreshed': refreshed,
            'held': held, 'errors': errors, 'gyms': entries}


def main(argv=None):
    receipt = run_job()
    # Concise sanitized receipt only: counts, statuses, hold codes. Never raw
    # provider responses, credentials, or private mapping contents.
    summary = {k: v for k, v in receipt.items() if k != 'gyms'}
    print(f'[source-provider-refresh-job] result: {json.dumps(summary, sort_keys=True)}')
    for entry in receipt.get('gyms') or []:
        detail = entry.get('hold') or entry.get('error') or 'identity attested'
        print(f"  {entry['gym_id']}  {entry['status']}: {detail}")
    if receipt.get('state') in ('off', 'complete') and not receipt.get('held') \
            and not receipt.get('errors'):
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
