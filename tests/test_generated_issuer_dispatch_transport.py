"""Tests for the UNWIRED issuer dispatch job and producer client (AGENT B).

Everything here uses in-memory fakes against the frozen store API; no real
store, database, credential or transport exists or is contacted.
"""
import json
import os
import uuid
from dataclasses import dataclass

import pytest

from agent import generated_issuer_dispatch_job as job_mod
from agent.generated_issuer_dispatch_job import (
    IssuerDispatchJob,
    IssuerDispatchJobHold,
)
from agent.generated_issuer_dispatch_client import (
    IssuerDispatchClient,
    IssuerDispatchClientHold,
)
from agent.generated_hosted_byte_issuer_worker import (
    DispatchLedger,
    IssuerDispatchHold,
    IssuerDispatchRequest,
    LedgerClaim,
)

TENANT_A = 'tenant-alpha'
TENANT_B = 'tenant-beta'
VERSION = str(uuid.uuid4())
URL = 'https://cdn.example.com/objects/file.png'
SHA = 'a' * 64
RECEIPT = str(uuid.uuid4())


def _manifest(tenant=TENANT_A, version=VERSION, url=URL, sha=SHA):
    return json.dumps({
        'gym_id': tenant,
        'artifact_version_id': version,
        'hosted_url': url,
        'delivered_sha256': sha,
    }, sort_keys=True).encode()


def _request(tenant=TENANT_A, version=VERSION, url=URL, sha=SHA):
    return IssuerDispatchRequest(
        tenant=tenant, artifact_version_id=version, hosted_url=url,
        expected_sha256=sha, manifest_bytes=_manifest(tenant, version, url, sha))


@dataclass(frozen=True)
class FakeRow:
    """Immutable pending row; tenant is SQL-established provenance."""
    tenant: str
    artifact_version_id: str
    hosted_url: str
    expected_sha256: str
    manifest_bytes: bytes


def _row(tenant=TENANT_A, version=VERSION, url=URL, sha=SHA):
    return FakeRow(tenant=tenant, artifact_version_id=version, hosted_url=url,
                   expected_sha256=sha,
                   manifest_bytes=_manifest(tenant, version, url, sha))


class FakeStore:
    """Frozen API surface: pending/submit/result. Records every call."""

    def __init__(self, rows=(), results=None, fail_pending=False):
        self.rows = list(rows)
        self.results = dict(results or {})
        self.fail_pending = fail_pending
        self.pending_calls = []
        self.submitted = []
        self.result_calls = []

    def pending(self, allowed_tenants, limit):
        self.pending_calls.append((frozenset(allowed_tenants), limit))
        if self.fail_pending:
            raise RuntimeError('store down')
        return list(self.rows)

    def submit(self, request):
        self.submitted.append(request)

    def result(self, tenant, version, binding):
        self.result_calls.append((tenant, version, binding))
        value = self.results.get((tenant, version, binding))
        if value is None:
            raise RuntimeError('not found')
        return value


class FakeLedger(DispatchLedger):
    def __init__(self):
        self.rows = {}

    def claim(self, key, binding_digest):
        if key in self.rows:
            binding, receipt = self.rows[key]
            return LedgerClaim(is_new=False, binding_digest=binding, receipt_id=receipt)
        self.rows[key] = (binding_digest, None)
        return LedgerClaim(is_new=True, binding_digest=binding_digest)

    def commit(self, key, binding_digest, receipt_id):
        binding, receipt = self.rows[key]
        assert binding == binding_digest and receipt in (None, receipt_id)
        self.rows[key] = (binding, receipt_id)


class FakeWorker:
    """Records dispatch calls; can be told to fail per request."""

    def __init__(self, outcome=None, fail=False):
        self.calls = []
        self.outcome = outcome or {'receipt_id': RECEIPT, 'status': 'issued'}
        self.fail = fail

    def dispatch(self, request, *, authenticated_tenant):
        self.calls.append((request, authenticated_tenant))
        if self.fail:
            raise IssuerDispatchHold('generated_issuer_issue_unavailable')
        return dict(self.outcome)


class _FakeWorkerType(FakeWorker):
    pass


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(job_mod.FLAG, raising=False)
    monkeypatch.delenv('AGENT_GENERATED_HOSTED_BYTE_ISSUER', raising=False)


def _make_job(monkeypatch, store, worker, tenants=(TENANT_A,), **kwargs):
    monkeypatch.setattr(job_mod, 'HostedByteIssuerWorker', _FakeWorkerType)
    monkeypatch.setenv(job_mod.FLAG, 'true')
    return IssuerDispatchJob(store=store, worker=worker,
                             allowed_tenants=tenants, **kwargs)


# --- job: OFF / unprovisioned -------------------------------------------------

def test_off_flag_returns_static_off_and_touches_nothing(monkeypatch):
    monkeypatch.setattr(job_mod, 'HostedByteIssuerWorker', _FakeWorkerType)
    store = FakeStore(rows=[_row()])
    worker = _FakeWorkerType()
    job = IssuerDispatchJob(store=store, worker=worker,
                            allowed_tenants=(TENANT_A,))
    result = job.run()
    assert result.status == 'generated_issuer_dispatch_off'
    assert result.ticks == 0 and result.processed == 0
    assert store.pending_calls == [] and store.submitted == []
    assert worker.calls == []


def test_unprovisioned_job_is_off_even_with_flag(monkeypatch):
    monkeypatch.setattr(job_mod, 'HostedByteIssuerWorker', _FakeWorkerType)
    monkeypatch.setenv(job_mod.FLAG, 'true')
    for job in (IssuerDispatchJob(store=None, worker=None,
                                  allowed_tenants=(TENANT_A,)),
                IssuerDispatchJob(store=FakeStore(), worker=None,
                                  allowed_tenants=(TENANT_A,)),
                IssuerDispatchJob(store=FakeStore(), worker=_FakeWorkerType(),
                                  allowed_tenants=())):
        result = job.run()
        assert result.status == 'generated_issuer_dispatch_off'
        assert result.processed == 0


# --- job: bounded ticks --------------------------------------------------------

def test_bounded_tick_count_respected(monkeypatch):
    store = FakeStore(rows=[_row()])
    worker = _FakeWorkerType()
    job = _make_job(monkeypatch, store, worker, max_ticks=3)
    result = job.run()
    # Rows never drain (fake store returns the same row each tick): exactly 3.
    assert result.ticks == 3
    assert len(store.pending_calls) == 3
    assert all(limit <= job_mod.MAX_BATCH_LIMIT
               for _, limit in store.pending_calls)
    assert result.processed == 3 and result.issued == 3


def test_empty_pending_stops_early(monkeypatch):
    store = FakeStore(rows=[])
    worker = _FakeWorkerType()
    job = _make_job(monkeypatch, store, worker, max_ticks=5)
    result = job.run()
    assert result.ticks == 0 and len(store.pending_calls) == 1


def test_limit_and_ticks_are_hard_capped(monkeypatch):
    monkeypatch.setenv(job_mod.FLAG, 'true')
    with pytest.raises(IssuerDispatchJobHold):
        IssuerDispatchJob(store=FakeStore(), allowed_tenants=(TENANT_A,),
                          batch_limit=job_mod.MAX_BATCH_LIMIT + 1)
    with pytest.raises(IssuerDispatchJobHold):
        IssuerDispatchJob(store=FakeStore(), allowed_tenants=(TENANT_A,),
                          max_ticks=job_mod.MAX_TICKS + 1)


def test_pending_called_with_allowed_tenants_and_cap(monkeypatch):
    store = FakeStore(rows=[_row()])
    worker = _FakeWorkerType()
    job = _make_job(monkeypatch, store, worker, batch_limit=7)
    job.run()
    assert store.pending_calls[0] == (frozenset({TENANT_A}), 7)


# --- job: per-request failure and cross-tenant holds ---------------------------

def test_per_request_failure_counted_not_raised_and_tick_continues(monkeypatch):
    rows = [_row(version=str(uuid.uuid4())) for _ in range(3)]
    store = FakeStore(rows=rows)
    worker = _FakeWorkerType(fail=True)
    job = _make_job(monkeypatch, store, worker)
    result = job.run()
    assert result.ticks == 1
    assert result.processed == 3
    assert result.failed == 3
    assert result.issued == 0
    # All three rows were attempted despite every dispatch raising.
    assert len(worker.calls) == 3


def test_tenant_comes_from_row_provenance_not_caller(monkeypatch):
    store = FakeStore(rows=[_row()])
    worker = _FakeWorkerType()
    job = _make_job(monkeypatch, store, worker)
    result = job.run()
    assert result.issued == 1
    request, authenticated = worker.calls[0]
    assert authenticated == TENANT_A == request.tenant


def test_cross_tenant_pending_row_is_held_not_dispatched(monkeypatch):
    store = FakeStore(rows=[_row(tenant=TENANT_B,
                                 url='https://cdn.example.com/objects/b.png')])
    worker = _FakeWorkerType()
    job = _make_job(monkeypatch, store, worker, tenants=(TENANT_A,))
    result = job.run()
    assert result.held == 1
    assert result.issued == 0
    assert worker.calls == []


def test_store_pending_failure_aborts_cleanly_with_count(monkeypatch):
    store = FakeStore(fail_pending=True)
    worker = _FakeWorkerType()
    job = _make_job(monkeypatch, store, worker)
    result = job.run()
    assert result.failed == 1
    assert result.ticks == 0
    assert worker.calls == []


# --- client --------------------------------------------------------------------

def test_client_requires_store():
    with pytest.raises(IssuerDispatchClientHold):
        IssuerDispatchClient(store=None)


def test_client_submit_delegates_and_returns_safe_status():
    store = FakeStore()
    client = IssuerDispatchClient(store=store)
    request = _request()
    assert client.submit(request) == {'status': 'submitted'}
    assert store.submitted == [request]


def test_client_rejects_caller_supplied_identity_spoofing():
    store = FakeStore()
    client = IssuerDispatchClient(store=store)
    request = _request()
    for kwargs in ({'tenant': TENANT_B}, {'authenticated_tenant': TENANT_B},
                   {'session_user': 'issuer'}, {'role': 'issuer'},
                   {'dsn': 'postgres://x'}, {'anything': 'extra'}):
        with pytest.raises(IssuerDispatchClientHold):
            client.submit(request, **kwargs)
    assert store.submitted == []


def test_client_rejects_non_frozen_request():
    client = IssuerDispatchClient(store=FakeStore())
    with pytest.raises(IssuerDispatchClientHold):
        client.submit({'tenant': TENANT_A})


def test_client_exposes_no_issuer_methods():
    client = IssuerDispatchClient(store=FakeStore())
    public = {name for name in dir(client) if not name.startswith('_')}
    assert public == {'result', 'submit'}
    for forbidden in ('issue', 'reconcile', 'commit', 'claim', 'dispatch',
                      'pending', 'lookup'):
        assert not hasattr(client, forbidden)


def test_client_result_returns_only_safe_status_and_receipt():
    store = FakeStore(results={
        (TENANT_A, VERSION, 'b' * 64): {
            'status': 'issued', 'receipt_id': RECEIPT,
            'hosted_url': URL, 'manifest': 'leak', 'gym_id': TENANT_A},
    })
    client = IssuerDispatchClient(store=store)
    value = client.result(TENANT_A, VERSION, 'b' * 64)
    assert value == {'status': 'issued', 'receipt_id': RECEIPT}
    assert store.result_calls == [(TENANT_A, VERSION, 'b' * 64)]


def test_client_result_readback_failure_is_static():
    client = IssuerDispatchClient(store=FakeStore())
    with pytest.raises(IssuerDispatchClientHold):
        client.result(TENANT_A, VERSION, 'b' * 64)


# ============================================================================
# COMPOSED tests: real IssuerDispatchJob + real GeneratedIssuerDispatchStore.
#
# The store module imports no psycopg and takes connections only from an
# injected factory, so the composition runs fully in-process: the stub layer
# below mimics the DRAFT SQL functions' COMMITTED semantics in memory, under
# the store's public API. No real database, DSN or network exists.
# ============================================================================

import hashlib

from agent.generated_hosted_byte_authority import (
    GeneratedHostedByteAuthority,
    HostedObjectReader,
)
from agent.generated_hosted_byte_issuer_worker import (
    HostedByteIssuerWorker,
    dispatch_key,
)
from agent.generated_issuer_dispatch_store import GeneratedIssuerDispatchStore

PAYLOAD = b'fake-hosted-image-bytes'
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
C_RECEIPT = str(uuid.uuid4())


def _c_manifest(tenant=TENANT_A, version=VERSION, url=URL, sha=PAYLOAD_SHA):
    return json.dumps({
        'gym_id': tenant,
        'artifact_version_id': version,
        'hosted_url': url,
        'delivered_sha256': sha,
    }, sort_keys=True).encode()


def _c_request(tenant=TENANT_A, version=VERSION, url=URL, sha=PAYLOAD_SHA):
    return IssuerDispatchRequest(
        tenant=tenant, artifact_version_id=version, hosted_url=url,
        expected_sha256=sha, manifest_bytes=_c_manifest(tenant, version, url, sha))


class _StubInfo:
    transaction_status = 0


class _StubResult:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return self._value

    def fetchall(self):
        return self._value


class _StubDispatchDB:
    """In-memory image of the DRAFT dispatch tables plus SQL call record."""

    def __init__(self):
        self.rows = {}      # dispatch_key -> row dict (queue + receipt state)
        self.ledger = {}    # dispatch_key -> (binding_digest, receipt_id|None)
        self.sql_calls = []
        self.pending_tenant_args = []

    # -- DRAFT SQL function semantics (committed results only) --------------

    def fn_submit(self, tenant, version, url, sha, manifest_sha, manifest,
                  binding, key):
        row = self.rows.get(key)
        if row is not None:
            if row['binding'] != binding:
                raise RuntimeError('binding conflict')
            return {'dispatch_key': key,
                    'safe_status': 'issued' if row['receipt'] else 'submitted',
                    'receipt_id': row['receipt']}
        self.rows[key] = dict(tenant=tenant, version=version, url=url, sha=sha,
                              manifest=manifest, binding=binding, receipt=None)
        return {'dispatch_key': key, 'safe_status': 'submitted',
                'receipt_id': None}

    def fn_pending(self, tenants, limit):
        # Issued rows (receipt recorded) are excluded by the SQL function.
        self.pending_tenant_args.append(tenants)
        out = []
        for row in self.rows.values():
            if row['receipt'] is None and row['tenant'] in tenants:
                out.append((row['tenant'], row['version'], row['url'],
                            row['sha'], row['manifest']))
            if len(out) >= limit:
                break
        return out

    def fn_claim(self, key, binding):
        if key in self.ledger:
            stored_binding, receipt = self.ledger[key]
            return {'is_new': False, 'binding_digest': stored_binding,
                    'receipt_id': receipt}
        self.ledger[key] = (binding, None)
        return {'is_new': True, 'binding_digest': binding, 'receipt_id': None}

    def fn_commit(self, key, binding, receipt):
        if key not in self.ledger:
            raise RuntimeError('missing key')
        stored_binding, stored_receipt = self.ledger[key]
        if stored_binding != binding:
            raise RuntimeError('binding mismatch')
        if stored_receipt is not None and stored_receipt != receipt:
            raise RuntimeError('receipt conflict')
        self.ledger[key] = (stored_binding, receipt)
        if key in self.rows:
            self.rows[key]['receipt'] = receipt
        return True

    def fn_result(self, tenant, version, binding):
        for row in self.rows.values():
            if (row['tenant'], row['version'], row['binding']) == (tenant, version, binding):
                return {'safe_status': 'issued' if row['receipt'] else 'submitted',
                        'receipt_id': row['receipt']}
        return None


class _StubDispatchConn:
    """Autocommit connection the real store accepts; routes SQL text to the DB."""

    autocommit = True
    info = _StubInfo()

    def __init__(self, db):
        self._db = db

    def execute(self, sql, params):
        self._db.sql_calls.append(sql)
        if 'generated_issuer_dispatch_submit_20261009' in sql:
            return _StubResult((self._db.fn_submit(*params),))
        if 'generated_issuer_dispatch_pending_20261009' in sql:
            return _StubResult(self._db.fn_pending(*params))
        if 'generated_issuer_dispatch_claim_20261009' in sql:
            return _StubResult((self._db.fn_claim(*params),))
        if 'generated_issuer_dispatch_commit_20261009' in sql:
            return _StubResult((self._db.fn_commit(*params),))
        if 'generated_issuer_dispatch_result_20261009' in sql:
            return _StubResult((self._db.fn_result(*params),))
        raise AssertionError('unexpected SQL: ' + sql)

    def close(self):
        pass


class _StubAuthorityConn:
    """Non-autocommit connection for the real authority's issue path."""

    autocommit = False
    info = _StubInfo()

    def __init__(self, db):
        self._db = db

    def execute(self, sql, params):
        self._db.sql_calls.append(sql)
        if 'generated_hosted_byte_authorized_20261009' in sql:
            return _StubResult((True,))
        if 'generated_hosted_byte_issue_20261009' in sql:
            tenant, version, url, sha, manifest_sha, data, manifest = params
            receipt = {
                'receipt_id': C_RECEIPT,
                'gym_id': tenant,
                'artifact_version_id': version,
                'hosted_url': url,
                'delivered_sha256': sha,
                'render_manifest': json.loads(manifest.decode('utf-8')),
            }
            self._db.issued.append((version, receipt))
            return _StubResult((receipt,))
        raise AssertionError('unexpected SQL: ' + sql)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _ComposedRig:
    """Real job -> real store -> stub SQL, with a real worker + authority."""

    def __init__(self, monkeypatch, requests, tenants=(TENANT_A,), **job_kwargs):
        self.db = _StubDispatchDB()
        self.db.issued = []
        self.store = GeneratedIssuerDispatchStore(lambda: _StubDispatchConn(self.db))
        for request in requests:
            self.store.submit(request)
        reader = HostedObjectReader(
            {TENANT_A: ['https://cdn.example.com/objects/']})
        monkeypatch.setattr(HostedObjectReader, 'read',
                            lambda self, tenant, url: PAYLOAD)
        authority = GeneratedHostedByteAuthority(
            lambda: _StubAuthorityConn(self.db), tenant_id=TENANT_A,
            reader=reader)
        self.worker = HostedByteIssuerWorker(authority=authority, ledger=self.store)
        monkeypatch.setenv(job_mod.FLAG, 'true')
        monkeypatch.setenv('AGENT_GENERATED_HOSTED_BYTE_ISSUER', 'true')
        self.job = IssuerDispatchJob(store=self.store, worker=self.worker,
                                     allowed_tenants=tenants, **job_kwargs)


def test_composed_pending_accepts_job_frozenset_and_row_dispatches(monkeypatch):
    request = _c_request()
    rig = _ComposedRig(monkeypatch, [request])
    result = rig.job.run()
    # The store's public API accepts the job's frozenset tenant collection
    # (frozen pending(allowed_tenants, limit) contract); the job passed its
    # frozenset straight through and the row flowed.
    assert rig.store.pending(frozenset({TENANT_A}), 10) == []
    assert len(rig.db.pending_tenant_args) >= 1
    assert set(rig.db.pending_tenant_args[0]) == {TENANT_A}
    assert result.status == 'generated_issuer_dispatch_ok'
    assert result.ticks == 1 and result.processed == 1
    assert result.issued == 1 and result.failed == 0 and result.held == 0
    # The row flowed through the real worker into the authority exactly once,
    # and the durable ledger commit is visible through the store's result API.
    assert [version for version, _ in rig.db.issued] == [VERSION]
    readback = rig.store.result(TENANT_A, VERSION, request.binding_digest())
    assert readback == {'status': 'issued', 'receipt_id': C_RECEIPT}
    key = dispatch_key(TENANT_A, VERSION)
    assert rig.db.ledger[key] == (request.binding_digest(), C_RECEIPT)


def test_composed_issued_rows_excluded_from_pending_do_not_redispatch(monkeypatch):
    version_b = str(uuid.uuid4())
    requests = [_c_request(),
                _c_request(version=version_b,
                           url='https://cdn.example.com/objects/b.png')]
    rig = _ComposedRig(monkeypatch, requests, max_ticks=1)
    first = rig.job.run()
    assert first.issued == 2 and first.failed == 0
    assert len(rig.db.issued) == 2
    # Second run: both rows are issued, so the SQL exclusion means pending is
    # empty and nothing re-dispatches — the authority sees no new issue call.
    second = rig.job.run()
    assert second.ticks == 0 and second.processed == 0
    assert second.issued == 0 and second.failed == 0
    assert len(rig.db.issued) == 2
    assert len(rig.db.pending_tenant_args) == 2


def test_composed_off_flag_means_zero_store_calls(monkeypatch):
    request = _c_request()
    db = _StubDispatchDB()
    db.issued = []
    store = GeneratedIssuerDispatchStore(lambda: _StubDispatchConn(db))
    store.submit(request)  # producer path only; one recorded SQL call
    reader = HostedObjectReader({TENANT_A: ['https://cdn.example.com/objects/']})
    authority = GeneratedHostedByteAuthority(
        lambda: _StubAuthorityConn(db), tenant_id=TENANT_A, reader=reader)
    worker = HostedByteIssuerWorker(authority=authority, ledger=store)
    calls_after_submit = len(db.sql_calls)
    job = IssuerDispatchJob(store=store, worker=worker,
                            allowed_tenants=(TENANT_A,))
    result = job.run()  # flag fixture keeps AGENT_GENERATED_ISSUER_DISPATCH off
    assert result.status == 'generated_issuer_dispatch_off'
    assert result.processed == 0
    assert len(db.sql_calls) == calls_after_submit
    assert db.pending_tenant_args == []
    assert db.issued == []
