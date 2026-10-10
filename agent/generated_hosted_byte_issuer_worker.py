"""UNWIRED issuer-side dispatch adapter for the trusted hosted-byte authority.

Accepts a strict frozen request plus a transport-authenticated tenant identity,
binds both in a deterministic durable dispatch key, and calls the trusted
authority's issue() at most once per durable dispatch. Lost/uncertain ACKs use
exact-binding reconcile(); nothing is re-issued blindly. Returns only the bound
receipt UUID plus a static safe status. No DSN, credential, scope, reader or
transport configuration is accepted from request content. OFF or unprovisioned
means no issuance. The durable ledger is an injected protocol; this package
ships no durable implementation (see docs/GENERATED_HOSTED_BYTE_ISSUER_WORKER_20261009.md).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .generated_hosted_byte_authority import (
    GeneratedHostedByteAuthority,
    HostedByteHold,
    _manifest,
    _sha,
    _tenant,
    _url,
    _uuid,
)

FLAG = 'AGENT_GENERATED_HOSTED_BYTE_ISSUER'
_UNCERTAIN = ('generated_authority_commit_uncertain',
              'generated_authority_cleanup_uncertain')


class IssuerDispatchHold(RuntimeError):
    """Static diagnostic only; never expose URLs, bytes, DSNs or exceptions."""


def enabled():
    return os.getenv(FLAG, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def dispatch_key(tenant, artifact_version_id):
    """Deterministic durable identity: authenticated tenant plus version UUID."""
    return _digest(('echo-generated-hosted-byte-dispatch-20261009:'
                    + _tenant(tenant) + ':' + _uuid(artifact_version_id)).encode())


def _binding_digest(hosted_url, expected_sha256, manifest_bytes):
    return _digest(json.dumps(
        dict(hosted_url=hosted_url, expected_sha256=expected_sha256,
             manifest_sha256=_digest(manifest_bytes)),
        sort_keys=True, separators=(',', ':')).encode())


@dataclass(frozen=True)
class IssuerDispatchRequest:
    """Strict frozen dispatch content; identity fields only, no configuration.

    Carries tenant, artifact version, exact hosted URL, expected byte SHA and
    the ORIGINAL manifest bytes. It cannot carry tenant URL scopes, issuer
    credentials, a DB factory, a reader or transport settings. Validation
    reuses the authority's exact binding rules, including manifest identity.
    """
    tenant: str
    artifact_version_id: str
    hosted_url: str
    expected_sha256: str
    manifest_bytes: bytes

    def __post_init__(self):
        try:
            if type(self.tenant) is not str:
                raise HostedByteHold('generated_tenant_invalid')
            _tenant(self.tenant)
            _uuid(self.artifact_version_id)
            _sha(self.expected_sha256)
            _url(self.hosted_url)
            _manifest(self.manifest_bytes, self.tenant, self.artifact_version_id,
                      self.hosted_url, self.expected_sha256)
        except HostedByteHold as hold:
            raise IssuerDispatchHold(str(hold)) from None

    def binding_digest(self):
        return _binding_digest(self.hosted_url, self.expected_sha256, self.manifest_bytes)


@dataclass(frozen=True)
class LedgerClaim:
    """Atomic claim result. is_new is True only for the first durable claim."""
    is_new: bool
    binding_digest: str
    receipt_id: str = None


class DispatchLedger:
    """Durable dispatch ledger protocol injected by the integration owner.

    REQUIRED SEMANTICS (fail closed if any cannot be met):
    - claim(key, binding_digest) atomically inserts (key, binding_digest,
      receipt NULL) and returns LedgerClaim(is_new=True, ...) when the key is
      absent, otherwise returns the committed row as LedgerClaim(is_new=False,
      stored binding_digest, stored receipt_id). The insert must be atomic
      across ALL issuer processes and durable (committed to shared storage)
      BEFORE claim returns; an in-memory dict or per-service local SQLite does
      NOT satisfy this across Railway services and must not be used in
      production. Two concurrent claims of one key must yield exactly one
      is_new=True, which is what bounds authority.issue() to at most once.
    - commit(key, binding_digest, receipt_id) durably records the receipt UUID
      exactly once. It must reject a missing key, a binding_digest mismatch,
      or a different existing receipt UUID, and must be idempotent for the
      same receipt UUID.
    - Restart semantics: after a crash between claim and commit the row exists
      with receipt NULL. A later dispatch of the same key observes
      is_new=False, receipt_id=None and MUST reconcile through the trusted
      authority instead of issuing again. Unblocking a permanently held row is
      an operator action by the integration owner, never this worker.
    """

    def claim(self, dispatch_key_value, binding_digest):
        raise NotImplementedError

    def commit(self, dispatch_key_value, binding_digest, receipt_id):
        raise NotImplementedError


class HostedByteIssuerWorker:
    """Stateless issuer adapter; all durable state lives in the injected ledger.

    Constructed by the integration owner with the trusted issuer authority
    (reader configured) and a durable DispatchLedger. Default OFF: dispatch
    holds unless the FLAG environment gate is explicitly enabled by the
    operator. No environment/DSN discovery beyond that gate; no background
    job, retries, CLI or consumer wiring is added.
    """

    def __init__(self, *, authority, ledger):
        if (type(authority) is not GeneratedHostedByteAuthority
                or authority._reader is None):
            raise IssuerDispatchHold('generated_issuer_authority_required')
        if not isinstance(ledger, DispatchLedger):
            raise IssuerDispatchHold('generated_issuer_durable_ledger_required')
        self._authority = authority
        self._ledger = ledger

    def _call(self, operation, *args):
        try:
            return operation(*args)
        except Exception:
            raise IssuerDispatchHold('generated_issuer_ledger_unavailable') from None

    @staticmethod
    def _receipt_uuid(value):
        try:
            return _uuid(value)
        except HostedByteHold:
            raise IssuerDispatchHold('generated_issuer_receipt_invalid') from None

    def _reconcile(self, key, request):
        try:
            receipt = self._authority.reconcile(
                artifact_version_id=request.artifact_version_id,
                hosted_url=request.hosted_url,
                expected_sha256=request.expected_sha256,
                manifest_bytes=request.manifest_bytes)
        except IssuerDispatchHold:
            raise
        except Exception:
            # Absent, changed or unavailable binding: hold; never issue again.
            raise IssuerDispatchHold('generated_issuer_reconcile_unavailable') from None
        receipt_id = self._receipt_uuid(receipt.get('receipt_id'))
        self._call(self._ledger.commit, key, request.binding_digest(), receipt_id)
        return {'receipt_id': receipt_id, 'status': 'reconciled'}

    def dispatch(self, request, *, authenticated_tenant):
        if not enabled():
            raise IssuerDispatchHold('generated_issuer_off')
        if type(request) is not IssuerDispatchRequest:
            raise IssuerDispatchHold('generated_issuer_request_invalid')
        try:
            if _tenant(authenticated_tenant) != request.tenant:
                raise IssuerDispatchHold('generated_issuer_cross_tenant')
            if self._authority.tenant_id != request.tenant:
                raise IssuerDispatchHold('generated_issuer_cross_tenant')
        except HostedByteHold:
            raise IssuerDispatchHold('generated_issuer_request_invalid') from None
        key = dispatch_key(request.tenant, request.artifact_version_id)
        binding = request.binding_digest()
        claim = self._call(self._ledger.claim, key, binding)
        if type(claim) is not LedgerClaim or claim.binding_digest != binding:
            # Same tenant+version with changed URL, SHA or manifest: conflict.
            raise IssuerDispatchHold('generated_issuer_binding_conflict')
        if claim.receipt_id is not None:
            # Replay of a durable completed dispatch: one receipt, no re-issue.
            return {'receipt_id': self._receipt_uuid(claim.receipt_id), 'status': 'replayed'}
        if not claim.is_new:
            # Crash between claim and commit: reconcile the exact frozen
            # identity; hold when the authority has no matching receipt.
            return self._reconcile(key, request)
        try:
            receipt = self._authority.issue(
                artifact_version_id=request.artifact_version_id,
                hosted_url=request.hosted_url,
                expected_sha256=request.expected_sha256,
                manifest_bytes=request.manifest_bytes)
        except IssuerDispatchHold:
            raise
        except HostedByteHold as hold:
            if str(hold) in _UNCERTAIN:
                # COMMIT may have reached the server: exact reconcile only.
                return self._reconcile(key, request)
            # Deterministic content/authorization hold; static code, no detail.
            raise IssuerDispatchHold(str(hold)) from None
        except Exception:
            raise IssuerDispatchHold('generated_issuer_issue_unavailable') from None
        receipt_id = self._receipt_uuid(receipt.get('receipt_id'))
        self._call(self._ledger.commit, key, binding, receipt_id)
        return {'receipt_id': receipt_id, 'status': 'issued'}
