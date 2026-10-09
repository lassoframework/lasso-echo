"""Tests for the UNWIRED issuer dispatch runtime bootstrap (Child B).

Everything is offline and synthetic: stub DB connections, an in-memory fake
issuer store, and a synthetic hosted-object reader. No credential, DSN,
network, real store or production configuration exists or is contacted.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent import generated_hosted_byte_authority as auth_mod
from agent import generated_hosted_byte_issuer_worker as worker_mod
from agent import generated_issuer_dispatch_job as job_mod
from agent import generated_issuer_dispatch_runtime as runtime_mod
from agent.generated_issuer_dispatch_runtime import (
    IssuerDispatchRuntime,
    IssuerDispatchRuntimeHold,
    RestrictedIssuerStoreView,
    TrustedTenantNamespace,
)

TENANT = 'gym-a'
OTHER_TENANT = 'gym-b'
VERSION = str(uuid.uuid4())
URL = 'https://cdn.example/gym-a/generated/a.png'
DATA = b'SYNTHETIC exact image bytes'
SHA = hashlib.sha256(DATA).hexdigest()
MANIFEST_BYTES = json.dumps(
    dict(gym_id=TENANT, artifact_version_id=VERSION, hosted_url=URL,
         delivered_sha256=SHA, engine='synthetic-test-only'),
    separators=(',', ':')).encode()
RECEIPT = dict(receipt_id=str(uuid.uuid4()), gym_id=TENANT,
               artifact_version_id=VERSION, hosted_url=URL,
               delivered_sha256=SHA,
               render_manifest=dict(gym_id=TENANT, artifact_version_id=VERSION,
                                    hosted_url=URL, delivered_sha256=SHA,
                                    engine='synthetic-test-only'))


class SyntheticReader(auth_mod.HostedObjectReader):
    def __init__(self, events):
        super().__init__({TENANT: ['https://cdn.example/gym-a/generated/']})
        self.events = events

    def read(self, tenant, url):
        self.events.append('GET')
        return DATA


class Connection:
    def __init__(self, events):
        self.events = events
        self.autocommit = False
        self.info = SimpleNamespace(transaction_status=0)

    def execute(self, sql, args):
        if 'authorized' in sql:
            self.events.append('authorize')
            value = True
        else:
            self.events.append('reconcile' if 'reconcile' in sql
                               else 'lookup' if 'lookup' in sql else 'issue')
            value = RECEIPT
        return SimpleNamespace(fetchone=lambda: (value,))

    def rollback(self):
        self.events.append('rollback')

    def commit(self):
        self.events.append('commit')

    def close(self):
        self.events.append('close')


@dataclass(frozen=True)
class FakeRow:
    """Immutable pending row; tenant is SQL-established provenance."""
    tenant: str
    artifact_version_id: str
    hosted_url: str
    expected_sha256: str
    manifest_bytes: bytes


class FakeIssuerStore:
    """Issuer-side fake: pending + DispatchLedger claim/commit.

    Also carries producer-side submit/result so tests prove the runtime view
    can never reach them.
    """

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.ledger = {}
        self.pending_calls = []
        self.producer_calls = []

    def pending(self, allowed_tenants, limit):
        self.pending_calls.append((frozenset(allowed_tenants), limit))
        return list(self.rows)[:limit]

    def claim(self, key, binding):
        row = self.ledger.get(key)
        if row is None:
            self.ledger[key] = dict(binding=binding, receipt=None)
            return worker_mod.LedgerClaim(is_new=True, binding_digest=binding)
        return worker_mod.LedgerClaim(is_new=False, binding_digest=row['binding'],
                                      receipt_id=row['receipt'])

    def commit(self, key, binding, receipt_id):
        row = self.ledger[key]
        assert row['binding'] == binding and row['receipt'] in (None, receipt_id)
        row['receipt'] = receipt_id
        return True

    def submit(self, request):
        self.producer_calls.append(('submit', request))

    def result(self, tenant, version, binding):
        self.producer_calls.append(('result', tenant))


def _row(tenant=TENANT):
    return FakeRow(tenant=tenant, artifact_version_id=VERSION, hosted_url=URL,
                   expected_sha256=SHA, manifest_bytes=MANIFEST_BYTES)


def _authority(events):
    return auth_mod.GeneratedHostedByteAuthority(
        lambda: Connection(events), tenant_id=TENANT,
        reader=SyntheticReader(events))


def _runtime(events, store, *, namespace=(TENANT,), **kw):
    return IssuerDispatchRuntime(
        store=store, authority=_authority(events),
        namespace=TrustedTenantNamespace(namespace), **kw)


@pytest.fixture(autouse=True)
def all_flags_on(monkeypatch):
    monkeypatch.setenv(runtime_mod.FLAG, '1')
    monkeypatch.setenv(job_mod.FLAG, '1')
    monkeypatch.setenv(worker_mod.FLAG, '1')


def hold_code(excinfo):
    assert isinstance(excinfo.value, IssuerDispatchRuntimeHold)
    return str(excinfo.value)


# OFF behaviour -------------------------------------------------------------

def test_off_returns_static_tally_with_zero_calls(monkeypatch):
    monkeypatch.delenv(runtime_mod.FLAG)
    events, store = [], FakeIssuerStore(rows=[_row()])
    result = _runtime(events, store).run_once()
    assert result == dict(status='generated_issuer_dispatch_off', ticks=0,
                          processed=0, issued=0, replayed=0, reconciled=0,
                          held=0, failed=0)
    assert events == [] and store.pending_calls == [] and store.ledger == {}


def test_runtime_off_overrides_armed_inner_flags(monkeypatch):
    # Job and worker flags stay armed; the runtime flag alone gates.
    monkeypatch.delenv(runtime_mod.FLAG)
    events, store = [], FakeIssuerStore(rows=[_row()])
    assert _runtime(events, store).run_once()['status'] == \
        'generated_issuer_dispatch_off'
    assert events == [] and store.pending_calls == []


@pytest.mark.parametrize('flag', [job_mod.FLAG, worker_mod.FLAG])
def test_inner_off_prevents_even_a_queue_read(monkeypatch, flag):
    monkeypatch.delenv(flag)
    events, store = [], FakeIssuerStore(rows=[_row()])
    assert _runtime(events, store).run_once() == job_mod.off_result().as_dict()
    assert events == [] and store.pending_calls == [] and store.ledger == {}


# Assembly validation -------------------------------------------------------

def test_string_store_rejected():
    with pytest.raises(IssuerDispatchRuntimeHold) as e:
        IssuerDispatchRuntime(store='postgresql://user:secret@host/db',
                              authority=_authority([]),
                              namespace=TrustedTenantNamespace((TENANT,)))
    assert hold_code(e) == 'generated_runtime_store_invalid'


def test_store_missing_issuer_surface_rejected():
    class ProducerOnly:
        def pending(self, allowed_tenants, limit):
            return []

    with pytest.raises(IssuerDispatchRuntimeHold) as e:
        IssuerDispatchRuntime(store=ProducerOnly(), authority=_authority([]),
                              namespace=TrustedTenantNamespace((TENANT,)))
    assert hold_code(e) == 'generated_runtime_store_invalid'


def test_dsn_string_authority_rejected():
    with pytest.raises(IssuerDispatchRuntimeHold) as e:
        IssuerDispatchRuntime(store=FakeIssuerStore(),
                              authority='postgresql://user:secret@host/db',
                              namespace=TrustedTenantNamespace((TENANT,)))
    assert hold_code(e) == 'generated_runtime_authority_invalid'


def test_wrong_authority_type_rejected():
    with pytest.raises(IssuerDispatchRuntimeHold) as e:
        IssuerDispatchRuntime(store=FakeIssuerStore(), authority=object(),
                              namespace=TrustedTenantNamespace((TENANT,)))
    assert hold_code(e) == 'generated_runtime_authority_invalid'


def test_namespace_must_cover_authority_tenant():
    with pytest.raises(IssuerDispatchRuntimeHold) as e:
        _runtime([], FakeIssuerStore(), namespace=(OTHER_TENANT,))
    assert hold_code(e) == 'generated_runtime_namespace_invalid'


def test_multi_tenant_namespace_polls_only_own_authority_tenant():
    events, store = [], FakeIssuerStore(rows=[_row()])
    result = _runtime(events, store, namespace=(OTHER_TENANT, TENANT)).run_once()
    assert result['issued'] == 1
    assert store.pending_calls == [(frozenset({TENANT}), 25)]


def test_namespace_rejects_string_and_empty():
    with pytest.raises(IssuerDispatchRuntimeHold):
        TrustedTenantNamespace(TENANT)
    with pytest.raises(IssuerDispatchRuntimeHold):
        TrustedTenantNamespace(())
    with pytest.raises(IssuerDispatchRuntimeHold):
        TrustedTenantNamespace((123,))


def test_unexpected_keyword_rejected():
    with pytest.raises(TypeError):
        IssuerDispatchRuntime(store=FakeIssuerStore(), authority=_authority([]),
                              namespace=TrustedTenantNamespace((TENANT,)),
                              dsn='postgresql://user:secret@host/db')


def test_view_hides_producer_surface():
    store = FakeIssuerStore()
    view = RestrictedIssuerStoreView(store)
    assert isinstance(view, worker_mod.DispatchLedger)
    assert callable(view.pending) and callable(view.claim) and callable(view.commit)
    assert not hasattr(view, 'submit') and not hasattr(view, 'result')


def test_readerless_authority_fails_closed_at_assembly():
    authority = auth_mod.GeneratedHostedByteAuthority(
        lambda: Connection([]), tenant_id=TENANT)
    with pytest.raises(IssuerDispatchRuntimeHold):
        IssuerDispatchRuntime(store=FakeIssuerStore(), authority=authority,
                              namespace=TrustedTenantNamespace((TENANT,)))


# Composed bounded run ------------------------------------------------------

def test_armed_run_issues_once_and_terminates():
    events, store = [], FakeIssuerStore(rows=[_row()])
    result = _runtime(events, store).run_once()
    assert result['status'] == 'generated_issuer_dispatch_ok'
    assert result['issued'] == 1 and result['processed'] == 1
    assert result['failed'] == 0 and result['held'] == 0
    # Producer surface never touched; exactly one authority issue.
    assert store.producer_calls == []
    assert events.count('issue') == 1
    # Bounded: the default single tick drains the batch and stops.
    assert result['ticks'] == 1
    assert len(store.pending_calls) == 1  # default single bounded tick


def test_second_run_replays_without_second_issue():
    events, store = [], FakeIssuerStore(rows=[_row()])
    runtime = _runtime(events, store)
    first = runtime.run_once()
    store.rows = [_row()]  # row still visible to pending on a later sweep
    second = runtime.run_once()
    assert first['issued'] == 1
    assert second['issued'] == 0 and second['replayed'] == 1
    assert events.count('issue') == 1


def test_cross_tenant_row_is_held_never_dispatched():
    events = []
    store = FakeIssuerStore(rows=[_row(tenant=OTHER_TENANT)])
    result = _runtime(events, store).run_once()
    assert result['held'] == 1 and result['issued'] == 0
    assert events == []


def test_bounded_limits_enforced():
    events = []
    store = FakeIssuerStore(rows=[_row() for _ in range(5)])
    result = _runtime(events, store, batch_limit=2, max_ticks=1).run_once()
    assert result['ticks'] == 1 and result['processed'] == 2
    with pytest.raises(IssuerDispatchRuntimeHold) as e:
        _runtime([], FakeIssuerStore(), max_ticks=11)
    assert hold_code(e) == 'generated_issuer_dispatch_ticks_invalid'


def test_pending_failure_is_counted_not_raised():
    class FailingStore(FakeIssuerStore):
        def pending(self, allowed_tenants, limit):
            raise RuntimeError('sensitive DSN=postgres://secret')

    result = _runtime([], FailingStore()).run_once()
    assert result['status'] == 'generated_issuer_dispatch_ok'
    assert result['failed'] == 1 and result['issued'] == 0


def test_no_secret_or_exception_leak_in_holds():
    with pytest.raises(IssuerDispatchRuntimeHold) as e:
        IssuerDispatchRuntime(store='postgresql://user:secret@host/db',
                              authority=_authority([]),
                              namespace=TrustedTenantNamespace((TENANT,)))
    text = str(e.value)
    for forbidden in ('secret', 'postgres', 'DSN', 'Traceback'):
        assert forbidden not in text
