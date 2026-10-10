"""UNWIRED issuer dispatch runtime bootstrap (Child B composition root).

Assembles the bounded issuer dispatch job entirely from injected pieces:

- a restricted issuer store (injected; must expose the issuer-side frozen API
  ``pending(allowed_tenants, limit)`` plus the ``DispatchLedger`` protocol
  ``claim``/``commit``; producer-side methods are wrapped away),
- a trusted hosted authority (``GeneratedHostedByteAuthority`` with its
  operator-configured tenant URL scope reader, provisioned by the integration
  owner),
- a trusted tenant namespace config (``TrustedTenantNamespace`` — the frozen
  set of tenants this runtime is granted to issue for),
- the existing bounded job (``IssuerDispatchJob``) and worker
  (``HostedByteIssuerWorker``).

Hard rules:
- Default OFF. ``AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME`` must be explicitly
  enabled AND the job flag (``AGENT_GENERATED_ISSUER_DISPATCH``) AND the
  worker flag (``AGENT_GENERATED_HOSTED_BYTE_ISSUER``) must be enabled, or
  ``run_once()`` returns a static off result having made zero calls into the
  store, authority, ledger, worker or job.
- Finite. ``run_once()`` performs exactly one bounded job run (at most
  ``max_ticks`` ticks, hard cap 10; at most ``batch_limit`` rows per tick,
  hard cap 100) and returns. There is no loop, no scheduling, no daemon, no
  retry, no CLI wiring.
- No secret discovery. This module reads only its own flag plus the existing
  job and worker flags. It accepts no DSN, credential, token, connection string,
  URL or file path in any parameter; string/bytes values passed as the store,
  authority or namespace are rejected, and unexpected keyword arguments are
  rejected by the fixed signature. Nothing is logged here; failures surface
  only as static hold codes and the job's safe tally.
- No broad service role. The injected store is wrapped in
  ``RestrictedIssuerStoreView`` so the assembled job and worker can reach
  only the issuer-side surface (``pending``/``claim``/``commit``); the
  producer-side ``submit``/``result`` methods, if the concrete store has
  them, are unreachable through this runtime. The store must be authenticated
  as the dedicated restricted issuer DB role (see the companion doc); this
  module cannot and does not verify the role itself.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .generated_hosted_byte_authority import (
    GeneratedHostedByteAuthority,
    HostedByteHold,
    _tenant,
)
from .generated_hosted_byte_issuer_worker import (
    DispatchLedger,
    HostedByteIssuerWorker,
    enabled as issuer_enabled,
)
from .generated_issuer_dispatch_job import (
    IssuerDispatchJob,
    IssuerDispatchJobHold,
    enabled as job_enabled,
    off_result,
)

FLAG = 'AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME'


class IssuerDispatchRuntimeHold(RuntimeError):
    """Static diagnostic only; never expose rows, tenants, URLs or exceptions."""


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class TrustedTenantNamespace:
    """Frozen set of tenants this runtime is granted to issue for.

    Values are validated with the authority's canonical tenant rules; the
    set is immutable and non-empty. This is operator-provisioned
    configuration, never request content.
    """
    tenants: frozenset

    def __init__(self, tenants):
        if isinstance(tenants, (str, bytes)):
            raise IssuerDispatchRuntimeHold('generated_runtime_namespace_invalid')
        try:
            values = frozenset(_tenant(v) for v in tenants)
        except (HostedByteHold, TypeError):
            raise IssuerDispatchRuntimeHold(
                'generated_runtime_namespace_invalid') from None
        if not values:
            raise IssuerDispatchRuntimeHold('generated_runtime_namespace_invalid')
        object.__setattr__(self, 'tenants', values)


class RestrictedIssuerStoreView(DispatchLedger):
    """Issuer-only view over the injected store.

    Exposes exactly ``pending``, ``claim`` and ``commit`` to the assembled
    job/worker. Producer-side methods (``submit``, ``result``) and any other
    attribute of the concrete store are unreachable through this view.
    """

    def __init__(self, store):
        if isinstance(store, (str, bytes)) or store is None:
            raise IssuerDispatchRuntimeHold('generated_runtime_store_invalid')
        for name in ('pending', 'claim', 'commit'):
            if not callable(getattr(store, name, None)):
                raise IssuerDispatchRuntimeHold('generated_runtime_store_invalid')
        self._store = store

    def pending(self, allowed_tenants, limit):
        return self._store.pending(allowed_tenants, limit)

    def claim(self, dispatch_key_value, binding_digest):
        return self._store.claim(dispatch_key_value, binding_digest)

    def commit(self, dispatch_key_value, binding_digest, receipt_id):
        return self._store.commit(dispatch_key_value, binding_digest, receipt_id)


class IssuerDispatchRuntime:
    """Composition root: injected pieces in, one bounded run out.

    Construction assembles the real worker and job and fails closed on any
    mis-provisioning. The namespace must contain the authority's own tenant;
    a runtime granted no namespace coverage for its authority can never
    dispatch and is rejected at assembly.
    """

    def __init__(self, *, store, authority, namespace,
                 batch_limit=None, max_ticks=None):
        if isinstance(authority, (str, bytes)) \
                or type(authority) is not GeneratedHostedByteAuthority:
            raise IssuerDispatchRuntimeHold('generated_runtime_authority_invalid')
        if not isinstance(namespace, TrustedTenantNamespace):
            raise IssuerDispatchRuntimeHold('generated_runtime_namespace_invalid')
        view = RestrictedIssuerStoreView(store)
        if authority.tenant_id not in namespace.tenants:
            raise IssuerDispatchRuntimeHold('generated_runtime_namespace_invalid')
        try:
            worker = HostedByteIssuerWorker(authority=authority, ledger=view)
            self._job = IssuerDispatchJob(
                store=view, worker=worker,
                allowed_tenants=frozenset({authority.tenant_id}),
                batch_limit=batch_limit, max_ticks=max_ticks)
        except IssuerDispatchJobHold as hold:
            raise IssuerDispatchRuntimeHold(str(hold)) from None
        except Exception:
            raise IssuerDispatchRuntimeHold(
                'generated_runtime_assembly_invalid') from None

    def run_once(self):
        """One bounded job run; OFF returns the static off tally untouched.

        Never raises row, tenant, URL or exception detail: any unexpected
        failure inside the run surfaces as one static hold code.
        """
        if not (enabled() and job_enabled() and issuer_enabled()):
            return off_result().as_dict()
        try:
            return self._job.run().as_dict()
        except Exception:
            raise IssuerDispatchRuntimeHold(
                'generated_runtime_run_failed') from None
