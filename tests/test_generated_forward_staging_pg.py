"""Run as a script: disposable PG17 generated-forward staging contract.

Positive end-to-end proof on synthetic facts only: minimal draft
forward-media/owner/gap SQL, synthetic gym/assets/calendar. An approved
eligible photo blocks generated fallback; a complete fresh zero-photo census
permits one pending generated gap bind/reservation while client approval is
preserved; a different-day repeat of the same generated visual is blocked.
No production credentials, remote providers, or persistent databases.
"""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import forward_media_guard as guard, forward_media_owner as owner
from tests.test_generated_owner_guard import candidate, trusted, image_bytes


def main():
 import psycopg
 pg=Path('/opt/homebrew/opt/postgresql@17/bin')
 assert shutil.disk_usage('/tmp').free>5*1024**3
 with tempfile.TemporaryDirectory(prefix='generated_staging_pg_',dir='/tmp') as tmp:
  root=Path(tmp);sock=root/'sock';sock.mkdir();data=root/'data';port=random.randint(41000,59000)
  subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
  subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),'-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
  try:
   def dsn(role='postgres'):return f'host={sock} port={port} dbname=postgres user={role}'
   admin=psycopg.connect(dsn(),autocommit=True)
   admin.execute("set statement_timeout='5s'")
   def sql(q,args=None):
    c=admin.execute(q,args);return c.fetchall() if c.description else None
   def lane(role):
    c=psycopg.connect(dsn(role));c.execute("set statement_timeout='5s'");c.commit();return c
   sql('create role anon;create role authenticated;create role service_role login;'
       'create table content_calendar(id uuid primary key,gym_id text,logical_post_id uuid,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
       'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);'
       'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);')
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql','DRAFT_fixer_generated_owner_20261007.sql','DRAFT_fixer_generated_gap_dispatch_20261007.sql'):
    sql((ROOT/'migrations'/name).read_text())
   sql('create role generated_owner login;grant fixer_forward_media_owner_20261006 to generated_owner;'
       'grant select,insert,update,delete on content_calendar to service_role;')
   baseline=str(uuid.uuid4())
   sql("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC policy',true,'complete_fleet_still_photo_history',null,'SYNTHETIC reconciliation','SYNTHETIC admin')")
   sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC policy',true,'[]','SYNTHETIC empty full fleet',0)",(baseline,))
   sql("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'",(baseline,))
   sql("insert into fixer_forward_media_claim_gate_20261006 values('gym',true)")
   today=sql('select current_date')[0][0];day=today+timedelta(days=1)
   service=lane('service_role');conn=lane('generated_owner')
   refs=('SYNTHETIC approved words','client-source:sha256:'+'a'*64,'sha256:'+'b'*64,'b'*64,'brand-colors:sha256:'+'c'*64)
   def dispatch(gym='gym',date_=day):
    request=str(uuid.uuid4())
    result=service.execute('select fixer_generated_gap_dispatch_20261007(%s,%s,%s,%s,%s)',
                           (request,gym,date_,'instagram','feed')).fetchone()[0]
    service.commit();return result
   def bind(q,row=None):
    logical=str(uuid.uuid4());group='vg_generated_'+uuid.UUID(logical).hex
    a=(q['request_id'],row or str(uuid.uuid4()),logical,group,*refs)
    out=conn.execute('select fixer_generated_gap_bind_20261007('+','.join(['%s']*9)+')',a).fetchone()[0]
    conn.commit();return a,out
   # 1) An approved eligible photo blocks generated fallback: bind is denied
   # and no placeholder calendar row is created.
   photoq=dispatch()
   pixels=image_bytes();fp=hashlib.md5(pixels).hexdigest()
   proof=dict(verdict='clean',provider='SYNTHETIC scanner',content_hash=fp,asset_id='photo',gym_id='gym',people_detected=False,observed_at='2026-10-07T00:00:00Z',sha256=hashlib.sha256(pixels).hexdigest())
   sql("insert into media_source values('source','gym','gym_drive','folder',true,'ready',now())")
   sql("insert into media_asset values('photo','source','gym',%s,null,'photo',true,false,'approved','clean',%s,'SYNTHETIC scanner',now(),%s::jsonb,false,999)",(fp,fp,json.dumps(proof)))
   try:bind(photoq)
   except psycopg.Error as e:assert 'depletion or sealed' in str(e),str(e)
   else:raise AssertionError('generated fallback bind unexpectedly accepted with eligible photo')
   conn.rollback()
   assert sql('select count(*) from content_calendar')[0][0]==0
   # 2) Complete fresh zero-photo census permits exactly one pending gap bind.
   sql("delete from media_asset;delete from media_source")
   q=dispatch(date_=today+timedelta(days=2));a,out=bind(q)
   assert out['bound'] and not out['replayed']
   r=sql('select status,variant_status,image_url,caption from content_calendar where id=%s',(a[1],))[0]
   assert r==('pending','active',None,refs[0])
   # Client approval is preserved: bind creates a pending row and never approves.
   conn.execute('select fixer_still_cutover_control_20261007(true,%s)',('SYNTHETIC staging fixture cutover',));conn.commit()
   persistence=owner.ForwardMediaOwnerPersistence(conn,'generated_owner',None)
   snap=trusted(guard.generated_snapshot(persistence,a[1]));conn.rollback()
   assert snap['photo_inventory_complete'] and snap['eligible_photo_count']==0 and snap['history_complete']
   original=candidate(snap,pixels)
   with patch('agent.visual_writer_prepare._own_media_url',lambda u:isinstance(u,str) and u.startswith('https://owned.example/')):
    reserved=guard.reserve_generated(persistence,a[1],original,snap,history_visuals=[],read_bytes=lambda u:pixels);conn.commit()
    assert reserved['reserved'] and not reserved['replayed']
    # Reservation binds the original but still does not approve for the client.
    r=sql('select status,image_url,source_media_asset_id from content_calendar where id=%s',(a[1],))[0]
    assert r[0]=='pending' and r[1]=='https://owned.example/generated.png' and r[2]=='generated-astra:'+original['job_id']
    # 3) The same generated visual on a different day is a blocked repeat.
    q2=dispatch(date_=today+timedelta(days=3));a2,out2=bind(q2)
    assert out2['bound']
    fresh=trusted(guard.generated_snapshot(persistence,a2[1]));conn.rollback()
    history=[{**h,'visual_sha256':h.get('visual_sha256') or 'sha256:'+hashlib.sha256(pixels).hexdigest()} for h in fresh['history']['rows']]
    repeat=candidate(fresh,pixels)
    try:
     guard.reserve_generated(persistence,a2[1],repeat,fresh,history_visuals=history,read_bytes=lambda u:pixels)
    except Exception as e:
     assert 'repeated historical generated visual' in str(e) or 'already reserved' in str(e),str(e)
    else:raise AssertionError('different-day generated visual repeat unexpectedly reserved')
    conn.rollback()
    assert sql('select count(*) from fixer_generated_reservation_20261007')[0][0]==1
    assert sql('select status,image_url from content_calendar where id=%s',(a2[1],))[0]==('pending',None)
   service.close();conn.close();admin.close()
   print('PASS: PG17 staging contract - approved eligible photo blocks generated fallback, fresh zero-photo census permits one pending bind/reservation with client approval preserved, different-day visual repeat blocked')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)

if __name__=='__main__':main()
