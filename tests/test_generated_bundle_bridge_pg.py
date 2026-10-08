"""Disposable assembled PG17 bridge proof using synthetic portal evidence only.

Actual portal and Echo draft functions, no network, production DSN or provider.
Run as a standalone script with the accepted shared psycopg test runtime.
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
from agent import generated_infographic_preparation as prep, generated_infographic_runtime as runtime, forward_media_guard as guard
from tests.test_generated_owner_guard import candidate, trusted, image_bytes

PG=Path('/opt/homebrew/opt/postgresql@17/bin')
PORTAL=ROOT.parent/'portal-brand-source-bundle-20261008/supabase/migrations/DRAFT_echo_source_brand_bundle.sql'


def main():
 import psycopg
 assert shutil.disk_usage('/tmp').free>5*1024**3
 with tempfile.TemporaryDirectory(prefix='generated_bundle_pg_',dir='/tmp') as tmp:
  root=Path(tmp);sock=root/'sock';sock.mkdir();port=random.randint(41000,59000)
  subprocess.run([str(PG/'initdb'),'-D',str(root/'data'),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
  subprocess.run([str(PG/'pg_ctl'),'-D',str(root/'data'),'-l',str(root/'pg.log'),'-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
  connections=[]
  try:
   def connect(role='postgres'):
    c=psycopg.connect(f'host={sock} port={port} dbname=postgres user={role}',autocommit=True)
    c.execute("set statement_timeout='8s'");connections.append(c);return c
   admin=connect()
   def sql(q,args=None):
    cur=admin.execute(q,args);return cur.fetchall() if cur.description else None
   def rpc(c,name,*args):
    return c.execute('select public.'+name+'('+','.join(['%s']*len(args))+')',args).fetchone()[0]
   def denied(fn,phrase=None):
    try:fn()
    except psycopg.Error as e:
     if phrase:assert phrase in str(e),str(e)
     return str(e)
    raise AssertionError('unsafe delegated operation accepted')
   sql('''create role anon;create role authenticated;create role service_role login bypassrls;
    create table content_calendar(id uuid primary key,gym_id text,logical_post_id uuid,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);
    create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);
    create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);
    create table gyms(id uuid primary key);
    create table app_users(id uuid primary key,clerk_user_id text unique,role text,email text);
    create table echo_intake_tokens(gym_id uuid primary key,echo_account_key text unique);
    create table tenant_alias(alias_key text primary key,tenant_id uuid);
   ''')
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql','DRAFT_fixer_generated_owner_20261007.sql','DRAFT_fixer_generated_gap_dispatch_20261007.sql','DRAFT_generated_source_palette_authority_20261007.sql','DRAFT_generated_send_lease_20261007.sql'):
    sql((ROOT/'migrations'/name).read_text())
   sql(PORTAL.read_text())
   sql((ROOT/'migrations/DRAFT_fixer_generated_bundle_bridge_20261008.sql').read_text())
   sql('create role generated_owner login;grant fixer_forward_media_owner_20261006 to generated_owner;grant select,insert,update on content_calendar to service_role;')
   owner,service=connect('generated_owner'),connect('service_role')
   baseline=uuid.uuid4()
   sql("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC policy',true,'complete_fleet_still_photo_history',null,'SYNTHETIC reconciliation','SYNTHETIC admin')")
   sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC policy',true,'[]','SYNTHETIC empty full fleet',0)",(baseline,))
   sql("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'",(baseline,))
   sql("insert into fixer_forward_media_claim_gate_20261006 values('gym',true)")
   rpc(owner,'fixer_still_cutover_control_20261007',True,'SYNTHETIC bundle fixture cutover')
   gym,web,social=[uuid.uuid4() for _ in range(3)]
   sql('insert into gyms values(%s)',(gym,))
   sql("insert into app_users values(%s,'SYNTHETIC Blake identity','owner','blake@lassoframework.com')",(uuid.uuid4(),))
   sql("insert into echo_intake_tokens values(%s,'gym')",(gym,))
   sql("insert into tenant_alias values('gym',%s)",(gym,))
   text='SYNTHETIC training fact';raw=(text+' #112233 #aabbcc').encode()
   for ident,kind,url,locator,data in ((web,'website','https://synthetic.test/',None,raw),(social,'social','https://api.apify.com/v2/synthetic','https://www.instagram.com/synthetic/','SYNTHETIC social bytes'.encode())):
    sql('''insert into echo_source_captures(id,gym_id,echo_account_key,source_kind,source_url,provider_account_id,source_locator,capture_provider,provider_response_id,source_revision,mapping_revision,mapping_evidence,fetched_at,raw_bytes)
     values(%s,%s,'gym',%s,%s,%s,%s,%s,%s,'SYNTHETIC source revision','SYNTHETIC mapping revision','{"synthetic":true}',clock_timestamp(),%s)''',
     (ident,gym,kind,url,'SYNTHETIC social account' if kind=='social' else None,locator,'apify' if kind=='social' else 'direct','SYNTHETIC provider response' if kind=='social' else None,data))
   spans=json.dumps([dict(key='training',capture_id=str(web),byte_offset=0,byte_length=len(text.encode()))])
   primary,secondary=raw.index(b'#112233'),raw.index(b'#aabbcc')
   b=rpc(service,'echo_source_brand_prepare',gym,'SYNTHETIC Blake identity',[web,social],web,primary,secondary,None,spans)
   # Composite function returns a tuple; read its exact canonical JSON instead.
   b=sql('select to_jsonb(b) from echo_source_brand_bundles b')[0][0]
   rpc(service,'echo_source_brand_decide',gym,'SYNTHETIC Blake identity',b['id'],b['content_sha256'],b['version'],uuid.uuid4(),'approve')
   denied(lambda:rpc(owner,'fixer_generated_source_brand_active_20261007','gym'),'observation required')
   def observe(report=None):
    return rpc(service,'echo_source_brand_revalidate',gym,b['id'],b['content_sha256'],[web,social],web,primary,secondary,spans,'SYNTHETIC validator v1',json.dumps(report or dict(selected_facts_status='supported_uncontradicted',identity_status='verified')))
   observe()
   active=rpc(owner,'fixer_generated_source_brand_active_20261007','gym')
   authority=runtime.delegated_copy(active['active'],'gym',caption=text)
   pins,derivation=authority['authority_pins'],authority['copy_derivation_receipt']
   validate=lambda p=pins,d=derivation:rpc(admin,'fixer_generated_bundle_validate_20261007','gym',json.dumps(p),json.dumps(d),text,prep.digest(authority['copy']),prep.digest(authority['palette']),authority['palette_revision'])
   validate()
   for field in pins:
    value=2 if field in ('bundle_version','observation_id') else 'wrong'
    denied(lambda field=field,value=value:validate({**pins,field:value}))
   denied(lambda:validate({**pins,'extra':True}),'strict delegated')
   denied(lambda:validate(pins,{**derivation,'caption':'fabricated'}),'derivation')
   denied(lambda:rpc(service,'fixer_reserve_generated_bundle_20261007',uuid.uuid4(),'{}','[]','{}','source-brand:sha256:'+'a'*64),'permission denied')
   denied(lambda:rpc(owner,'fixer_reserve_generated_20261007',uuid.uuid4(),'{}','[]','{}','client-source:sha256:'+'a'*64),'permission denied')
   day=sql('select current_date')[0][0]+timedelta(days=1)
   q=rpc(service,'fixer_generated_gap_dispatch_20261007',uuid.uuid4(),'gym',day,'instagram','feed')
   rid,logical=uuid.uuid4(),uuid.uuid4();group='vg_generated_'+logical.hex
   args=(q['request_id'],rid,logical,group,text,authority['source_revision'],authority['palette_revision'],prep.digest(authority['palette']),authority['palette']['evidence_ref'],json.dumps(pins),json.dumps(derivation))
   # Photo-first failure is checked against the real snapshot (unknown source).
   sql("insert into media_source values('SYNTHETIC source','gym','gym_drive','folder',false,'ready',clock_timestamp())")
   denied(lambda:rpc(owner,'fixer_generated_gap_bind_bundle_20261007',*args),'depletion or sealed')
   assert sql('select count(*) from content_calendar')[0][0]==0
   sql('delete from media_source')
   assert rpc(owner,'fixer_generated_gap_bind_bundle_20261007',*args)['bound']
   assert sql('select status,generated_authority_pins from content_calendar where id=%s',(rid,))[0]==('pending',None)
   assert rpc(owner,'fixer_generated_gap_bind_bundle_20261007',*args)['replayed']
   historical_row=uuid.uuid4()
   sql("insert into content_calendar(id,gym_id,post_date,account,format,status,variant_status,caption,image_url) values(%s,'other-gym',current_date-1,'instagram','feed','published','active','SYNTHETIC prior','https://owned.example/generated.png')",(historical_row,))
   snap=rpc(owner,'fixer_generated_snapshot_20261007',rid)
   import io
   from PIL import Image
   first=Image.open(io.BytesIO(image_bytes())).resize((1024,1280))
   stream=io.BytesIO();first.save(stream,format='PNG');pixels=stream.getvalue()
   c=candidate(trusted(snap),pixels)
   c.update(schema_version=2,width=1024,height=1280,
    storage_key='echo-generated-originals/gym/'+c['original_sha256']+'.png',
    authority_pins=pins,copy_derivation_receipt=derivation,
    palette_revision=authority['palette_revision'],copy_digest=prep.digest(authority['copy']),palette_digest=prep.digest(authority['palette']))
   binding={k:c[k] for k in (*prep.BINDING_FIELDS,'copy_digest','palette_digest','review_policy_id','authority_pins','copy_derivation_receipt')}
   c['job_id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'echo-astra:'+prep.canonical(binding)))
   assert rpc(admin,'fixer_generated_bundle_job_20261007',json.dumps(c))==uuid.UUID(c['job_id'])
   m=dict(tenant_id='gym',source_asset_id='generated-astra:'+c['job_id'],image_url=c['original_url'],image_fingerprint='md5:'+c['original_md5'],image_length=c['original_length'],thumbnail_url=None,thumbnail_fingerprint=None,thumbnail_length=None,operation='same_object',render_recipe=None,render_evidence_ref='generated-astra:'+c['job_id'])
   m['manifest_digest']='sha256:'+prep.digest(m)
   reserve=lambda cc=c,visuals=():rpc(owner,'fixer_reserve_generated_bundle_20261007',rid,json.dumps(cc),json.dumps(visuals),json.dumps(m),authority['source_revision'])
   # Different trusted historical bytes/phash do not authorize reuse of their
   # exact URL. This recreates the independent reviewer's confirmed PG defect.
   opposite='scene:phash64:'+format(int(c['original_phash'].split(':')[-1],16)^((1<<64)-1),'016x')
   historical=[dict(h,visual_sha256='sha256:'+'0'*64,phash=opposite) for h in snap['history']['rows']]
   assert historical and all(h['visual_url']==c['original_url'] for h in historical)
   denied(lambda:reserve(visuals=historical),'repeated historical generated visual')
   assert sql('select count(*) from fixer_generated_reservation_20261007')[0][0]==0
   sql('delete from content_calendar where id=%s',(historical_row,))
   snap=rpc(owner,'fixer_generated_snapshot_20261007',rid)
   c.update({k:snap[k] for k in ('copy_revision','inventory_revision','history_revision')})
   binding={k:c[k] for k in (*prep.BINDING_FIELDS,'copy_digest','palette_digest','review_policy_id','authority_pins','copy_derivation_receipt')}
   c['job_id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'echo-astra:'+prep.canonical(binding)))
   m.update(source_asset_id='generated-astra:'+c['job_id'],render_evidence_ref='generated-astra:'+c['job_id'])
   m['manifest_digest']='sha256:'+prep.digest({k:v for k,v in m.items() if k!='manifest_digest'})
   denied(lambda:reserve({**c,'schema_version':1}),'generated original authority')
   denied(lambda:reserve({**c,'job_id':str(uuid.uuid4())}),'job identity')
   assert reserve()['reserved']
   fresh_snap=rpc(owner,'fixer_generated_snapshot_20261007',rid)
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,fresh_snap['inventory_revision'],True,0,'SYNTHETIC zero local inventory')
   assert reserve()['replayed']
   readback=rpc(service,'fixer_generated_publish_readback_20261007',rid)
   assert readback['copy_derivation_receipt']==derivation and readback['authority_pins']==pins
   sql("update content_calendar set status='approved' where id=%s",(rid,))
   assert sql('select generated_authority_pins from content_calendar where id=%s',(rid,))[0][0]==pins
   # Advancing the observation leaves immutable reservation pins stale; no send.
   observe()
   denied(lambda:validate(),'immutable pins changed')
   sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",(uuid.uuid4(),rid))
   claim=sql('select publish_claim_token from content_calendar where id=%s',(rid,))[0][0]
   denied(lambda:rpc(service,'generated_send_acquire_20261007',uuid.uuid4(),'gym',rid,claim,c['job_id'],json.dumps(pins)))
   # Refresh through real B after a new observation, preserving immutable old
   # reservations and explicit same-date/logical visual sibling exceptions.
   sql("update content_calendar set status='pending',publish_claim_token=null,publish_reservation_day=null where id=%s",(rid,))
   active=rpc(owner,'fixer_generated_source_brand_active_20261007','gym')
   fresh=runtime.delegated_copy(active['active'],'gym',caption=text)
   p=fresh['authority_pins'];d=fresh['copy_derivation_receipt']
   snap=rpc(owner,'fixer_generated_snapshot_20261007',rid)
   import io
   from PIL import Image
   image=Image.frombytes('RGB',(128,128),random.Random(71).randbytes(128*128*3)).resize((1024,1280))
   stream=io.BytesIO();image.save(stream,format='PNG');pixels=stream.getvalue()
   replacement=candidate(trusted(snap),pixels)
   cc={**c,**replacement,**{k:snap[k] for k in ('copy_revision','history_revision','inventory_revision')},
    'schema_version':2,'width':1024,'height':1280,
    'storage_key':'echo-generated-originals/gym/'+replacement['original_sha256']+'.png',
    'original_url':'https://owned.example/generated-fresh.png',
    'authority_pins':p,'copy_derivation_receipt':d,'copy_digest':prep.digest(fresh['copy']),
    'palette_revision':fresh['palette_revision'],'palette_digest':prep.digest(fresh['palette'])}
   binding={k:cc[k] for k in (*prep.BINDING_FIELDS,'copy_digest','palette_digest','review_policy_id','authority_pins','copy_derivation_receipt')}
   cc['job_id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'echo-astra:'+prep.canonical(binding)))
   mm={**m,'image_url':cc['original_url'],'image_fingerprint':'md5:'+cc['original_md5'],'image_length':cc['original_length'],'source_asset_id':'generated-astra:'+cc['job_id'],'render_evidence_ref':'generated-astra:'+cc['job_id']}
   mm['manifest_digest']='sha256:'+prep.digest({k:v for k,v in mm.items() if k!='manifest_digest'})
   visuals=[{**h,'phash':c['original_phash'],'visual_sha256':'sha256:'+c['original_sha256']} for h in snap['history']['rows']]
   assert rpc(owner,'fixer_reserve_generated_bundle_20261007',rid,json.dumps(cc),json.dumps(visuals),json.dumps(mm),fresh['source_revision'])['reserved']
   sql("update content_calendar set status='approved' where id=%s",(rid,))
   sql('alter role fixer_forward_media_attester_20261006 login')
   revision=rpc(admin,'fixer_forward_media_attestation_request_20261006',rid)['revision']
   # Actual independent attester API reads exact synthetic PNG bytes twice;
   # real claim RPC commits the required immutable lineage receipt.
   with patch('agent.visual_writer_prepare._own_media_url',lambda url: url.startswith('https://owned.example/')):
    receipt=guard.attest(str(rid),revision,connection_factory=lambda:connect(guard.ROLE),
     original_verifier=lambda snapshot,source:source==pixels,read_bytes=lambda url:pixels)
   row,job,claim=rid,uuid.UUID(cc['job_id']),uuid.uuid4()
   sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",(claim,row))
   assert rpc(service,'fixer_claim_forward_media_20261006',row,claim,receipt['evidence_id'],revision) is True
   attempt=uuid.uuid4()
   assert rpc(service,'generated_send_acquire_20261007',attempt,'gym',row,claim,job,json.dumps(p))['state']=='reserved'
   denied(lambda:observe(),'freezes portal')
   denied(lambda:sql("update app_users set role='executive' where clerk_user_id='SYNTHETIC Blake identity'"),'freezes actor')
   denied(lambda:sql('truncate echo_source_captures'),'freezes portal')
   assert rpc(service,'generated_send_begin_20261007',attempt)['authorize_send']
   assert not rpc(service,'generated_send_begin_20261007',attempt)['authorize_send']
   assert rpc(service,'generated_send_validate_20261007',attempt)['authorize_send']
   evidence=json.dumps(dict(actor='SYNTHETIC test publisher',receipt_ref='SYNTHETIC unknown transport'))
   assert rpc(service,'generated_send_outcome_20261007',attempt,'unknown',evidence,False)['state']=='unknown'
   denied(lambda:observe(),'freezes portal')
   assert not rpc(service,'generated_send_validate_20261007',attempt)['authorize_send']
   print('PASS assembled portal + B/gap + delegated approval/acquire/begin/validate + stale evidence + durable unknown fences; synthetic local only')
  finally:
   for c in connections:c.close()
   subprocess.run([str(PG/'pg_ctl'),'-D',str(root/'data'),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)

if __name__=='__main__':main()
