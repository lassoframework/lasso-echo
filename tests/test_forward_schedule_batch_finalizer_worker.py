"""Offline contract for the isolated service-role staged-batch finalizer.

Fake PostgREST only: no network, no database, no attester/owner credentials.
The worker must never read content_calendar (never refreeze old rows, never
infer outcomes from mutable rows) and must resolve ambiguous finalize outcomes
solely through the persisted status RPC receipt."""
import json
import uuid

import pytest

from agent import forward_schedule_batch_finalizer_worker as finalizer
from agent.portal_calendar_store import SupabaseCalendarStore

TENANT = 'pierce'
BATCH = str(uuid.UUID(int=100))
M1, M2 = str(uuid.UUID(int=101)), str(uuid.UUID(int=102))
O1 = str(uuid.UUID(int=103))
R1, R2 = str(uuid.UUID(int=104)), str(uuid.UUID(int=105))
EV1, EV2 = str(uuid.UUID(int=106)), str(uuid.UUID(int=107))
REVISION = 'a' * 32
DIGEST = 'b' * 64
SECRET = 'sk-live-never-log-this'

LOGICAL1, LOGICAL2 = str(uuid.UUID(int=108)), str(uuid.UUID(int=109))
ATT = {row: {role: str(uuid.uuid4()) for role in ('original', 'delivered', 'thumbnail')}
       for row in (M1, M2)}


def member(row_id, position, logical, evidence=EV1):
    return {'batch_id': BATCH, 'position': position, 'calendar_row_id': row_id,
            'logical_post_id': logical, 'post_date': '2026-10-10',
            'gym_id': 'gym-1', 'tenant_id': TENANT,
            'source_media_url': 'https://owned.example/' + row_id + '/source',
            'image_url': 'https://owned.example/' + row_id + '/image',
            'thumbnail_url': None, 'observation_digest': None,
            'staged_snapshot': {}}


MEMBERS = [member(M1, 0, LOGICAL1), member(M2, 1, LOGICAL2)]
OLD_SNAPSHOT = {'id': O1, 'gym_id': 'gym-1', 'status': 'pending',
                'variant_status': 'active', 'caption': None}
OLD_ROWS = [{'batch_id': BATCH, 'position': 0, 'calendar_row_id': O1,
             'tenant_id': TENANT, 'old_snapshot': OLD_SNAPSHOT}]


def status_payload(state='staged', receipt=None):
    return {'batch_id': BATCH, 'tenant_id': TENANT, 'request_digest': DIGEST,
            'state': state, 'member_row_ids': [M1, M2], 'observation_row_ids': [],
            'old_row_ids': [O1], 'finalize_receipt': receipt}


RECEIPT = {'batch_id': BATCH, 'state': 'finalized', 'row_ids': [M1, M2],
           'reservation_ids': [R1, R2], 'archived_old_row_ids': [O1]}


class Response:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, *, status=None, eligible=None, snapshot=None,
                 lineage=None, attestations=None, members=None, old_rows=None,
                 finalize_payload=RECEIPT, finalize_error=None, finalize_status=200):
        self.status = status if status is not None else status_payload()
        self.eligible = eligible or {'eligible': True, 'mode': 'staged',
                                     'tenant_id': TENANT, 'batch_id': BATCH,
                                     'reason': None}
        self.snapshot = snapshot or {'revision': REVISION, 'tenant_id': TENANT}
        self.lineage = lineage if lineage is not None else [
            {'evidence_id': EV1, 'tenant_id': TENANT}]
        self.attestations = (attestations if attestations is not None
                             else self._default_attestations())
        self.members = MEMBERS if members is None else members
        self.old_rows = OLD_ROWS if old_rows is None else old_rows
        self.finalize_payload = finalize_payload
        self.finalize_error = finalize_error
        self.finalize_status = finalize_status
        self.posts = []
        self.gets = []

    def _default_attestations(self):
        rows = []
        for m, ev in ((MEMBERS[0], EV1), (MEMBERS[1], EV2)):
            for role in ('original', 'delivered', 'thumbnail'):
                url = (m['source_media_url'] if role == 'original' else m['image_url'])
                rows.append({'attestation_id': ATT[m['calendar_row_id']][role],
                             'role': role, 'media_url': url,
                             'lineage_receipt_id': ev})
        return rows

    def post(self, url, headers=None, json=None, timeout=None):
        fn = url.split('rpc/')[-1]
        self.posts.append((fn, json))
        if fn == 'forward_schedule_batch_status_20261008':
            return Response(self.status)
        if fn == 'forward_schedule_preparation_eligible_20261008':
            return Response(self.eligible)
        if fn == 'fixer_forward_media_attestation_request_20261006':
            return Response(self.snapshot)
        if fn == 'finalize_forward_schedule_staged_batch_20261008':
            if self.finalize_error is not None:
                raise self.finalize_error
            return Response(self.finalize_payload, self.finalize_status)
        raise AssertionError('unexpected rpc: ' + fn)

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit('/', 1)[-1]
        self.gets.append((table, params))
        if table == 'forward_schedule_stage_batch_20261008':
            return Response([{'batch_id': BATCH}])
        if table == 'forward_schedule_stage_member_20261008':
            return Response(list(self.members))
        if table == 'forward_schedule_stage_old_row_20261008':
            return Response(list(self.old_rows))
        if table == 'fixer_forward_media_lineage_20261006':
            row = (params or {}).get('calendar_row_id', '')
            ev = EV1 if row.endswith(M1) else EV2
            return Response([{'evidence_id': ev, 'tenant_id': TENANT}]
                            if self.lineage else [])
        if table == 'forward_media_visual_attestation':
            lineage_filter = (params or {}).get('lineage_receipt_id', '')
            rows = [r for r in self.attestations
                    if 'eq.' + r['lineage_receipt_id'] == lineage_filter]
            return Response(rows)
        if table == 'content_calendar':
            raise AssertionError('the finalizer must never read mutable calendar rows')
        raise AssertionError('unexpected table read: ' + table)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name in finalizer._FORBIDDEN_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(finalizer.WORKER_ENV, 'true')
    monkeypatch.setenv('AGENT_FORWARD_SCHEDULE_RESERVATION', 'true')
    monkeypatch.setenv(finalizer.TENANTS_ENV, TENANT)


def run(fake, **kwargs):
    store = SupabaseCalendarStore(url='http://store.example', service_key='k', http=fake)
    return finalizer.run_once(store=store, **kwargs)


def test_disabled_never_touches_the_store(monkeypatch):
    monkeypatch.setenv(finalizer.WORKER_ENV, 'false')
    fake = FakeHttp()
    assert run(fake) == {'status': 'disabled', 'batches': []}
    assert not fake.posts and not fake.gets


def test_ambiguous_reservation_flag_never_runs(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_SCHEDULE_RESERVATION', 'maybe')
    fake = FakeHttp()
    assert run(fake)['status'] == 'disabled'
    assert not fake.posts


def test_isolated_lane_credentials_hold_before_any_call(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_DSN', SECRET)
    result = run(FakeHttp())
    assert result['status'] == 'hold'
    assert result['reason'] == 'isolated_lane_credentials_present'
    assert SECRET not in json.dumps(result)


def test_happy_path_finalizes_once_with_persisted_membership_and_old_snapshots():
    fake = FakeHttp()
    result = run(fake)
    batch = result['batches'][0]
    assert batch['status'] == 'finalized' and batch['resolved'] == 'finalize_receipt'
    finalize_calls = [args for fn, args in fake.posts if fn.startswith('finalize')]
    assert len(finalize_calls) == 1
    args = finalize_calls[0]
    assert args['p_tenant_id'] == TENANT and args['p_batch_id'] == BATCH
    assert [c['calendar_row_id'] for c in args['p_candidates']] == [M1, M2]
    assert [c['logical_post_id'] for c in args['p_candidates']] == [LOGICAL1, LOGICAL2]
    for candidate in candidate_list(args):
        assert len(set(candidate['attestation_ids'])) == 3
        assert candidate['expected_revision'] == REVISION
    assert args['p_expected_old_rows'] == [OLD_SNAPSHOT]
    assert all(table != 'content_calendar' for table, _ in fake.gets)


def candidate_list(args):
    return args['p_candidates']


def test_already_finalized_batch_is_readback_only():
    fake = FakeHttp(status=status_payload('finalized', RECEIPT))
    result = run(fake)
    assert result['batches'][0]['status'] == 'already_finalized'
    assert not any(fn.startswith('finalize') for fn, _ in fake.posts)


def test_ambiguous_finalize_resolves_solely_through_status_receipt():
    # Lost finalize response; the persisted readback carries the terminal receipt.
    calls = {'finalize': 0}
    fake = FakeHttp(finalize_error=OSError(SECRET))
    original_status = fake.status
    def post(url, headers=None, json=None, timeout=None):
        if url.endswith('finalize_forward_schedule_staged_batch_20261008'):
            calls['finalize'] += 1
            fake.status = status_payload('finalized', RECEIPT)
        return FakeHttp.post(fake, url, headers=headers, json=json, timeout=timeout)
    fake.post = post
    result = run(fake)
    batch = result['batches'][0]
    assert calls['finalize'] == 1
    assert batch['status'] == 'finalized' and batch['resolved'] == 'status_readback'
    assert SECRET not in json.dumps(result)


def test_ambiguous_finalize_with_staged_readback_holds_unknown():
    fake = FakeHttp(finalize_error=OSError('lost'))
    result = run(fake)
    batch = result['batches'][0]
    assert batch['status'] == 'hold' and batch['reason'] == 'finalize_outcome_unknown'
    assert result['status'] == 'partial_hold'


def test_malformed_finalize_response_resolves_through_readback_receipt():
    fake = FakeHttp(finalize_payload={'row_ids': [M1]})
    fake_status_after = status_payload('finalized', RECEIPT)
    def post(url, headers=None, json=None, timeout=None):
        if url.endswith('finalize_forward_schedule_staged_batch_20261008'):
            fake.status = fake_status_after
        return FakeHttp.post(fake, url, headers=headers, json=json, timeout=timeout)
    fake.post = post
    result = run(fake)
    assert result['batches'][0]['status'] == 'finalized'
    assert result['batches'][0]['resolved'] == 'status_readback'


@pytest.mark.parametrize('code,reason', [('23514', 'finalize_held'),
                                         ('23505', 'finalize_held'),
                                         ('22023', 'finalize_arguments_invalid'),
                                         ('55000', 'finalize_gate_off')])
def test_definite_sql_refusal_holds_without_readback_success(code, reason):
    fake = FakeHttp(finalize_payload={'code': code, 'message': 'refused'},
                    finalize_status=400)
    result = run(fake)
    batch = result['batches'][0]
    assert batch['status'] == 'hold' and batch['reason'] == reason
    # A definite refusal never resolves as finalized from the same call.
    assert 'resolved' not in batch


def test_ineligible_member_holds_and_never_finalizes():
    fake = FakeHttp(eligible={'eligible': False, 'mode': None, 'tenant_id': TENANT,
                              'batch_id': None, 'reason': 'content/media binding changed'})
    result = run(fake)
    assert result['batches'][0]['reason'] == 'member_not_preparable'
    assert not any(fn.startswith('finalize') for fn, _ in fake.posts)


def test_missing_visual_role_holds_and_never_finalizes():
    rows = FakeHttp()._default_attestations()
    fake = FakeHttp(attestations=[r for r in rows if not (
        r['lineage_receipt_id'] == EV1 and r['role'] == 'thumbnail')])
    result = run(fake)
    assert result['batches'][0]['reason'] == 'member_visual_proof_incomplete'
    assert not any(fn.startswith('finalize') for fn, _ in fake.posts)


def test_missing_lineage_holds_and_never_finalizes():
    fake = FakeHttp(lineage=[])
    result = run(fake)
    assert result['batches'][0]['reason'] == 'member_lineage_unavailable'


def test_tenant_outside_allowlist_never_finalizes():
    result = run(FakeHttp(), settings=finalizer.Settings(('other',)))
    # settings outside the configured allowlist are rejected before any call
    assert result['reason'] == 'tenant_outside_configured_allowlist'


def test_batch_tenant_outside_allowlist_holds():
    fake = FakeHttp(status=dict(status_payload(), tenant_id='other'))
    result = run(fake)
    assert result['batches'][0]['reason'] == 'tenant_outside_configured_allowlist'
    assert not any(fn.startswith('finalize') for fn, _ in fake.posts)


def test_membership_readback_mismatch_is_unknown_never_finalized():
    fake = FakeHttp(members=[MEMBERS[0]])
    result = run(fake)
    assert result['batches'][0]['reason'] == 'batch_outcome_unknown'
    assert not any(fn.startswith('finalize') for fn, _ in fake.posts)


def test_old_row_readback_mismatch_is_unknown_never_finalized():
    fake = FakeHttp(old_rows=[])
    result = run(fake)
    assert result['batches'][0]['reason'] == 'batch_outcome_unknown'
    assert not any(fn.startswith('finalize') for fn, _ in fake.posts)


def test_explicit_batch_allowlist_skips_discovery(monkeypatch):
    monkeypatch.setenv(finalizer.BATCHES_ENV, BATCH)
    fake = FakeHttp()
    result = run(fake)
    assert result['batches'][0]['status'] == 'finalized'
    assert not any(table == 'forward_schedule_stage_batch_20261008'
                   for table, _ in fake.gets)


def test_run_forever_disabled_logs_and_returns(monkeypatch, capsys=None):
    monkeypatch.setenv(finalizer.WORKER_ENV, 'false')
    logs = []
    finalizer.run_forever(logger=logs.append)
    assert json.loads(logs[0])['status'] == 'disabled'
