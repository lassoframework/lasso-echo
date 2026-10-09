from pathlib import Path
from datetime import timedelta
import uuid,json,hashlib,io,tempfile,os
from types import SimpleNamespace
from unittest.mock import patch
import psycopg
from psycopg.types.json import Jsonb
from PIL import Image
from agent import generated_infographic_preparation as prep, generated_infographic_runtime as runtime, forward_media_guard as guard, generated_client_admission as client, generated_hosted_byte_authority as hosted, forward_media_visual_index as visual, forward_schedule_batch_finalizer_worker as finalworker
from agent.portal_calendar_store import SupabaseCalendarStore
from tests.test_generated_owner_guard import candidate,trusted,image_bytes


def synthetic_ordinary_certificate(s, rid, fp, lengths=(10, 10, 30)):
    admin = s.admin
    tenant, url, image, thumbnail = s.one(
        "select gym_id||'|'||source_media_url||'|'||image_url||'|'||coalesce(thumbnail_url,'')"
        " from content_calendar where id=%s", (rid,)).split("|")
    existing = s.one("select source_asset_id from fixer_forward_media_original_registry_20261006"
                     " where tenant_id=%s and source_url=%s", (tenant, url))
    asset = existing or "original_" + uuid.uuid4().hex
    if not existing:
        s.sql("insert into fixer_forward_media_original_registry_20261006"
              "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)"
              " values(%s,%s,%s,%s,%s,'owner-verified-original')", (tenant, asset, url, fp, lengths[0]))
    # Certified positive clearance chain required by the real owner photo
    # clearance guards: key -> certificate -> exact owner reservation (whose
    # manifest_json must equal the rendition row below) -> clearance bound to
    # the reservation receipt -> the exact signed rendition manifest.
    s.sql("insert into fixer_forward_media_photo_key_20261007"
          " values('SYNTHETIC key','SYNTHETIC auditor','SYNTHETIC role','SYNTHETIC policy',%s,true)"
          " on conflict (key_id) do nothing", (uuid.uuid4().hex + uuid.uuid4().hex,))
    digest = "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    manifest = {"manifest_digest": digest, "tenant_id": tenant, "source_asset_id": asset,
                "image_url": image, "image_fingerprint": fp, "image_length": lengths[1],
                "thumbnail_url": thumbnail or None, "thumbnail_fingerprint": fp if thumbnail else None,
                "thumbnail_length": lengths[2] if thumbnail else None,
                "operation": "same_object", "render_recipe": None, "render_evidence_ref": "owner-verified-render"}
    receipt_ref = "photo-audit:sha256:" + uuid.uuid4().hex + uuid.uuid4().hex
    original = {"tenant_id": tenant, "source_asset_id": asset, "source_url": url,
                "source_fingerprint": fp, "source_length": lengths[0],
                "registry_evidence_ref": "owner-verified-original"}
    source_id='synthetic-source-'+rid
    sha=s.ordinary_sha
    observed=s.one('select clock_timestamp()').isoformat()
    s.sql("insert into media_source(id,gym_id,kind,folder_id,active,sync_status,sync_finished_at) values(%s,%s,'gym_drive','SYNTHETIC folder',true,'ready',clock_timestamp())",(source_id,tenant))
    moderation=dict(verdict='clean',provider='SYNTHETIC',content_hash=fp[4:],asset_id=asset,gym_id=tenant,people_detected=False,observed_at=observed,sha256=sha)
    s.sql("insert into media_asset(id,source_id,gym_id,content_hash,rendition_url,kind,eligible,excluded_by_coach,review_status,moderation_status,review_content_hash,reviewed_by,reviewed_at,moderation_json,people_detected,used_count) values(%s,%s,%s,%s,%s,'photo',true,false,'approved','clean',%s,'SYNTHETIC reviewer',clock_timestamp(),%s,false,0)",(asset,source_id,tenant,fp[4:],url,fp[4:],Jsonb(moderation)))
    source_ref='source-receipt:sha256:'+hashlib.sha256(('SYNTHETIC source '+rid).encode()).hexdigest()
    s.sql("insert into fixer_forward_media_source_receipt_20261007(receipt_ref,calendar_row_id,row_revision,binding_revision,tenant_id,source_asset_id,source_id,folder_id,exact_source_url,source_fingerprint,source_sha256,source_length,evidence_json) values(%s,%s,%s,%s,%s,%s,%s,'SYNTHETIC folder',%s,%s,%s,%s,'SYNTHETIC verified bytes')",(source_ref,rid,'a'*32,'b'*32,tenant,asset,source_id,url,fp,'sha256:'+sha,lengths[0]))
    calendar=s.one('select to_jsonb(c) from content_calendar c where id=%s',(rid,))
    candidate=dict(tenant_id=tenant,post_date=calendar['post_date'],group_key=calendar['visual_group_key'],source_asset_id=asset,source_sha256='sha256:'+sha,source_receipt_ref=source_ref,source_fingerprint=fp,image_fingerprint=fp,thumbnail_fingerprint=None)
    audit_id = str(uuid.uuid4())
    s.sql("insert into fixer_forward_media_photo_certificate_20261007"
          "(audit_id,receipt_ref,calendar_row_id,key_id,baseline_id,generation,spine_digest,"
          "payload_json,signature_hex,verified_by)"
          " values(%s,%s,%s,'SYNTHETIC key',%s,1,'SYNTHETIC spine','{}',%s,'SYNTHETIC verifier')",
          (audit_id, receipt_ref, rid, s.baseline_id, uuid.uuid4().hex * 4))
    s.sql("insert into fixer_owner_photo_reservation_20261007"
          "(audit_id,receipt_ref,calendar_row_id,original_json,manifest_json,candidate_json)"
          " values(%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)",
          (audit_id, receipt_ref, rid, json.dumps(original), json.dumps(manifest),json.dumps(candidate)))
    s.sql("insert into fixer_forward_media_history_clearance_20261006"
          "(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,"
          "decision,history_evidence_ref) values(%s,%s,%s,%s,%s,'owner-verified-original',"
          "'cleared_unused',%s) on conflict do nothing",
          (tenant, asset, url, fp, lengths[0], receipt_ref))
    s.sql("insert into fixer_forward_media_render_manifest_20261006"
          "(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,"
          "thumbnail_url,thumbnail_fingerprint,thumbnail_length,operation,render_evidence_ref)"
          " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,'same_object','owner-verified-render')",
          (digest, tenant, asset, image, fp, lengths[1],
           thumbnail or None, fp if thumbnail else None, lengths[2] if thumbnail else None))
    s.sql("update content_calendar set source_media_asset_id=%s,render_manifest_digest=%s where id=%s",
          (asset, digest, rid))
    evidence = str(uuid.uuid4())
    revision = s.one("select fixer_forward_media_attestation_request_20261006(%s)->>'revision'", (rid,))
    conn = s.connect()
    conn.execute("set role fixer_forward_media_attester_20261006")
    conn.execute("select fixer_attest_forward_media_20261006"
                 "(%s,%s,%s,%s,%s,%s,%s,null,null,'same_object','controlled-runtime-test')",
                 (rid, revision, evidence, fp, lengths[0], fp, lengths[1]))
    conn.execute("reset role")
    conn.close()
    return evidence


class PgWorkerStore:
 _reservation_uuid=staticmethod(SupabaseCalendarStore._reservation_uuid)
 def __init__(self,connection,lost=False):self.connection=connection;self.lost=lost;self.finalcalls=0
 def _reservation_rpc(self,name,args,timeout=30):
  values=[Jsonb(v) if isinstance(v,(dict,list)) else v for v in args.values()]
  call='select public.'+name+'('+','.join(k+'=>%s' for k in args)+')'
  result=self.connection.execute(call,values).fetchone()[0]
  if name=='finalize_forward_schedule_staged_batch_20261008':
   self.finalcalls+=1
   if self.lost:
    self.lost=False
    from agent.portal_calendar_store import ReservationStoreError
    raise ReservationStoreError(0,'SYNTHETIC finalizer response lost')
  return result
 def _rest(self,table):return table
 def _headers(self):return {}
 def _client(self):return self
 def get(self,table,params,headers,timeout):
  from psycopg import sql as psql
  assert table in ('forward_schedule_stage_member_20261008','forward_schedule_stage_old_row_20261008','fixer_forward_media_lineage_20261006','forward_media_visual_attestation')
  filters=[];values=[]
  for key,value in params.items():
   if key in ('select','limit','order'):continue
   assert value.startswith('eq.');filters.append(psql.SQL('{}=%s').format(psql.Identifier(key)));values.append(value[3:])
  orders=[]
  for part in params.get('order','').split(','):
   if part:
    key,direction=part.split('.');assert direction in ('asc','desc');orders.append(psql.SQL('{} '+direction).format(psql.Identifier(key)))
  query=psql.SQL('select to_jsonb(t) from public.{} t where ').format(psql.Identifier(table))+psql.SQL(' and ').join(filters)
  if orders:query+=psql.SQL(' order by ')+psql.SQL(',').join(orders)
  query+=psql.SQL(' limit %s');values.append(int(params.get('limit','100')))
  rows=[r[0] for r in self.connection.execute(query,values).fetchall()]
  return SimpleNamespace(status_code=200,json=lambda:rows)

def run_ordinary(db,connect,sql,rpc,baseline):
 service=connect('service_role')
 os.environ['AGENT_S3_PUBLIC_BASE_URL']='https://owned.example'
 day=sql('select current_date')[0][0]+timedelta(days=1)
 sql("update forward_schedule_reservation_gate_20261008 set enabled=true where singleton")
 # Ordinary batch must pass the new public wrapper by exact delegation.
 # Only prerequisite owner certificate facts are synthetic fixture rows;
 # stage, independent byte observations, worker and finalizer are actual code.
 import random
 class Fixture:
  admin=db;baseline_id=baseline
  ordinary_sha=''
  def connect(self,role='postgres'):return connect(role)
  def sql(self,q,args=None,conn=None):return sql(q,args,conn or db)
  def one(self,q,args=None,conn=None):
   rows=self.sql(q,args,conn);return rows[0][0] if rows else None
 ordinary=str(uuid.uuid4());ordinary_logical=str(uuid.uuid4());ordinary_batch=uuid.uuid4()
 img=Image.frombytes('RGB',(128,128),random.Random(71).randbytes(128*128*3));buf=io.BytesIO();img.save(buf,format='PNG');ordinary_bytes=buf.getvalue()
 ordinary_url='https://owned.example/ordinary/'+ordinary+'.png'
 planned=dict(id=ordinary,gym_id='gym',post_date=(day+timedelta(days=1)).isoformat(),account='instagram',format='feed',status='pending',variant_status='candidate',caption='SYNTHETIC ordinary pending',logical_post_id=ordinary_logical,visual_group_key='vg_'+uuid.uuid4().hex,source_media_url=ordinary_url,image_url=ordinary_url)
 request=prep.canonical(dict(members=[dict(row=planned,observation=None)],old_rows=[]))
 rpc(service,'stage_forward_schedule_batch_20261008','gym',ordinary_batch,request,hashlib.sha256(request.encode()).hexdigest())
 fixture=Fixture();fixture.ordinary_sha=hashlib.sha256(ordinary_bytes).hexdigest()
 synthetic_ordinary_certificate(fixture,ordinary,'md5:'+hashlib.md5(ordinary_bytes).hexdigest(),lengths=(len(ordinary_bytes),)*3)
 ordinary_revision=rpc(db,'fixer_forward_media_attestation_request_20261006',ordinary)['revision']
 ordinary_lineage=sql('select evidence_id from fixer_forward_media_lineage_20261006 where calendar_row_id=%s',(ordinary,))[0][0]
 visual.attest(ordinary,ordinary_revision,str(ordinary_lineage),connection_factory=lambda:connect(guard.ROLE,False),read_bytes=lambda url:ordinary_bytes)
 ordinary_store=PgWorkerStore(connect('service_role'));ordinary_status=finalworker.batch_status(ordinary_store,str(ordinary_batch))
 ordinary_receipt,ordinary_via=finalworker.finalize_batch(ordinary_store,ordinary_status)
 assert ordinary_via=='finalize_receipt'
 repeat=finalworker.batch_status(ordinary_store,str(ordinary_batch))
 assert repeat['finalize_receipt']==ordinary_receipt
 terminal_args=sql('select finalize_request from forward_schedule_stage_batch_20261008 where batch_id=%s',(ordinary_batch,))[0][0]
 assert rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',ordinary_batch,Jsonb(terminal_args['candidates']),Jsonb(terminal_args['expected_old_rows']))==ordinary_receipt
 assert sql('select status,variant_status,creative_origin from content_calendar where id=%s',(ordinary,))[0]==('pending','active',None)
 print('PASS ordinary non-generated staged batch through actual worker/public wrapper + exact terminal retry',flush=True)


def run(db,sock,port):
 conns=[]
 def connect(role='postgres',auto=True):
  con=psycopg.connect(host=str(sock),port=port,user=role,dbname='postgres',autocommit=auto);conns.append(con);return con
 def sql(q,args=None,con=db):
  cur=con.execute(q,args);return cur.fetchall() if cur.description else None
 def rpc(con,name,*args):
  return con.execute('select public.'+name+'('+','.join(['%s']*len(args))+')',args).fetchone()[0]
 def denied(fn,label):
  try:fn()
  except Exception as e:print('PASS HOLD',label,str(e).splitlines()[0],flush=True);return
  raise AssertionError('unexpected permission '+label)
 try:
  sql("create role generated_owner login;grant fixer_forward_media_owner_20261006 to generated_owner;alter role fixer_forward_media_attester_20261006 login;alter role service_role login;grant select,insert,update on content_calendar to service_role;")
  owner=connect('generated_owner');service=connect('service_role');baseline=uuid.uuid4()
  sql("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC policy',true,'complete_fleet_still_photo_history',null,'SYNTHETIC reconciliation','SYNTHETIC admin')")
  sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC policy',true,'[]','SYNTHETIC empty full fleet',0)",(baseline,))
  sql("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'",(baseline,))
  sql("insert into fixer_forward_media_claim_gate_20261006 values('gym',true)")
  rpc(owner,'fixer_still_cutover_control_20261007',True,'SYNTHETIC cutover')
  if os.getenv('ONLY_ORDINARY'):
   run_ordinary(db,connect,sql,rpc,baseline);return
  gym,web,social=[uuid.uuid4() for _ in range(3)]
  sql('alter table gyms add column slug text,add column name text');sql("insert into gyms(id,slug,name) values(%s,'synthetic','Synthetic Gym')",(gym,));sql("insert into app_users values(%s,'SYNTHETIC Blake identity','owner','blake@lassoframework.com')",(uuid.uuid4(),))
  sql("insert into echo_intake_tokens values(%s,'gym')",(gym,));sql("insert into echo_gym_settings values(%s,false,'synthetic')",(gym,))
  sql("insert into fixer_generated_portal_tenant_map_20261008(echo_account_key,gym_id,approval_evidence_ref,approved_by) values('gym',%s,'SYNTHETIC map approval','SYNTHETIC owner')",(gym,))
  text='SYNTHETIC training fact';raw=(text+' #112233 #aabbcc').encode()
  for ident,kind,url,locator,data in ((web,'website','https://synthetic.test/',None,raw),(social,'social','https://api.apify.com/v2/synthetic','https://www.instagram.com/synthetic/',b'SYNTHETIC social bytes')):
   sql('''insert into echo_source_captures(id,gym_id,echo_account_key,source_kind,source_url,provider_account_id,source_locator,capture_provider,provider_response_id,source_revision,mapping_revision,mapping_evidence,fetched_at,raw_bytes)
     values(%s,%s,'gym',%s,%s,%s,%s,%s,%s,'SYNTHETIC source','SYNTHETIC mapping','{"synthetic":true}',clock_timestamp(),%s)''',(ident,gym,kind,url,'12345' if kind=='social' else None,locator,'apify' if kind=='social' else 'direct','SYNTHETIC response' if kind=='social' else None,data))
  spans=json.dumps([dict(key='training',capture_id=str(web),byte_offset=0,byte_length=len(text.encode()))]);primary,secondary=raw.index(b'#112233'),raw.index(b'#aabbcc')
  rpc(service,'echo_source_brand_prepare',gym,'SYNTHETIC Blake identity',[web,social],web,primary,secondary,None,spans)
  b=sql('select to_jsonb(b) from echo_source_brand_bundles b')[0][0]
  rpc(service,'echo_source_brand_decide',gym,'SYNTHETIC Blake identity',b['id'],b['content_sha256'],b['version'],uuid.uuid4(),'approve')
  rpc(service,'echo_source_brand_revalidate',gym,b['id'],b['content_sha256'],[web,social],web,primary,secondary,spans,'SYNTHETIC validator',json.dumps(dict(selected_facts_status='supported_uncontradicted',identity_status='verified')))
  active=rpc(owner,'fixer_generated_source_brand_active_20261007','gym');authority=runtime.delegated_copy(active['active'],'gym',caption=text);pins,derivation=authority['authority_pins'],authority['copy_derivation_receipt']
  day=sql('select current_date')[0][0]+timedelta(days=1)
  q=rpc(service,'fixer_generated_gap_dispatch_20261007',uuid.uuid4(),'gym',day,'instagram','feed')
  rid,logical=uuid.uuid4(),uuid.uuid4();group='vg_generated_'+logical.hex
  args=(q['request_id'],rid,logical,group,text,authority['source_revision'],authority['palette_revision'],prep.digest(authority['palette']),authority['palette']['evidence_ref'],json.dumps(pins),json.dumps(derivation))
  assert rpc(owner,'fixer_generated_gap_bind_bundle_20261007',*args)['bound']
  snap=rpc(owner,'fixer_generated_snapshot_20261007',rid)
  rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,snap['inventory_revision'],True,0,'SYNTHETIC current zero')
  first=Image.open(io.BytesIO(image_bytes())).resize((1024,1280));stream=io.BytesIO();first.save(stream,format='PNG');pixels=stream.getvalue()
  from agent import config
  config.S3_PUBLIC_BASE_URL='https://owned.example'
  os.environ['AGENT_S3_PUBLIC_BASE_URL']='https://owned.example'
  c=candidate(trusted(snap),pixels);c['original_url']='https://owned.example/echo-generated-originals/gym/'+c['original_sha256']+'.png';c.update(schema_version=2,width=1024,height=1280,storage_key='echo-generated-originals/gym/'+c['original_sha256']+'.png',authority_pins=pins,copy_derivation_receipt=derivation,palette_revision=authority['palette_revision'],copy_digest=prep.digest(authority['copy']),palette_digest=prep.digest(authority['palette']))
  binding={k:c[k] for k in (*prep.BINDING_FIELDS,'copy_digest','palette_digest','review_policy_id','authority_pins','copy_derivation_receipt')};c['job_id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'echo-astra:'+prep.canonical(binding)))
  m=dict(tenant_id='gym',source_asset_id='generated-astra:'+c['job_id'],image_url=c['original_url'],image_fingerprint='md5:'+c['original_md5'],image_length=c['original_length'],thumbnail_url=None,thumbnail_fingerprint=None,thumbnail_length=None,operation='same_object',render_recipe=None,render_evidence_ref='generated-astra:'+c['job_id']);m['manifest_digest']='sha256:'+prep.digest(m)
  sql("create role issuer_a login;create role reader_a login;grant generated_hosted_byte_issuer_20261009 to issuer_a;grant generated_hosted_byte_reader_20261009 to reader_a;insert into generated_hosted_byte_principals_20261009 values('issuer_a','gym',true,true),('reader_a','gym',false,true);insert into generated_client_control_20261009(owner_principal,gym_id,reader_principal) values('generated_owner','gym','reader_a')")
  class Reader(hosted.HostedObjectReader):
   def __init__(self):super().__init__({'gym':['https://owned.example/echo-generated-originals/gym/']})
   def read(self,tenant,url):assert tenant=='gym' and url==c['original_url'];return pixels
  reader=hosted.GeneratedHostedByteAuthority(lambda:connect('reader_a',False),tenant_id='gym');issuer=hosted.GeneratedHostedByteAuthority(lambda:connect('issuer_a',False),tenant_id='gym',reader=Reader())
  with tempfile.TemporaryDirectory(prefix='generated-client-journal-') as journal_path:
   jobs=prep.SQLiteGenerationJobs(Path(journal_path)/'jobs.sqlite');admission=client.GeneratedClientAdmission(jobs=jobs,authority=reader)
   new_id=client.candidate_row(str(rid),c['job_id']);version=client.artifact_version(new_id,c['job_id'])
   plan=rpc(owner,'generated_client_plan_20261009',rid,new_id,version,Jsonb(c),Jsonb(m))
   frozen=admission.journal.freeze(str(rid),c,m,authority['source_revision'],stage_plan=plan)
   issued=issuer.issue(artifact_version_id=version,hosted_url=c['original_url'],expected_sha256=c['original_sha256'],manifest_bytes=frozen['manifest_bytes'])
   admission.journal.attach_receipt(str(rid),issued['receipt_id']);print('PASS separate issuer + durable frozen manifest',flush=True)
   owner.autocommit=False
   with patch.dict(os.environ,{'AGENT_GENERATED_CLIENT_ADMISSION':'true'}):
    denied(lambda:admission.stage(SimpleNamespace(_conn=owner),str(rid),c,[],m,authority['source_revision']),'default OFF control')
    sql("update generated_client_control_20261009 set enabled=true")
    try:
     prepared=admission.stage(SimpleNamespace(_conn=owner),str(rid),c,[],m,authority['source_revision'])
    except client.AdmissionHold:
     rpc(owner,'generated_client_prepare_staged_20261009',rid,new_id,Jsonb(c),Jsonb([]),Jsonb(m),authority['source_revision'],version,issued['receipt_id'],frozen['manifest_bytes'])
     raise
    admission.before_commit(str(rid));owner.commit();admission.committed(str(rid))
   assert prepared['prepared'] and prepared['calendar_row_id']==new_id
   admission.journal.state(str(rid),'committing')
   with patch.dict(os.environ,{'AGENT_GENERATED_CLIENT_ADMISSION':'true'}):
    reconciled=admission.stage(SimpleNamespace(_conn=owner),str(rid),c,[],m,authority['source_revision'])
   assert reconciled['replayed'] and reconciled['stage_plan']==prepared['stage_plan']
   owner.rollback();admission.committed(str(rid))
   print('PASS unknown owner commit exact immutable preparation reconciliation',flush=True)
   assert sql('select count(*) from content_calendar where id=%s',(new_id,))[0][0]==0
   print('PASS committed lookup + detached owner preparation, new row absent',flush=True)
   owner.autocommit=True
   sql("update forward_schedule_reservation_gate_20261008 set enabled=true where singleton")
   batch=uuid.uuid4();payload=prep.canonical(dict(members=[dict(row=plan['planned_row'],observation=None)],old_rows=[plan['old_snapshot']]))
   staged=rpc(service,'stage_forward_schedule_batch_20261008','gym',batch,payload,hashlib.sha256(payload.encode()).hexdigest())
   assert staged['state']=='staged' and rpc(service,'stage_forward_schedule_batch_20261008','gym',batch,payload,hashlib.sha256(payload.encode()).hexdigest())==staged;print('PASS actual ordinary forward stage inserts pending inactive generated row',flush=True)
   denied(lambda:sql("update content_calendar set status='approved' where id=%s",(new_id,)),'approve before finalization')
   denied(lambda:sql("update content_calendar set variant_status='active',media_not_ready_reason=null where id=%s",(new_id,)),'direct activation')
   revision=rpc(db,'fixer_forward_media_attestation_request_20261006',new_id)['revision']
   lineage=guard.attest(new_id,revision,connection_factory=lambda:connect(guard.ROLE,False),original_verifier=lambda snap,source:source==pixels,read_bytes=lambda url:pixels)
   vis=visual.attest(new_id,revision,lineage['evidence_id'],connection_factory=lambda:connect(guard.ROLE,False),read_bytes=lambda url:pixels)
   candidates=[dict(calendar_row_id=new_id,logical_post_id=str(logical),expected_revision=revision,attestation_ids=list(vis['attestation_ids'].values()))]
   # Exact old-placeholder change must rollback before rebind/reservation.
   sql('update content_calendar set scheduled_at=clock_timestamp() where id=%s',(rid,))
   denied(lambda:rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(candidates),Jsonb([plan['old_snapshot']])),'late placeholder edit')
   assert str(sql('select calendar_row_id from fixer_generated_gap_request_20261007 where request_id=%s',(q['request_id'],))[0][0])==str(rid)
   assert sql('select variant_status from content_calendar where id=%s',(new_id,))[0][0]=='candidate'
   assert sql('select count(*) from forward_schedule_reservation')[0][0]==0
   sql('update content_calendar set scheduled_at=null where id=%s',(rid,))
   print('PASS failed finalizer leaves gap old, candidate inactive, zero slot reservations',flush=True)
   def assert_rollback():
    assert str(sql('select calendar_row_id from fixer_generated_gap_request_20261007 where request_id=%s',(q['request_id'],))[0][0])==str(rid)
    assert sql('select variant_status from content_calendar where id=%s',(new_id,))[0][0]=='candidate'
    assert sql('select count(*) from forward_schedule_reservation')[0][0]==0
   # Newest positive census must supersede earlier frozen zero authority.
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),new_id,snap['inventory_revision'],True,1,'SYNTHETIC late photo')
   denied(lambda:rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(candidates),Jsonb([plan['old_snapshot']])),'late photo')
   assert_rollback()
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),new_id,snap['inventory_revision'],True,0,'SYNTHETIC restored zero')
   sql("delete from generated_hosted_byte_principals_20261009 where principal='reader_a'")
   denied(lambda:rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(candidates),Jsonb([plan['old_snapshot']])),'reader grant revoked')
   assert_rollback();sql("insert into generated_hosted_byte_principals_20261009 values('reader_a','gym',false,true)")
   # Exact finalizer checks source/brand again; changed latest approved
   # observation cannot reuse original pins. Rollback restores the fixture.
   service.execute('begin')
   rpc(service,'echo_source_brand_revalidate',gym,b['id'],b['content_sha256'],[web,social],web,primary,secondary,spans,'SYNTHETIC late observation',json.dumps(dict(selected_facts_status='supported_uncontradicted',identity_status='verified')))
   denied(lambda:rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(candidates),Jsonb([plan['old_snapshot']])),'late source brand observation')
   service.execute('rollback');assert_rollback()
   historical=uuid.uuid4()
   sql("insert into content_calendar(id,gym_id,post_date,account,format,status,variant_status,caption,image_url) values(%s,'other-gym',current_date-1,'instagram','feed','published','active','SYNTHETIC historical',%s)",(historical,c['original_url']))
   denied(lambda:rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(candidates),Jsonb([plan['old_snapshot']])),'late historical derivative')
   assert_rollback();sql('delete from content_calendar where id=%s',(historical,))
   bad_candidates=[{**candidates[0],'attestation_ids':[str(uuid.uuid4()) for _ in range(3)]}]
   denied(lambda:rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(bad_candidates),Jsonb([plan['old_snapshot']])),'reservation proof refusal after exact rebind')
   assert_rollback()
   assert sql('select count(*) from generated_client_finalizer_decision_20261009')[0][0]==0
   print('PASS reservation refusal rolls back gap rebind, private decision and activation',flush=True)
   worker_store=PgWorkerStore(connect('service_role'),lost=True);other_store=PgWorkerStore(connect('service_role'))
   status=finalworker.batch_status(worker_store,str(batch))
   from concurrent.futures import ThreadPoolExecutor
   with ThreadPoolExecutor(max_workers=2) as pool:
    f1=pool.submit(finalworker.finalize_batch,worker_store,dict(status));f2=pool.submit(finalworker.finalize_batch,other_store,dict(status))
    finalized,via=f1.result();second,via2=f2.result()
   assert via=='status_readback' and via2=='finalize_receipt' and finalized==second
   assert worker_store.finalcalls==1 and other_store.finalcalls==1
   assert sql('select count(*) from forward_schedule_reservation')[0][0]==1
   print('PASS concurrent actual worker finalizers + lost ACK resolves persisted receipt without redispatch',flush=True)
   assert finalized['state']=='finalized' and rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(candidates),Jsonb([plan['old_snapshot']]))==finalized
   print('PASS actual separate finalizer worker function + service SQL + exact terminal receipt retry',flush=True)
   clerk='synthetic-client-owner';client_id=uuid.uuid4();sql('insert into app_users values(%s,%s,\'client\',\'synthetic@test.invalid\')',(client_id,clerk));sql("insert into gym_assignments values(%s,%s,'client_owner')",(client_id,gym))
   snapshot=dict(caption=text,media_url=c['original_url'],day_key=day.isoformat(),format='feed',platform='instagram',creative_origin='generated',generated_artifact_version_id=version,generated_artifact_sha256=c['original_sha256'])
   approved=sql('select to_jsonb(r) from approve_calendar_row_if_media_ready(%s,%s,%s) r',(new_id,'gym',Jsonb(snapshot)),service);assert len(approved)==1;digest=approved[0][0]['approval_digest']
   for invalid_role in ('coach','owner'):
    sql('update app_users set role=%s where id=%s',(invalid_role,client_id))
    assert sql('select count(*) from calendar_stamp_verified_approval(%s,%s,%s,%s)',(gym,new_id,clerk,digest),service)[0][0]==0
   sql("update app_users set role='client' where id=%s",(client_id,))
   sql("update gym_assignments set relationship='coach' where app_user_id=%s",(client_id,))
   assert sql('select count(*) from calendar_stamp_verified_approval(%s,%s,%s,%s)',(gym,new_id,clerk,digest),service)[0][0]==0
   sql("update gym_assignments set relationship='client_owner' where app_user_id=%s",(client_id,))
   try:
    unproved=rpc(service,'claim_calendar_publish_slot_owned',new_id,'gym',day,'America/New_York',2,False,False)
    assert unproved is None
   except psycopg.errors.CheckViolation:pass
   assert sql('select publish_claim_token from content_calendar where id=%s',(new_id,))[0][0] is None
   print('PASS coach, staff, revoked assignment and unproved/autonomous approval hold',flush=True)
   assert sql('select count(*) from calendar_stamp_verified_approval(%s,%s,%s,%s)',(gym,new_id,clerk,digest),service)[0][0]==1
   denied(lambda:sql("update content_calendar set caption=caption||' changed' where id=%s",(new_id,)),'post-approval edit')
   sql("update app_users set role='coach' where id=%s",(client_id,))
   denied(lambda:rpc(service,'claim_calendar_publish_slot_owned',new_id,'gym',day,'America/New_York',2,True,False),'client role revoked before claim')
   assert sql('select publish_claim_token from content_calendar where id=%s',(new_id,))[0][0] is None
   sql("update app_users set role='client' where id=%s",(client_id,))
   token=rpc(service,'claim_calendar_publish_slot_owned',new_id,'gym',day,'America/New_York',2,True,False);assert token
   print('PASS exact client snapshot authenticated human stamp + guarded claim (no provider send)',flush=True)
 finally:
  for c in conns:c.close()
