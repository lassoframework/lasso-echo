"""UNWIRED bounded issuer-dispatch polling job for the hosted-byte authority.

Runs a finite number of ticks. Each tick pulls at most a hard-capped number of
immutable pending dispatch requests from an injected store (frozen API:
pending(allowed_tenants, limit)), maps each through the existing
HostedByteIssuerWorker.dispatch semantics, and tallies only safe status
counts. The authenticated tenant for every dispatch comes from the
SQL-established provenance carried on the pending row itself, never from a
caller-supplied field.

Default OFF: unless the FLAG environment gate is explicitly enabled AND a
store plus a worker (or authority + durable ledger) are provisioned, run()
performs zero store/authority calls and returns a static 'off' status. No
infinite loop, no retries beyond the bounded tick count, no background
daemon, no environment/DSN discovery beyond the flag gate, no CLI wiring.

Store protocol (injected, owned by the integration owner; this module never
imports or creates a store implementation):
- pending(allowed_tenants, limit) -> iterable of immutable rows, each
  exposing tenant, artifact_version_id, hosted_url, expected_sha256 and
  manifest_bytes, where tenant is SQL-established provenance.
- submit(request) / result(tenant, version, binding) are producer-side and
  are never called by this job.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .generated_hosted_byte_issuer_worker import (
    DispatchLedger,
    HostedByteIssuerWorker,
    IssuerDispatchHold,
    IssuerDispatchRequest,
)
from .generated_hosted_byte_authority import GeneratedHostedByteAuthority, _tenant

FLAG = 'AGENT_GENERATED_ISSUER_DISPATCH'

DEFAULT_BATCH_LIMIT = 25
MAX_BATCH_LIMIT = 100
DEFAULT_MAX_TICKS = 1
MAX_TICKS = 10

_SAFE_STATUSES = frozenset({'issued', 'replayed', 'reconciled'})


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


class IssuerDispatchJobHold(RuntimeError):
    """Static diagnostic only; never expose rows, tenants, URLs or exceptions."""


def _bounded_int(value, default, maximum, code):
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= maximum:
        raise IssuerDispatchJobHold(code)
    return value


def _allowed_tenants(values):
    try:
        tenants = frozenset(_tenant(v) for v in values)
    except Exception:
        raise IssuerDispatchJobHold('generated_issuer_dispatch_tenants_invalid') from None
    return tenants


@dataclass(frozen=True)
class IssuerDispatchJobResult:
    """Safe tally only: counts and a static status, no row or error detail."""
    status: str
    ticks: int
    processed: int
    issued: int
    replayed: int
    reconciled: int
    held: int
    failed: int

    def as_dict(self):
        return dict(status=self.status, ticks=self.ticks, processed=self.processed,
                    issued=self.issued, replayed=self.replayed,
                    reconciled=self.reconciled, held=self.held, failed=self.failed)


_OFF_RESULT = IssuerDispatchJobResult(
    status='generated_issuer_dispatch_off', ticks=0, processed=0,
    issued=0, replayed=0, reconciled=0, held=0, failed=0)


def off_result():
    return _OFF_RESULT


class IssuerDispatchJob:
    """Finite polling runner over an injected store and issuer worker.

    Provisioned by the integration owner with either a ready
    HostedByteIssuerWorker, or the trusted authority plus a durable
    DispatchLedger from which the worker is built. OFF or unprovisioned means
    run() returns the static off result without touching the store, the
    worker, the authority or the ledger.
    """

    def __init__(self, *, store=None, worker=None, authority=None, ledger=None,
                 allowed_tenants=(), batch_limit=None, max_ticks=None):
        self._store = store
        if worker is not None:
            if not isinstance(worker, HostedByteIssuerWorker):
                raise IssuerDispatchJobHold('generated_issuer_dispatch_worker_invalid')
            self._worker = worker
        elif authority is not None or ledger is not None:
            if (type(authority) is not GeneratedHostedByteAuthority
                    or not isinstance(ledger, DispatchLedger)):
                raise IssuerDispatchJobHold('generated_issuer_dispatch_worker_invalid')
            self._worker = HostedByteIssuerWorker(authority=authority, ledger=ledger)
        else:
            self._worker = None
        self._allowed = _allowed_tenants(allowed_tenants or ())
        self._batch_limit = _bounded_int(
            batch_limit, DEFAULT_BATCH_LIMIT, MAX_BATCH_LIMIT,
            'generated_issuer_dispatch_limit_invalid')
        self._max_ticks = _bounded_int(
            max_ticks, DEFAULT_MAX_TICKS, MAX_TICKS,
            'generated_issuer_dispatch_ticks_invalid')

    def _provisioned(self):
        return (self._store is not None and self._worker is not None
                and bool(self._allowed)
                and callable(getattr(self._store, 'pending', None)))

    def _request_from_row(self, row):
        """Map a pending row to a frozen request; tenant is row provenance only."""
        try:
            tenant = _tenant(row.tenant)
        except Exception:
            raise IssuerDispatchJobHold('generated_issuer_dispatch_row_invalid') from None
        if tenant not in self._allowed:
            # SQL-established provenance outside the granted set: hold, never dispatch.
            raise IssuerDispatchJobHold('generated_issuer_dispatch_cross_tenant')
        try:
            request = IssuerDispatchRequest(
                tenant=tenant,
                artifact_version_id=row.artifact_version_id,
                hosted_url=row.hosted_url,
                expected_sha256=row.expected_sha256,
                manifest_bytes=row.manifest_bytes)
        except Exception:
            raise IssuerDispatchJobHold('generated_issuer_dispatch_row_invalid') from None
        return tenant, request

    def run(self):
        """Execute at most max_ticks bounded ticks; return a safe tally."""
        if not enabled() or not self._provisioned():
            return _OFF_RESULT
        ticks = processed = issued = replayed = reconciled = held = failed = 0
        for _ in range(self._max_ticks):
            try:
                rows = list(self._store.pending(self._allowed, self._batch_limit))
            except Exception:
                failed += 1
                break
            if not rows:
                break
            ticks += 1
            for row in rows[:self._batch_limit]:
                processed += 1
                try:
                    tenant, request = self._request_from_row(row)
                except IssuerDispatchJobHold as hold:
                    if str(hold) == 'generated_issuer_dispatch_cross_tenant':
                        held += 1
                    else:
                        failed += 1
                    continue
                try:
                    outcome = self._worker.dispatch(
                        request, authenticated_tenant=tenant)
                except Exception:
                    # Per-request failure is counted, never raised with detail,
                    # and never aborts the tick.
                    failed += 1
                    continue
                status = outcome.get('status') if isinstance(outcome, dict) else None
                if status in _SAFE_STATUSES:
                    if status == 'issued':
                        issued += 1
                    elif status == 'replayed':
                        replayed += 1
                    else:
                        reconciled += 1
                else:
                    failed += 1
        return IssuerDispatchJobResult(
            status='generated_issuer_dispatch_ok', ticks=ticks, processed=processed,
            issued=issued, replayed=replayed, reconciled=reconciled,
            held=held, failed=failed)
