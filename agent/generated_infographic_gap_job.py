"""Finite cron entrypoint for the generated infographic gap owner scan.

Default OFF. This module adds no owner path of its own: when armed it narrows
the existing explicit owner tenant allowlist to this job's allowlist and runs
exactly one `generated_infographic_runtime --scan` pass, which is the sole
owner execution (`generated_infographic_gap_owner.run_pending`). The runtime
owns flag gating, journal checks, owner identity, binding and the owner DB
connection lifecycle; this wrapper never opens a database and never loops.

Missing/invalid cron allowlist, or a cron tenant not present in the owner
allowlist, holds without running anything. The job process always terminates;
concurrent binds are fenced by the owner's journal phase and SQL locks, not by
a local lock file.
"""
from __future__ import annotations

import json
import os
import re

from . import generated_infographic_runtime as runtime

FLAG = 'AGENT_GENERATED_GAP_CRON'
CRON_TENANTS_ENV = 'AGENT_GENERATED_GAP_TENANTS'
TENANTS_ENV = 'AGENT_FORWARD_MEDIA_OWNER_TENANTS'
_TENANT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')


class JobHold(RuntimeError):
    """Static diagnostic only; never tenant data, credentials or source bytes."""


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _cron_tenants():
    tenants = tuple(dict.fromkeys(
        t.strip() for t in os.getenv(CRON_TENANTS_ENV, '').split(',') if t.strip()))
    if not tenants or len(tenants) > 32 or any(not _TENANT.fullmatch(t) for t in tenants):
        raise JobHold('generated_gap_cron_tenants_required')
    return tenants


def _scoped_owner_tenants(cron_tenants):
    owner = set(t.strip() for t in os.getenv(TENANTS_ENV, '').split(',') if t.strip())
    if not owner or any(not _TENANT.fullmatch(t) for t in owner):
        raise JobHold('explicit_tenant_allowlist_required')
    if any(t not in owner for t in cron_tenants):
        raise JobHold('generated_gap_cron_tenant_not_allowlisted')
    return ','.join(cron_tenants)


def run():
    """One finite pass; returns a process exit code. No resources are opened here."""
    if not enabled():
        report = dict(ok=False, held=True, reason='generated_gap_cron_disabled')
        print(json.dumps(report, sort_keys=True))
        return 0
    try:
        cron_tenants = _cron_tenants()
        scoped = _scoped_owner_tenants(cron_tenants)
        previous = os.environ.get(TENANTS_ENV)
        os.environ[TENANTS_ENV] = scoped
        try:
            return runtime.main(['--scan'])
        finally:
            if previous is None:
                os.environ.pop(TENANTS_ENV, None)
            else:
                os.environ[TENANTS_ENV] = previous
    except JobHold as exc:
        report = dict(ok=False, held=True, reason=str(exc))
        print(json.dumps(report, sort_keys=True))
        return 2


def main(argv=None):
    raise SystemExit(run())


if __name__ == '__main__':
    main()
