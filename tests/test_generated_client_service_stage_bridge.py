"""Actual store transport plus durable journals; no production authority claims."""
import copy
import hashlib
import json
import uuid

import pytest

from agent import generated_client_service_stage_bridge as bridge
from agent import gbp_drive_use_journal as staged_journal
from agent.generated_client_admission import ClientAdmissionJournal
from agent.generated_infographic_preparation import SQLiteGenerationJobs
from agent.portal_calendar_store import SupabaseCalendarStore, _canonical_json, forward_batch_identity


def uid():
    return str(uuid.uuid4())


class Response:
    status_code = 200
    def __init__(self, value):
        self.value = value
    def json(self):
        return copy.deepcopy(self.value)


class Transport:
    def __init__(self, preparation):
        self.preparation = preparation
        self.calls = []
        self.status = None
        self.lost_ack = False
        self.status_patch = {}
        self.preparation_error = False
        self.stage_error = False
        self.alias = []

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return Response(self.alias)

    def post(self, url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        args = kwargs['json']
        self.calls.append((name, copy.deepcopy(args)))
        if name == bridge.PREPARATION_RPC:
            if self.preparation_error:
                raise RuntimeError('secret transport detail')
            return Response(self.preparation)
        if name == 'stage_forward_schedule_batch_20261008':
            if self.stage_error:
                raise RuntimeError('secret stage error')
            request = json.loads(args['p_request'])
            assert hashlib.sha256(args['p_request'].encode()).hexdigest() == args['p_request_digest']
            assert args['p_batch_id'] == forward_batch_identity(args['p_tenant_id'], args['p_request_digest'])
            self.status = dict(batch_id=args['p_batch_id'], tenant_id=args['p_tenant_id'],
                               request_digest=args['p_request_digest'], state='staged',
                               member_row_ids=[m['row']['id'] for m in request['members']],
                               old_row_ids=[r['id'] for r in request['old_rows']],
                               observation_row_ids=[], finalize_receipt=None)
            if self.lost_ack:
                raise RuntimeError('secret lost ACK')
            return Response(self.status)
        if name == 'forward_schedule_batch_status_20261008':
            return Response(None if self.status is None else {**self.status, **self.status_patch})
        raise AssertionError('unexpected authority: ' + name)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv(bridge.FLAG, 'true')
    monkeypatch.setenv('AGENT_GBP_STAGED_JOURNAL', 'true')
    monkeypatch.setattr(staged_journal, '_canonical_db_path', lambda: tmp_path / 'stage.sqlite')
    journal = ClientAdmissionJournal(SQLiteGenerationJobs(tmp_path / 'generation.sqlite'))
    placeholder, row, version, receipt = uid(), uid(), uid(), uid()
    planned = dict(id=row, gym_id='gym', logical_post_id=uid(), status='pending',
                   variant_status='candidate', media_not_ready_reason=None, caption='Frozen caption')
    old = dict(id=placeholder, gym_id='gym', status='pending', caption='Placeholder', optional=None)
    plan = dict(placeholder_row_id=placeholder, planned_row=planned, old_snapshot=old)
    candidate = dict(gym_id='gym', original_url='https://example.test/original.png', original_sha256='a'*64)
    binding = dict(calendar_row_id=row, candidate=candidate, stage_plan=plan,
                   reservation_manifest={}, source_revision='r1')
    raw = _canonical_json(dict(**binding, gym_id='gym', artifact_version_id=version,
                              hosted_url=candidate['original_url'], delivered_sha256=candidate['original_sha256'])).encode()
    with journal._connect() as con:
        con.execute('INSERT INTO generated_client_admissions VALUES(?,?,?,?,?,?)',
                    (placeholder, json.dumps(binding), version, raw, receipt, 'committed'))
    preparation = dict(admitted=True, reserved=True, prepared=True, calendar_row_id=row,
                       artifact_version_id=version, receipt_id=receipt,
                       manifest_sha256=hashlib.sha256(raw).hexdigest(), stage_plan=plan)
    http = Transport(preparation)
    store = SupabaseCalendarStore(url='https://example.test', service_key='test', http=http)
    return bridge.GeneratedClientServiceStageBridge(journal=journal, store=store), placeholder, http


def test_default_off_has_no_service_calls(lane, monkeypatch):
    obj, placeholder, http = lane
    monkeypatch.delenv(bridge.FLAG)
    with pytest.raises(bridge.ServiceStageHold, match='disabled'):
        obj.stage(placeholder)
    assert http.calls == []


@pytest.mark.parametrize('flag', ['', 'false', 'ambiguous'])
def test_requires_existing_durable_stage_gate(lane, monkeypatch, flag):
    obj, placeholder, http = lane
    monkeypatch.setenv('AGENT_GBP_STAGED_JOURNAL', flag)
    with pytest.raises(bridge.ServiceStageHold, match='durable_stage_required'):
        obj.stage(placeholder)
    assert not http.calls


def test_exact_stage_and_restart_replays_same_batch(lane):
    obj, placeholder, http = lane
    first = obj.stage(placeholder)
    assert first['state'] == 'staged' and first['finalize_receipt'] is None
    renewed = bridge.GeneratedClientServiceStageBridge(
        journal=ClientAdmissionJournal(SQLiteGenerationJobs(obj.journal.path)), store=obj.store)
    assert renewed.stage(placeholder) == first
    stages = [args for name, *tail in http.calls if name == 'stage_forward_schedule_batch_20261008' for args in tail]
    assert stages[0] == stages[1]
    request = json.loads(stages[0]['p_request'])
    assert request['old_rows'][0]['optional'] is None
    assert request['members'][0]['row']['status'] == 'pending'
    assert request['members'][0]['observation'] is None
    assert sum(c[0] == 'forward_schedule_batch_status_20261008' for c in http.calls) == 2


def test_lost_ack_only_succeeds_after_exact_status(lane):
    obj, placeholder, http = lane
    http.lost_ack = True
    status = obj.stage(placeholder)
    assert status['state'] == 'staged'
    durable = staged_journal.get_forward_stage(status['batch_id'])
    assert durable['stage_receipt'] == status
    assert sum(c[0] == 'stage_forward_schedule_batch_20261008' for c in http.calls) == 1
    assert http.calls[-1][0] == 'forward_schedule_batch_status_20261008'


@pytest.mark.parametrize('patch', [dict(batch_id=uid()), dict(tenant_id='foreign'),
    dict(request_digest='0'*64), dict(member_row_ids=[uid()]), dict(old_row_ids=[uid()]),
    dict(observation_row_ids=[uid()]), dict(state='finalized'), dict(finalize_receipt={})])
def test_lost_ack_bad_status_is_static_hold(lane, patch):
    obj, placeholder, http = lane
    http.lost_ack = True
    http.status_patch = patch
    with pytest.raises(bridge.ServiceStageHold, match='^generated_client_service_stage_unverified$'):
        obj.stage(placeholder)


@pytest.mark.parametrize('key,value', [('calendar_row_id', uid()), ('artifact_version_id', uid()),
    ('receipt_id', uid()), ('manifest_sha256', '0'*64), ('stage_plan', {}), ('prepared', False)])
def test_forged_preparation_never_stages(lane, key, value):
    obj, placeholder, http = lane
    http.preparation[key] = value
    with pytest.raises(bridge.ServiceStageHold, match='preparation_unverified'):
        obj.stage(placeholder)
    assert len(http.calls) == 1


def test_service_read_failure_is_static_and_no_stage(lane):
    obj, placeholder, http = lane
    http.preparation_error = True
    with pytest.raises(bridge.ServiceStageHold, match='^generated_client_service_preparation_unverified$'):
        obj.stage(placeholder)
    assert len(http.calls) == 1


def test_absent_batch_after_transport_failure_never_succeeds(lane):
    obj, placeholder, http = lane
    http.stage_error = True
    with pytest.raises(bridge.ServiceStageHold, match='stage_unverified'):
        obj.stage(placeholder)
    assert http.calls[-1][0] == 'forward_schedule_batch_status_20261008'


def test_alias_change_stops_before_stage_rpc(lane):
    obj, placeholder, http = lane
    http.alias = [dict(alias_key='gym', tenant_id='foreign')]
    with pytest.raises(bridge.ServiceStageHold, match='stage_unverified'):
        obj.stage(placeholder)
    assert not any(c[0] == 'stage_forward_schedule_batch_20261008' for c in http.calls)


def test_persisted_binding_disagreement_never_calls_service(lane):
    obj, placeholder, http = lane
    with obj.journal._connect() as con:
        binding = obj.journal.load(placeholder)['binding']
        binding['stage_plan']['old_snapshot']['caption'] = 'Changed'
        con.execute('UPDATE generated_client_admissions SET binding=? WHERE row_id=?',
                    (json.dumps(binding), placeholder))
    with pytest.raises(bridge.ServiceStageHold, match='preparation_unverified'):
        obj.stage(placeholder)
    assert not http.calls


def test_status_must_bind_durably_before_success(lane, monkeypatch):
    obj, placeholder, http = lane
    http.lost_ack = True
    def hold(*args, **kwargs):
        raise staged_journal.JournalHold('secret local failure')
    monkeypatch.setattr(staged_journal, 'record_forward_stage_receipt', hold)
    with pytest.raises(bridge.ServiceStageHold, match='^generated_client_service_stage_unverified$'):
        obj.stage(placeholder)
    assert http.status['state'] == 'staged'
