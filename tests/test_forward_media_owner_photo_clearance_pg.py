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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import forward_media_owner as owner, forward_media_guard as guard, forward_media_attester as attester, media_host
from agent.forward_media_source_history import SourceHistoryStore
from agent.forward_media_source_verifier import verify_source
from agent.forward_media_photo_certificate import IndependentPhotoAuditor,digest,PhotoCertificateHold
from agent.forward_media_owner_photo_prepare import prepare_remote_photo,stage_prepared_photo
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
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql'):
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
   sql('insert into media_asset values(%s,%s,%s,null,null)',(FILE,'source','gym'))
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
   sql('insert into media_asset values(%s,%s,%s,null,null)',(other_file,'source','gym'))
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
     if sql("select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'select public.fixer_forward_media_source_record%'")[0][0]:break
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
   first=ready_claim(rid);second=ready_claim(new_row(fmt='story'));third=ready_claim(new_row(fmt='feed',account='facebook'))
   assert claim(first) is True and claim(first) is True
   assert claim(second) is True and claim(third) is True
   assert claim(first) is True
   assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006')[0][0]==3
   other=ready_claim(new_row(day='2026-10-11'),'2026-10-11')
   fourth=ready_claim(new_row(fmt='story'))
   with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    good=pool.submit(claim,fourth);bad=pool.submit(claim,other)
    assert good.result(timeout=8) is True
    denied(lambda:bad.result(timeout=8))
   assert claim(first) is True
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
   print('PASS PG17 owner signed certificate + exact bytes/source/recipe grant; OFF/drift/direct bypass holds; atomic reservation+authority; concurrent prepared-visual corpus hold; replay+3 exact-rendition siblings and different Story rendition hold; concurrent different-day hold; unknown history/epoch/revocation holds')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)

if __name__=='__main__':main()
