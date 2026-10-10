"""Disposable PG17 direct SQL historical exclusion fence acceptance.

All bytes, Drive metadata, signing keys, corpus judgments and roles SYNTHETIC.
No production connection, credential provisioning or provider I/O.
"""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import forward_media_owner as owner, forward_media_guard as guard, forward_media_attester as attester, media_host, forward_media_owner_worker as worker
from agent.forward_media_source_history import SourceHistoryStore
from agent.forward_media_source_verifier import verify_source
from agent.forward_media_photo_certificate import IndependentPhotoAuditor,digest,PhotoCertificateHold
from agent.forward_media_owner_photo_prepare import prepare_remote_photo,stage_prepared_photo,reconcile_owner_photo
from tests.test_forward_media_source_verifier import Drive,Hosted,FILE,FOLDER,URL
from tests.test_forward_media_owner_two_phase_pg import png
from tests.test_forward_media_photo_certificate import fixtures


def pg17_bin():
 candidates=[]
 configured=os.environ.get('PG17_BIN')
 if configured:candidates.append(Path(configured))
 for candidate in (Path('/opt/homebrew/opt/postgresql@17/bin'),Path('/usr/local/opt/postgresql@17/bin')):
  candidates.append(candidate)
 candidates.extend(Path('/usr/lib/postgresql').glob('17/bin'))
 pg_config=shutil.which('pg_config')
 if pg_config:
  result=subprocess.run([pg_config,'--bindir'],capture_output=True,text=True,check=False)
  if result.returncode==0:candidates.append(Path(result.stdout.strip()))
 initdb=shutil.which('initdb')
 if initdb:candidates.append(Path(initdb).resolve().parent)
 for candidate in candidates:
  if (candidate/'initdb').is_file() and (candidate/'pg_ctl').is_file() and (candidate/'postgres').is_file():
   version=subprocess.run([str(candidate/'postgres'),'--version'],capture_output=True,text=True,check=False)
   if version.returncode==0 and ' 17.' in version.stdout:return candidate
 raise RuntimeError('PostgreSQL 17 binaries required (set PG17_BIN or install PostgreSQL 17)')


def main():
 import psycopg
 pg=pg17_bin()
 assert shutil.disk_usage('/tmp').free>5*1024**3
 with tempfile.TemporaryDirectory(prefix='owner_photo_pg_',dir='/tmp') as tmp:
  root=Path(tmp);sock=root/'sock';sock.mkdir();data=root/'data';port=random.randint(41000,59000)
  subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
  subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),'-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
  try:
   def dsn(role='postgres'):return f'host={sock} port={port} dbname=postgres user={role}'
   admin=psycopg.connect(dsn(),autocommit=True)
   admin.execute("set statement_timeout='5s'")
   def sql(q,args=None):
    with admin.cursor() as c:
     c.execute(q,args);return c.fetchall() if c.description else None
   def lane(role):
    c=psycopg.connect(dsn());c.execute('set role '+role);c.commit();return c
   def denied(fn,reason=None):
    try:fn();raise AssertionError('unsafe grant unexpectedly accepted')
    except psycopg.Error as e:
     if reason:assert reason in str(e),str(e)
   sql('create role anon;create role authenticated;create role service_role;'
       'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
       'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
       'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
   sql("alter table media_asset add column eligible boolean default true,add column excluded_by_coach boolean default false,add column review_status text default 'approved',add column moderation_status text default 'clean',add column review_content_hash text,add column reviewed_by text default 'SYNTHETIC reviewer',add column reviewed_at timestamptz default now(),add column moderation_json jsonb,add column people_detected boolean default false,add column used_count integer default 0")
   def approved_asset(asset_id,data_bytes,tenant='gym'):
    md5=hashlib.md5(data_bytes).hexdigest()
    proof={'verdict':'clean','provider':'SYNTHETIC scanner','content_hash':md5,'asset_id':asset_id,'gym_id':tenant,'people_detected':False,'observed_at':'2026-10-07T00:00:00Z','sha256':hashlib.sha256(data_bytes).hexdigest()}
    sql('update media_asset set content_hash=%s,review_content_hash=%s,moderation_json=%s::jsonb where id=%s',(md5,md5,json.dumps(proof),asset_id))
   stack=('logical_post_id_20261004.sql','DRAFT_fixer_forward_media_claim_20261006.sql',
    'DRAFT_fixer_forward_visual_index_20261008.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
    'DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_owner_transport_20261007.sql',
    'DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql',
    'DRAFT_fixer_generated_owner_20261007.sql','DRAFT_fixer_forward_schedule_reservation_20261008.sql',
    'DRAFT_fixer_forward_schedule_stage_20261008.sql','DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql',
    'DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql')
   for name in stack:sql((ROOT/'migrations'/name).read_text())
   catalog=sql("select oid,proowner,proacl,prosecdef,proconfig,pg_get_functiondef(oid) from pg_proc where pronamespace='public'::regnamespace order by oid")
   sql((ROOT/'migrations/DRAFT_fixer_photo_historical_clearance_20261008.sql').read_text())
   after={row[0]:row for row in sql("select oid,proowner,proacl,prosecdef,proconfig,pg_get_functiondef(oid) from pg_proc where pronamespace='public'::regnamespace order by oid")}
   for row in catalog:assert row[:5]==after[row[0]][:5],row
   sql('create role photo_owner login;grant fixer_forward_media_owner_20261006 to photo_owner;'
       'create role photo_auditor login;grant fixer_forward_media_photo_auditor_20261007 to photo_auditor;'
       'grant select,insert,update,delete on content_calendar to service_role;')
   for name in list(os.environ):
    if name in owner.forbidden_credential_names(os.environ):os.environ.pop(name)
   os.environ['FORWARD_MEDIA_OWNER_DSN']=dsn('photo_owner')
   os.environ['FORWARD_MEDIA_OWNER_ROLE']='photo_owner'
   source_bytes=png('blue');drive=Drive();drive.data=source_bytes
   drive.meta['size']=str(len(source_bytes));drive.meta['md5Checksum']=hashlib.md5(source_bytes).hexdigest()
   hosted=Hosted(source_bytes)
   sql('insert into media_source values(%s,%s,%s,%s,true)',('source','gym','gym_drive',FOLDER))
   sql('insert into media_asset(id,source_id,gym_id) values(%s,%s,%s)',(FILE,'source','gym'))
   approved_asset(FILE,source_bytes)
   def new_row(day='2026-10-10',fmt='feed',account='instagram'):
    rid=str(uuid.uuid4())
    sql("insert into content_calendar(id,gym_id,post_date,account,format,status,variant_status,visual_group_key,source_media_asset_id,source_media_url,image_url,caption) values(%s,'gym',%s,%s,%s,'approved','active','group',%s,%s,%s,'SYNTHETIC creative')",(rid,day,account,fmt,FILE,URL,URL))
    return rid
   rid=new_row();revision=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(rid,))[0][0]
   conn=psycopg.connect(dsn('photo_owner'));p=owner.ForwardMediaOwnerPersistence(conn,'photo_owner',hosted);store=SourceHistoryStore(p)
   current=store.snapshot(rid,revision);verified_source=verify_source(current,drive,hosted)
   store.stage_source(verified_source);conn.commit()
   recipe=attester.make_still_recipe('identity')
   candidate={'calendar_row_id':rid,'tenant_id':'gym','group_key':'group','post_date':'2026-10-10','source_asset_id':FILE,'source_url':URL,'image_url':URL,
     'source_fingerprint':verified_source.original.source_fingerprint,'source_sha256':verified_source.evidence['source_sha256'],'source_length':len(source_bytes),'source_receipt_ref':verified_source.receipt_ref,
     'image_fingerprint':verified_source.original.source_fingerprint,'image_sha256':verified_source.evidence['source_sha256'],'image_length':len(source_bytes),
     'render_recipe_digest':digest(recipe),'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(rid,))[0][0]}
   _,key,_,private=fixtures(candidate=candidate)
   sql("insert into fixer_forward_media_photo_policy_20261007 values(%s,true,'complete_fleet_still_photo_history','SYNTHETIC reviewed video ruling','SYNTHETIC cutover','SYNTHETIC admin')",(key['policy_id'],))
   sql('insert into fixer_forward_media_photo_key_20261007 values(%s,%s,%s,%s,%s,true)',(key['key_id'],key['auditor_id'],'photo_auditor',key['policy_id'],key['public_key_hex']))
   baseline=str(uuid.uuid4())
   history={'history_key':'SYNTHETIC full historical photo','resolved':True,'media_kind':'still_photo','visual_sha256':digest('SYNTHETIC different historic visual'),'published_binding_ref':'SYNTHETIC preserved complete fleet history'}
   sql('insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,%s,true,%s::jsonb,%s,1)',(baseline,key['policy_id'],json.dumps([history]),'SYNTHETIC complete corpus'))
   sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s where singleton',(baseline,))
   packet,_,_,_=fixtures(candidate=candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0],private=private)
   auditor_conn=psycopg.connect(dsn('photo_auditor'));IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(packet);auditor_conn.commit()
   prepared=prepare_remote_photo(current,drive_reader=drive,hosted_reader=hosted,recipe=recipe,auditor=IndependentPhotoAuditor(conn,'photo_owner'),audit_id=packet['payload']['audit_id']);conn.rollback()
   # Positive zero-exclusion control: an actual direct certificate append has
   # already passed; owner attempt reservation remains available when enabled.
   sql("update fixer_forward_media_photo_state_20261007 set enabled=true,routes_reconciled_ref='SYNTHETIC reconciled'")
   audit=packet['payload']['audit_id']
   sql('set role photo_owner')
   assert sql('select fixer_owner_photo_reserve_20261007(%s,%s)',(audit,str(uuid.uuid4())))[0][0]
   sql('reset role')
   stage_prepared_photo(p,prepared);conn.commit()
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==1
   # Otherwise valid reviewed-video exclusion accepted by the frozen snapshot.
   exclusion={'history_key':'SYNTHETIC excluded video','media_kind':'reviewed_video_scope_exclusion',
    'published_binding_ref':'SYNTHETIC verified video publication'}
   policy=key['policy_id']; excluded_baseline=str(uuid.uuid4())
   sql('insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count,excluded_rows_json,excluded_video_manifest_ref) values(%s,%s,true,%s::jsonb,%s,2,%s::jsonb,%s)',(excluded_baseline,policy,json.dumps([history]),'SYNTHETIC corpus',json.dumps([exclusion]),'SYNTHETIC reviewed video manifest'))
   sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=generation+1 where singleton',(excluded_baseline,))
   snap=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0]
   assert snap['policy_approved'] and snap['scope_complete'],snap
   assert sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0]['excluded_rows_count']==1
   # Use an already recorded certificate: replay/attempt/reservation cannot
   # regain authority by avoiding a new Python verification call.
   excluded_packet,_,_,_=fixtures(candidate=candidate,snapshot=snap,private=private)
   from agent.forward_media_photo_certificate import canonical
   calls=[('select fixer_forward_media_photo_record_20261007(%s,%s,%s)',
     (canonical(excluded_packet['payload']),excluded_packet['signature_hex'],'photo-audit:sha256:'+hashlib.sha256((canonical(excluded_packet['payload'])+'\n'+excluded_packet['signature_hex']).encode()).hexdigest())),
    ('select fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',(audit,'{}','{}')),
    ('select fixer_prepare_owner_staged_photo_20261008(%s,%s::jsonb,%s::jsonb)',(audit,'{}','{}')),
    ('select fixer_owner_photo_reserve_20261007(%s,%s)',(audit,str(uuid.uuid4()))),
    ('select fixer_reconcile_owner_photo_20261007(%s)',(audit,)),
    ('select fixer_forward_media_provenance_lookup_20261006(%s)',(rid,)),
    ('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[])',(rid,str(uuid.uuid4()),revision,[]))]
   original_record=next(row[5] for row in catalog if 'CREATE OR REPLACE FUNCTION public.fixer_forward_media_photo_record_20261007(' in row[5])
   old_conn=psycopg.connect(dsn())
   try:
    old_conn.execute(original_record)
    old_conn.execute('set role photo_auditor')
    assert old_conn.execute(calls[0][0],calls[0][1]).fetchone()[0]
    old_conn.rollback()
   finally:old_conn.close()
   before=sql('select count(*) from fixer_forward_media_photo_certificate_20261007')[0][0]
   for query,args in calls:
    try:sql(query,args);raise AssertionError('direct SQL exclusion bypass: '+query)
    except psycopg.Error as exc:
     assert exc.sqlstate=='23514' and 'frame-aware certificate schema' in str(exc),str(exc)
   assert sql('select count(*) from fixer_forward_media_photo_certificate_20261007')[0][0]==before
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==1
   assert sql('select count(*) from forward_schedule_reservation')[0][0]==0
   assert not sql("select has_function_privilege('service_role','fixer_forward_media_photo_record_20261007(text,text,text)','execute')")[0][0]
   assert not sql("select has_function_privilege('anon','fixer_assert_photo_no_exclusions_20261008()','execute')")[0][0]
   # Failed direct call aborts its transaction without altering baseline state.
   generation_before=sql('select generation from fixer_forward_media_photo_state_20261007')[0][0]
   transaction=psycopg.connect(dsn())
   try:
    transaction.execute('update fixer_forward_media_photo_state_20261007 set generation=generation+1')
    try:transaction.execute(calls[3][0],calls[3][1]);raise AssertionError('unexpected authority')
    except psycopg.Error:transaction.rollback()
   finally:transaction.close()
   assert sql('select generation from fixer_forward_media_photo_state_20261007')[0][0]==generation_before
   # Reapplying retains OID/ACL and does not duplicate guards.
   sql((ROOT/'migrations/DRAFT_fixer_photo_historical_clearance_20261008.sql').read_text())
   print('PASS: PG17 composed direct SQL exclusions, certificate positive control, OID/ACL preservation, rollback and reapply')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)


def test_historical_sql_guard_pg():main()

if __name__=='__main__':main()
