"""Generated owner local tests; no provider/storage/production access."""
import hashlib
import io
import json
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image

from agent import forward_media_guard as guard, forward_media_owner as owner, visual_scene


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
        'photo_inventory_complete':True,'eligible_photo_count':0,'history_complete':True,
        'copy_verified':True,'palette_verified':True,**(snapshot or {})}


class Conn:
    autocommit = False
    def __init__(self): self.calls = []; self.events = []
    def rollback(self): self.events.append('rollback')
    def cursor(self): return Cursor(self)


class Cursor:
    def __init__(self, conn): self.conn=conn
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,q,args=None):
        self.conn.calls.append((q,args))
        self.result = ('owner',) if 'current_user' in q else ({'reserved':True},)
    def fetchone(self): return self.result


@pytest.fixture
def lane(monkeypatch):
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
    manifest=json.loads(args[-1])
    assert manifest['operation']=='same_object' and manifest['thumbnail_url'] is None
    assert manifest['source_asset_id']=='generated-astra:'+c['job_id']


@pytest.mark.parametrize('field,value',[
 ('photo_inventory_complete',False),('eligible_photo_count',1),('history_complete',False),
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
    with pytest.raises(guard.ForwardMediaVerificationHold,match='historical visual bytes'):
        run(lane,history_visuals=[{'visual_url':'https://owned.example/old','visual_sha256':'sha256:'+'0'*64}])


def test_replay_allows_new_history_revision_but_leaves_sql_recheck_in_charge(lane):
    lane[2]['history_revision']='new census containing this reservation'
    assert run(lane)['reserved'] is True


def test_broad_connection_or_role_refused(lane):
    p,c,s,read=lane
    p._expected_owner='service_role'
    with pytest.raises(owner.OwnerPersistenceError): run(lane)
    with pytest.raises(guard.ForwardMediaVerificationHold):
        guard.reserve_generated(SimpleNamespace(),str(uuid.uuid4()),c,s,history_visuals=[],read_bytes=read)
