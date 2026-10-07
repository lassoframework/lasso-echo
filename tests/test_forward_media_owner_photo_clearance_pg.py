"""Disposable PG17 owner photo grant + replay/sibling/negative epoch proof.

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


def main():
 import psycopg
 pg=Path('/opt/homebrew/opt/postgresql@17/bin')
 assert shutil.disk_usage('/tmp').free>5*1024**3
 with tempfile.TemporaryDirectory(prefix='owner_photo_pg_',dir='/tmp') as tmp:
  root=Path(tmp);sock=root/'sock';sock.mkdir();data=root/'data';port=random.randint(41000,59000)
  subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
  subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),'-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
  try:
   def dsn(role='postgres'):return f'host={sock} port={port} dbname=postgres user={role}'
   admin=psycopg.connect(dsn(),autocommit=True)
   def sql(q,args=None):
    with admin.cursor() as c:
     c.execute(q,args);return c.fetchall() if c.description else None
   def lane(role):
    c=psycopg.connect(dsn());c.execute('set role '+role);return c
   def denied(fn,reason=None):
    try:fn();raise AssertionError('unsafe grant unexpectedly accepted')
    except psycopg.Error as e:
     if reason:assert reason in str(e),str(e)
   sql('create role anon;create role authenticated;create role service_role;'
       'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
       'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
       'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
   sql("alter table media_asset add column eligible boolean default true,add column excluded_by_coach boolean default false,add column review_status text default 'approved',add column moderation_status text default 'clean',add column review_content_hash text,add column reviewed_by text default 'SYNTHETIC reviewer',add column reviewed_at timestamptz default now(),add column moderation_json jsonb,add column people_detected boolean default false,add column used_count integer default 0")
   def approved_asset(asset_id,data_bytes):
    md5=hashlib.md5(data_bytes).hexdigest()
    proof={'verdict':'clean','provider':'SYNTHETIC scanner','content_hash':md5,'asset_id':asset_id,'gym_id':'gym','people_detected':False,'observed_at':'2026-10-07T00:00:00Z','sha256':hashlib.sha256(data_bytes).hexdigest()}
    sql('update media_asset set content_hash=%s,review_content_hash=%s,moderation_json=%s::jsonb where id=%s',(md5,md5,json.dumps(proof),asset_id))
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql'):
    sql((ROOT/'migrations'/name).read_text())
   sql('create role photo_owner login;grant fixer_forward_media_owner_20261006 to photo_owner;'
       'create role photo_auditor login;grant fixer_forward_media_photo_auditor_20261007 to photo_auditor;'
       'grant select,update on content_calendar to service_role;')
   for name in list(os.environ):
    if owner._FORBIDDEN_ENV_NAME.search(name):os.environ.pop(name)
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
   sql("insert into fixer_forward_media_photo_policy_20261007 values(%s,true,'complete_fleet_still_photo_history',null,'SYNTHETIC cutover','SYNTHETIC admin')",(key['policy_id'],))
   sql('insert into fixer_forward_media_photo_key_20261007 values(%s,%s,%s,%s,%s,true)',(key['key_id'],key['auditor_id'],'photo_auditor',key['policy_id'],key['public_key_hex']))
   baseline=str(uuid.uuid4())
   history={'history_key':'SYNTHETIC full historical photo','resolved':True,'media_kind':'still_photo','visual_sha256':digest('SYNTHETIC different historic visual'),'published_binding_ref':'SYNTHETIC preserved complete fleet history'}
   sql('insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,%s,true,%s::jsonb,%s,1)',(baseline,key['policy_id'],json.dumps([history]),'SYNTHETIC complete corpus'))
   sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s where singleton',(baseline,))
   packet,_,_,_=fixtures(candidate=candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0],private=private)
   auditor_conn=psycopg.connect(dsn('photo_auditor'));IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(packet);auditor_conn.commit()
   owner_auditor=IndependentPhotoAuditor(conn,'photo_owner')
   prepared=prepare_remote_photo(current,drive_reader=drive,hosted_reader=hosted,recipe=recipe,auditor=owner_auditor,audit_id=packet['payload']['audit_id']);conn.rollback()
   assert sql('select enabled from fixer_forward_media_photo_state_20261007')[0][0] is False
   denied(lambda:stage_prepared_photo(p,prepared),'OFF');conn.rollback()
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==0
   sql("update fixer_forward_media_photo_state_20261007 set enabled=true,routes_reconciled_ref='SYNTHETIC all routes reconciled'")
   sql("update content_calendar set caption='tampered creative' where id=%s",(rid,))
   denied(lambda:stage_prepared_photo(p,prepared));conn.rollback()
   sql("update content_calendar set caption='SYNTHETIC creative' where id=%s",(rid,))
   # Independent different visual approved against the same frozen corpus;
   # concurrent owner preparation must wait, then reject that stale review after
   # the first candidate becomes a reserved visual. No pairwise blind spot.
   other_file=FILE+'Second';other_url=URL+'&second=1';other_bytes=png('green')
   sql('insert into media_asset(id,source_id,gym_id) values(%s,%s,%s)',(other_file,'source','gym'))
   approved_asset(other_file,other_bytes)
   other_rid=new_row();sql('update content_calendar set source_media_asset_id=%s,source_media_url=%s,image_url=%s where id=%s',(other_file,other_url,other_url,other_rid))
   class OtherDrive(Drive):
    def original_bytes(self,file_id):
     assert file_id==other_file;return self.data
   other_drive=OtherDrive();other_drive.data=other_bytes;other_drive.meta.update(id=other_file,size=str(len(other_bytes)),md5Checksum=hashlib.md5(other_bytes).hexdigest())
   other_revision=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(other_rid,))[0][0]
   other_current=store.snapshot(other_rid,other_revision);other_source=verify_source(other_current,other_drive,Hosted(other_bytes))
   store.stage_source(other_source);conn.commit()
   other_candidate={**candidate,'calendar_row_id':other_rid,'source_asset_id':other_file,'source_url':other_url,'image_url':other_url,
    'source_fingerprint':other_source.original.source_fingerprint,'source_sha256':other_source.evidence['source_sha256'],'source_length':len(other_bytes),'source_receipt_ref':other_source.receipt_ref,
    'image_fingerprint':other_source.original.source_fingerprint,'image_sha256':other_source.evidence['source_sha256'],'image_length':len(other_bytes),
    'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(other_rid,))[0][0]}
   other_packet,_,_,_=fixtures(candidate=other_candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0],private=private)
   IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(other_packet);auditor_conn.commit()
   other_prepared=prepare_remote_photo(other_current,drive_reader=other_drive,hosted_reader=Hosted(other_bytes),recipe=recipe,auditor=owner_auditor,audit_id=other_packet['payload']['audit_id']);conn.rollback()
   sql("update media_asset set review_status='pending_review',moderation_status='pending' where id=%s",(FILE,))
   denied(lambda:stage_prepared_photo(p,prepared),'current exact same gym source');conn.rollback()
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==0
   sql("update media_asset set review_status='approved',moderation_status='clean' where id=%s",(FILE,))
   sql("update media_asset set moderation_json=jsonb_set(moderation_json,'{observed_at}',to_jsonb('malformed timestamp'::text)) where id=%s",(FILE,))
   assert sql('select fixer_owner_photo_source_ready_20261007(%s,%s)',(FILE,candidate['source_sha256']))[0][0] is False
   approved_asset(FILE,source_bytes)
   sql("update media_asset set moderation_json=jsonb_set(moderation_json,'{sha256}',to_jsonb(%s::text)) where id=%s",('0'*64,FILE))
   denied(lambda:stage_prepared_photo(p,prepared),'current exact same gym source');conn.rollback()
   approved_asset(FILE,source_bytes)
   sql("update content_calendar set media_not_ready_reason='SYNTHETIC preserve safety hold' where id=%s",(rid,))
   denied(lambda:stage_prepared_photo(p,prepared),'unsent canonical candidate');conn.rollback()
   assert sql('select media_not_ready_reason from content_calendar where id=%s',(rid,))[0][0]=='SYNTHETIC preserve safety hold'
   sql('update content_calendar set media_not_ready_reason=null where id=%s',(rid,))
   result=stage_prepared_photo(p,prepared)
   assert result['clearance']['decision']=='cleared_unused'
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==0
   def competing_owner():
    with psycopg.connect(dsn('photo_owner')) as other_conn:
     other_conn.execute("set statement_timeout='6s'")
     other_p=owner.ForwardMediaOwnerPersistence(other_conn,'photo_owner',Hosted(other_bytes))
     try:stage_prepared_photo(other_p,other_prepared);return 'unsafe second grant'
     except psycopg.errors.CheckViolation as exc:
      assert 'complete current independently reviewed' in str(exc),str(exc)
      other_conn.rollback();return 'stale pairwise corpus held'
   with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    waiting=pool.submit(competing_owner)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
     if sql("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select public.fixer_prepare_owner_photo%'")[0][0]:break
     time.sleep(.02)
    else:raise AssertionError('second owner did not wait behind first graph transaction')
    conn.commit()
    assert waiting.result(timeout=8)=='stale pairwise corpus held'
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==1
   assert sql('select count(*) from fixer_forward_media_render_manifest_20261006')[0][0]==1
   snapshot=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0]
   assert len(snapshot['rows'])==3 and all(h['resolved'] for h in snapshot['rows'])
   # A fresh real signature with dishonest exact-byte nonmatch judgments
   # cannot clear already reserved visuals; SQL has an independent byte fence.
   duplicate=new_row();dup_revision=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(duplicate,))[0][0]
   dup_current=store.snapshot(duplicate,dup_revision);dup_source=verify_source(dup_current,drive,hosted)
   store.stage_source(dup_source);conn.commit()
   dup_candidate={**candidate,'calendar_row_id':duplicate,'source_receipt_ref':dup_source.receipt_ref,
    'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(duplicate,))[0][0]}
   dup_packet,_,_,_=fixtures(candidate=dup_candidate,snapshot=snapshot,private=private)
   IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(dup_packet);auditor_conn.commit()
   dup_prepared=prepare_remote_photo(dup_current,drive_reader=drive,hosted_reader=hosted,recipe=recipe,auditor=owner_auditor,audit_id=dup_packet['payload']['audit_id']);conn.rollback()
   denied(lambda:stage_prepared_photo(p,dup_prepared),'already used cleared or reserved');conn.rollback()
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==1
   # Direct SQL cannot replace the signed deterministic recipe either.
   bad_manifest=prepared.manifest.row();bad_manifest['render_recipe']={'unsigned':'recipe'}
   with lane('photo_owner') as owner_lane:
    denied(lambda:owner_lane.execute('select fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',(packet['payload']['audit_id'],json.dumps(verified_source.original.row()),json.dumps(bad_manifest))),'exact certified')
   # Three siblings below share ONE exact source and ONE exact rendition. A
   # separately burned Story rendition without signed coverage remains held.
   changed_story=new_row(fmt='story')
   sql("update content_calendar set image_url='https://media.example.test/different-story-burn.png' where id=%s",(changed_story,))
   with lane('service_role') as service:
    denied(lambda:service.execute('select fixer_bind_forward_media_manifest_20261006(%s)',(changed_story,)),'matching immutable render manifest')
   with lane('photo_owner') as owner_lane:
    denied(lambda:owner_lane.execute("insert into fixer_forward_media_render_manifest_20261006(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,operation,render_recipe,render_evidence_ref) values(%s,'gym',%s,%s,%s,%s,'render',%s::jsonb,'unsigned-story-proof')",('sha256:'+'d'*64,FILE,'https://media.example.test/different-story-burn.png','md5:'+'d'*32,123,json.dumps(recipe))),'exact signed rendition reservation')
   # Simulate an unsigned rendition already present BEFORE this migration's
   # INSERT guard. Only the synthetic administrator can construct this state;
   # the final provenance boundary still refuses to inherit source clearance.
   sql('alter table fixer_forward_media_render_manifest_20261006 disable trigger certified_photo_rendition')
   try:
    sql("insert into fixer_forward_media_render_manifest_20261006(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,operation,render_recipe,render_evidence_ref) values(%s,'gym',%s,%s,%s,%s,'rehost',%s::jsonb,'SYNTHETIC pre-migration unsigned rendition')",('sha256:'+'d'*64,FILE,'https://media.example.test/different-story-burn.png',candidate['image_fingerprint'],len(source_bytes),json.dumps(recipe)))
   finally:
    sql('alter table fixer_forward_media_render_manifest_20261006 enable trigger certified_photo_rendition')
   with lane('service_role') as service:
    assert service.execute('select fixer_bind_forward_media_manifest_20261006(%s)',(changed_story,)).fetchone()[0]
   with lane(guard.ROLE) as verifier:
    denied(lambda:verifier.execute('select fixer_forward_media_provenance_lookup_20261006(%s)',(changed_story,)),'exact signed rendition reservation')
   # The old certificate cannot clear a second asset/visual by raw owner writes.
   with lane('photo_owner') as owner_lane:
    denied(lambda:owner_lane.execute("insert into fixer_forward_media_history_clearance_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref) values('gym',%s,%s,%s,%s,%s,'cleared_unused','invented-proof')",(FILE,URL,candidate['source_fingerprint'],len(source_bytes),verified_source.receipt_ref)),'certified owner reservation')
   for role in ('service_role','anon','authenticated','photo_auditor'):
    with lane(role) as wrong:
     denied(lambda:wrong.execute('select fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',(packet['payload']['audit_id'],json.dumps(verified_source.original.row()),json.dumps(prepared.manifest.row()))))
   sql("insert into fixer_forward_media_claim_gate_20261006 values('gym',true)")
   media_host.config.S3_PUBLIC_BASE_URL='https://media.example.test'
   def ready_claim(row_id,day='2026-10-10'):
    with lane('service_role') as service:assert service.execute('select fixer_bind_forward_media_manifest_20261006(%s)',(row_id,)).fetchone()[0]
    token=str(uuid.uuid4());sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=%s where id=%s",(token,day,row_id))
    rev=sql('select fixer_forward_media_attestation_request_20261006(%s)',(row_id,))[0][0]['revision']
    evidence=guard.attest(row_id,rev,connection_factory=lambda:lane(guard.ROLE),read_bytes=hosted.read,
      original_verifier=attester.make_original_verifier(lambda *a:verified_source.original.row(),expected_revision=rev))
    # Attestation writes do not change the outgoing calendar revision.
    eid=sql('select evidence_id from fixer_forward_media_lineage_20261006 where calendar_row_id=%s',(row_id,))[0][0]
    return row_id,token,eid,rev
   def claim(binding):
    with lane('service_role') as service:return service.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',binding).fetchone()[0]
   # Before ANY claim, an unsigned different day/group must fail at the
   # actual attester caller, so it cannot steal the source's first occupancy.
   for unsigned in (new_row(day='2026-10-11'),new_row()):
    if sql('select post_date::text from content_calendar where id=%s',(unsigned,))[0][0]=='2026-10-10':
     sql("update content_calendar set visual_group_key='unsigned-other-group' where id=%s",(unsigned,))
    try:ready_claim(unsigned,'2026-10-11');raise AssertionError('unsigned date/group attested')
    except guard.ForwardMediaVerificationHold as exc:
     assert 'signed tenant date and group' in str(exc.__cause__),str(exc.__cause__)
   with lane('service_role') as service:
    assert service.execute('select fixer_bind_forward_media_manifest_20261006(%s)',(rid,)).fetchone()[0]
   # Prepared adapter replay after the binder changes full row revision, and
   # fresh process reconciliation, preserve the ONE existing authority grant.
   assert stage_prepared_photo(p,prepared)['replayed'] is True;conn.commit()
   assert reconcile_owner_photo(p,packet['payload']['audit_id'])['replayed'] is True;conn.rollback()
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==1
   first=ready_claim(rid);second=ready_claim(new_row(fmt='story'));third=ready_claim(new_row(fmt='feed',account='facebook'))
   assert claim(first) is True and claim(first) is True
   # A safety downgrade that owns the asset row before a claim must serialize
   # first; the real claim then sees pending moderation instead of stale clean.
   safety_writer=psycopg.connect(dsn())
   safety_writer.execute("update media_asset set review_status='pending_review',moderation_status='pending' where id=%s",(FILE,))
   with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    waiting_claim=pool.submit(claim,first)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
     if sql("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select fixer_claim_forward_media%' ")[0][0]:break
     time.sleep(.02)
    else:raise AssertionError('claim did not wait behind current safety update')
    safety_writer.commit()
    denied(lambda:waiting_claim.result(timeout=8),'current approved byte-bound same-gym source')
   safety_writer.close()
   denied(lambda:claim(first),'current approved byte-bound same-gym source')
   sql("update media_asset set review_status='approved',moderation_status='clean',used_count=used_count+1 where id=%s",(FILE,))
   assert reconcile_owner_photo(p,packet['payload']['audit_id'])['replayed'] is True;conn.rollback()
   assert claim(second) is True and claim(third) is True
   assert claim(first) is True
   assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006')[0][0]==3
   fourth=ready_claim(new_row(fmt='story'))
   with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    good=pool.submit(claim,fourth);bad=pool.submit(ready_claim,new_row(day='2026-10-11'),'2026-10-11')
    assert good.result(timeout=8) is True
    try:bad.result(timeout=8);raise AssertionError('unsigned day attested concurrently')
    except guard.ForwardMediaVerificationHold as exc:
     assert 'signed tenant date and group' in str(exc.__cause__),str(exc.__cause__)
   assert claim(first) is True
   # A fresh independent certificate after prior sends has reviewed the now
   # reserved/published visuals. Real production run_once discovers it, commits
   # durable admission before remote readers, and atomically persists outcome.
   fresh_packet,_,_,_=fixtures(candidate=other_candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0],private=private)
   IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(fresh_packet);auditor_conn.commit()
   sql('insert into fixer_forward_media_observation_20261007(calendar_row_id,row_revision,observation_digest,tenant_id,gym_id,source_asset_id,source_exact_url,delivered_exact_url,calendar_snapshot,observation_json,digest_input) values(%s,%s,%s,\'gym\',\'gym\',%s,%s,%s,\'{}\'::jsonb,%s,\'SYNTHETIC producer recipe\')',(other_rid,other_revision,'a'*64,other_file,other_url,other_url,json.dumps({'recipe':recipe})))
   os.environ[worker.WORKER_ENV]='true';os.environ[worker.PHOTO_CLEARANCE_ENV]='true';os.environ[worker.TENANTS_ENV]='gym'
   class IdleHosted(Hosted):
    def read(self,url):
     assert sql("select count(*) from pg_stat_activity where usename='photo_owner' and state='idle in transaction'")[0][0]==0
     return super().read(url)
   with patch.object(owner,'HostedObjectReader',return_value=IdleHosted(other_bytes)),patch('agent.forward_media_source_verifier.OriginalDriveReader',return_value=other_drive):
    live_report=worker.run_once()
   assert live_report['status']=='complete',live_report
   assert len(live_report['rows'])==1 and live_report['rows'][0]['status']=='persisted',live_report
   assert live_report['rows'][0]['audit_id']==fresh_packet['payload']['audit_id']
   assert sql("select state,outcome->>'status' from fixer_owner_photo_progress_20261007 where audit_id=%s",(fresh_packet['payload']['audit_id'],))[0]==('final','persisted')
   from agent.forward_media_owner_photo_prepare import run_photo_pass
   conn.rollback()
   empty=run_photo_pass(persistence=p,reader=Hosted(other_bytes),drive_reader=other_drive,tenants=('gym',),limit=25)
   assert empty=={'status':'complete','rows':[]},empty
   reconciled=reconcile_owner_photo(p,fresh_packet['payload']['audit_id'])
   assert reconciled['replayed'] is True and reconciled['progress']['state']=='final';conn.rollback()
   def signed_runtime_candidate(color,tag):
    data_bytes=png(color);asset_id=FILE+tag;url=URL+'&'+tag+'=1';row_id=new_row()
    sql('insert into media_asset(id,source_id,gym_id) values(%s,%s,%s)',(asset_id,'source','gym'))
    approved_asset(asset_id,data_bytes)
    sql('update content_calendar set source_media_asset_id=%s,source_media_url=%s,image_url=%s where id=%s',(asset_id,url,url,row_id))
    class CandidateDrive(Drive):
     def original_bytes(self,file_id):
      assert file_id==asset_id;return self.data
    candidate_drive=CandidateDrive();candidate_drive.data=data_bytes
    candidate_drive.meta.update(id=asset_id,size=str(len(data_bytes)),md5Checksum=hashlib.md5(data_bytes).hexdigest())
    revision_now=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(row_id,))[0][0]
    source_now=verify_source(store.snapshot(row_id,revision_now),candidate_drive,Hosted(data_bytes))
    store.stage_source(source_now);conn.commit()
    candidate_now={**candidate,'calendar_row_id':row_id,'source_asset_id':asset_id,'source_url':url,'image_url':url,
     'source_fingerprint':source_now.original.source_fingerprint,'source_sha256':source_now.evidence['source_sha256'],
     'source_length':len(data_bytes),'source_receipt_ref':source_now.receipt_ref,
     'image_fingerprint':source_now.original.source_fingerprint,'image_sha256':source_now.evidence['source_sha256'],'image_length':len(data_bytes),
     'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(row_id,))[0][0]}
    certificate,_,_,_=fixtures(candidate=candidate_now,snapshot=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0],private=private)
    IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(certificate);auditor_conn.commit()
    sql('insert into fixer_forward_media_observation_20261007(calendar_row_id,row_revision,observation_digest,tenant_id,gym_id,source_asset_id,source_exact_url,delivered_exact_url,calendar_snapshot,observation_json,digest_input) values(%s,%s,%s,\'gym\',\'gym\',%s,%s,%s,\'{}\'::jsonb,%s,\'SYNTHETIC producer recipe\')',(row_id,revision_now,'a'*64,asset_id,url,url,json.dumps({'recipe':recipe})))
    return certificate,data_bytes,candidate_drive
   lost_packet,lost_bytes,lost_drive=signed_runtime_candidate('red','LostCommit')
   class LostCommitResponse:
    def __init__(self,raw,fail_on=2):self.raw=raw;self.commits=0;self.fail_on=fail_on
    def __getattr__(self,name):return getattr(self.raw,name)
    def commit(self):
     self.commits+=1;self.raw.commit()
     if self.commits==self.fail_on:raise RuntimeError('SYNTHETIC COMMIT response lost')
   wrapped=LostCommitResponse(psycopg.connect(dsn('photo_owner')))
   lost_persistence=owner.ForwardMediaOwnerPersistence(wrapped,'photo_owner',Hosted(lost_bytes))
   with patch.object(owner,'HostedObjectReader',return_value=IdleHosted(lost_bytes)),patch('agent.forward_media_source_verifier.OriginalDriveReader',return_value=lost_drive),patch.object(owner.ForwardMediaOwnerPersistence,'connect_from_environment',return_value=lost_persistence):
    lost_report=worker.run_once()
   assert lost_report=={'status':'hold','reason':'uncertain_authority_commit','rows':[]},lost_report
   assert wrapped.commits==2 and wrapped.raw.closed
   conn.rollback()
   lost_readback=reconcile_owner_photo(p,lost_packet['payload']['audit_id']);conn.rollback()
   assert lost_readback['progress']['state']=='final' and lost_readback['progress']['outcome']['status']=='persisted'
   crash_packet,crash_bytes,crash_drive=signed_runtime_candidate('black','ReadCrash')
   class FailedRemote(Hosted):
    def read(self,url):
     assert sql('select state from fixer_owner_photo_progress_20261007 where audit_id=%s',(crash_packet['payload']['audit_id'],))[0][0]=='quarantine'
     raise RuntimeError('SYNTHETIC remote crash after durable admission')
   with patch.object(owner,'HostedObjectReader',return_value=FailedRemote(crash_bytes)),patch('agent.forward_media_source_verifier.OriginalDriveReader',return_value=crash_drive):
    crash_report=worker.run_once()
   assert crash_report['status']=='partial_hold' and len(crash_report['rows'])==1,crash_report
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007 where audit_id=%s',(crash_packet['payload']['audit_id'],))[0][0]==0
   assert sql('select state from fixer_owner_photo_progress_20261007 where audit_id=%s',(crash_packet['payload']['audit_id'],))[0][0]=='quarantine'
   conn.rollback()
   assert run_photo_pass(persistence=p,reader=FailedRemote(crash_bytes),drive_reader=crash_drive,tenants=('gym',),limit=25)=={'status':'complete','rows':[]}
   initial_packet,initial_bytes,initial_drive=signed_runtime_candidate('yellow','InitialCommitLost')
   class NoRemote(Hosted):
    def read(self,url):raise AssertionError('remote read after uncertain admission COMMIT')
   initial_wrapped=LostCommitResponse(psycopg.connect(dsn('photo_owner')),fail_on=1)
   initial_persistence=owner.ForwardMediaOwnerPersistence(initial_wrapped,'photo_owner',NoRemote(initial_bytes))
   with patch.object(owner,'HostedObjectReader',return_value=NoRemote(initial_bytes)),patch('agent.forward_media_source_verifier.OriginalDriveReader',return_value=initial_drive),patch.object(owner.ForwardMediaOwnerPersistence,'connect_from_environment',return_value=initial_persistence):
    initial_report=worker.run_once()
   assert initial_report=={'status':'hold','reason':'uncertain_authority_commit','rows':[]},initial_report
   assert initial_wrapped.commits==1 and initial_wrapped.raw.closed
   assert sql('select state from fixer_owner_photo_progress_20261007 where audit_id=%s',(initial_packet['payload']['audit_id'],))[0][0]=='quarantine'
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007 where audit_id=%s',(initial_packet['payload']['audit_id'],))[0][0]==0
   # Unknown newly observed legacy history holds an existing grant; removing the
   # synthetic unknown row restores the same epoch without a new positive audit.
   unknown=str(uuid.uuid4());sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','published','https://media.example.test/unknown.png')",(unknown,))
   denied(lambda:claim(first),'unknown historical');sql('delete from content_calendar where id=%s',(unknown,))
   assert claim(first) is True
   # A changed published sibling cannot hide behind its old claim receipt.
   sql("update content_calendar set status='published',image_url='https://media.example.test/unknown-new-render.png' where id=%s",(second[0],))
   denied(lambda:claim(first),'unknown historical')
   sql("update content_calendar set status='publishing',image_url=%s where id=%s",(URL,second[0]))
   assert claim(first) is True
   # Explicit monotonic epoch invalidation holds both fresh sends and replay.
   sql('update fixer_forward_media_photo_state_20261007 set generation=1')
   denied(lambda:claim(first),'retired epoch')
   denied(lambda:sql('update fixer_forward_media_photo_state_20261007 set generation=0'),'cannot regress')
   sql('insert into fixer_owner_photo_revocation_20261007 values(%s,%s)',(packet['payload']['audit_id'],'SYNTHETIC revoke'))
   denied(lambda:claim(first),'revoked')
   conn.close();auditor_conn.close();admin.close()
   print('PASS PG17 signed owner grant; OFF/moderation/byte-binding/safety-hold/direct bypass holds; atomic reservation+authority; concurrent stale corpus hold; signed date/group before first claim; post-binder replay + stored-signature reconciliation; IG/FB/Story siblings; different Story rendition hold; production owner-worker factory with acknowledged outcome; initial/final lost COMMIT and remote crash quarantine/no-retry; unknown history/epoch/revocation holds')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)

if __name__=='__main__':main()
