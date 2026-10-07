"""Run as a script for real disposable PG17 generated reservation/claim proof.

Synthetic provider receipts, palettes, copy, corpus and bytes only. No production
credential, remote provider, or persistent database is touched.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import forward_media_guard as guard, forward_media_owner as owner, forward_media_prepare as prepare
from tests.test_generated_owner_guard import candidate, trusted, image_bytes


def main():
 import psycopg
 pg=Path('/opt/homebrew/opt/postgresql@17/bin')
 assert shutil.disk_usage('/tmp').free>5*1024**3
 with tempfile.TemporaryDirectory(prefix='generated_owner_pg_',dir='/tmp') as tmp:
  root=Path(tmp);sock=root/'sock';sock.mkdir();data=root/'data';port=random.randint(41000,59000)
  subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
  subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),'-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
  try:
   def dsn(role='postgres'): return f'host={sock} port={port} dbname=postgres user={role}'
   admin=psycopg.connect(dsn(),autocommit=True)
   admin.execute("set statement_timeout='5s'")
   def sql(q,args=None):
    c=admin.execute(q,args);return c.fetchall() if c.description else None
   def lane(role):
    c=psycopg.connect(dsn(role));c.execute("set statement_timeout='5s'");c.commit();return c
   def denied(fn,reason=None):
    try:fn();raise AssertionError('unsafe generated authority unexpectedly accepted')
    except (psycopg.Error,guard.ForwardMediaVerificationHold) as e:
     if reason:assert reason in str(e),str(e)
   sql('create role anon;create role authenticated;create role service_role login;'
       'create table content_calendar(id uuid primary key,gym_id text,logical_post_id uuid,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
       'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);'
       'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);')
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql','DRAFT_fixer_generated_owner_20261007.sql'):
    sql((ROOT/'migrations'/name).read_text())
   sql('create role generated_owner login;grant fixer_forward_media_owner_20261006 to generated_owner;'
       'alter role fixer_forward_media_attester_20261006 login;'
       'grant select,insert,update,delete on content_calendar to service_role;')
   baseline=str(uuid.uuid4())
   sql("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC policy',true,'complete_fleet_still_photo_history',null,'SYNTHETIC reconciliation','SYNTHETIC admin')")
   sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC policy',true,'[]','SYNTHETIC empty full fleet',0)",(baseline,))
   sql("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'",(baseline,))
   sql("insert into fixer_forward_media_claim_gate_20261006 values('gym',true),('other',true)")
   logical=str(uuid.uuid4())
   def row(day='2026-10-10',gym='gym',fmt='feed',caption='SYNTHETIC approved copy'):
    rid=str(uuid.uuid4())
    sql("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) values(%s,%s,%s,%s,'instagram',%s,'approved','active','vg_group',%s)",(rid,gym,logical,day,fmt,caption));return rid
   conn=lane('generated_owner');p=owner.ForwardMediaOwnerPersistence(conn,'generated_owner',None)
   pixels=image_bytes();rid=row()
   snap=trusted(guard.generated_snapshot(p,rid));conn.rollback()
   c=candidate(snap,pixels)
   with patch('agent.visual_writer_prepare._own_media_url',lambda u: isinstance(u,str) and u.startswith('https://owned.example/')):
    def reserve(rid,candidate_=c,visuals=None,original=pixels):
     fresh=trusted(guard.generated_snapshot(p,rid));conn.rollback()
     out=guard.reserve_generated(p,rid,candidate_,fresh,history_visuals=visuals or [],read_bytes=lambda u:original if u==candidate_['original_url'] else pixels);conn.commit();return out
    result=reserve(rid);assert not result['replayed']
    assert reserve(rid)['replayed']
    assert sql('select count(*) from fixer_generated_reservation_20261007')[0][0]==1
    assert sql("select source_media_url=image_url and thumbnail_url is null from content_calendar where id=%s",(rid,))[0][0]
    sibling=row(fmt='story');assert reserve(sibling)['replayed']
    # A retry cannot bind old bytes to another date, tenant or logical copy.
    for bad in (row(day='2026-10-11'),row(gym='other'),row(caption='different copy')):
     denied(lambda:reserve(bad));conn.rollback()
    bad=dict(c);bad['provider_response_id']='different response'
    denied(lambda:reserve(rid,bad),'identity conflict');conn.rollback()
    # New ready photo with used_count=999 but no durable use blocks fallback.
    sql("insert into media_source values('source','gym','gym_drive','folder',true,'ready',now())")
    fp=hashlib.md5(pixels).hexdigest()
    proof={'verdict':'clean','provider':'SYNTHETIC scanner','content_hash':fp,'asset_id':'photo','gym_id':'gym','people_detected':False,'observed_at':'2026-10-07T00:00:00Z','sha256':hashlib.sha256(pixels).hexdigest()}
    sql("insert into media_asset values('photo','source','gym',%s,null,'photo',true,false,'approved','clean',%s,'SYNTHETIC scanner',now(),%s::jsonb,false,999)",(fp,fp,json.dumps(proof)))
    assert guard.generated_snapshot(p,rid)['eligible_photo_count']==1;conn.rollback()
    denied(lambda:reserve(rid),'fresh verified');conn.rollback()
    sql("delete from media_asset where id='photo';delete from media_source where id='source'")
    # Original bytes reserve permanently even after the anchor calendar is gone.
    later=row(day='2026-10-12');fresh=trusted(guard.generated_snapshot(p,later));conn.rollback()
    other=candidate(fresh,pixels)
    history=guard.generated_snapshot(p,later)['history']['rows'];conn.rollback()
    history=[{**h,'visual_sha256':'sha256:'+hashlib.sha256(pixels).hexdigest()} for h in history]
    denied(lambda:reserve(later,other,history),'repeated historical');conn.rollback()
    # Different original bytes encoding the same visual also fail across gyms.
    import io
    from PIL import Image
    out=io.BytesIO();Image.open(io.BytesIO(pixels)).save(out,format='PNG',compress_level=0)
    reencoded=out.getvalue();assert reencoded!=pixels
    other_row=row(gym='other');fresh=trusted(guard.generated_snapshot(p,other_row));conn.rollback()
    near=candidate(fresh,reencoded);near['original_url']='https://owned.example/reencoded.png'
    history=guard.generated_snapshot(p,other_row)['history']['rows'];conn.rollback()
    history=[{**h,'visual_sha256':'sha256:'+hashlib.sha256(pixels).hexdigest()} for h in history]
    assert near['original_phash']==c['original_phash'] and near['original_md5']!=c['original_md5']
    denied(lambda:reserve(other_row,near,history,reencoded),'repeated historical');conn.rollback()
    # A published unknown row invalidates all current generated provenance.
    unknown=row(day='2026-10-09');sql("update content_calendar set status='published',published_at=now(),image_url='https://owned.example/unknown' where id=%s",(unknown,))
    # Missing old source IDs/URLs do not block the complete delivered-visual
    # inventory. Their exact delivered bytes must still be observed before use.
    assert guard.generated_snapshot(p,rid)['history_complete'];conn.rollback()
    denied(lambda:reserve(rid),'perceptual identity unresolved');conn.rollback()
    sql('delete from content_calendar where id=%s',(unknown,))
    # Two independently credentialed attester/claim transactions preserve the
    # existing original-lineage/fingerprint path; no alternate provider bypass.
    revision=sql('select fixer_forward_media_attestation_request_20261006(%s)',(rid,))[0][0]['revision']
    receipt=guard.attest(rid,revision,connection_factory=lambda:lane(guard.ROLE),
      original_verifier=lambda snap,source:source==pixels,read_bytes=lambda u:pixels)
    token=str(uuid.uuid4())
    sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",(token,rid))
    service=lane('service_role')
    assert service.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',(rid,token,receipt['evidence_id'],revision)).fetchone()[0] is True
    service.commit()
    assert service.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',(rid,token,receipt['evidence_id'],revision)).fetchone()[0] is True
    service.commit();service.close()
    assert reserve(sibling)['replayed']
    # Stale contenders cannot write inventory while graph proof is uncommitted.
    lock=lane('generated_owner');lock.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',('fixer_forward_graph_20261006',))
    denied(lambda:sql("insert into media_source values('racing','gym','gym_drive','folder',true,'ready',now())"),'authority busy')
    lock.rollback();lock.close()
    # Raw producer paths and generated table writes stay refused.
    service=lane('service_role')
    denied(lambda:service.execute('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb)',(rid,json.dumps(c),'[]',json.dumps(result['manifest']))),'permission denied')
    service.rollback()
    denied(lambda:service.execute('insert into fixer_generated_reservation_20261007(job_id,calendar_row_id,group_key,candidate_json,manifest_json,receipt_ref) values(%s,%s,%s,%s::jsonb,%s::jsonb,%s)',(str(uuid.uuid4()),rid,'group','{}','{}','fake')),'permission denied')
    service.rollback();service.close()
    # Hold unknown history at the actual send boundary even after lineage.
    unknown=row(day='2026-10-08');sql("update content_calendar set status='published',image_url='https://owned.example/unknown' where id=%s",(unknown,))
    service=lane('service_role')
    denied(lambda:service.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',(rid,token,receipt['evidence_id'],revision)),'perceptual identity unresolved')
    service.rollback();service.close()
    # Source-null historic photos are safe to compare as delivered visuals;
    # they do not require an impossible original-source backfill for fresh art.
    from agent.visual_scene import scene_fingerprint, classify_scene
    def noise(seed):
     rng=random.Random(seed);im=Image.frombytes('RGB',(128,128),rng.randbytes(128*128*3))
     buf=io.BytesIO();im.save(buf,format='PNG');return buf.getvalue()
    old_bytes=noise(1);fresh_bytes=noise(2)
    assert classify_scene(scene_fingerprint(fresh_bytes),[scene_fingerprint(old_bytes),c['original_phash']]).kind=='distinct'
    future=row(day='2026-10-15',gym='other')
    fresh=trusted(guard.generated_snapshot(p,future));conn.rollback()
    fresh_candidate=candidate(fresh,fresh_bytes);fresh_candidate['original_url']='https://owned.example/fresh.png'
    history=guard.generated_snapshot(p,future)['history']['rows'];conn.rollback()
    def read_historical(url):
     return fresh_bytes if url==fresh_candidate['original_url'] else old_bytes if url=='https://owned.example/unknown' else pixels
    visuals=[{**h,'visual_sha256':'sha256:'+hashlib.sha256(read_historical(h['visual_url'])).hexdigest()} for h in history]
    assert guard.reserve_generated(p,future,fresh_candidate,fresh,history_visuals=visuals,read_bytes=read_historical)['reserved']
    conn.commit()
    assert sql('select source_media_asset_id is null and source_media_url is null from content_calendar where id=%s',(unknown,))[0][0]
   conn.close();admin.close()
   print('PASS: PG17 generated owner atomic reservation, replay, siblings, different day/gym/copy, original+pHash collision, used_count ambiguity, current photo-first, unread history hold, final attestation/claim/replay, inventory serialization, role denial and fresh generation against source-null delivered history')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)


if __name__=='__main__':main()
