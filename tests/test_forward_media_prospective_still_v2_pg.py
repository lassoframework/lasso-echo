"""Disposable PG17, complete composed authority, genuine Ed25519 schema 2.

All assets, reviews, policy rulings and 113 accounted video rows are synthetic.
No production, provider, GHL or network operations. SQL relies on authenticated
independent verifier, with real Ed25519 verification in Python before append.
"""
from dataclasses import asdict
import copy
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import forward_media_attester as attester, forward_media_prepare as prepare
from agent.forward_media_still_certificate_v2 import IndependentStillPhotoAuditorV2, verify_still_v2
from agent.forward_media_photo_certificate import canonical, digest, PhotoCertificateHold
from tests.test_forward_media_photo_certificate import fixtures
from tests.test_forward_media_owner_two_phase_pg import png

MIGRATIONS=(
 'logical_post_id_20261004.sql','DRAFT_fixer_forward_media_claim_20261006.sql',
 'DRAFT_fixer_forward_visual_index_20261008.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
 'DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_owner_transport_20261007.sql',
 'DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql',
 'DRAFT_fixer_generated_owner_20261007.sql','DRAFT_fixer_forward_schedule_reservation_20261008.sql',
 'DRAFT_fixer_forward_schedule_stage_20261008.sql','DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql',
 'DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql',
 'DRAFT_fixer_photo_historical_clearance_20261008.sql','DRAFT_fixer_prospective_photo_authority_20261008.sql',
 'DRAFT_fixer_photo_historical_clearance_20261008.sql','DRAFT_fixer_prospective_still_v2_20261008.sql')


def pg17_bin():
 candidates=[]
 configured=os.environ.get('PG17_BIN')
 if configured:candidates.append(Path(configured))
 candidates.extend((Path('/opt/homebrew/opt/postgresql@17/bin'),Path('/usr/local/opt/postgresql@17/bin')))
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


def main(*, runtime_check=None, extra_migrations=(), genuine_sources=False):
 import psycopg
 pg=pg17_bin()
 assert shutil.disk_usage('/tmp').free>5*1024**3
 with tempfile.TemporaryDirectory(prefix='still_v2_pg_',dir='/tmp') as tmp:
  work=Path(tmp); sock=work/'sock';sock.mkdir();data=work/'data';port=random.randint(41000,59000)
  subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
  subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(work/'pg.log'),'-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
  try:
   dsn=f'host={sock} port={port} dbname=postgres user=postgres'
   admin=psycopg.connect(dsn,autocommit=True)
   def sql(q,args=None,role=None):
    conn=admin if role is None else psycopg.connect(dsn)
    try:
     with conn.cursor() as c:
      if role:c.execute('set role '+role)
      c.execute(q,args);result=c.fetchall() if c.description else None
     if role:conn.commit()
     return result
    finally:
     if role:conn.close()
   def denied(q,args=None,role='service_role',fragment=None):
    try:sql(q,args,role);raise AssertionError('unexpected authority success')
    except psycopg.Error as exc:
     if fragment:assert fragment in str(exc),str(exc)
   sql('create role anon;create role authenticated;create role service_role;'
       'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
       'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
       'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,eligible boolean default true,excluded_by_coach boolean default false,review_status text default \'approved\',moderation_status text default \'clean\',review_content_hash text,reviewed_by text default \'SYNTHETIC reviewer\',reviewed_at timestamptz default now(),moderation_json jsonb,people_detected boolean default false,used_count integer default 0);')
   for name in (*MIGRATIONS, *extra_migrations):
    try:sql((ROOT/'migrations'/name).read_text())
    except Exception as exc:raise AssertionError(name+': '+str(exc)) from exc
   assert sql("select current_setting('server_version_num')::integer between 170000 and 179999")[0][0]
   sql('create role photo_owner login;grant fixer_forward_media_owner_20261006 to photo_owner;'
       'create role photo_auditor;grant fixer_forward_media_photo_auditor_20261007 to photo_auditor;'
       'grant select,insert,update,delete on content_calendar to service_role;')
   _,key,_,private=fixtures()
   sql("insert into fixer_forward_media_photo_policy_20261007 values(%s,true,'complete_fleet_still_photo_history','SYNTHETIC narrow still-only ruling','SYNTHETIC cutover','SYNTHETIC admin')",(key['policy_id'],))
   sql('insert into fixer_forward_media_photo_key_20261007 values(%s,%s,%s,%s,%s,true)',(key['key_id'],key['auditor_id'],'photo_auditor',key['policy_id'],key['public_key_hex']))
   history=[{'history_key':'SYNTHETIC historical still','resolved':True,'media_kind':'still_photo','visual_sha256':digest('other historic still'),'published_binding_ref':'SYNTHETIC retained still'}]
   videos=[{'history_key':'video:'+str(i),'media_kind':'reviewed_video_scope_exclusion','published_binding_ref':'SYNTHETIC video:'+str(i)} for i in range(113)]
   baseline=str(uuid.uuid4())
   sql('insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,excluded_video_manifest_ref,excluded_rows_json,declared_full_fleet_row_count) values(%s,%s,true,%s::jsonb,%s,%s,%s::jsonb,114)',(baseline,key['policy_id'],json.dumps(history),'SYNTHETIC full still history','SYNTHETIC 113 individually classified videos',json.dumps(videos)))
   sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s where singleton',(baseline,))
   sql("update fixer_forward_media_photo_state_20261007 set enabled=true,routes_reconciled_ref='SYNTHETIC complete owner routes' where singleton")
   sql('update forward_schedule_reservation_gate_20261008 set enabled=true')
   if genuine_sources:
    sql("alter table media_asset add column kind text default 'photo'")
   recipe=attester.make_still_recipe('identity')

   def seed(tenant='gym',day='2026-10-10',logical=None,data_bytes=None):
    data_bytes=data_bytes or png('blue'); md5=hashlib.md5(data_bytes).hexdigest();sha=hashlib.sha256(data_bytes).hexdigest()
    rid=str(uuid.uuid4());logical=logical or str(uuid.uuid4());asset='asset_'+uuid.uuid4().hex;source='src_'+uuid.uuid4().hex;url='https://scratch.example/'+uuid.uuid4().hex+'.png';group='vg_'+uuid.uuid4().hex
    sql("insert into media_source values(%s,%s,'gym_drive',%s,true)",(source,tenant,'FolderOriginal1234567' if genuine_sources else 'folder'))
    moderation={'verdict':'clean','provider':'SYNTHETIC scanner','content_hash':md5,'asset_id':asset,'gym_id':tenant,'people_detected':False,'observed_at':'2026-10-08T00:00:00Z','sha256':sha}
    sql('insert into media_asset(id,source_id,gym_id,content_hash,review_content_hash,moderation_json) values(%s,%s,%s,%s,%s,%s::jsonb)',(asset,source,tenant,md5,md5,json.dumps(moderation)))
    row={'id':rid,'gym_id':tenant,'post_date':day,'account':'instagram','format':'feed','status':'pending','logical_post_id':logical,'source_media_url':url,'image_url':url,'source_media_asset_id':asset,'visual_group_key':group}
    observation={'schema_version':1,'provenance_status':'unverified','tenant':tenant,'source_asset_id':asset,'source_exact_url':url,'delivered_exact_url':url,'source_sha256':sha,'delivered_sha256':sha,'source_byte_length':len(data_bytes),'delivered_byte_length':len(data_bytes),'recipe':recipe,'hold_reasons':[]}
    raw=canonical(observation);obs={'digest_input':raw,'observation_json':canonical(dict(observation,observation_digest=hashlib.sha256(raw.encode()).hexdigest()))}
    batch=str(uuid.uuid4());req=canonical({'members':[{'row':row,'observation':obs}],'old_rows':[]})
    sql('select stage_forward_schedule_batch_20261008(%s,%s,%s,%s)',(tenant,batch,req,hashlib.sha256(req.encode()).hexdigest()),'service_role')
    receipt='source-receipt:'+digest(rid)
    if genuine_sources:
     from agent import forward_media_owner as owner
     from agent.forward_media_source_history import SourceHistoryStore
     from agent.forward_media_source_verifier import verify_source
     class SourceDrive:
      def metadata(self,file_id):
       if file_id=='FolderOriginal1234567':
        return {'id':file_id,'mimeType':'application/vnd.google-apps.folder','trashed':False,'version':'2'}
       assert file_id==asset
       return {'id':asset,'mimeType':'image/png','trashed':False,'version':'3',
        'parents':['FolderOriginal1234567'],'size':str(len(data_bytes)),'md5Checksum':md5}
      def original_bytes(self,file_id):
       assert file_id==asset;return data_bytes
     class SourceHosted:
      def read(self,exact_url):
       assert exact_url==url;return data_bytes
     revision=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(rid,))[0][0]
     source_conn=psycopg.connect(dsn);source_conn.execute('set role photo_owner');source_conn.commit()
     persistence=owner.ForwardMediaOwnerPersistence(source_conn,'photo_owner',SourceHosted())
     try:
      store=SourceHistoryStore(persistence)
      source_snapshot=store.snapshot(rid,revision)
      verified=verify_source(source_snapshot,SourceDrive(),SourceHosted())
      store.stage_source(verified);source_conn.commit();receipt=verified.receipt_ref
     finally:source_conn.close()
    else:
     sql('insert into fixer_forward_media_source_receipt_20261007(receipt_ref,calendar_row_id,row_revision,binding_revision,tenant_id,source_asset_id,source_id,folder_id,exact_source_url,source_fingerprint,source_sha256,source_length,evidence_json) select %s,r.id,md5(to_jsonb(r)::text),md5(jsonb_build_array(to_jsonb(a),to_jsonb(s))::text),%s,%s,%s,\'folder\',%s,%s,%s,%s,\'SYNTHETIC independently verified original\' from content_calendar r join media_asset a on a.id=r.source_media_asset_id join media_source s on s.id=a.source_id where r.id=%s',(receipt,tenant,asset,source,url,'md5:'+md5,'sha256:'+sha,len(data_bytes),rid))
    candidate={'calendar_row_id':rid,'tenant_id':tenant,'group_key':group,'post_date':day,'source_asset_id':asset,'source_url':url,'image_url':url,'source_fingerprint':'md5:'+md5,'source_sha256':'sha256:'+sha,'source_length':len(data_bytes),'image_fingerprint':'md5:'+md5,'image_sha256':'sha256:'+sha,'image_length':len(data_bytes),'source_receipt_ref':receipt,'render_recipe_digest':digest(recipe),'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(rid,))[0][0],'logical_post_id':logical}
    snapshot=sql('select fixer_still_photo_snapshot_v2_20261008()')[0][0]
    dispositions=[{'history_key':h['history_key'],'disposition':'reviewed_visual_nonmatch','inspected_sha256':h['visual_sha256'],'published_binding_ref':h['published_binding_ref'],'review_evidence_ref':'SYNTHETIC independent per-object still review'} for h in snapshot['rows']]
    payload={'schema_version':2,'candidate_media_kind':'still_photo','audit_id':str(uuid.uuid4()),'auditor_id':key['auditor_id'],'key_id':key['key_id'],'policy_id':key['policy_id'],'baseline_id':snapshot['baseline_id'],'generation':snapshot['generation'],'spine_digest':snapshot['spine_digest'],'candidate':candidate,'dispositions':dispositions,'disposition_digest':digest(dispositions),'decision':'no_prior_published_still_image_or_derivative_use','stated_visual_uncertainty':'SYNTHETIC still review. Prior video frames remain unreviewed and outside this precise scope.','scope':'published_still_images_and_derivatives','accounted_video_digest':snapshot['excluded_rows_digest'],'accounted_videos':[{'history_key':v['history_key'],'published_binding_ref':v['published_binding_ref'],'disposition':'accounted_out_of_scope_video_frames_unreviewed','review_evidence_ref':'SYNTHETIC independently classified video binding'} for v in snapshot['accounted_video_rows']]}
    packet={'payload':payload,'signature_hex':private.sign(canonical(payload).encode()).hex()}
    audit_conn=psycopg.connect(dsn);audit_conn.execute('set role photo_auditor')
    try:cert=IndependentStillPhotoAuditorV2(audit_conn,'photo_auditor').submit(packet);audit_conn.commit()
    finally:audit_conn.close()
    original=prepare.OriginalRegistration(tenant,asset,url,'md5:'+md5,len(data_bytes),receipt)
    manifest=prepare.build_render_manifest(original,url,data_bytes,'same_object',cert.receipt_ref,render_recipe=recipe)
    original_json=asdict(original)
    manifest=asdict(manifest)
    return {'rid':rid,'logical':logical,'batch':batch,'tenant':tenant,'day':day,'asset':asset,'url':url,'sha':sha,'md5':md5,'bytes':data_bytes,'packet':packet,'original':original_json,'manifest':manifest}

   def prepare_and_attest(c,phash=0,delivered_phash=None):
    sql('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',(c['packet']['payload']['audit_id'],json.dumps(c['original']),json.dumps(c['manifest'])),'photo_owner')
    sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)',(c['rid'],),'service_role')
    attest(c,phash=phash,delivered_phash=delivered_phash)

   def attest(c,phash=0,delivered_phash=None):
    rev=sql('select fixer_forward_media_attestation_request_20261006(%s)->>\'revision\'',(c['rid'],))[0][0]
    ev=str(uuid.uuid4())
    sql('select fixer_attest_forward_media_20261006(%s,%s,%s,%s,%s,%s,%s,null,null,\'same_object\',\'SYNTHETIC trusted byte read\')',(c['rid'],rev,ev,'md5:'+c['md5'],len(c['bytes']),'md5:'+c['md5'],len(c['bytes'])),'fixer_forward_media_attester_20261006')
    reads=sql('select source_read_receipt,image_read_receipt from fixer_forward_media_lineage_20261006 where evidence_id=%s',(ev,))[0]
    ids=[];nrev=int(rev[:15],16)
    for role,read in [('original',reads[0]),('delivered',reads[1]),('thumbnail',reads[1])]:
     aid=str(uuid.uuid4());ids.append(aid)
     sql('insert into forward_media_visual_attestation(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(aid,c['tenant'],c['url'],role,c['sha'],c['md5'],len(c['bytes']),phash if role=='original' or delivered_phash is None else delivered_phash,nrev,ev,read),'fixer_forward_media_attester_20261006')
    c.update(rev=rev,evidence=ev,ids=ids)
   def admit(c):
    return sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',(c['rid'],c['logical'],c['rev'],c['ids'],c['packet']['payload']['audit_id']),'photo_owner')[0][0]

   if runtime_check is not None:
    runtime_check(sql=sql,denied=denied,seed=seed,attest=attest,dsn=dsn)
    admin.close();return

   c=seed()
   denied('select fixer_prepare_owner_staged_photo_20261008(%s,%s::jsonb,%s::jsonb)',(c['packet']['payload']['audit_id'],json.dumps(c['original']),json.dumps(c['manifest'])),'photo_owner','exclusions')
   sql('update forward_prospective_photo_gate_20261008 set enabled=true')
   # Independent verification rejects signature tampering and unknown kinds.
   snap=sql('select fixer_still_photo_snapshot_v2_20261008()')[0][0]
   approved=sql('select fixer_forward_media_photo_approved_key_20261007(%s)',(key['key_id'],))[0][0]
   bad=copy.deepcopy(c['packet']);bad['signature_hex']='0'*128
   try:verify_still_v2(bad,approved,snap);raise AssertionError('tampered signature accepted')
   except PhotoCertificateHold as exc:assert str(exc)=='certificate_signature_invalid'
   unknown_snap=copy.deepcopy(snap);unknown_snap['accounted_video_rows'][0]['media_kind']='unknown'
   try:verify_still_v2(c['packet'],approved,unknown_snap);raise AssertionError('unknown kind accepted')
   except PhotoCertificateHold as exc:assert 'unknown_kind_hold' in str(exc)
   incomplete=copy.deepcopy(c['packet']['payload']);incomplete['audit_id']=str(uuid.uuid4());incomplete['accounted_videos'].pop()
   text=canonical(incomplete);sig=private.sign(text.encode()).hex();ref='photo-audit:sha256:'+hashlib.sha256((text+'\n'+sig).encode()).hexdigest()
   denied('select fixer_still_photo_record_v2_20261008(%s,%s,%s)',(text,sig,ref),'photo_auditor','complete separately versioned')
   prepare_and_attest(c,delivered_phash=(1<<32)-1)
   sql('update forward_prospective_photo_gate_20261008 set enabled=false')
   denied('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',(c['rid'],c['logical'],c['rev'],c['ids'],c['packet']['payload']['audit_id']),'photo_owner','OFF')
   sql('update forward_prospective_photo_gate_20261008 set enabled=true')
   occupancy=admit(c);assert occupancy==admit(c)
   sql('update forward_schedule_reservation_gate_20261008 set enabled=true')
   sql('update forward_media_visual_gate_20261008 set enabled=true')
   candidates=[{'calendar_row_id':c['rid'],'logical_post_id':c['logical'],'expected_revision':c['rev'],'attestation_ids':c['ids'],'expected_reservation_id':None}]
   args=(c['tenant'],c['batch'],json.dumps(candidates),'[]')
   fin=sql('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',args,'service_role')[0][0]
   assert fin==sql('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',args,'service_role')[0][0]
   sql('insert into fixer_forward_media_claim_gate_20261006 values(%s,true)',(c['tenant'],))
   token=str(uuid.uuid4())
   sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",(token,c['rid']))
   claimargs=(c['rid'],token,c['evidence'],c['rev'],c['ids'])
   assert sql('select fixer_forward_visual_index_claim_20261008(%s,%s,%s,%s,%s::uuid[])',claimargs,'service_role')[0][0]
   assert sql('select fixer_forward_visual_index_claim_20261008(%s,%s,%s,%s,%s::uuid[])',claimargs,'service_role')[0][0]
   # Unknown published history holds finalization/claim replay, then removal
   # of this synthetic unknown restores the original immutable receipts.
   unknown=str(uuid.uuid4())
   sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,image_url) values(%s,'unknown-gym','2026-10-09','published','active','https://scratch.example/unknown')",(unknown,))
   denied('select fixer_forward_visual_index_claim_20261008(%s,%s,%s,%s,%s::uuid[])',claimargs,'service_role','unknown or ambiguous')
   denied('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',args,'service_role','unknown or ambiguous')
   sql('delete from content_calendar where id=%s',(unknown,))
   assert sql('select fixer_forward_visual_index_claim_20261008(%s,%s,%s,%s,%s::uuid[])',claimargs,'service_role')[0][0]

   # A second independent visual may prepare, but cannot skip permanent
   # admission at reserve, finalization or claim; an aborted admission leaves
   # neither occupancy nor a row binding and does not activate its stage.
   pending=seed(data_bytes=png('green'));prepare_and_attest(pending,phash=-1)
   pending_candidates=[{'calendar_row_id':pending['rid'],'logical_post_id':pending['logical'],'expected_revision':pending['rev'],'attestation_ids':pending['ids'],'expected_reservation_id':None}]
   pending_args=(pending['tenant'],pending['batch'],json.dumps(pending_candidates),'[]')
   denied('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',pending_args,'service_role','requires permanent prospective admission')
   denied('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[],null)',(pending['rid'],pending['logical'],pending['rev'],pending['ids']),'service_role','requires permanent prospective admission')
   denied('select fixer_forward_visual_index_claim_20261008(%s,%s,%s,%s,%s::uuid[])',(pending['rid'],str(uuid.uuid4()),pending['evidence'],pending['rev'],pending['ids']),'service_role','requires permanent prospective admission')
   rollback_conn=psycopg.connect(dsn)
   rollback_conn.execute('set role photo_owner')
   rollback_conn.execute('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',(pending['rid'],pending['logical'],pending['rev'],pending['ids'],pending['packet']['payload']['audit_id']))
   rollback_conn.rollback();rollback_conn.close()
   assert sql('select count(*) from forward_prospective_photo_occupancy_20261008')[0][0]==1
   assert sql('select variant_status from content_calendar where id=%s',(pending['rid'],))[0][0]=='candidate'

   sql('select release_forward_slot_20261008(%s,\'SYNTHETIC release\')',(fin['reservation_ids'][0],),'service_role')
   assert sql('select count(*) from forward_prospective_photo_occupancy_20261008')[0][0]==1
   # Permanence survives schedule release. A fresh signed packet cannot
   # overrule used still bytes for a different tenant, date or logical post.
   for tenant,day in [('other-gym','2026-10-10'),('gym','2026-10-11'),('gym','2026-10-10')]:
    competitor=seed(tenant=tenant,day=day,data_bytes=c['bytes'])
    denied('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',(competitor['packet']['payload']['audit_id'],json.dumps(competitor['original']),json.dumps(competitor['manifest'])),'photo_owner','already used cleared or reserved')
    # Trusted original receipt already provides the exact conflict key;
    # neither absence of a manifest nor a fake claim token bypasses it.
    denied('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[],null)',(competitor['rid'],competitor['logical'],c['rev'],c['ids']),'service_role','permanent prospective still occupancy conflict')
    denied('select fixer_forward_visual_index_claim_20261008(%s,%s,%s,%s,%s::uuid[])',(competitor['rid'],str(uuid.uuid4()),c['evidence'],c['rev'],c['ids']),'service_role','permanent prospective still occupancy conflict')
    cmp_candidates=[{'calendar_row_id':competitor['rid'],'logical_post_id':competitor['logical'],'expected_revision':c['rev'],'attestation_ids':c['ids'],'expected_reservation_id':None}]
    denied('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',(tenant,competitor['batch'],json.dumps(cmp_candidates),'[]'),'service_role','permanent prospective still occupancy conflict')
   advisory=sql('select check_reservation_conflicts_20261008(%s,%s,%s,%s,%s)',('other-gym','2026-10-10',str(uuid.uuid4()),c['sha'],0),'service_role')[0][0]
   assert advisory['allowed'] is False and any(x['kind']=='permanent_prospective_still' for x in advisory['conflicts'])
   # Synthetic per-role pHashes deliberately differ by >30 bits: only the
   # occupied delivered/thumbnail hashes match this independent contender.
   role_match=seed(data_bytes=png('red'));prepare_and_attest(role_match,phash=(1<<32)-1)
   denied('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',(role_match['rid'],role_match['logical'],role_match['rev'],role_match['ids'],role_match['packet']['payload']['audit_id']),'photo_owner','permanent prospective still occupancy conflict')
   denied('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[],null)',(role_match['rid'],role_match['logical'],role_match['rev'],role_match['ids']),'service_role','permanent prospective still occupancy conflict')
   inverse=seed(data_bytes=png('yellow'));prepare_and_attest(inverse,phash=-1)
   inverse_read=sql('select source_read_receipt from fixer_forward_media_lineage_20261006 where evidence_id=%s',(inverse['evidence'],))[0][0]
   occupied_read=sql('select source_read_receipt from fixer_forward_media_lineage_20261006 where evidence_id=%s',(c['evidence'],))[0][0]
   sql("insert into fixer_forward_media_lineage_20261006(evidence_id,calendar_row_id,row_revision,operation,tenant_id,group_key,source_read_receipt,image_read_receipt,source_asset_id,manifest_digest,render_evidence_ref,verified_by) values(%s,%s,'SYNTHETIC inverse historical revision','render','gym','SYNTHETIC inverse transform',%s,%s,%s,%s,'SYNTHETIC candidate ancestor to occupied derivative','SYNTHETIC history attester')",(str(uuid.uuid4()),str(uuid.uuid4()),inverse_read,occupied_read,inverse['asset'],inverse['manifest']['manifest_digest']))
   denied('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',(inverse['rid'],inverse['logical'],inverse['rev'],inverse['ids'],inverse['packet']['payload']['audit_id']),'photo_owner','permanent prospective still occupancy conflict')
   denied('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[],null)',(inverse['rid'],inverse['logical'],inverse['rev'],inverse['ids']),'service_role','permanent prospective still occupancy conflict')
   # Seed one durable trusted derivative edge in the synthetic history graph.
   # Green has distinct bytes and pHash distance 64; the ancestry alone blocks.
   blue_read=sql('select source_read_receipt from fixer_forward_media_lineage_20261006 where evidence_id=%s',(c['evidence'],))[0][0]
   green_read=sql('select source_read_receipt from fixer_forward_media_lineage_20261006 where evidence_id=%s',(pending['evidence'],))[0][0]
   sql("insert into fixer_forward_media_lineage_20261006(evidence_id,calendar_row_id,row_revision,operation,tenant_id,group_key,source_read_receipt,image_read_receipt,source_asset_id,manifest_digest,render_evidence_ref,verified_by) values(%s,%s,'SYNTHETIC historical revision','render','gym','SYNTHETIC historic transform',%s,%s,%s,%s,'SYNTHETIC durable independently verified derivative edge','SYNTHETIC history attester')",(str(uuid.uuid4()),str(uuid.uuid4()),blue_read,green_read,c['asset'],c['manifest']['manifest_digest']))
   denied('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[],null)',(pending['rid'],pending['logical'],pending['rev'],pending['ids']),'service_role','permanent prospective still occupancy conflict')
   denied('select fixer_forward_media_provenance_lookup_20261006(%s)',(pending['rid'],),'fixer_forward_media_attester_20261006','permanent prospective still occupancy conflict')
   assert sql('select jsonb_array_length(excluded_rows_json) from fixer_forward_media_photo_baseline_20261007 where baseline_id=%s',(baseline,))[0][0]==113
   sql('update forward_prospective_photo_gate_20261008 set enabled=false')
   denied('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',args,'service_role','OFF')
   # Turning off the feature preserves the permanent conflict fence.
   denied('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[],null)',(pending['rid'],pending['logical'],pending['rev'],pending['ids']),'service_role','permanent prospective still occupancy conflict')
   print('PASS: complete PG17 stack, 113 accounted videos retained, v1 HOLD, real Ed25519 v2, owner staged preparation, permanent admission, owned staged finalization/replay and final claim/replay; no-admission bypass holds; unknown history/signature/accounting holds; atomic rollback; tenant/date/logical, all occupied-role pHashes and BOTH derivative directions rejected after release; OFF holds with fences preserved. No provider publication asserted.')
   admin.close()
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','stop'],check=True,capture_output=True,timeout=60)


def test_still_v2_pg():main()
if __name__=='__main__':main()
