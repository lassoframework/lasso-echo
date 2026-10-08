"""Synthetic delegated bundle consumption; no production/providing evidence."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from agent import generated_infographic_runtime as runtime
from agent import generated_infographic_preparation as prep
from agent import generated_infographic_gap_owner as gap
from agent import forward_media_guard as guard
from agent import forward_media_owner as owner
from test_generated_infographic_preparation import case


def identity():
    return str(uuid.uuid4())


def snapshot(gym, base, now):
    text = 'Practice with guidance'
    website = b'<style>#112233 #44AA77</style> ' + text.encode()
    social = ('{"caption":"' + text + '"}').encode()
    captures = []
    for kind, data in [('website', website), ('social', social)]:
        captures.append(dict(id=identity(), gym_id=gym, echo_account_key=base,
            source_kind=kind, source_url='https://gym.example.test/' if kind == 'website' else 'https://api.apify.com/v2/datasets/test/items',
            source_locator=None if kind == 'website' else 'https://www.instagram.com/verified_gym/',
            capture_provider='direct' if kind == 'website' else 'apify',
            provider_response_id=None if kind == 'website' else 'dataset:test:1',
            provider_account_id=None if kind == 'website' else 'social-account:1',
            source_revision='capture-v1', mapping_revision='map-v1', mapping_evidence={'binding':'gym'},
            fetched_at=now.isoformat(), bytes_sha256=hashlib.sha256(data).hexdigest(),
            bytes_base64=base64.b64encode(data).decode()))
    website_capture = captures[0]
    return dict(schema_version=1, gym_id=gym, echo_account_key=base,
        fact_policy='delegated_supported_facts', captures=captures,
        palette=dict(capture_id=website_capture['id'], bytes_sha256=website_capture['bytes_sha256'],
            primary='#112233',secondary='#44AA77',primary_byte_offset=7,secondary_byte_offset=15),
        selected_facts=[dict(key='coaching',capture_id=website_capture['id'],
            bytes_sha256=website_capture['bytes_sha256'], source_locator=website_capture['source_url'],
            byte_offset=website.index(text.encode()),byte_length=len(text.encode()),text=text)])


def frozen(value):
    raw = json.dumps(value, ensure_ascii=False)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def active():
    now = datetime.now(timezone.utc) - timedelta(minutes=2)
    gym, base, bundle_id = identity(), 'same-gym', identity()
    raw, sha = frozen(snapshot(gym, base, now))
    bundle = dict(id=bundle_id,gym_id=gym,echo_account_key=base,schema_version=1,version=1,
        capture_ids=[c['id'] for c in json.loads(raw)['captures']],snapshot_bytes=raw,content_sha256=sha,
        source_revision='bundle-source-v1',palette_revision='bundle-palette-v1',created_at=now.isoformat())
    receipt = dict(id=1,gym_id=gym,bundle_id=bundle_id,bundle_version=1,content_sha256=sha,
        actor_clerk_user_id='user_authenticated',actor_authority='blake',action='approve',
        purpose='echo_source_brand_configuration',request_id=identity(),created_at=now.isoformat())
    observation = dict(id=1,gym_id=gym,bundle_id=bundle_id,configuration_sha256=sha,
        snapshot_bytes=raw,content_sha256=sha,validator_revision='trusted-validator-v1',
        validation_report=dict(selected_facts_status='supported_uncontradicted',identity_status='verified'),
        created_at=now.isoformat())
    return dict(bundle=bundle,approval_receipt=receipt,observation=observation,
                fact_approval_mode='delegated_policy',fact_validation='supported_uncontradicted')


def observe(active, mutate):
    value = json.loads(active['observation']['snapshot_bytes'])
    mutate(value)
    raw, sha = frozen(value)
    active['observation'].update(snapshot_bytes=raw, content_sha256=sha)


def derive(active, **kw):
    return runtime.delegated_copy(active, 'same-gym', local_date='2026-10-08', **kw)


def test_configuration_receipt_is_not_caption_approval(active):
    authority = derive(active)
    assert authority['caption'] == 'Practice with guidance'
    assert authority['copy_approved'] is False
    assert authority['authority_pins']['mode'] == 'delegated_policy'
    assert authority['authority_pins']['configuration_sha256'] == active['bundle']['content_sha256']
    assert authority['copy_derivation_receipt']['fact_witness'] == json.loads(active['observation']['snapshot_bytes'])['selected_facts'][0]


@pytest.mark.parametrize('mutation',[
    lambda a:a.update(observation=None),
    lambda a:a.update(fact_validation='pending_collector_validation'),
    lambda a:a['observation']['validation_report'].update(selected_facts_status='contradicted'),
    lambda a:a['observation']['validation_report'].update(identity_status='changed'),
    lambda a:a['approval_receipt'].update(action='revoke'),
    lambda a:a['approval_receipt'].update(actor_authority='coach'),
    lambda a:a['approval_receipt'].update(purpose='caption_approval'),
    lambda a:a['approval_receipt'].update(bundle_version=2),
    lambda a:a['approval_receipt'].update(content_sha256='0'*64),
    lambda a:a['approval_receipt'].update(gym_id=identity()),
    lambda a:a['bundle'].update(echo_account_key='other-gym'),
    lambda a:a['bundle'].update(content_sha256='0'*64),
    lambda a:a['observation'].update(content_sha256='0'*64),
    lambda a:a['observation'].update(configuration_sha256='0'*64),
    lambda a:a['observation'].update(validator_revision=''),
    lambda a:a['observation'].update(id=True),
])
def test_missing_changed_or_revoked_authority_holds(active, mutation):
    mutation(active)
    with pytest.raises(runtime.RuntimeHold):
        derive(active)


@pytest.mark.parametrize('mutation',[
    lambda s:s['captures'][0].update(gym_id=identity()),
    lambda s:s['captures'][0].update(echo_account_key='other-gym'),
    lambda s:s['captures'][0].update(bytes_sha256='0'*64),
    lambda s:s['captures'][0].update(bytes_base64='not base64'),
    lambda s:s['captures'][0].update(fetched_at=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()),
    lambda s:s['captures'][0].update(fetched_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()),
    lambda s:s['captures'][1].update(provider_account_id='different-account'),
    lambda s:s['captures'][1].update(source_locator='https://www.instagram.com/other_gym/'),
    lambda s:s['captures'][1].update(mapping_revision='different-mapping'),
    lambda s:s['palette'].update(primary_byte_offset=8),
    lambda s:s['palette'].update(primary_byte_offset=True),
    lambda s:s['palette'].update(primary='#000000'),
    lambda s:s['selected_facts'][0].update(byte_offset=0),
    lambda s:s['selected_facts'][0].update(byte_length=True),
    lambda s:s['selected_facts'][0].update(text='Invented fact'),
    lambda s:s['selected_facts'][0].update(bytes_sha256='0'*64),
    lambda s:s['selected_facts'].append(dict(s['selected_facts'][0])),
    lambda s:s.update(selected_facts=[]),
])
def test_hash_recomputed_does_not_validate_wrong_fact_identity_color_or_freshness(active, mutation):
    observe(active, mutation)
    with pytest.raises(runtime.RuntimeHold):
        derive(active)


def test_changed_selected_text_and_color_even_with_exact_current_witness_hold(active):
    def mutate(s):
        c=s['captures'][0]
        data=base64.b64decode(c['bytes_base64']).replace(b'Practice with guidance',b'Always guaranteed wins')
        c.update(bytes_base64=base64.b64encode(data).decode(), bytes_sha256=hashlib.sha256(data).hexdigest())
        s['palette']['bytes_sha256']=c['bytes_sha256']
        s['selected_facts'][0].update(text='Always guaranteed wins',byte_length=22,bytes_sha256=c['bytes_sha256'])
    observe(active,mutate)
    with pytest.raises(runtime.RuntimeHold): derive(active)


def test_old_configuration_with_fresh_shifted_observation_remains_valid(active):
    initial=derive(active)
    # Only configuration may be old; current observation remains fresh.
    stale=json.loads(active['bundle']['snapshot_bytes'])
    for c in stale['captures']: c['fetched_at']=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    raw,sha=frozen(stale)
    active['bundle'].update(snapshot_bytes=raw,content_sha256=sha)
    active['approval_receipt']['content_sha256']=sha
    active['observation']['configuration_sha256']=sha
    def shifted(s):
        c=s['captures'][0];data=b'irrelevant padding '+base64.b64decode(c['bytes_base64'])
        c.update(id=identity(),bytes_base64=base64.b64encode(data).decode(),bytes_sha256=hashlib.sha256(data).hexdigest())
        s['palette'].update(capture_id=c['id'],bytes_sha256=c['bytes_sha256'],primary_byte_offset=26,secondary_byte_offset=34)
        s['selected_facts'][0].update(capture_id=c['id'],bytes_sha256=c['bytes_sha256'],byte_offset=s['selected_facts'][0]['byte_offset']+19)
    observe(active, shifted)
    refreshed=derive(active)
    assert refreshed['caption']==initial['caption']
    assert refreshed['authority_pins']!=initial['authority_pins']


def test_unsupported_caption_holds(active):
    with pytest.raises(runtime.RuntimeHold,match='generated_bundle_caption_unsupported'):
        derive(active,caption='An invented caption')


def test_delegated_candidate_is_strict_and_job_pins_are_immutable(active,case):
    authority=derive(active)
    case.snapshot.update(copy=authority['copy'],palette=authority['palette'],copy_approved=False,
        copy_verified=True,copy_digest=prep.digest(authority['copy']),
        authority_pins=authority['authority_pins'],copy_derivation_receipt=authority['copy_derivation_receipt'])
    first=prep.prepare_candidate(case.request,case.snapshot,jobs=case.jobs,provider=case.provider,
        reviewer=case.reviewer,storage=case.storage,enabled=True)
    assert first['ok'],first
    candidate=first['candidate']
    assert candidate['schema_version']==2
    assert prep.validate_candidate(candidate,case.data)==candidate
    job,binding=runtime._generation_binding(case.request,case.snapshot)
    assert job==candidate['job_id']
    for key in ('authority_pins','copy_derivation_receipt'):
        altered=copy.deepcopy(candidate);altered.pop(key)
        with pytest.raises(prep.PreparationHold):prep.validate_candidate(altered)
    altered=copy.deepcopy(candidate);altered['authority_pins']['observation_id']=2
    with pytest.raises(prep.PreparationHold):prep.validate_candidate(altered)
    altered=copy.deepcopy(candidate);altered['unknown']=True
    with pytest.raises(prep.PreparationHold):prep.validate_candidate(altered)
    changed=copy.deepcopy(case.snapshot);changed['authority_pins']['observation_id']=2
    assert runtime._generation_binding(case.request,changed)[0]!=job


def test_delegated_copy_cannot_be_relabelled_explicit_approval(active,case):
    a=derive(active)
    case.snapshot.update(copy=a['copy'],copy_digest=prep.digest(a['copy']),authority_pins=a['authority_pins'],
        copy_derivation_receipt=a['copy_derivation_receipt'],copy_verified=True,copy_approved=True)
    result=prep.prepare_candidate(case.request,case.snapshot,jobs=case.jobs,provider=case.provider,
        reviewer=case.reviewer,storage=case.storage,enabled=True)
    assert result['reason']=='generated_bundle_policy_invalid'
    assert case.provider.calls==0


class Conn:
    autocommit=False
    def __init__(self):self.rollbacks=0;self.info=SimpleNamespace(transaction_status=0)
    def rollback(self):self.rollbacks+=1


def test_missing_owner_bridge_holds_before_provider(active,case,monkeypatch):
    monkeypatch.setenv(runtime.FLAG,'true')
    monkeypatch.setattr(guard,'enabled',lambda:True)
    monkeypatch.setattr(owner,'check_environment',lambda:None)
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',lambda:(('same-gym',),25))
    conn=Conn();persistence=owner.ForwardMediaOwnerPersistence(conn,'owner',SimpleNamespace(read=lambda u:case.data))
    snap={**case.request,'account':'instagram','format':'feed','group_key':'vg',
        'copy':dict(gym_id='same-gym',local_date=case.request['local_date'],logical_post_id=case.request['logical_post_id'],group_key='vg',caption=derive(active)['caption'])}
    monkeypatch.setattr(guard,'generated_snapshot',lambda p,r:snap)
    result=runtime.run_calendar_row('same-gym',SimpleNamespace(key='same-gym_ig',platform='instagram'),identity(),
        persistence=persistence,jobs=case.jobs,provider=case.provider,reviewer=case.reviewer,storage=case.storage)
    assert result['reason']=='generated_bundle_owner_bridge_unavailable'
    assert case.provider.calls==0 and conn.rollbacks>0


def test_owner_snapshot_consumes_bundle_without_local_sources(active,case,monkeypatch):
    conn=Conn();p=SimpleNamespace(_conn=conn,_reader=SimpleNamespace(read=lambda u:case.data))
    snap={**case.request,'account':'instagram','format':'feed','group_key':'vg','history_complete':True,
        'photo_inventory_complete':True,'eligible_photo_count':0,
        'history':dict(rows=[],scope_complete=True,spine_digest=case.request['history_revision']),
        'copy':dict(gym_id='same-gym',local_date=case.request['local_date'],logical_post_id=case.request['logical_post_id'],group_key='vg',caption=derive(active)['caption'])}
    monkeypatch.setattr(guard,'generated_snapshot',lambda p,r:copy.deepcopy(snap))
    loader=runtime.OwnerSnapshotLoader(p,bundle_reader=lambda base:copy.deepcopy(active))
    result,visuals=loader.load(identity(),'same-gym',SimpleNamespace(key='same-gym_ig',platform='instagram'))
    assert result['copy_approved'] is False and result['copy_verified'] is True
    assert result['authority_pins']==derive(active)['authority_pins']
    assert visuals==[]
    snap['eligible_photo_count']=1
    with pytest.raises(prep.PreparationHold,match='generated_photo_available'):
        loader.load(identity(),'same-gym',SimpleNamespace(key='same-gym_ig',platform='instagram'))


def test_full_owner_generation_replay_and_current_publish_binding(active,case,monkeypatch):
    monkeypatch.setenv(runtime.FLAG,'true')
    monkeypatch.setattr(guard,'enabled',lambda:True)
    monkeypatch.setattr(owner,'check_environment',lambda:None)
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',lambda:(('same-gym',),25))
    class RuntimeConn(Conn):
        def commit(self):pass
    conn=RuntimeConn();p=owner.ForwardMediaOwnerPersistence(conn,'owner',None)
    row_id=identity();account=SimpleNamespace(key='same-gym_ig',platform='instagram')
    a=derive(active)
    snap={**case.request,'account':'instagram','format':'feed','group_key':'vg_bound',
        'photo_inventory_complete':True,'eligible_photo_count':0,'history_complete':True,
        'history':dict(rows=[],scope_complete=True,spine_digest=case.request['history_revision']),
        'copy':dict(gym_id='same-gym',local_date=case.request['local_date'],logical_post_id=case.request['logical_post_id'],group_key='vg_bound',caption=a['caption'])}
    monkeypatch.setattr(guard,'generated_snapshot',lambda p,r:copy.deepcopy(snap))
    def read(url):return case.storage.get_bytes(url.split('images.example.test/')[1])
    loader=runtime.OwnerSnapshotLoader(p,bundle_reader=lambda base:copy.deepcopy(active),reader=read)
    reservations=[]
    def reserve(p,r,c,current,**kw):
        assert current['copy_approved'] is False
        assert c['authority_pins']==current['authority_pins']==a['authority_pins']
        prep.validate_candidate(c,read(c['original_url']))
        reservations.append(copy.deepcopy(c));return dict(reserved=True,receipt_ref='synthetic-receipt')
    monkeypatch.setattr(guard,'reserve_generated',reserve)
    kwargs=dict(persistence=p,loader=loader,jobs=case.jobs,provider=case.provider,reviewer=case.reviewer,storage=case.storage)
    assert runtime.run_calendar_row('same-gym',account,row_id,**kwargs)['ok']
    assert runtime.run_calendar_row('same-gym',account,row_id,**kwargs)['ok']
    assert case.provider.calls==1
    c=reservations[0]
    row=dict(id=row_id,gym_id='same-gym',account='instagram',format='feed',caption=a['caption'],
        post_date=c['local_date'],logical_post_id=c['logical_post_id'],visual_group_key='vg_bound',
        image_url=c['original_url'],source_media_url=c['original_url'],thumbnail_url=None,
        source_media_asset_id=runtime.PREFIX+c['job_id'],render_manifest_digest='synthetic-manifest')
    binding=dict(schema_version=2,job_id=c['job_id'],calendar_row_id=row_id,gym_id='same-gym',account='instagram',
        local_date=c['local_date'],logical_post_id=c['logical_post_id'],group_key='vg_bound',
        original_url=c['original_url'],manifest_digest='synthetic-manifest',source_revision=a['source_revision'],
        copy_digest=c['copy_digest'],palette_revision=c['palette_revision'],palette_digest=c['palette_digest'],
        receipt_ref='synthetic-receipt',authority_pins=c['authority_pins'],copy_derivation_receipt=c['copy_derivation_receipt'])
    assert runtime.validate_publish_palette(row,readback=lambda r:copy.deepcopy(binding),bundle_reader=lambda b:copy.deepcopy(active))
    # Unchanged text and pixels still hold after a new immutable observation.
    active['observation']['id']=2
    result=runtime.run_calendar_row('same-gym',account,row_id,**kwargs)
    assert result['reason']=='generated_approved_source_changed' and case.provider.calls==1
    with pytest.raises(runtime.RuntimeHold,match='generated_bundle_publish_binding_changed'):
        runtime.validate_publish_palette(row,readback=lambda r:binding,bundle_reader=lambda b:active)


def test_gap_uses_supported_current_bundle_and_propagates_pins(active,case,monkeypatch):
    monkeypatch.setenv(runtime.FLAG,'true')
    monkeypatch.setattr(guard,'enabled',lambda:True)
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',lambda:(('same-gym',),25))
    now=datetime.now(timezone.utc)
    class Transport:
        def pending(self,tenants,limit,windows):
            self.request=dict(request_id=identity(),gym_id='same-gym',account='instagram',format='feed',local_date=windows['same-gym'][0])
            return [self.request]
        def bind(self,request,**kw):
            self.binding=kw
            assert kw['authority']['copy_approved'] is False
            return dict(calendar_row_id=identity())
        def record(self,request,row,result):self.result=result
    t=Transport()
    result=gap.run_pending(persistence=None,jobs=case.jobs,transport=t,bundle_reader=lambda base:copy.deepcopy(active),
        accounts=lambda key:SimpleNamespace(key=key,platform='instagram'),
        row_runner=lambda *args,**kw:dict(ok=True,reserved=True),now=now)
    assert result['ok'] and t.result['reserved']
    assert t.binding['authority']['authority_pins']['observation_id']==1
    assert t.binding['source_revision'].startswith('source-brand:sha256:')


def test_gap_missing_semantic_validation_never_binds(active,case,monkeypatch):
    monkeypatch.setenv(runtime.FLAG,'true');monkeypatch.setattr(guard,'enabled',lambda:True)
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',lambda:(('same-gym',),25))
    active['observation']=None;active['fact_validation']='pending_collector_validation'
    class Transport:
        binds=0
        def pending(self,tenants,limit,windows):return [dict(request_id=identity(),gym_id='same-gym',account='instagram',format='feed',local_date=windows['same-gym'][0])]
        def bind(self,*args,**kw):self.binds+=1
    t=Transport()
    result=gap.run_pending(persistence=None,jobs=case.jobs,transport=t,bundle_reader=lambda base:active,
        accounts=lambda key:SimpleNamespace(key=key,platform='instagram'),now=datetime.now(timezone.utc))
    assert result['rows'][0]['reason']=='generated_bundle_fact_validation_required'
    assert t.binds==case.provider.calls==0


def test_real_python_B_seam_uses_canonical_reservation_and_fresh_authority(active,case,monkeypatch):
    authority=derive(active)
    case.snapshot.update(copy=authority['copy'],palette=authority['palette'],copy_approved=False,
        copy_verified=True,copy_digest=prep.digest(authority['copy']),authority_pins=authority['authority_pins'],
        copy_derivation_receipt=authority['copy_derivation_receipt'],palette_digest=prep.digest(authority['palette']),
        palette_verified=True,approved_source_revision=authority['source_revision'],palette_revision=authority['palette_revision'])
    case.request['palette_revision']=authority['palette_revision']
    result=prep.prepare_candidate(case.request,case.snapshot,jobs=case.jobs,provider=case.provider,
        reviewer=case.reviewer,storage=case.storage,enabled=True)
    assert result['ok'],result
    candidate=result['candidate']
    class ReservationConn(Conn):
        def __init__(self):super().__init__();self.calls=[]
        def cursor(self):return self
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,q,args):self.calls.append((q,args))
        def fetchone(self):return ({'reserved':True,'receipt_ref':'SYNTHETIC'},)
    conn=ReservationConn();p=owner.ForwardMediaOwnerPersistence(conn,'owner',None)
    monkeypatch.setattr(p,'_assert_owner_identity',lambda:None)
    monkeypatch.setattr(runtime,'_owner_bundle_readback',lambda persistence,base:copy.deepcopy(active))
    db={**case.request,'photo_inventory_complete':True,'eligible_photo_count':0,'history_complete':True,'history':{'rows':[]}}
    monkeypatch.setattr(guard,'generated_snapshot',lambda p,r:db)
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url',lambda u:u.startswith('https://images.example.test/'))
    reads=[]
    def read(url):
        assert conn.rollbacks>0
        reads.append(url);return case.data
    reservation=guard.reserve_generated(p,identity(),candidate,case.snapshot,history_visuals=[],read_bytes=read)
    assert reservation['reserved']
    q,args=conn.calls[-1]
    assert 'fixer_reserve_generated_bundle_20261007' in q
    assert json.loads(args[1])['authority_pins']==authority['authority_pins']
    assert args[-1]==authority['source_revision']
    assert len(reads)==2
    active['observation']['id']+=1
    with pytest.raises(guard.ForwardMediaVerificationHold,match='generated canonical bundle changed'):
        guard.reserve_generated(p,identity(),candidate,case.snapshot,history_visuals=[],read_bytes=lambda u:pytest.fail('must hold before bytes'))
