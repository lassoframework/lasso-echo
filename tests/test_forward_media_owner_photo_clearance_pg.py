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
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql'):
    sql((ROOT/'migrations'/name).read_text())
   sql((ROOT/'migrations'/'DRAFT_fixer_photo_historical_clearance_20261008.sql').read_text())
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
   sql("insert into fixer_forward_media_photo_policy_20261007 values(%s,true,'complete_fleet_still_photo_history',null,'SYNTHETIC cutover','SYNTHETIC admin')",(key['policy_id'],))
   sql('insert into fixer_forward_media_photo_key_20261007 values(%s,%s,%s,%s,%s,true)',(key['key_id'],key['auditor_id'],'photo_auditor',key['policy_id'],key['public_key_hex']))
   baseline=str(uuid.uuid4())
   history={'history_key':'SYNTHETIC full historical photo','resolved':True,'media_kind':'still_photo','visual_sha256':digest('SYNTHETIC different historic visual'),'published_binding_ref':'SYNTHETIC preserved complete fleet history'}
   sql('insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,%s,true,%s::jsonb,%s,1)',(baseline,key['policy_id'],json.dumps([history]),'SYNTHETIC complete corpus'))
   sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s where singleton',(baseline,))
   packet,_,_,_=fixtures(candidate=candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0],private=private)
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
   other_packet,_,_,_=fixtures(candidate=other_candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0],private=private)
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
   snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0]
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
   os.environ['AGENT_S3_PUBLIC_BASE_URL']='https://media.example.test'
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
   # Exact independent-review reproduction: sibling's first real claim is
   # staged but not committed; an unrelated unknown publication cannot commit
   # inside that authority transaction. All three census DML paths fail fast,
   # including a writer which already owns a calendar row (no deadlock/upgrade).
   known_row=new_row()
   unknown_during=str(uuid.uuid4())
   pending_claim=lane('service_role')
   assert pending_claim.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',second).fetchone()[0] is True
   denied(lambda:sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','published','https://media.example.test/unknown-review.png')",(unknown_during,)),'census authority busy')
   assert sql('select count(*) from content_calendar where id=%s',(unknown_during,))[0][0]==0
   denied(lambda:sql("update content_calendar set status='published' where id=%s",(known_row,)),'census authority busy')
   denied(lambda:sql('delete from content_calendar where id=%s',(known_row,)),'census authority busy')
   for statement,args in (
    ("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','published','https://media.example.test/service-unknown.png')",(str(uuid.uuid4()),)),
    ("update content_calendar set status='published' where id=%s",(known_row,)),
    ('delete from content_calendar where id=%s',(known_row,))):
    with lane('service_role') as census_writer:
     denied(lambda:census_writer.execute(statement,args),'census authority busy')
   prelocked=psycopg.connect(dsn())
   prelocked.execute('select id from content_calendar where id=%s for update',(known_row,))
   denied(lambda:prelocked.execute("update content_calendar set status='published' where id=%s",(known_row,)),'census authority busy')
   prelocked.rollback();prelocked.close()
   # Normal provider receipt persistence by the already-owned transaction takes
   # the same exclusive census lock again, without an upgrade or self-deadlock.
   pending_claim.execute("update content_calendar set status='published',published_at=now(),late_post_id='SYNTHETIC provider receipt' where id=%s",(second[0],))
   pending_claim.commit();pending_claim.close()
   denied(lambda:sql('truncate content_calendar'),'offline reconciliation')
   assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006 where calendar_row_id=%s',(second[0],))[0][0]==1
   sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','published','https://media.example.test/unknown-review.png')",(unknown_during,))
   denied(lambda:claim(second),'unknown historical')
   sql('delete from content_calendar where id=%s',(unknown_during,))
   # Strong inversion regression: older RPC has already locked the next
   # sibling row. The real claimant acquires census authority and waits for it.
   # The older writer's UPDATE fails immediately instead of waiting for census
   # while retaining the row that claimant needs, then rollback lets claim pass.
   old_writer=psycopg.connect(dsn())
   old_writer.execute('select id from content_calendar where id=%s for update',(third[0],))
   with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    waiting_sibling=pool.submit(claim,third)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
     if sql("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select fixer_claim_forward_media%' ")[0][0]:break
     time.sleep(.02)
    else:raise AssertionError('sibling claim did not wait for older row lock')
    denied(lambda:old_writer.execute("update content_calendar set caption='SYNTHETIC older RPC change' where id=%s",(third[0],)),'census authority busy')
    old_writer.rollback()
    assert waiting_sibling.result(timeout=8) is True
   old_writer.close()
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
    good=pool.submit(claim,fourth);sibling=pool.submit(claim,first)
    assert good.result(timeout=8) is True
    assert sibling.result(timeout=8) is True
   assert claim(first) is True
   # Actual production_callbacks transaction used by guard.attest, through its
   # real exact-role DSN/factory. Pause at its first object read, AFTER provenance
   # captured provenance, and overlap a real service claim/calendar write.
   # Network work must retain no transaction; final authority revalidates.
   from agent import visual_writer_prepare
   sql('alter role '+guard.ROLE+' login')
   from agent.forward_media_lane import unknown_environment_names
   prior_owner_env={name:os.environ.pop(name) for name in unknown_environment_names(os.environ,'attester')}
   prior_attester_env={name:os.environ.get(name) for name in (
    'AGENT_FORWARD_MEDIA_GUARD','AGENT_FORWARD_MEDIA_ATTESTER_DSN','AGENT_FORWARD_MEDIA_ATTESTER_ROLE')}
   os.environ['AGENT_FORWARD_MEDIA_GUARD']='true'
   os.environ['AGENT_FORWARD_MEDIA_ATTESTER_DSN']=dsn(guard.ROLE)
   os.environ['AGENT_FORWARD_MEDIA_ATTESTER_ROLE']=guard.ROLE
   remote_entered=threading.Event();remote_release=threading.Event()
   def fresh_production_attestation_row():
    row_id=new_row(fmt='story')
    with lane('service_role') as service:
     assert service.execute('select fixer_bind_forward_media_manifest_20261006(%s)',(row_id,)).fetchone()[0]
    sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day='2026-10-10' where id=%s",(str(uuid.uuid4()),row_id))
    revision=sql('select fixer_forward_media_attestation_request_20261006(%s)',(row_id,))[0][0]['revision']
    return row_id,revision
   production_row,production_revision=fresh_production_attestation_row()
   def paused_production_bytes(url):
    remote_entered.set()
    assert remote_release.wait(timeout=8),'production attester remote phase was not released'
    return hosted.read(url)
   try:
    with patch.object(guard,'read_public_object',side_effect=paused_production_bytes),concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
     production_attest=pool.submit(guard.attest,production_row,production_revision)
     assert remote_entered.wait(timeout=5),'real production_callbacks did not reach object read'
     assert sql("select count(*) from pg_stat_activity where usename=%s and state='idle' and xact_start is null",(guard.ROLE,))[0][0]==1
     concurrent_claim=pool.submit(claim,third)
     assert concurrent_claim.result(timeout=3) is True
     sql('update content_calendar set status=status where id=%s',(third[0],))
     remote_release.set()
     production_proof=production_attest.result(timeout=8)
     assert production_proof['revision']==production_revision
    # Edits during the unlocked network phase cannot inherit captured proof.
    for mutation in ('binding','history','moderation'):
     changed_row,changed_revision=fresh_production_attestation_row()
     remote_entered.clear();remote_release.clear()
     unknown_id=None
     with patch.object(guard,'read_public_object',side_effect=paused_production_bytes),concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
      changed_attest=pool.submit(guard.attest,changed_row,changed_revision)
      assert remote_entered.wait(timeout=5)
      assert sql("select count(*) from pg_stat_activity where usename=%s and state='idle' and xact_start is null",(guard.ROLE,))[0][0]==1
      if mutation=='binding':
       sql("update content_calendar set image_url='https://media.example.test/changed.png' where id=%s",(changed_row,))
      elif mutation=='history':
       unknown_id=str(uuid.uuid4())
       sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','published','https://media.example.test/unknown-network.png')",(unknown_id,))
      else:
       sql("update media_asset set moderation_status='pending' where id=%s",(FILE,))
      remote_release.set()
      try:changed_attest.result(timeout=8);raise AssertionError('changed authority accepted: '+mutation)
      except guard.ForwardMediaVerificationHold as exc:
       assert isinstance(exc.__cause__,psycopg.errors.CheckViolation),(mutation,exc.__cause__)
     assert sql('select count(*) from fixer_forward_media_lineage_20261006 where calendar_row_id=%s',(changed_row,))[0][0]==0
     if unknown_id:sql('delete from content_calendar where id=%s',(unknown_id,))
     if mutation=='moderation':sql("update media_asset set moderation_status='clean' where id=%s",(FILE,))
    # Reverse order: a claim already owns shared graph+census. Production
    # provenance waits for exclusive graph BEFORE census and resumes on COMMIT.
    reverse_row,reverse_revision=fresh_production_attestation_row()
    held_claim=lane('service_role')
    assert held_claim.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',third).fetchone()[0] is True
    with patch.object(guard,'read_public_object',side_effect=hosted.read),concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
     waiting_attester=pool.submit(guard.attest,reverse_row,reverse_revision)
     deadline=time.monotonic()+5
     while time.monotonic()<deadline:
      if sql("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select public.fixer_forward_media_provenance_lookup%' ")[0][0]:break
      time.sleep(.02)
     else:raise AssertionError('production attester did not wait for preceding claim')
     held_claim.commit();held_claim.close()
     assert waiting_attester.result(timeout=8)['revision']==reverse_revision
    for other_group in ('service_role','fixer_forward_media_owner_20261006'):
     sql('grant '+other_group+' to '+guard.ROLE)
     try:
      with lane(guard.ROLE) as mixed:
       denied(lambda:attester.production_callbacks(mixed,production_row,expected_revision=production_revision),'isolated attester provenance identity')
     finally:
      sql('revoke '+other_group+' from '+guard.ROLE)
   finally:
    remote_release.set()
    for name,value in prior_attester_env.items():
     if value is None:os.environ.pop(name,None)
     else:os.environ[name]=value
   os.environ.update(prior_owner_env)
   # A fresh independent certificate after prior sends has reviewed the now
   # reserved/published visuals. Real production run_once discovers it, commits
   # durable admission before remote readers, and atomically persists outcome.
   fresh_packet,_,_,_=fixtures(candidate=other_candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0],private=private)
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
    certificate,_,_,_=fixtures(candidate=candidate_now,snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0],private=private)
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
   # Real independently signed feed/story crops of ONE approved original
   # share the immutable source clearance only for this exact gym/date/group.
   # Exercise preparation AFTER the original's real claim receipts exist too.
   rendered_objects={URL:source_bytes}
   class RenditionHosted(Hosted):
    def read(self,url):return rendered_objects[url]
   rendition_hosted=RenditionHosted()
   def signed_rendition(name,tag,day='2026-10-10',group='group',tenant='gym',thumbnail_name=None):
    recipe_now=attester.make_still_recipe(name,caption='SYNTHETIC distinct story '+(tag if thumbnail_name else ''),gym_name='SYNTHETIC gym',thumbnail_name=thumbnail_name)
    replay_now=attester.replay_still_recipe(source_bytes,recipe_now,has_thumbnail=thumbnail_name is not None)
    image_bytes=replay_now['image_bytes']
    thumbnail_url=('https://media.example.test/thumbnail-'+tag+'.png' if thumbnail_name else None)
    if thumbnail_name=='delivered_image':thumbnail_url='https://media.example.test/signed-'+tag+'.png'
    if thumbnail_url:rendered_objects[thumbnail_url]=replay_now['thumbnail_bytes']
    image_url='https://media.example.test/signed-'+tag+'.png'
    rendered_objects[image_url]=image_bytes
    row_id=new_row(day=day,fmt='story' if name=='story_photo' else 'feed')
    asset_id,source_url,drive_now=FILE,URL,drive
    if tenant!='gym':
     asset_id=FILE+'OtherTenant'+tag;source_url=URL+'&tenant='+tag;rendered_objects[source_url]=source_bytes
     sql('insert into media_source values(%s,%s,%s,%s,true)',('other-source-'+tag,tenant,'gym_drive',FOLDER))
     sql('insert into media_asset(id,source_id,gym_id) values(%s,%s,%s)',(asset_id,'other-source-'+tag,tenant))
     approved_asset(asset_id,source_bytes,tenant)
     class TenantDrive(Drive):
      def original_bytes(self,file_id):assert file_id==asset_id;return source_bytes
     drive_now=TenantDrive();drive_now.data=source_bytes
     drive_now.meta.update(id=asset_id,size=str(len(source_bytes)),md5Checksum=hashlib.md5(source_bytes).hexdigest())
    sql('update content_calendar set gym_id=%s,visual_group_key=%s,source_media_asset_id=%s,source_media_url=%s,image_url=%s,thumbnail_url=%s where id=%s',
        (tenant,group,asset_id,source_url,image_url,thumbnail_url,row_id))
    revision_now=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(row_id,))[0][0]
    current_now=store.snapshot(row_id,revision_now)
    source_now=verify_source(current_now,drive_now,rendition_hosted)
    store.stage_source(source_now);conn.commit()
    candidate_now={**candidate,'calendar_row_id':row_id,'tenant_id':tenant,'group_key':group,'post_date':day,
     'source_asset_id':asset_id,'source_url':source_url,'source_receipt_ref':source_now.receipt_ref,
     'image_url':image_url,'image_fingerprint':'md5:'+hashlib.md5(image_bytes).hexdigest(),
     'image_sha256':'sha256:'+hashlib.sha256(image_bytes).hexdigest(),'image_length':len(image_bytes),
     'render_recipe_digest':digest(recipe_now),
     'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(row_id,))[0][0]}
    if thumbnail_url:
     candidate_now.update(thumbnail_url=thumbnail_url,thumbnail_sha256='sha256:'+hashlib.sha256(replay_now['thumbnail_bytes']).hexdigest(),
      thumbnail_fingerprint='md5:'+hashlib.md5(replay_now['thumbnail_bytes']).hexdigest(),thumbnail_length=len(replay_now['thumbnail_bytes']))
    cert_now,_,_,_=fixtures(candidate=candidate_now,snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0],private=private)
    IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(cert_now);auditor_conn.commit()
    prepared_now=prepare_remote_photo(current_now,drive_reader=drive_now,hosted_reader=rendition_hosted,
       recipe=recipe_now,auditor=owner_auditor,audit_id=cert_now['payload']['audit_id']);conn.rollback()
    return row_id,prepared_now,cert_now
   def rendition_claim(row_id):
    with lane('service_role') as service:assert service.execute('select fixer_bind_forward_media_manifest_20261006(%s)',(row_id,)).fetchone()[0]
    token=str(uuid.uuid4())
    sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day='2026-10-10' where id=%s",(token,row_id))
    rev=sql('select fixer_forward_media_attestation_request_20261006(%s)',(row_id,))[0][0]['revision']
    with lane(guard.ROLE) as verifier:
     original_check,renderer=attester.production_callbacks(verifier,row_id,expected_revision=rev)
    guard.attest(row_id,rev,connection_factory=lambda:lane(guard.ROLE),read_bytes=rendition_hosted.read,
       original_verifier=original_check,controlled_renderer=renderer)
    eid=sql('select evidence_id from fixer_forward_media_lineage_20261006 where calendar_row_id=%s',(row_id,))[0][0]
    return row_id,token,eid,rev
   signed_crops=[]
   for name in ('feed_autofit_4x5','story_photo'):
    row_id,prepared_now,cert_now=signed_rendition(name,name)
    counts=sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]
    # Story runtime filenames mix case and underscores. The signed Python
    # codepoint order must match SQL even on an en_US database locale.
    canonical_sql=sql('select fixer_owner_photo_canonical_20261007(%s::jsonb)',(json.dumps(prepared_now.manifest.render_recipe),))[0][0]
    assert canonical_sql==json.dumps(prepared_now.manifest.render_recipe,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    staged=stage_prepared_photo(p,prepared_now)
    assert staged['registry']==verified_source.original.row(),staged
    assert staged['clearance']==result['clearance'],staged
    assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==counts[0]
    conn.commit()
    assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0]==counts[0]+1
    assert sql('select count(*) from fixer_forward_media_render_manifest_20261006')[0][0]==counts[1]+1
    assert sql("select count(*) from fixer_forward_media_original_registry_20261006 where tenant_id='gym' and source_asset_id=%s",(FILE,))[0][0]==1
    assert stage_prepared_photo(p,prepared_now)['replayed'] is True;conn.commit()
    assert reconcile_owner_photo(p,cert_now['payload']['audit_id'])['manifest']==prepared_now.manifest.row();conn.rollback()
    binding_now=rendition_claim(row_id)
    assert claim(binding_now) is True and claim(binding_now) is True
    signed_crops.append((prepared_now,cert_now,binding_now))
   assert signed_crops[0][0].manifest.image_fingerprint!=signed_crops[1][0].manifest.image_fingerprint
   assert signed_crops[0][0].manifest.manifest_digest!=signed_crops[1][0].manifest.manifest_digest
   for tag,day,group,tenant in (
       ('wrong-day','2026-10-11','group','gym'),
       ('wrong-group','2026-10-10','other-group','gym'),
       ('wrong-tenant','2026-10-10','group','other-gym'),
       ('duplicate-render','2026-10-10','group','gym')):
    row_id,prepared_now,cert_now=signed_rendition('story_photo',tag,day,group,tenant)
    before=sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]
    denied(lambda:stage_prepared_photo(p,prepared_now),'already used cleared or reserved');conn.rollback()
    assert sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]==before
   # Real transformed thumbnail, reused same-day rendered bytes, and image
   # alias traverse signed preparation -> immutable persistence -> attester ->
   # claim/send replay. The image remains a distinct signed sibling rendition.
   thumbnail_bindings=[]
   for thumbnail_name,tag in (('feed_autofit_4x5','thumb-transformed'),('delivered_image','thumb-alias')):
    row_id,thumb_prepared,thumb_cert=signed_rendition('story_photo',tag,thumbnail_name=thumbnail_name)
    assert thumb_prepared.thumbnail_bytes==rendered_objects[thumb_prepared.manifest.thumbnail_url]
    counts=sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]
    # Wrong frozen bytes cannot persist even after SQL stages the reservation;
    # caller rollback restores the exact before-counts.
    from dataclasses import replace
    try:
     stage_prepared_photo(p,replace(thumb_prepared,thumbnail_bytes=png('white')))
     raise AssertionError('wrong retained thumbnail persisted')
    except owner.OwnerPersistenceError:conn.rollback()
    except PhotoCertificateHold:conn.rollback()
    assert sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]==counts
    bad_manifest=thumb_prepared.manifest.row();bad_manifest['thumbnail_fingerprint']='md5:'+'e'*32
    denied(lambda:conn.execute('select fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',
     (thumb_cert['payload']['audit_id'],json.dumps(thumb_prepared.source.original.row()),json.dumps(bad_manifest))),'exact certified');conn.rollback()
    bad_recipe=thumb_prepared.manifest.row();bad_recipe['render_recipe']={**bad_recipe['render_recipe'],'thumbnail':None}
    denied(lambda:conn.execute('select fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',
     (thumb_cert['payload']['audit_id'],json.dumps(thumb_prepared.source.original.row()),json.dumps(bad_recipe))),'exact certified');conn.rollback()
    revision_thumb=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(row_id,))[0][0]
    sql('insert into fixer_forward_media_observation_20261007(calendar_row_id,row_revision,observation_digest,tenant_id,gym_id,source_asset_id,source_exact_url,delivered_exact_url,calendar_snapshot,observation_json,digest_input) values(%s,%s,%s,\'gym\',\'gym\',%s,%s,%s,\'{}\'::jsonb,%s,\'SYNTHETIC thumbnail recipe\')',(row_id,revision_thumb,'a'*64,FILE,URL,thumb_prepared.manifest.image_url,json.dumps({'recipe':thumb_prepared.manifest.render_recipe})))
    class IdleRenditions(RenditionHosted):
     def read(self,url):
      assert conn.info.transaction_status==psycopg.pq.TransactionStatus.IDLE,'thumbnail remote read held a DB transaction'
      return super().read(url)
    conn.rollback()
    runtime_thumb=run_photo_pass(persistence=p,reader=IdleRenditions(),drive_reader=drive,tenants=('gym',),limit=25)
    assert runtime_thumb['status']=='complete' and len(runtime_thumb['rows'])==1,runtime_thumb
    assert runtime_thumb['rows'][0]['audit_id']==thumb_cert['payload']['audit_id']
    assert sql("select state from fixer_owner_photo_progress_20261007 where audit_id=%s",(thumb_cert['payload']['audit_id'],))[0][0]=='final'
    snap_thumb=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0]
    assert any(h['history_key']=='owner-reserved-thumbnail:'+thumb_cert['payload']['audit_id']
      and h['visual_sha256']==thumb_cert['payload']['candidate']['thumbnail_sha256'] for h in snap_thumb['rows'])
    assert reconcile_owner_photo(p,thumb_cert['payload']['audit_id'])['manifest']==thumb_prepared.manifest.row();conn.rollback()
    binding_thumb=rendition_claim(row_id)
    assert claim(binding_thumb) is True and claim(binding_thumb) is True
    # Hosted bytes change after owner preparation: final actual attester refuses
    # a new attestation; existing immutable claim remains the tested safe tuple.
    saved=rendered_objects[thumb_prepared.manifest.thumbnail_url]
    rendered_objects[thumb_prepared.manifest.thumbnail_url]=png('white')
    try:
     with lane(guard.ROLE) as verifier:
      original_check,renderer=attester.production_callbacks(verifier,row_id,expected_revision=binding_thumb[3])
     guard.attest(row_id,binding_thumb[3],connection_factory=lambda:lane(guard.ROLE),read_bytes=rendition_hosted.read,
      original_verifier=original_check,controlled_renderer=renderer)
     raise AssertionError('changed hosted thumbnail attested')
    except guard.ForwardMediaVerificationHold:pass
    rendered_objects[thumb_prepared.manifest.thumbnail_url]=saved
    assert claim(binding_thumb) is True
    thumbnail_bindings.append((thumb_prepared,thumb_cert,binding_thumb))
   # New signed candidates containing those thumbnail bytes across date/tenant
   # cannot create any authority. Existing source collision also remains held.
   for tag,day,tenant in (('thumb-other-day','2026-10-11','gym'),('thumb-other-tenant','2026-10-10','thumb-other-gym')):
    row_id,thumb_prepared,thumb_cert=signed_rendition('story_photo',tag,day=day,tenant=tenant,thumbnail_name='feed_autofit_4x5')
    before=sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]
    denied(lambda:stage_prepared_photo(p,thumb_prepared),'already used cleared or reserved');conn.rollback()
    assert sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]==before
   # Isolate the THUMBNAIL collision: originals and delivered images differ
   # byte-for-byte (valid PNG trailing data), while deterministic crops agree.
   # Synthetic real signatures cannot claim nonmatch for these known bytes.
   def independent_thumbnail(tag,day='2026-10-11',tenant='gym',color='blue',thumbnail_name='feed_autofit_4x5',image_source_alias=True):
    data_bytes=png(color)+tag.encode();asset_id=FILE+tag;source_id='source-'+tag
    source_url='https://media.example.test/original-'+tag+'.png';row_id=new_row(day=day)
    sql('insert into media_source values(%s,%s,%s,%s,true)',(source_id,tenant,'gym_drive',FOLDER))
    sql('insert into media_asset(id,source_id,gym_id) values(%s,%s,%s)',(asset_id,source_id,tenant))
    approved_asset(asset_id,data_bytes,tenant)
    recipe_now=attester.make_still_recipe('identity',thumbnail_name=thumbnail_name)
    replay_now=attester.replay_still_recipe(data_bytes,recipe_now,has_thumbnail=True)
    thumb_url='https://media.example.test/thumb-independent-'+tag+'.png'
    image_url=source_url if image_source_alias else 'https://media.example.test/image-independent-'+tag+'.png'
    rendered_objects[source_url]=data_bytes;rendered_objects[image_url]=data_bytes;rendered_objects[thumb_url]=replay_now['thumbnail_bytes']
    sql('update content_calendar set gym_id=%s,source_media_asset_id=%s,source_media_url=%s,image_url=%s,thumbnail_url=%s where id=%s',
     (tenant,asset_id,source_url,image_url,thumb_url,row_id))
    class FreshDrive(Drive):
     def original_bytes(self,file_id):assert file_id==asset_id;return data_bytes
    fresh_drive=FreshDrive();fresh_drive.data=data_bytes
    fresh_drive.meta.update(id=asset_id,size=str(len(data_bytes)),md5Checksum=hashlib.md5(data_bytes).hexdigest())
    revision_now=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(row_id,))[0][0]
    current_now=store.snapshot(row_id,revision_now);source_now=verify_source(current_now,fresh_drive,rendition_hosted)
    store.stage_source(source_now);conn.commit()
    candidate_now={**candidate,'calendar_row_id':row_id,'tenant_id':tenant,'post_date':day,'source_asset_id':asset_id,
     'source_url':source_url,'image_url':image_url,'source_receipt_ref':source_now.receipt_ref,
     'source_fingerprint':source_now.original.source_fingerprint,'image_fingerprint':source_now.original.source_fingerprint,
     'source_sha256':source_now.evidence['source_sha256'],'image_sha256':source_now.evidence['source_sha256'],
     'source_length':len(data_bytes),'image_length':len(data_bytes),'render_recipe_digest':digest(recipe_now),
     'thumbnail_url':thumb_url,'thumbnail_sha256':'sha256:'+hashlib.sha256(replay_now['thumbnail_bytes']).hexdigest(),
     'thumbnail_fingerprint':'md5:'+hashlib.md5(replay_now['thumbnail_bytes']).hexdigest(),'thumbnail_length':len(replay_now['thumbnail_bytes']),
     'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(row_id,))[0][0]}
    cert_now,_,_,_=fixtures(candidate=candidate_now,snapshot=sql('select fixer_forward_media_photo_snapshot_exclusion_20261008()')[0][0],private=private)
    IndependentPhotoAuditor(auditor_conn,'photo_auditor').submit(cert_now);auditor_conn.commit()
    prepared_now=prepare_remote_photo(current_now,drive_reader=fresh_drive,hosted_reader=rendition_hosted,recipe=recipe_now,auditor=owner_auditor,audit_id=cert_now['payload']['audit_id']);conn.rollback()
    return prepared_now,cert_now
   # Identity images also traverse the actual owner -> attester -> claim.
   # Operation comes from all URLs and bytes, including a separately hosted
   # identical thumbnail (rehost) and transformed thumbnail (render).
   for thumbnail_name,tag,image_source_alias,operation,color in (
       ('feed_autofit_4x5','identity-source-transformed-thumb',True,'render','purple'),
       ('feed_autofit_4x5','identity-rehost-transformed-thumb',False,'render','orange'),
       ('identity','identity-source-separate-thumb',True,'rehost','pink')):
    identity_prepared,identity_cert=independent_thumbnail(tag,day='2026-10-10',color=color,
       thumbnail_name=thumbnail_name,image_source_alias=image_source_alias)
    row_id=identity_cert['payload']['candidate']['calendar_row_id']
    identity_source=identity_prepared.source.source_bytes
    assert identity_prepared.image_bytes==identity_source
    assert identity_prepared.manifest.operation==operation
    assert identity_prepared.manifest.thumbnail_url not in (identity_prepared.source.original.source_url,identity_prepared.manifest.image_url)
    assert (identity_prepared.thumbnail_bytes==identity_source)==(operation=='rehost')
    stage_prepared_photo(p,identity_prepared);conn.commit()
    binding_identity=rendition_claim(row_id)
    assert sql('select operation from fixer_forward_media_lineage_20261006 where calendar_row_id=%s',(row_id,))[0][0]==operation
    assert claim(binding_identity) is True and claim(binding_identity) is True
   for tag,day,tenant in (('thumb-only-date','2026-10-11','gym'),('thumb-only-tenant','2026-10-10','thumb-only-other-gym')):
    thumb_prepared,thumb_cert=independent_thumbnail(tag,day,tenant)
    assert thumb_cert['payload']['candidate']['source_sha256']!=candidate['source_sha256']
    assert thumb_cert['payload']['candidate']['image_sha256']!=candidate['image_sha256']
    assert thumb_cert['payload']['candidate']['thumbnail_sha256']==thumbnail_bindings[0][1]['payload']['candidate']['thumbnail_sha256']
    before=sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]
    denied(lambda:stage_prepared_photo(p,thumb_prepared),'already used cleared or reserved');conn.rollback()
    assert sql('select (select count(*) from fixer_owner_photo_reservation_20261007),(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]==before
   concurrent_a,concurrent_cert_a=independent_thumbnail('thumb-concurrent-a',color='cyan')
   concurrent_b,concurrent_cert_b=independent_thumbnail('thumb-concurrent-b',tenant='concurrent-other-gym',color='cyan')
   assert concurrent_cert_a['payload']['candidate']['source_sha256']!=concurrent_cert_b['payload']['candidate']['source_sha256']
   assert concurrent_cert_a['payload']['candidate']['thumbnail_sha256']==concurrent_cert_b['payload']['candidate']['thumbnail_sha256']
   stage_prepared_photo(p,concurrent_a)
   def competing_thumbnail():
    with psycopg.connect(dsn('photo_owner')) as competing_conn:
     competing_conn.execute("set statement_timeout='6s'")
     competing_p=owner.ForwardMediaOwnerPersistence(competing_conn,'photo_owner',rendition_hosted)
     try:stage_prepared_photo(competing_p,concurrent_b);return 'unsafe thumbnail grant'
     except psycopg.errors.CheckViolation as exc:
      assert 'complete current independently reviewed' in str(exc),str(exc)
      competing_conn.rollback();return 'concurrent thumbnail stale held'
   with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    waiting=pool.submit(competing_thumbnail)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
     if sql("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select public.fixer_prepare_owner_photo%' ")[0][0]:break
     time.sleep(.02)
    else:raise AssertionError('thumbnail contender did not serialize at graph')
    conn.commit();assert waiting.result(timeout=8)=='concurrent thumbnail stale held'
   assert sql('select count(*) from fixer_owner_photo_reservation_20261007 where audit_id=%s',(concurrent_cert_b['payload']['audit_id'],))[0][0]==0
   unknown_crop=str(uuid.uuid4())
   sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','published','https://media.example.test/unknown-crop-history.png')",(unknown_crop,))
   denied(lambda:claim(signed_crops[0][2]),'unknown historical')
   denied(lambda:claim(thumbnail_bindings[0][2]),'unknown historical')
   denied(lambda:reconcile_owner_photo(p,signed_crops[0][1]['payload']['audit_id']),'complete known current history');conn.rollback()
   sql('delete from content_calendar where id=%s',(unknown_crop,))
   # Rendition revocation blocks only that exact signed rendition. The original
   # anchor's revocation, tested below, still blocks the entire shared source.
   sql('insert into fixer_owner_photo_revocation_20261007 values(%s,%s)',(signed_crops[1][1]['payload']['audit_id'],'SYNTHETIC story revoke'))
   denied(lambda:claim(signed_crops[1][2]),'revoked')
   assert claim(signed_crops[0][2]) is True and claim(first) is True
   sql('insert into fixer_owner_photo_revocation_20261007 values(%s,%s)',(thumbnail_bindings[0][1]['payload']['audit_id'],'SYNTHETIC thumbnail rendition revoke'))
   denied(lambda:claim(thumbnail_bindings[0][2]),'revoked')
   assert claim(thumbnail_bindings[1][2]) is True
   # Explicit monotonic epoch invalidation holds both fresh sends and replay.
   sql('update fixer_forward_media_photo_state_20261007 set generation=1')
   denied(lambda:claim(first),'retired epoch')
   denied(lambda:claim(thumbnail_bindings[1][2]),'retired epoch')
   denied(lambda:sql('update fixer_forward_media_photo_state_20261007 set generation=0'),'cannot regress')
   sql('insert into fixer_owner_photo_revocation_20261007 values(%s,%s)',(packet['payload']['audit_id'],'SYNTHETIC revoke'))
   denied(lambda:claim(first),'revoked')
   denied(lambda:claim(signed_crops[0][2]),'revoked')
   conn.close();auditor_conn.close();admin.close()
   print('PASS PG17 signed owner grant; OFF/moderation/byte-binding/safety-hold/direct bypass holds; atomic reservation+authority; concurrent stale corpus hold; signed date/group before first claim; post-binder replay + stored-signature reconciliation; IG/FB/Story siblings; census INSERT/UPDATE/DELETE held through claim COMMIT for admin+service; prelocked writer fail-fast; own provider receipt and concurrent siblings allowed; actual production_callbacks IDLE remote reads permit claim/write; binding/history/moderation drift denied at fresh final authority; both-order concurrency without graph upgrade; unsigned Story rendition hold; independently signed distinct feed/story crops share original+clearance after claims; wrong date/group/tenant/duplicate-render atomic denial; separate rendition revocation; production owner-worker factory with acknowledged outcome; initial/final lost COMMIT and remote crash quarantine/no-retry; unknown history/epoch/revocation holds')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)

if __name__=='__main__':main()
