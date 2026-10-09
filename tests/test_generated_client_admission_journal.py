"""Durable consumer identities and reader role checks; no provider/live claims."""
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest
from agent import generated_client_admission as client
from agent import generated_infographic_preparation as prep
from agent import generated_hosted_byte_authority as hosted
from test_generated_infographic_preparation import case
from test_generated_canonical_owner import active, derive


@pytest.fixture
def prepared(case, active):
    authority = derive(active)
    case.snapshot.update(copy=authority['copy'],palette=authority['palette'],
        palette_revision=authority['palette_revision'],copy_approved=False,copy_verified=True,
        authority_pins=authority['authority_pins'],copy_derivation_receipt=authority['copy_derivation_receipt'],
        copy_digest=prep.digest(authority['copy']))
    case.request['palette_revision']=authority['palette_revision']
    result=prep.prepare_candidate(case.request,case.snapshot,jobs=case.jobs,
        provider=case.provider,reviewer=case.reviewer,storage=case.storage,enabled=True)
    assert result['ok'],result
    return case,result['candidate'],authority['source_revision']


def test_freeze_survives_reload_and_receipt_is_immutable(prepared):
    case,candidate,source=prepared
    journal=client.ClientAdmissionJournal(case.jobs);row=str(uuid.uuid4());manifest={'same':'object'}
    first=journal.freeze(row,candidate,manifest,source)
    assert first['state']=='awaiting_receipt' and first['receipt_id'] is None
    assert type(first['manifest_bytes']) is bytes
    raw=json.loads(first['manifest_bytes'])
    assert raw['artifact_version_id']==first['artifact_version_id']
    assert raw['candidate']==candidate and raw['reservation_manifest']==manifest
    receipt=str(uuid.uuid4());journal.attach_receipt(row,receipt)
    second=client.ClientAdmissionJournal(prep.SQLiteGenerationJobs(case.jobs.path))
    assert second.freeze(row,candidate,manifest,source)['manifest_bytes']==first['manifest_bytes']
    assert second.load(row)['receipt_id']==receipt
    with pytest.raises(client.AdmissionHold,match='receipt_changed'):
        second.attach_receipt(row,str(uuid.uuid4()))
    for change in ({'manifest_digest':'changed'},):
        with pytest.raises(client.AdmissionHold,match='binding_changed'):
            second.freeze(row,candidate,change,source)


def test_dispatch_and_commit_quarantine_keep_exact_identity(prepared):
    case,candidate,source=prepared;journal=client.ClientAdmissionJournal(case.jobs);row=str(uuid.uuid4())
    frozen=journal.freeze(row,candidate,{},source);receipt=str(uuid.uuid4());journal.attach_receipt(row,receipt)
    journal.state(row,'dispatching')
    with pytest.raises(client.AdmissionHold): journal.state(row,'dispatching')
    journal.state(row,'committing')
    restored=client.ClientAdmissionJournal(prep.SQLiteGenerationJobs(case.jobs.path)).load(row)
    assert restored['state']=='committing' and restored['receipt_id']==receipt
    assert restored['manifest_bytes']==frozen['manifest_bytes']


def test_disabled_does_not_lookup_or_stage(prepared,monkeypatch):
    case,candidate,source=prepared;monkeypatch.delenv(client.FLAG,raising=False)
    authority=hosted.GeneratedHostedByteAuthority(lambda: (_ for _ in ()).throw(AssertionError('no DB')),tenant_id=candidate['gym_id'])
    admission=client.GeneratedClientAdmission(jobs=case.jobs,authority=authority)
    with pytest.raises(client.AdmissionHold,match='disabled'):
        admission.stage(None,str(uuid.uuid4()),candidate,[],{},source)


def test_receipt_pending_exports_manifest_before_lookup(prepared,monkeypatch):
    case,candidate,source=prepared;monkeypatch.setenv(client.FLAG,'true');row=str(uuid.uuid4())
    authority=hosted.GeneratedHostedByteAuthority(lambda: (_ for _ in ()).throw(AssertionError('no DB')),tenant_id=candidate['gym_id'])
    admission=client.GeneratedClientAdmission(jobs=case.jobs,authority=authority)
    planned_id=client.candidate_row(row,candidate['job_id'])
    admission.journal.freeze(row,candidate,{},source,stage_plan={'placeholder_row_id':row,'planned_row':{'id':planned_id}})
    with pytest.raises(client.AdmissionHold,match='receipt_pending'):
        admission.stage(None,row,candidate,[],{},source)
    assert admission.journal.load(row)['manifest_bytes']


def test_issuer_adapter_and_authenticated_issuer_role_are_rejected(prepared,monkeypatch):
    case,candidate,source=prepared
    reader=hosted.HostedObjectReader({candidate['gym_id']:['https://cdn.example/'+candidate['gym_id']+'/']})
    with pytest.raises(client.AdmissionHold,match='reader_only'):
        client.GeneratedClientAdmission(jobs=case.jobs,authority=hosted.GeneratedHostedByteAuthority(
            lambda:None,tenant_id=candidate['gym_id'],reader=reader))
    class Conn:
        autocommit=False;info=SimpleNamespace(transaction_status=0)
        def execute(self,q,*args):
            assert 'not pg_has_role' in q
            return SimpleNamespace(fetchone=lambda:(False,))
        def rollback(self):pass
        def close(self):self.closed=True
    conn=Conn();row=str(uuid.uuid4());monkeypatch.setenv(client.FLAG,'true')
    authority=hosted.GeneratedHostedByteAuthority(lambda:conn,tenant_id=candidate['gym_id'])
    admission=client.GeneratedClientAdmission(jobs=case.jobs,authority=authority)
    admission.journal.freeze(row,candidate,{},source,stage_plan={'placeholder_row_id':row,'planned_row':{'id':client.candidate_row(row,candidate['job_id'])}});admission.journal.attach_receipt(row,str(uuid.uuid4()))
    with pytest.raises(client.AdmissionHold,match='committed_receipt_unavailable'):
        admission.stage(None,row,candidate,[],{},source)
    assert conn.closed and admission.journal.load(row)['state']=='ready'


@pytest.mark.parametrize('capture', [False, True])
def test_generated_approval_requires_exact_snapshot_and_atomic_rpc_even_without_capture(monkeypatch,capture):
    from agent import portal_social as ps
    monkeypatch.setenv(client.FLAG,'true')
    monkeypatch.setattr(ps.config,'approval_capture_enabled',lambda:capture)
    monkeypatch.setattr(ps.config,'approval_proof_enabled',lambda:False)
    monkeypatch.setattr(ps,'_action_gates',lambda *a,**k:None)
    row=dict(id=str(uuid.uuid4()),gym_id='same-gym',status='pending',image_url='https://cdn.example/original.png',
             media_not_ready_reason=None,creative_origin='generated',generated_artifact_version_id=str(uuid.uuid4()),
             generated_artifact_sha256='a'*64,caption='Exact text',post_date='2026-10-10',format='feed',account='instagram')
    expected=dict(caption=row['caption'],media_url=row['image_url'],day_key=row['post_date'],format='feed',
                  platform='instagram',**{k:row[k] for k in ps._GENERATED_ROW_KEYS})
    monkeypatch.setattr(ps,'_sb_load_owned_row',lambda *a,**k:(row,None))
    class Atomic:
        def __init__(self):self.calls=[]
        def approve_ready(self,*args,**kw):
            self.calls.append((args,kw));return {**row,'status':'approved','approval_digest':'digest'}
        def set_status(self,*a):raise AssertionError('must not use generic setter')
    store=Atomic()
    for invalid in (None,{k:v for k,v in expected.items() if k!='creative_origin'},
                    {**expected,'generated_artifact_sha256':'b'*64}):
        assert ps._handle_approve_supabase('same-gym',row['id'],'spoofed-body-actor',None,store,expected_creative=invalid)[0]==409
    assert store.calls==[]
    status,body=ps._handle_approve_supabase('same-gym',row['id'],'spoofed-body-actor',None,store,expected_creative=expected)
    assert status==200 and body['approval_digest']=='digest'
    assert store.calls==[(('same-gym',row['id']),{'expected_creative':expected})]
    # Shape alone cannot select the unsafe fallback when no atomic RPC exists.
    assert ps._handle_approve_supabase('same-gym',row['id'],'actor',None,object(),expected_creative=expected)[0]==409


def test_staged_plan_and_new_row_identity_survive_retry(prepared):
    case,candidate,source=prepared;placeholder=str(uuid.uuid4())
    row=client.candidate_row(placeholder,candidate['job_id'])
    plan={'placeholder_row_id':placeholder,'planned_row':{'id':row},'old_snapshot':{'id':placeholder},'gap_snapshot':{'request_id':str(uuid.uuid4())}}
    journal=client.ClientAdmissionJournal(case.jobs)
    frozen=journal.freeze(placeholder,candidate,{},source,stage_plan=plan)
    assert row != placeholder and frozen['binding']['calendar_row_id']==row
    assert frozen['artifact_version_id']==client.artifact_version(row,candidate['job_id'])
    receipt=str(uuid.uuid4());journal.attach_receipt(placeholder,receipt)
    reloaded=client.ClientAdmissionJournal(prep.SQLiteGenerationJobs(case.jobs.path))
    assert reloaded.freeze(placeholder,candidate,{},source,stage_plan=plan)['manifest_bytes']==frozen['manifest_bytes']
    with pytest.raises(client.AdmissionHold,match='binding_changed'):
        reloaded.freeze(placeholder,candidate,{},source,stage_plan={**plan,'gap_snapshot':{'request_id':str(uuid.uuid4())}})
