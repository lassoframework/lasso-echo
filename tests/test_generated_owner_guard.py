"""Generated owner local tests; no provider/storage/production access."""
import hashlib
import io
import json
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image

from agent import forward_media_guard as guard, forward_media_owner as owner, visual_scene

APPROVED_SOURCE_REVISION = 'client-source:sha256:' + 'a' * 64


def image_bytes():
    im = Image.new('RGB', (128, 128), 'white')
    for x in range(128):
        for y in range(128):
            im.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, (x+y) % 256))
    out = io.BytesIO(); im.save(out, format='PNG'); return out.getvalue()


def candidate(snapshot, data, job=None):
    return {**{k: snapshot[k] for k in ('gym_id','local_date','logical_post_id','copy_revision',
                'palette_revision','inventory_revision','history_revision','copy_digest','palette_digest')},
        'schema_version':1,'source_type':'generated_astra_infographic','job_id':job or str(uuid.uuid4()),
        'provider':'astra','model':'gpt-6-astra','provider_response_id':'SYNTHETIC provider response',
        'provider_output_id':'SYNTHETIC original output','original_sha256':hashlib.sha256(data).hexdigest(),
        'original_md5':hashlib.md5(data).hexdigest(),'original_url':'https://owned.example/generated.png',
        'original_length':len(data),'original_phash':visual_scene.scene_fingerprint(data),
        'width':128,'height':128,'storage_key':'generated/'+hashlib.sha256(data).hexdigest()+'.png',
        'storage_readback_sha256':hashlib.sha256(data).hexdigest(),
        'review_response_id':'SYNTHETIC review','review_policy_id':'gym-infographic-copy-palette-v1'}


def trusted(snapshot=None):
    return {'gym_id':'gym','local_date':'2026-10-10','logical_post_id':'group',
        'copy_revision':'copy','palette_revision':'palette','inventory_revision':'inventory',
        'history_revision':'history','copy_digest':'copy digest','palette_digest':'palette digest',
        'approved_source_revision':APPROVED_SOURCE_REVISION,
        'photo_inventory_complete':True,'eligible_photo_count':0,'history_complete':True,
        'local_census_current':True,'copy_verified':True,'palette_verified':True,**(snapshot or {})}


class Conn:
    autocommit = False
    def __init__(self):
        self.calls = []; self.events = []
        self.snapshot = {**trusted(), 'history': {'rows': []}}
    def rollback(self): self.events.append('rollback')
    def cursor(self): return Cursor(self)


class Cursor:
    def __init__(self, conn): self.conn=conn
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,q,args=None):
        self.conn.calls.append((q,args))
        self.result = (('owner',) if 'current_user' in q else
                       (self.conn.snapshot,) if 'fixer_generated_snapshot' in q else ({'reserved':True},))
    def fetchone(self): return self.result


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setenv('AGENT_S3_PUBLIC_BASE_URL', 'https://owned.example')
    monkeypatch.setattr('agent.visual_writer_prepare._own_media_url',lambda u: isinstance(u,str) and u.startswith('https://owned.example/'))
    conn=Conn(); p=owner.ForwardMediaOwnerPersistence(conn,'owner',None)
    data=image_bytes(); snap=trusted(); c=candidate(snap,data)
    def read(url): conn.events.append('read'); return data
    return p,c,snap,read


def run(lane, **kwargs):
    p,c,s,read=lane
    return guard.reserve_generated(p,str(uuid.uuid4()),c,s,history_visuals=kwargs.pop('history_visuals',[]),read_bytes=kwargs.pop('read_bytes',read))


def test_owner_computes_original_and_ends_read_transaction_before_remote_work(lane):
    result=run(lane)
    assert result['reserved'] is True
    p,c,_,_=lane
    assert p._conn.events == ['rollback','read','read']
    q,args=p._conn.calls[-1]
    assert 'fixer_reserve_generated' in q
    manifest=json.loads(args[-2])
    assert manifest['operation']=='same_object' and manifest['thumbnail_url'] is None
    assert manifest['source_asset_id']=='generated-astra:'+c['job_id']


@pytest.mark.parametrize('field,value',[
 ('photo_inventory_complete',False),('local_census_current',False),('eligible_photo_count',1),('history_complete',False),
 ('palette_verified',False),('copy_verified',False),('palette_revision','changed'),
 ('copy_revision','changed'),('inventory_revision','changed'),('palette_digest','changed'),
 ('copy_digest','changed'),('gym_id','other'),('local_date','2026-10-11'),('logical_post_id','other')])
def test_stale_owner_or_unknown_supply_holds(lane,field,value):
    lane[2][field]=value
    with pytest.raises(guard.ForwardMediaVerificationHold): run(lane)
    assert not lane[0]._conn.events


@pytest.mark.parametrize('field,value',[
 ('provider','other'),('model','other'),('source_type','photo'),('provider_response_id',''),
 ('job_id','bad'),('original_sha256','0'*64),('original_md5','0'*32),
 ('original_length',1),('original_phash','scene:phash64:0000000000000000'),
 ('storage_readback_sha256','0'*64),('width',129)])
def test_invalid_generation_or_byte_receipt_holds(lane,field,value):
    lane[1][field]=value
    with pytest.raises(guard.ForwardMediaVerificationHold): run(lane)
    assert not any('fixer_reserve_generated' in q for q,_ in lane[0]._conn.calls)


def test_mutable_original_holds(lane):
    reads=iter([image_bytes(),b'changed'])
    with pytest.raises(guard.ForwardMediaVerificationHold,match='changed during'):
        run(lane,read_bytes=lambda u:next(reads))


def test_historical_visual_hash_must_match_actual_bytes(lane):
    lane[0]._conn.snapshot['history']['rows']=[{'history_key':'old','published_binding_ref':'bound','visual_url':'https://owned.example/old','visual_sha256':None}]
    with pytest.raises(guard.ForwardMediaVerificationHold,match='historical visual bytes'):
        run(lane,history_visuals=[{'history_key':'old','published_binding_ref':'bound','visual_url':'https://owned.example/old','visual_sha256':'sha256:'+'0'*64}])


def test_history_url_reads_are_deduplicated_within_owner_run(lane):
    p,c,s,read=lane
    p._conn.snapshot['history']['rows']=[{'history_key':'old:'+str(i),'published_binding_ref':'bound:'+str(i),
        'visual_url':'https://owned.example/shared-old','visual_sha256':None} for i in range(1398)]
    calls=[]
    def reader(url): calls.append(url);return image_bytes()
    assert run(lane,read_bytes=reader)['reserved']
    assert calls==[c['original_url'],'https://owned.example/shared-old',c['original_url']]
    assert len(json.loads(p._conn.calls[-1][1][2]))==1398


def test_sql_issued_exact_history_proof_survives_missing_remote_object(lane):
    p,c,s,_=lane
    p._conn.snapshot['history']['rows']=[{'history_key':'sealed:old','published_binding_ref':'sealed tuple',
        'visual_url':'https://owned.example/deleted-old','visual_sha256':'sha256:'+c['original_sha256'],
        'phash':c['original_phash'],'history_proof_ref':'generated-history:sha256:'+'1'*64}]
    calls=[]
    def reader(url):
        calls.append(url)
        if url!=c['original_url']: raise RuntimeError('historical object unavailable')
        return image_bytes()
    assert run(lane,read_bytes=reader)['reserved']
    assert calls==[c['original_url'],c['original_url']]
    # Collision judgment is still enforced atomically by SQL, not this cache.
    assert json.loads(p._conn.calls[-1][1][2])[0]['phash']==c['original_phash']


def test_caller_cache_flag_cannot_skip_read_without_current_sql_proof(lane):
    p,c,s,_=lane
    item={'history_key':'old','published_binding_ref':'bound','visual_url':'https://owned.example/old','visual_sha256':None}
    p._conn.snapshot['history']['rows']=[item]
    forged={**item,'visual_sha256':'sha256:'+c['original_sha256'],'phash':c['original_phash'],
            'history_proof_ref':'generated-history:sha256:'+'1'*64}
    calls=[]
    def reader(url):calls.append(url);return image_bytes()
    assert run(lane,history_visuals=[forged],read_bytes=reader)['reserved']
    assert 'https://owned.example/old' in calls


def test_current_database_unknown_photo_supply_holds_before_remote_io(lane):
    lane[0]._conn.snapshot['photo_inventory_complete']=False
    with pytest.raises(guard.ForwardMediaVerificationHold,match='database depletion'):
        run(lane)
    assert not lane[0]._conn.events


@pytest.mark.parametrize('field', ['account', 'format'])
def test_exact_database_account_and_format_rechecked(lane,field):
    lane[2][field]='expected'
    lane[0]._conn.snapshot[field]='changed'
    with pytest.raises(guard.ForwardMediaVerificationHold,match='snapshot changed: '+field):
        run(lane)
    assert not lane[0]._conn.events


def test_changed_historical_binding_invalidates_cached_proof(lane):
    p,c,s,_=lane
    p._conn.snapshot['history']['rows']=[{'history_key':'old','published_binding_ref':'new binding',
        'visual_url':'https://owned.example/old','visual_sha256':None}]
    stale={'history_key':'old','published_binding_ref':'old binding','visual_url':'https://owned.example/old',
        'visual_sha256':'sha256:'+c['original_sha256'],'phash':c['original_phash'],
        'history_proof_ref':'generated-history:sha256:'+'1'*64}
    reads=[]
    def reader(url):reads.append(url);return image_bytes()
    with pytest.raises(guard.ForwardMediaVerificationHold,match='outside database inventory'):
        run(lane,history_visuals=[stale],read_bytes=reader)
    assert 'https://owned.example/old' in reads


def test_replay_allows_new_history_revision_but_leaves_sql_recheck_in_charge(lane):
    lane[2]['history_revision']='new census containing this reservation'
    assert run(lane)['reserved'] is True


def test_broad_connection_or_role_refused(lane):
    p,c,s,read=lane
    p._expected_owner='service_role'
    with pytest.raises(owner.OwnerPersistenceError): run(lane)
    with pytest.raises(guard.ForwardMediaVerificationHold):
        guard.reserve_generated(SimpleNamespace(),str(uuid.uuid4()),c,s,history_visuals=[],read_bytes=read)
