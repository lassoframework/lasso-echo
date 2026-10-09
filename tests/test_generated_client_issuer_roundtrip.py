"""Owner -> authenticated queue -> issuer -> reader round trip (fake queue).

Proves durable exact manifest submit/readback/resume across restart and lost
ACK, the photo-first gate before any queue/issuer work, and fail-closed
behavior on stale, cross-tenant or revoked evidence. The queue is the injected
trusted dependency; one composed test drives the real IssuerDispatchClient.
"""
import uuid

import pytest
from agent import generated_client_admission as client
from agent import generated_hosted_byte_authority as hosted
from agent import generated_infographic_preparation as prep
from agent.generated_hosted_byte_issuer_worker import IssuerDispatchRequest, dispatch_key
from agent.generated_issuer_dispatch_client import IssuerDispatchClient
from test_generated_infographic_preparation import case
from test_generated_canonical_owner import active, derive


@pytest.fixture
def prepared(case, active):
    authority = derive(active)
    case.snapshot.update(copy=authority['copy'], palette=authority['palette'],
        palette_revision=authority['palette_revision'], copy_approved=False, copy_verified=True,
        authority_pins=authority['authority_pins'], copy_derivation_receipt=authority['copy_derivation_receipt'],
        copy_digest=prep.digest(authority['copy']))
    case.request['palette_revision'] = authority['palette_revision']
    result = prep.prepare_candidate(case.request, case.snapshot, jobs=case.jobs,
        provider=case.provider, reviewer=case.reviewer, storage=case.storage, enabled=True)
    assert result['ok'], result
    return case, result['candidate'], authority['source_revision']


class FakeIssuerStore:
    """Durable in-memory store implementing the frozen submit/result API.

    submit is exactly-once per dispatch key: an identical replay returns the
    committed row; a changed binding on the same key is a conflict. issued_at
    simulates the separate issuer worker completing asynchronously.
    """
    def __init__(self):
        self.rows = {}
        self.submit_calls = []
        self.result_calls = []
        self.issued = False
        self.drop_readback = 0

    def submit(self, request):
        if type(request) is not IssuerDispatchRequest:
            raise TypeError('request')
        key = dispatch_key(request.tenant, request.artifact_version_id)
        self.submit_calls.append(request)
        existing = self.rows.get(key)
        if existing is not None:
            if existing['request'] != request:
                raise ValueError('conflict')
            return {'dispatch_key': key, 'status': 'issued' if self.issued else 'submitted',
                    'receipt_id': existing['receipt']}
        receipt = str(uuid.uuid4())
        self.rows[key] = {'request': request, 'receipt': receipt}
        return {'dispatch_key': key, 'status': 'submitted', 'receipt_id': None}

    def result(self, tenant, version, binding):
        self.result_calls.append((tenant, version, binding))
        if self.drop_readback:
            self.drop_readback -= 1
            raise TimeoutError('lost ACK')
        matches = [r for r in self.rows.values()
                   if r['request'].tenant == tenant
                   and r['request'].artifact_version_id == version
                   and r['request'].binding_digest() == binding]
        if not matches:
            return None
        if not self.issued:
            return {'status': 'submitted', 'receipt_id': None}
        return {'status': 'issued', 'receipt_id': matches[0]['receipt']}


def admission_for(prepared, monkeypatch):
    case, candidate, source = prepared
    monkeypatch.setenv(client.FLAG, 'true')
    monkeypatch.setattr('agent.generated_infographic_runtime._require_latest_local_depletion', lambda *args: None)
    authority = hosted.GeneratedHostedByteAuthority(
        lambda: (_ for _ in ()).throw(AssertionError('no DB')), tenant_id=candidate['gym_id'])
    return client.GeneratedClientAdmission(jobs=case.jobs, authority=authority)


def frozen_row(prepared, admission):
    case, candidate, source = prepared
    row = str(uuid.uuid4())
    return row, admission.journal.freeze(row, candidate, {'same': 'object'}, source)


def test_round_trip_submit_readback_resume_across_restart(prepared, monkeypatch):
    admission = admission_for(prepared, monkeypatch)
    row, frozen = frozen_row(prepared, admission)
    store = FakeIssuerStore()
    queue = IssuerDispatchClient(store=store)
    with pytest.raises(client.AdmissionHold, match='receipt_pending'):
        admission.queue_receipt(row, queue=queue, persistence=object())
    store.issued = True
    receipt = admission.queue_receipt(row, queue=queue, persistence=object())
    uuid.UUID(receipt)
    # Exact manifest bytes reached the queue unchanged; submit is idempotent.
    request = store.submit_calls[0]
    assert request.manifest_bytes == frozen['manifest_bytes']
    assert request.tenant == prepared[1]['gym_id']
    assert request.artifact_version_id == frozen['artifact_version_id']
    # Restart: a fresh journal on the same durable path resumes and verifies.
    restored = client.GeneratedClientAdmission(
        jobs=prep.SQLiteGenerationJobs(prepared[0].jobs.path), authority=admission.authority)
    restored.journal = client.ClientAdmissionJournal(prep.SQLiteGenerationJobs(prepared[0].jobs.path))
    again = restored.queue_receipt(row, queue=queue, persistence=object())
    assert again == receipt and len(store.rows) == 1
    assert all(r.manifest_bytes == frozen['manifest_bytes'] for r in store.submit_calls)


def test_lost_ack_resumes_without_new_issue(prepared, monkeypatch):
    admission = admission_for(prepared, monkeypatch)
    row, frozen = frozen_row(prepared, admission)
    store = FakeIssuerStore()
    store.issued = True
    store.drop_readback = 1  # issuer committed; the ACK/readback was lost
    queue = IssuerDispatchClient(store=store)
    with pytest.raises(client.AdmissionHold, match='queue_unavailable'):
        admission.queue_receipt(row, queue=queue, persistence=object())
    assert admission.journal.load(row)['receipt_id'] is None
    # Resume after restart: replay submits the identical manifest, never a
    # second durable dispatch row, and the committed receipt attaches.
    restored = client.ClientAdmissionJournal(prep.SQLiteGenerationJobs(prepared[0].jobs.path))
    admission.journal = restored
    receipt = admission.queue_receipt(row, queue=queue, persistence=object())
    assert receipt == store.rows[dispatch_key(prepared[1]['gym_id'], frozen['artifact_version_id'])]['receipt']
    assert len(store.rows) == 1 and len(store.submit_calls) == 2
    assert restored.load(row)['receipt_id'] == receipt


def test_photo_first_gate_blocks_before_any_queue_call(prepared, monkeypatch):
    admission = admission_for(prepared, monkeypatch)
    row, frozen = frozen_row(prepared, admission)
    store = FakeIssuerStore()
    queue = IssuerDispatchClient(store=store)
    monkeypatch.setattr('agent.generated_infographic_runtime._require_latest_local_depletion', lambda *args: (_ for _ in ()).throw(RuntimeError('probe failed')))
    with pytest.raises(client.AdmissionHold, match='photo_gate_unverified'):
        admission.queue_receipt(row, queue=queue, persistence=object())
    with pytest.raises(client.AdmissionHold, match='photo_gate_unverified'):
        admission.queue_receipt(row, queue=queue, persistence=None)
    assert store.submit_calls == [] and store.result_calls == []


def test_cross_tenant_evidence_fails_closed(prepared, monkeypatch):
    case, candidate, source = prepared
    monkeypatch.setenv(client.FLAG, 'true')
    other = hosted.GeneratedHostedByteAuthority(
        lambda: (_ for _ in ()).throw(AssertionError('no DB')), tenant_id='other-tenant')
    admission = client.GeneratedClientAdmission(jobs=case.jobs, authority=other)
    row = str(uuid.uuid4())
    admission.journal.freeze(row, candidate, {}, source)
    store = FakeIssuerStore()
    with pytest.raises(client.AdmissionHold, match='tenant_mismatch'):
        admission.queue_receipt(row, queue=IssuerDispatchClient(store=store),
                                persistence=object())
    assert store.submit_calls == []


def test_stale_or_revoked_receipt_fails_closed(prepared, monkeypatch):
    admission = admission_for(prepared, monkeypatch)
    row, frozen = frozen_row(prepared, admission)
    store = FakeIssuerStore()
    store.issued = True
    queue = IssuerDispatchClient(store=store)
    receipt = admission.queue_receipt(row, queue=queue, persistence=object())
    # Revoked: the queue no longer reports the row as issued.
    store.issued = False
    with pytest.raises(client.AdmissionHold, match='receipt_stale'):
        admission.queue_receipt(row, queue=queue, persistence=object())
    # Stale: a different receipt UUID for the same binding never replaces.
    store.issued = True
    key = dispatch_key(prepared[1]['gym_id'], frozen['artifact_version_id'])
    store.rows[key]['receipt'] = str(uuid.uuid4())
    with pytest.raises(client.AdmissionHold, match='receipt_stale'):
        admission.queue_receipt(row, queue=queue, persistence=object())
    assert admission.journal.load(row)['receipt_id'] == receipt


def test_flag_off_and_protocol_enforced(prepared, monkeypatch):
    case, candidate, source = prepared
    monkeypatch.delenv(client.FLAG, raising=False)
    admission = admission_for(prepared, monkeypatch)
    monkeypatch.delenv(client.FLAG, raising=False)
    row = str(uuid.uuid4())
    admission.journal.freeze(row, candidate, {}, source)
    with pytest.raises(client.AdmissionHold, match='disabled'):
        admission.queue_receipt(row, queue=IssuerDispatchClient(store=FakeIssuerStore()),
                                persistence=object())
    monkeypatch.setenv(client.FLAG, 'true')
    with pytest.raises(client.AdmissionHold, match='queue_protocol_required'):
        admission.queue_receipt(row, queue=object(), persistence=object())

# Compose the real row runtime, durable candidate journal and producer client.
# The owner SQL seam is offline; its pending plan/manifest freeze is modeled.
from test_generated_infographic_runtime import system
from agent import generated_infographic_runtime as runtime, forward_media_guard as guard


def composed(system, monkeypatch):
    s = system
    monkeypatch.setenv(client.FLAG, 'true')
    authority = hosted.GeneratedHostedByteAuthority(lambda: None, tenant_id='same-gym')
    admission = client.GeneratedClientAdmission(jobs=s.case.jobs, authority=authority)
    store = FakeIssuerStore()
    queue = IssuerDispatchClient(store=store)
    def reserve(persistence, row, candidate, current, **kwargs):
        assert persistence._conn.events[-1] == 'rollback'
        admission.journal.freeze(row, candidate, {'owner_manifest': 'frozen'}, current['approved_source_revision'])
        frozen = admission.journal.load(row)
        if not frozen['receipt_id']:
            raise client.AdmissionHold('generated_client_receipt_pending')
        return dict(reserved=True, admitted=True, prepared=True, calendar_row_id=row,
                    receipt_ref=frozen['receipt_id'])
    monkeypatch.setattr(guard, 'reserve_generated', reserve)
    def run():
        return runtime.run_calendar_row('same-gym', s.account, s.row_id,
            persistence=s.persistence, loader=s.loader, jobs=s.case.jobs,
            provider=s.case.provider, reviewer=s.case.reviewer, storage=s.case.storage,
            client_admission=admission, issuer_dispatch=queue)
    return run, admission, store


@pytest.mark.parametrize('lost_ack', [False, True])
def test_actual_owner_row_queue_pending_restart_reuses_candidate(system, monkeypatch, lost_ack):
    run, admission, store = composed(system, monkeypatch)
    store.issued = lost_ack
    store.drop_readback = int(lost_ack)
    first = run()
    assert first['held'] and len(store.rows) == 1
    frozen = admission.journal.load(system.row_id)
    admission.journal = client.ClientAdmissionJournal(prep.SQLiteGenerationJobs(system.case.jobs.path))
    store.issued = True
    second = run()
    assert second['ok'] and second['admitted'] and second['prepared']
    assert system.case.provider.calls == 1
    assert all(r.manifest_bytes == frozen['manifest_bytes'] for r in store.submit_calls)
    store.issued = False
    third = run()
    assert third['reason'] == 'generated_client_receipt_stale'
    assert system.case.provider.calls == 1


@pytest.mark.parametrize('change', [dict(local_available=1), dict(enabled=False),
                                  dict(observed_at='2000-01-01T00:00:00Z')])
def test_actual_owner_photo_gate_never_generates_or_queues(system, monkeypatch, change):
    run, admission, store = composed(system, monkeypatch)
    system.census.update(change)
    assert run()['held']
    assert system.case.provider.calls == 0
    assert not store.submit_calls and not store.result_calls


def test_actual_owner_cross_tenant_holds_before_provider_and_queue(system, monkeypatch):
    run, admission, store = composed(system, monkeypatch)
    admission.authority = hosted.GeneratedHostedByteAuthority(lambda: None, tenant_id='other-gym')
    assert run()['reason'] == 'generated_client_tenant_mismatch'
    assert system.case.provider.calls == 0 and not store.submit_calls
