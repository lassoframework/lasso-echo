"""UNWIRED PostgreSQL queue + dispatch ledger store for the issuer worker.

Implements the frozen shared API against the DRAFT migration
migrations/DRAFT_generated_issuer_dispatch_20261009.sql: submit(request),
result(tenant, version, binding), pending(allowed_tenants, limit), plus the
DispatchLedger protocol claim(key, binding_digest) / commit(key,
binding_digest, receipt_id) consumed by HostedByteIssuerWorker.

Every operation opens a fresh dedicated AUTOCOMMIT connection from the
injected factory, so a claim's INSERT ... ON CONFLICT row is committed to
shared storage BEFORE claim returns is_new=True, and a crash between claim
and commit leaves the durable row with receipt NULL. SQL authenticates
session_user plus an admin-provisioned tenant grant; a tenant value from
request content is never trusted alone. All failures surface as static safe
codes only: no SQL text, DSNs, URLs, bytes or exception detail. No
environment/DSN discovery, flag, background job, retry or consumer wiring.
"""
from __future__ import annotations

import hashlib
import re

from .generated_hosted_byte_authority import (
    HostedByteHold,
    _sha,
    _tenant,
    _uuid,
)
from .generated_hosted_byte_issuer_worker import (
    DispatchLedger,
    IssuerDispatchRequest,
    LedgerClaim,
    _binding_digest,
    dispatch_key,
)


class GeneratedIssuerDispatchHold(RuntimeError):
    """Static diagnostic only; never expose URLs, bytes, DSNs or exceptions."""


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise GeneratedIssuerDispatchHold("generated_dispatch_key_invalid")
    return value


class GeneratedIssuerDispatchStore(DispatchLedger):
    """Shared PostgreSQL store; all durable state lives in the DRAFT tables.

    The injected connection factory supplies one idle autocommit connection
    per call, authenticated as the producer or issuer LOGIN provisioned by
    the integration owner. Dependency injection is an operator/test boundary,
    never a request-content tool.
    """

    def __init__(self, connection_factory):
        if not callable(connection_factory):
            raise GeneratedIssuerDispatchHold("generated_dispatch_store_factory_required")
        self._factory = connection_factory

    def _open(self):
        conn = None
        try:
            conn = self._factory()
            if conn.autocommit is not True or int(conn.info.transaction_status) != 0:
                raise ValueError()
            return conn
        except Exception:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            raise GeneratedIssuerDispatchHold(
                "generated_dispatch_store_connection_invalid") from None

    @staticmethod
    def _close(conn):
        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _denied(error, mapping, fallback):
        state = getattr(error, "sqlstate", None)
        if isinstance(state, str) and state in mapping:
            return mapping[state]
        return fallback

    def _call(self, mapping, fallback, operation):
        conn = self._open()
        try:
            return operation(conn)
        except GeneratedIssuerDispatchHold:
            raise
        except Exception as error:
            raise GeneratedIssuerDispatchHold(
                self._denied(error, mapping, fallback)) from None
        finally:
            self._close(conn)

    # Producer API ---------------------------------------------------------

    def submit(self, request):
        """Store the immutable original request bytes exactly once per key.

        An identical replay returns the committed row; the same tenant and
        version with a changed binding is a static conflict, never a row.
        """
        if type(request) is not IssuerDispatchRequest:
            raise GeneratedIssuerDispatchHold("generated_dispatch_request_invalid")
        key = dispatch_key(request.tenant, request.artifact_version_id)
        manifest_sha = hashlib.sha256(request.manifest_bytes).hexdigest()
        binding = request.binding_digest()

        def run(conn):
            return conn.execute(
                "select public.generated_issuer_dispatch_submit_20261009"
                "(%s,%s,%s,%s,%s,%s,%s,%s)",
                (request.tenant, request.artifact_version_id, request.hosted_url,
                 request.expected_sha256, manifest_sha, request.manifest_bytes,
                 binding, key)).fetchone()[0]

        value = self._call({"42501": "generated_dispatch_submit_rejected",
                            "23514": "generated_dispatch_binding_conflict",
                            "P0002": "generated_dispatch_submit_rejected"},
                           "generated_dispatch_store_unavailable", run)
        if (not isinstance(value, dict) or value.get("dispatch_key") != key
                or value.get("safe_status") not in ("submitted", "issued")):
            raise GeneratedIssuerDispatchHold("generated_dispatch_store_unavailable")
        receipt = value.get("receipt_id")
        if receipt is not None:
            try:
                receipt = _uuid(str(receipt))
            except HostedByteHold:
                raise GeneratedIssuerDispatchHold(
                    "generated_dispatch_store_unavailable") from None
        return {"dispatch_key": key, "status": value["safe_status"],
                "receipt_id": receipt}

    def result(self, tenant, version, binding):
        """Return only the exact committed safe status and receipt UUID."""
        try:
            tenant, version, binding = _tenant(tenant), _uuid(version), _sha(binding)
        except HostedByteHold:
            raise GeneratedIssuerDispatchHold("generated_dispatch_result_invalid") from None

        def run(conn):
            return conn.execute(
                "select public.generated_issuer_dispatch_result_20261009(%s,%s,%s)",
                (tenant, version, binding)).fetchone()[0]

        value = self._call({"42501": "generated_dispatch_result_rejected"},
                           "generated_dispatch_store_unavailable", run)
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("safe_status") not in ("submitted", "issued"):
            raise GeneratedIssuerDispatchHold("generated_dispatch_store_unavailable")
        receipt = value.get("receipt_id")
        if receipt is not None:
            try:
                receipt = _uuid(str(receipt))
            except HostedByteHold:
                raise GeneratedIssuerDispatchHold(
                    "generated_dispatch_store_unavailable") from None
        return {"status": value["safe_status"], "receipt_id": receipt}

    # Issuer API -----------------------------------------------------------

    def pending(self, allowed_tenants, limit):
        """Immutable queued requests for tenants the issuer is granted.

        Rows already issued (queue status/receipt or a committed ledger
        receipt) are never returned again. allowed_tenants may be any
        non-string iterable (the worker job passes a frozenset); values are
        normalized and deduplicated here and passed to SQL only as a bound
        text[] parameter, never interpolated into SQL text.
        """
        if (isinstance(allowed_tenants, (str, bytes))
                or type(limit) is not int or not 1 <= limit <= 1000):
            raise GeneratedIssuerDispatchHold("generated_dispatch_pending_invalid")
        try:
            raw = list(allowed_tenants)
        except TypeError:
            raise GeneratedIssuerDispatchHold(
                "generated_dispatch_pending_invalid") from None
        if not raw:
            raise GeneratedIssuerDispatchHold("generated_dispatch_pending_invalid")
        try:
            tenants = list(dict.fromkeys(_tenant(t) for t in raw))
        except HostedByteHold:
            raise GeneratedIssuerDispatchHold("generated_dispatch_pending_invalid") from None

        def run(conn):
            return conn.execute(
                "select gym_id, artifact_version_id, hosted_url, expected_sha256,"
                " manifest_bytes from public.generated_issuer_dispatch_pending_20261009(%s,%s)",
                (tenants, limit)).fetchall()

        rows = self._call({"42501": "generated_dispatch_pending_rejected",
                           "23514": "generated_dispatch_pending_invalid"},
                          "generated_dispatch_store_unavailable", run)
        requests = []
        for gym_id, version, url, sha, manifest in rows:
            try:
                requests.append(IssuerDispatchRequest(
                    tenant=gym_id, artifact_version_id=str(version), hosted_url=url,
                    expected_sha256=sha, manifest_bytes=bytes(manifest)))
                continue
            except Exception:
                # A stored row that fails the frozen request validation can
                # never be issued. Quarantine exactly that row and CONTINUE
                # with healthy rows: one poison row must never abort the
                # batch. SQL pending excludes quarantined rows, so it never
                # reappears and never starves later valid work.
                pass
            self._quarantine_row(gym_id, version, url, sha, manifest)
        return requests

    def _quarantine_row(self, gym_id, version, url, sha, manifest):
        """Durable quarantine of one invalid row; fail closed and LOUD.

        A successful quarantine lets the pending loop continue with healthy
        rows. A failed or uncertain quarantine (derivation error, connection
        or RPC failure, or any non-true RPC result) raises a static-safe hold
        instead of silently skipping: a silently unquarantined oldest poison
        row would starve healthy work behind it forever while pending reports
        empty/success. One bounded RPC attempt per row per call; no retries,
        no unbounded loops. The row is never issued and the ledger and
        authority are never touched.
        """
        try:
            key = dispatch_key(_tenant(gym_id), _uuid(str(version)))
            binding = _binding_digest(url, sha, bytes(manifest))
        except Exception:
            raise GeneratedIssuerDispatchHold(
                "generated_dispatch_quarantine_unavailable") from None

        def run(conn):
            return conn.execute(
                "select public.generated_issuer_dispatch_quarantine_20261009(%s,%s,%s)",
                (key, binding, "generated_dispatch_request_invalid")).fetchone()[0]

        try:
            value = self._call(
                {"42501": "generated_dispatch_quarantine_rejected",
                 "23514": "generated_dispatch_quarantine_rejected",
                 "P0002": "generated_dispatch_quarantine_rejected"},
                "generated_dispatch_store_unavailable", run)
        except GeneratedIssuerDispatchHold:
            raise
        except Exception:
            raise GeneratedIssuerDispatchHold(
                "generated_dispatch_quarantine_unavailable") from None
        if value is not True:
            raise GeneratedIssuerDispatchHold(
                "generated_dispatch_quarantine_unavailable")

    # DispatchLedger protocol ----------------------------------------------

    def claim(self, dispatch_key_value, binding_digest):
        key, binding = _key(dispatch_key_value), _key(binding_digest)

        def run(conn):
            return conn.execute(
                "select public.generated_issuer_dispatch_claim_20261009(%s,%s)",
                (key, binding)).fetchone()[0]

        # The function call is the whole autocommit transaction: the inserted
        # ledger row is durable before this returns is_new=True.
        value = self._call({"42501": "generated_dispatch_claim_rejected",
                            "23514": "generated_dispatch_binding_conflict",
                            "P0002": "generated_dispatch_claim_rejected"},
                           "generated_dispatch_store_unavailable", run)
        if (not isinstance(value, dict) or type(value.get("is_new")) is not bool
                or not isinstance(value.get("binding_digest"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["binding_digest"])):
            raise GeneratedIssuerDispatchHold("generated_dispatch_store_unavailable")
        receipt = value.get("receipt_id")
        if receipt is not None:
            try:
                receipt = _uuid(str(receipt))
            except HostedByteHold:
                raise GeneratedIssuerDispatchHold(
                    "generated_dispatch_store_unavailable") from None
        return LedgerClaim(is_new=value["is_new"],
                           binding_digest=value["binding_digest"],
                           receipt_id=receipt)

    def commit(self, dispatch_key_value, binding_digest, receipt_id):
        """Commit exactly one receipt verifiable as committed authority.

        SQL rejects a missing key, a binding mismatch, a different existing
        receipt, and any receipt UUID that is not the authority's committed
        receipt for this exact binding (same artifact version, hosted URL,
        expected sha and manifest identity). A fabricated receipt can never
        commit; the same receipt UUID is idempotent.
        """
        key, binding = _key(dispatch_key_value), _key(binding_digest)
        try:
            receipt = _uuid(receipt_id)
        except HostedByteHold:
            raise GeneratedIssuerDispatchHold("generated_dispatch_commit_invalid") from None

        def run(conn):
            return conn.execute(
                "select public.generated_issuer_dispatch_commit_20261009(%s,%s,%s)",
                (key, binding, receipt)).fetchone()[0]

        # Missing key, binding mismatch and a different existing receipt all
        # map to one static rejection; the same receipt UUID is idempotent.
        value = self._call({"42501": "generated_dispatch_commit_rejected",
                            "23514": "generated_dispatch_commit_rejected",
                            "P0002": "generated_dispatch_commit_rejected"},
                           "generated_dispatch_store_unavailable", run)
        if value is not True:
            raise GeneratedIssuerDispatchHold("generated_dispatch_store_unavailable")
        return True
