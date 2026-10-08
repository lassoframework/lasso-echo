"""Offline publisher queue -> isolated owner bind -> fresh row runner proof."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from agent import generated_infographic_runtime as runtime
from agent import generated_infographic_gap_owner as gap
from agent import client_infographic_fill as fill
from test_generated_infographic_runtime import system, run, Conn
from test_generated_infographic_preparation import case
from test_generated_canonical_owner import active, derive

NOW = '2026-10-07T12:00:00+00:00'

class Store:
    def __init__(self, rows=()): self.rows, self.calls = list(rows), []
    def list_month(self,*a): return self.rows
    def _client(self): return self
    def _rest(self,path): return path
    def _headers(self,extra): return extra
    def post(self,url,**kw):
        self.calls.append((url,kw))
        p=kw['json']
        result=dict(dispatched=True,request_id=p['p_request_id'],gym_id=p['p_gym'],
                    local_date=p['p_local_date'],account=p['p_account'],format=p['p_format'])
        return SimpleNamespace(status_code=200,json=lambda:result)
    def insert_rows(self,*a): pytest.fail('publisher must not create calendar rows')

@pytest.fixture
def scheduler(monkeypatch):
    monkeypatch.setenv(runtime.FLAG,'true')
    monkeypatch.setattr(fill,'real_media_status',lambda *a,**k:(fill.MEDIA_DEPLETED,{}))
    monkeypatch.setattr('agent.accounts.get_account',lambda key: None)
    return SimpleNamespace(key='same-gym_ig',platform='instagram')


def test_queue_payload_has_no_owner_authority_and_is_idempotent(scheduler,monkeypatch):
    monkeypatch.setattr('agent.accounts.get_account', lambda key:
        SimpleNamespace(key=key,platform='facebook_page'))
    store=Store()
    first=runtime.run_scheduled('same-gym',scheduler,store,now=NOW)
    again=runtime.run_scheduled('same-gym',scheduler,store,now=NOW)
    assert first['ok'] and first['dispatched']==4 and first['request_ids']==again['request_ids']
    assert all(set(kw['json'])=={'p_request_id','p_gym','p_local_date','p_account','p_format'}
               for _,kw in store.calls)
    assert all(url=='rpc/fixer_generated_gap_dispatch_20261007' for url,_ in store.calls)

@pytest.mark.parametrize('state,reason',[(fill.MEDIA_AVAILABLE,'generated_photo_available'),
                                        ('uncertain','generated_photo_inventory_uncertain')])
def test_photo_first_dispatch(scheduler,monkeypatch,state,reason):
    monkeypatch.setattr(fill,'real_media_status',lambda *a,**k:(state,{}))
    store=Store()
    assert runtime.run_scheduled('same-gym',scheduler,store,now=NOW)['reason']==reason
    assert not store.calls


def test_unreadable_or_occupied_calendar_never_dispatches(scheduler):
    store=Store([dict(account='instagram',format='feed',post_date='2026-10-08',status='on_hold'),
                 dict(account='instagram',format='feed',post_date='2026-10-09',status='approved')])
    assert runtime.run_scheduled('same-gym',scheduler,store,now=NOW)['dispatched']==0
    store.list_month=lambda *a: (_ for _ in ()).throw(OSError())
    assert runtime.run_scheduled('same-gym',scheduler,store,now=NOW)['dispatched']==0
    assert not store.calls


def test_dispatch_wrong_gym_account_holds(scheduler):
    store=Store()
    scheduler.key='other-gym_ig'
    assert runtime.run_scheduled('same-gym',scheduler,store,now=NOW)['reason']=='generated_account_gym_mismatch'
    assert not store.calls


def test_off_does_not_read_or_dispatch(monkeypatch):
    monkeypatch.delenv(runtime.FLAG,raising=False)
    assert runtime.run_scheduled('gym',None,None)['reason']=='generated_runtime_disabled'
    assert gap.run_pending(persistence=None)['reason']=='generated_runtime_disabled'

class Transport:
    def __init__(self,request,s): self.request,self.s,self.events=request,s,[]
    def pending(self,tenants,limit,local_windows):
        assert tenants==('same-gym',) and limit==25
        assert local_windows=={'same-gym':['2026-10-08','2026-10-09']}
        self.events.append('discover');return [self.request]
    def bind(self,request,**refs):
        self.events.append('bind')
        assert refs['source_revision'].startswith('source-brand:sha256:')
        assert refs['caption']==self.s.source.text and refs['palette']==self.s.palette
        return dict(bound=True,calendar_row_id=self.s.row_id)
    def record(self,request,rid,result):
        self.events.append('record');assert rid==self.s.row_id


def execute(s,transport,**kwargs):
    return gap.run_pending(persistence=s.persistence,jobs=s.case.jobs,transport=transport,
        bundle_reader=lambda base:s.active,accounts=lambda key:s.account,now=NOW,
        row_runner=lambda *a,**kw:run(s),**kwargs)


def test_discovered_gap_bound_row_runs_real_offline_A_preparation_and_reserve(system):
    request=dict(request_id=str(uuid.uuid4()),gym_id='same-gym',local_date=system.case.request['local_date'],
                 account='instagram',format='feed')
    transport=Transport(request,system)
    result=execute(system,transport)
    assert result['ok'] and result['rows'][0]['reserved']
    assert transport.events==['discover','bind','record']
    assert system.case.provider.calls==system.case.reviewer.calls==1
    assert len(system.reserved)==1

@pytest.mark.parametrize('change,reason',[
    ({'gym_id':'other'},'generated_gap_request_invalid'),
    ({'format':'story'},'generated_gap_request_invalid'),
    ({'local_date':'2026-10-12'},'generated_gap_date_expired'),
    ({'request_id':'not-uuid'},'generated_gap_owner_unavailable'),
])
def test_discovery_binding_mismatch_before_provider(system,change,reason):
    request=dict(request_id=str(uuid.uuid4()),gym_id='same-gym',local_date=system.case.request['local_date'],
                 account='instagram',format='feed',**{})
    request.update(change)
    transport=Transport(request,system)
    result=execute(system,transport)
    assert result['rows'][0]['reason']==reason and transport.events==['discover']
    assert system.case.provider.calls==0


def test_no_approved_copy_does_not_bind(system):
    system.active['fact_validation']='pending_collector_validation'
    request=dict(request_id=str(uuid.uuid4()),gym_id='same-gym',local_date=system.case.request['local_date'],
                 account='instagram',format='feed')
    transport=Transport(request,system)
    result=execute(system,transport)
    assert result['rows'][0]['reason']=='generated_bundle_fact_validation_required'
    assert transport.events==['discover']


def test_copy_revoked_after_bind_prevents_provider(system):
    request=dict(request_id=str(uuid.uuid4()),gym_id='same-gym',local_date=system.case.request['local_date'],
                 account='instagram',format='feed')
    transport=Transport(request,system);bind=transport.bind
    def revoke(*a,**kw):
        value=bind(*a,**kw);system.active['fact_validation']='pending_collector_validation';return value
    transport.bind=revoke
    result=execute(system,transport)
    assert result['rows'][0]['reason']=='generated_bundle_fact_validation_required'
    assert system.case.provider.calls==0


def test_bind_commit_uncertainty_quarantines_request(system,monkeypatch):
    transport=gap.GapOwnerTransport(system.persistence,system.case.jobs)
    request=dict(request_id=str(uuid.uuid4()),gym_id='same-gym',local_date=system.case.request['local_date'],
                 account='instagram',format='feed')
    calls=[]
    def rpc(op,args):
        calls.append(op)
        return dict(bound=True,calendar_row_id=args[1],logical_post_id=args[2],
                    **{k:request[k] for k in ('gym_id','local_date','account','format')})
    monkeypatch.setattr(transport,'rpc',rpc)
    system.conn.commit=lambda: (_ for _ in ()).throw(TimeoutError())
    for _ in range(2):
        with pytest.raises(runtime.RuntimeHold,match='generated_gap_commit_uncertain'):
            transport.bind(request,caption=system.source.text,source_revision='ref',
                           palette=system.palette,palette_revision='palette-v1',authority=derive(system.active))
    assert calls==['bind_bundle'] and system.case.provider.calls==0


def test_acknowledged_bind_rollback_allows_same_exact_refs_retry(system,monkeypatch):
    transport=gap.GapOwnerTransport(system.persistence,system.case.jobs)
    request=dict(request_id=str(uuid.uuid4()),gym_id='same-gym',local_date=system.case.request['local_date'],
                 account='instagram',format='feed')
    monkeypatch.setattr(transport,'rpc',lambda *a: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(runtime.RuntimeHold,match='generated_gap_binding_unavailable'):
        transport.bind(request,caption=system.source.text,source_revision='ref',
                       palette=system.palette,palette_revision='palette-v1',authority=derive(system.active))
    assert transport.phase(request['request_id'])=='ready'
    with pytest.raises(runtime.RuntimeHold,match='generated_gap_binding_changed'):
        transport.bind(request,caption='Different caption',source_revision='ref',
                       palette=system.palette,palette_revision='palette-v1',authority=derive(system.active))


def test_discovery_supplies_each_gym_timezone_window_before_limit(system,monkeypatch):
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',lambda:(('east','west'),25))
    monkeypatch.setattr('agent.config.posting_timezone_for',lambda base:
                        'Pacific/Kiritimati' if base=='east' else 'Pacific/Honolulu')
    seen=[]
    transport=SimpleNamespace(pending=lambda tenants,limit,windows:seen.append(windows) or [])
    result=gap.run_pending(persistence=system.persistence,jobs=system.case.jobs,transport=transport,
                           now='2026-10-07T12:00:00+00:00')
    assert result['ok']
    assert seen==[{'east':['2026-10-09','2026-10-10'],'west':['2026-10-08','2026-10-09']}]
