"""UNWIRED producer-side client for the issuer dispatch store (enqueue/readback).

Delegates to an injected store implementing the frozen API:
- submit(request): stores immutable original manifest bytes plus
  tenant/version/binding. The store authenticates the session_user and its
  tenant grant in SQL; the request's tenant field is content, never identity.
- result(tenant, version, binding): returns only the exact committed safe
  status and receipt UUID.

This client accepts NO caller-supplied tenant identity: submit() takes only
the frozen IssuerDispatchRequest content object; any explicit identity
keyword is rejected. It exposes no issue, reconcile, claim or commit methods
and holds no credentials, DSN or transport configuration. This module never
imports or creates a store implementation; the store is injected by the
integration owner.
"""
from __future__ import annotations

from .generated_hosted_byte_issuer_worker import IssuerDispatchRequest
from .generated_hosted_byte_authority import _tenant, _uuid


class IssuerDispatchClientHold(RuntimeError):
    """Static diagnostic only; never expose tenants, URLs, bytes or exceptions."""


class IssuerDispatchClient:
    """Enqueue/readback only. The store is the sole identity authority."""

    def __init__(self, *, store):
        if (store is None
                or not callable(getattr(store, 'submit', None))
                or not callable(getattr(store, 'result', None))):
            raise IssuerDispatchClientHold('generated_issuer_client_store_required')
        self._store = store

    def submit(self, request, **kwargs):
        """Enqueue one frozen request. Identity keywords are always rejected."""
        if kwargs:
            # No extra caller fields of any kind are accepted on submit; the
            # request content object is the entire payload and identity comes
            # from the store's authenticated session, never from the caller.
            raise IssuerDispatchClientHold('generated_issuer_client_identity_rejected')
        if type(request) is not IssuerDispatchRequest:
            raise IssuerDispatchClientHold('generated_issuer_client_request_invalid')
        try:
            self._store.submit(request)
        except Exception:
            raise IssuerDispatchClientHold('generated_issuer_client_submit_failed') from None
        return {'status': 'submitted'}

    def result(self, tenant, version, binding):
        """Read back only the committed safe status and receipt UUID.

        tenant is a lookup filter; the store still validates the session's
        tenant grant in SQL, so a caller cannot read another tenant's row.
        Returns {'status', 'receipt_id'} and nothing else.
        """
        try:
            tenant = _tenant(tenant)
            version = _uuid(version)
            if type(binding) is not str or len(binding) != 64:
                raise ValueError()
            value = self._store.result(tenant, version, binding)
        except IssuerDispatchClientHold:
            raise
        except Exception:
            raise IssuerDispatchClientHold('generated_issuer_client_result_failed') from None
        if not isinstance(value, dict):
            raise IssuerDispatchClientHold('generated_issuer_client_result_failed')
        status = value.get('status')
        receipt_id = value.get('receipt_id')
        if type(status) is not str or not status:
            raise IssuerDispatchClientHold('generated_issuer_client_result_failed')
        if receipt_id is not None:
            try:
                receipt_id = _uuid(receipt_id)
            except Exception:
                raise IssuerDispatchClientHold('generated_issuer_client_result_failed') from None
        return {'status': status, 'receipt_id': receipt_id}
