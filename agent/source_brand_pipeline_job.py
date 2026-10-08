"""source_brand_pipeline_job.py — finite, default-OFF single-process
capture->observation sequential cron lane.

WHY THIS EXISTS: the two separate cron lanes (agent.source_brand_capture_job
and agent.source_brand_observation_job) run as separate Railway services. Two
services with two independent volumes do NOT share the local SQLite capture
receipt journal — the observation service's journal would be empty, so every
observation would hold on an unverifiable capture. This module runs BOTH
phases inside ONE process on ONE Railway service, so both phases use the SAME
private durable journal directory (ECHO_SOURCE_CAPTURE_JOURNAL_DIR) mounted
on that single service. There is no cross-service journal assumption at all.

SEQUENCING (fail-closed):
  1. Capture phase runs first via source_brand_capture_job.run_job(environ).
  2. Observation runs ONLY when the capture receipt is state 'complete' with
     zero holds and zero errors. Any capture whole-run hold, per-gym hold, or
     per-gym error means observation NEVER runs — no observation after a
     capture hold/error, ever. The pipeline then exits nonzero.

GATES (all fail-closed; everything defaults OFF):
  - ECHO_SOURCE_PIPELINE_JOB_ENABLED must equal 'true' or the run exits
    cleanly with NO I/O at all (no journals, no network, no files) and code 0.
  - BOTH phase jobs must be explicitly enabled: ECHO_SOURCE_CAPTURE_JOB_ENABLED
    and ECHO_SOURCE_OBSERVATION_JOB_ENABLED must each equal 'true', else the
    whole pipeline holds (phase_enablement_required). There is no global
    unlock — enabling the pipeline alone runs nothing.
  - BOTH phase allowlists must be configured and must match EXACTLY (same
    canonical gym UUID set): ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST and
    ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST. Missing, malformed, or mismatched
    allowlists hold the whole pipeline (tenant_allowlist_required /
    pipeline_allowlist_mismatch) before any phase runs.
  - Per-run work stays bounded: each phase enforces its own MAX_GYMS
    fail-closed capacity gate (default 50, hard clamp 1..50); the pipeline
    adds no wider bound of its own.
  - All underlying startup gates apply unchanged inside each phase (see
    source_brand_startup.py): runner/collector/ingest enable flags, the
    private operator-owned approved-mappings file, the private durable
    journal dir ECHO_SOURCE_CAPTURE_JOURNAL_DIR (one shared volume on THIS
    single service), collector Supabase config, the Zernio credential, and
    the producer gate ECHO_SOURCE_OBSERVATION_ENABLED.

RECEIPTS: bounded per-gym receipts from both phases are carried through
verbatim (each already sanitized: counts, gym UUIDs, statuses, hold codes,
request/observation IDs, content_sha256 only — never raw source bytes,
credentials, report bodies, or mapping contents).

RECEIVER API:
  run_job(environ=None, *, logger=None, _capture=None, _observe=None)
  `_capture`/`_observe` are callables (environ) -> receipt; they are the
  injectable test seams. Production defaults are exactly
  source_brand_capture_job.run_job and source_brand_observation_job.run_job,
  called with the SAME environ dict, in that order.

CLI: `python -m agent.source_brand_pipeline_job [--day YYYY-MM-DD]`.
  Exit codes: OFF or both phases complete with zero holds/errors -> 0; any
  armed hold or error in either phase (or observation skipped) -> 1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import source_brand_capture_job as capture_phase
from . import source_brand_observation_job as observation_phase

_JOB_FLAG = 'ECHO_SOURCE_PIPELINE_JOB_ENABLED'
_CAPTURE_FLAG = 'ECHO_SOURCE_CAPTURE_JOB_ENABLED'
_OBSERVATION_FLAG = 'ECHO_SOURCE_OBSERVATION_JOB_ENABLED'
_CAPTURE_ALLOWLIST_ENV = 'ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST'
_OBSERVATION_ALLOWLIST_ENV = 'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST'
_DAY_ENV = 'ECHO_SOURCE_CAPTURE_JOB_DAY'


def _hold(code, **extra):
    return {'state': 'held', 'hold': code, 'capture': None,
            'observation': None, **extra}


def _phase_ok(receipt):
    return (isinstance(receipt, dict) and receipt.get('state') == 'complete'
            and receipt.get('held') == 0 and receipt.get('errors') == 0)


def run_job(environ=None, *, logger=None, _capture=None, _observe=None):
    """Capture-then-observe over the shared allowlist in ONE process.

    Both phases receive the same environ mapping, so both resolve the same
    ECHO_SOURCE_CAPTURE_JOURNAL_DIR private durable journal on this service's
    single volume.
    """
    env = dict(os.environ if environ is None else environ)
    log = logger or (lambda m: print(f'[source-pipeline-job] {m}'))
    if env.get(_JOB_FLAG) != 'true':
        return {'state': 'off'}
    if env.get(_CAPTURE_FLAG) != 'true' or env.get(_OBSERVATION_FLAG) != 'true':
        log('held: both phase jobs must be explicitly enabled — no global unlock')
        return _hold('phase_enablement_required')
    capture_allow = capture_phase.parse_allowlist(env.get(_CAPTURE_ALLOWLIST_ENV))
    observe_allow = observation_phase.parse_allowlist(env.get(_OBSERVATION_ALLOWLIST_ENV))
    if capture_allow is None or observe_allow is None:
        log('held: tenant allowlist missing or invalid — no fallback')
        return _hold('tenant_allowlist_required')
    if set(capture_allow) != set(observe_allow):
        # Fail-closed: the two phases must target exactly the same canonical
        # gym set. Never run observation over a set capture did not cover.
        log('held: capture and observation allowlists differ — no inference')
        return _hold('pipeline_allowlist_mismatch')

    run_capture = _capture or (lambda e: capture_phase.run_job(environ=e))
    run_observe = _observe or (lambda e: observation_phase.run_job(environ=e))

    capture_receipt = run_capture(env)
    if not _phase_ok(capture_receipt):
        # Never run observation after a capture hold/error/off state.
        log('held: capture phase did not complete cleanly — observation skipped')
        return {'state': 'held', 'hold': 'capture_phase_incomplete',
                'capture': capture_receipt, 'observation': None}

    observation_receipt = run_observe(env)
    receipt = {'state': 'complete', 'capture': capture_receipt,
               'observation': observation_receipt}
    if not _phase_ok(observation_receipt):
        receipt['state'] = 'held'
        receipt['hold'] = 'observation_phase_incomplete'
    return receipt


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument('--day', default=None,
                   help='scheduled UTC day YYYY-MM-DD (forwarded to the capture phase)')
    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    if args.day:
        os.environ[_DAY_ENV] = args.day
    receipt = run_job()
    # Concise sanitized receipt only — counts, statuses, hold codes. Never raw
    # source bytes, credentials, report bodies, or private mapping contents.
    def _summary(phase_receipt):
        if not isinstance(phase_receipt, dict):
            return phase_receipt
        return {k: v for k, v in phase_receipt.items() if k != 'gyms'}
    summary = {k: (_summary(v) if k in ('capture', 'observation') else v)
               for k, v in receipt.items()}
    print(f'[source-pipeline-job] result: {json.dumps(summary, sort_keys=True, default=str)}')
    if receipt.get('state') == 'off':
        return 0
    if receipt.get('state') == 'complete':
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
