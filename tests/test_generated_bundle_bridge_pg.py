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
PORTAL=ROOT.parent/'portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.sql'


def main(source_mode=None):
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
   mapping='fixer_generated_portal_tenant_map_20261008'
   assert sql("select to_regclass('public.tenant_alias')")[0][0] is None
   assert sql('select count(*) from '+mapping)[0][0]==0
   # Shared intake plumbing never enrolls or approves a generated bridge tenant.
   denied(lambda:rpc(owner,'fixer_generated_source_brand_active_20261007','gym'),'mapping missing')
   other_gym=uuid.uuid4()
   sql('insert into gyms values(%s)',(other_gym,))
   put_mapping=lambda mapped_gym=gym:sql('insert into '+mapping+'(echo_account_key,gym_id,approval_evidence_ref,approved_by) values(%s,%s,%s,%s)',('gym',mapped_gym,'SYNTHETIC independently approved tenant pair','SYNTHETIC release owner'))
   denied(lambda:sql('insert into '+mapping+'(echo_account_key,gym_id,approval_evidence_ref,approved_by) values(%s,%s,%s,%s)',('gym',gym,'','SYNTHETIC release owner')),'approval_evidence_ref')
   # Even BYPASSRLS service_role and isolated generation roles have no direct ACL.
   for role in ('anon','authenticated','service_role','fixer_forward_media_owner_20261006',
     'fixer_forward_media_attester_20261006','generated_authority_owner_20261007',
     'generated_authority_publisher_20261007','generated_send_reconciler_20261007'):
    role_conn=connect();role_conn.execute('set role '+role)
    for statement in ('select * from '+mapping,
      "insert into "+mapping+"(echo_account_key,gym_id,approval_evidence_ref,approved_by) values('unsafe','"+str(gym)+"','unsafe','unsafe')",
      "update "+mapping+" set approval_evidence_ref='unsafe'",
      'delete from '+mapping,'truncate '+mapping):
     denied(lambda statement=statement:role_conn.execute(statement),'permission denied')
   put_mapping()

   text='SYNTHETIC training fact';raw=(text+' #112233 #aabbcc').encode()
   for ident,kind,url,locator,data in ((web,'website','https://synthetic.test/',None,raw),(social,'social','https://api.apify.com/v2/synthetic','https://www.instagram.com/synthetic/','SYNTHETIC social bytes'.encode())):
    sql('''insert into echo_source_captures(id,gym_id,echo_account_key,source_kind,source_url,provider_account_id,source_locator,capture_provider,provider_response_id,source_revision,mapping_revision,mapping_evidence,fetched_at,raw_bytes)
     values(%s,%s,'gym',%s,%s,%s,%s,%s,%s,'SYNTHETIC source revision','SYNTHETIC mapping revision','{"synthetic":true}',clock_timestamp(),%s)''',
     (ident,gym,kind,url,'12345' if kind=='social' else None,locator,'apify' if kind=='social' else 'direct','SYNTHETIC provider response' if kind=='social' else None,data))
   spans=json.dumps([dict(key='training',capture_id=str(web),byte_offset=0,byte_length=len(text.encode()))])
   primary,secondary=raw.index(b'#112233'),raw.index(b'#aabbcc')
   capture_ids=[web] if source_mode=='website_only_no_connected_instagram_v2' else [web,social]
   def attest_provider(**changes):
    status=dict(gym_id=str(gym),echo_account_key='gym',provider='zernio',source='zernio_authenticated_accounts',
     mapping_revision='SYNTHETIC provider map',lookup_status='complete',authenticated=True,
     observed_at=sql('select clock_timestamp()')[0][0].isoformat(),profile_id='SYNTHETIC profile',
     response_sha256='a'*64,instagram=dict(connected=source_mode=='website_and_social_v2',
      account_id='SYNTHETIC account' if source_mode=='website_and_social_v2' else None,
      platform_user_id='12345' if source_mode=='website_and_social_v2' else None,
      handle='synthetic' if source_mode=='website_and_social_v2' else None))
    status.update(changes)
    return rpc(service,'echo_source_brand_attest_provider',gym,'gym',json.dumps(status),uuid.uuid4())
   prepare_args=(gym,'SYNTHETIC Blake identity',capture_ids,web,primary,secondary,None,spans)
   if source_mode:
    attest_provider()
    prepare_args+= (source_mode,)
   b=rpc(service,'echo_source_brand_prepare',*prepare_args)
   # Composite function returns a tuple; read its exact canonical JSON instead.
   b=sql('select to_jsonb(b) from echo_source_brand_bundles b')[0][0]
   assert b['schema_version']==(2 if source_mode else 1)
   if source_mode:assert json.loads(b['snapshot_bytes'])['source_policy']['mode']==source_mode
   rpc(service,'echo_source_brand_decide',gym,'SYNTHETIC Blake identity',b['id'],b['content_sha256'],b['version'],uuid.uuid4(),'approve')
   denied(lambda:rpc(owner,'fixer_generated_source_brand_active_20261007','gym'),'observation required')
   def observe(report=None):
    return rpc(service,'echo_source_brand_revalidate',gym,b['id'],b['content_sha256'],capture_ids,web,primary,secondary,spans,'SYNTHETIC validator v1',json.dumps(report or dict(selected_facts_status='supported_uncontradicted',identity_status='verified')))
   observe()
   sql('delete from '+mapping)
   denied(lambda:rpc(owner,'fixer_generated_source_brand_active_20261007','gym'),'mapping missing')
   put_mapping(other_gym)
   denied(lambda:rpc(owner,'fixer_generated_source_brand_active_20261007','gym'),'tenant bijection')
   sql('update '+mapping+' set gym_id=%s where echo_account_key=%s',(gym,'gym'))
   active=rpc(owner,'fixer_generated_source_brand_active_20261007','gym')
   assert active['gym_id']==str(gym) and active['echo_account_key']=='gym'
   # A source read holds both canonical account and portal UUID locks until COMMIT.
   # Both remap directions and the independent intake registry fail fast.
   reader=connect();reader.execute('begin')
   rpc(reader,'fixer_generated_source_brand_active_20261007','gym')
   for statement,parameters in (
      ('update '+mapping+' set gym_id=%s where echo_account_key=%s',(other_gym,'gym')),
      ('update '+mapping+' set echo_account_key=%s where gym_id=%s',('other',gym)),
      ('delete from '+mapping+' where gym_id=%s',(gym,)),
      ('update echo_intake_tokens set echo_account_key=%s where gym_id=%s',('other',gym))):
    denied(lambda statement=statement,parameters=parameters:sql(statement,parameters),'send decision busy')
   reader.execute('rollback')
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
   if source_mode:
    # Latest failed/changed provider evidence holds the entire bridge despite
    # immutable approved configuration and a previously valid observation.
    for change in (dict(lookup_status='unavailable',authenticated=False),
      dict(instagram=dict(connected=True,account_id='SYNTHETIC changed account',platform_user_id='67890',handle='changed'))):
     attest_provider(**change)
     denied(lambda:rpc(owner,'fixer_generated_source_brand_active_20261007','gym'),'observation required')
     denied(lambda:rpc(owner,'fixer_generated_gap_bind_bundle_20261007',*args),'observation required')
     assert sql('select count(*) from content_calendar')[0][0]==0
     attest_provider()
     assert rpc(owner,'fixer_generated_source_brand_active_20261007','gym')['gym_id']==str(gym)
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
   # Census authority is owner-only; a missing census holds pre-generation and
   # reservation. Record the trusted complete zero census for this revision.
   denied(lambda:rpc(service,'fixer_generated_local_census_authority_20261008','gym',snap['inventory_revision']),'permission denied')
   census0=rpc(owner,'fixer_generated_local_census_authority_20261008','gym',snap['inventory_revision'])
   assert census0['enabled'] and census0['receipt_id'] is None
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,snap['inventory_revision'],True,0,'SYNTHETIC zero local inventory')
   census0=rpc(owner,'fixer_generated_local_census_authority_20261008','gym',snap['inventory_revision'])
   assert census0['receipt_id'] and census0['local_complete'] and census0['local_available']==0
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
   # A -> B -> A must not revive the old A/zero receipt. Record B's
   # positive census against an actual changed snapshot, then restore A.
   def revision_toggle_positive(row):
    revision_a=rpc(owner,'fixer_generated_snapshot_20261007',row)['inventory_revision']
    sql("insert into media_source values('SYNTHETIC toggle source','gym','gym_drive','toggle',false,'ready',clock_timestamp())")
    revision_b=rpc(owner,'fixer_generated_snapshot_20261007',row)['inventory_revision']
    assert revision_b!=revision_a
    rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),row,revision_b,True,1,'SYNTHETIC B local photo arrival')
    sql("delete from media_source where id='SYNTHETIC toggle source'")
    assert rpc(owner,'fixer_generated_snapshot_20261007',row)['inventory_revision']==revision_a
    assert rpc(owner,'fixer_generated_local_census_authority_20261008','gym',revision_a)['receipt_id'] is None
   revision_toggle_positive(rid)
   denied(lambda:reserve(),'reservation requires fresh local depletion')
   assert sql('select count(*) from fixer_generated_reservation_20261007')[0][0]==0
   # Late local photo arrival between preflight and reserve: the newer positive
   # census at the SAME inventory revision overrides the older zero; no
   # reservation, generated image or pending card staging is possible.
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,snap['inventory_revision'],True,1,'SYNTHETIC late local photo arrival')
   assert rpc(owner,'fixer_generated_local_census_authority_20261008','gym',snap['inventory_revision'])['local_available']==1
   denied(lambda:reserve(),'reservation requires fresh local depletion')
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,snap['inventory_revision'],False,0,'SYNTHETIC incomplete local census')
   denied(lambda:reserve(),'reservation requires fresh local depletion')
   epoch=sql('select epoch_id from fixer_still_cutover_20261007')[0][0]
   # Isolate age: a temporary synthetic epoch contains only a complete zero
   # row. A newer incomplete row in the original epoch cannot mask this check.
   stale_epoch=uuid.uuid4()
   sql('update fixer_still_cutover_20261007 set epoch_id=%s where singleton',(stale_epoch,))
   sql("insert into fixer_still_inventory_20261007 values(%s,%s,'gym',%s,true,0,'SYNTHETIC aged newest census',clock_timestamp()-interval '20 minutes')",(uuid.uuid4(),stale_epoch,snap['inventory_revision']))
   aged=rpc(owner,'fixer_generated_local_census_authority_20261008','gym',snap['inventory_revision'])
   assert aged['local_complete'] and aged['local_available']==0
   assert aged['epoch_id']==str(stale_epoch)
   denied(lambda:reserve(),'reservation requires fresh local depletion')
   sql('update fixer_still_cutover_20261007 set epoch_id=%s where singleton',(epoch,))
   assert sql('select count(*) from fixer_generated_reservation_20261007')[0][0]==0
   # Legitimate photo-exhausted fallback: a newest complete fresh zero census.
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,snap['inventory_revision'],True,0,'SYNTHETIC confirmed zero local inventory')
   assert reserve()['reserved']
   if source_mode:
    for change in (dict(lookup_status='unavailable',authenticated=False),
      dict(instagram=dict(connected=True,account_id='SYNTHETIC changed account',platform_user_id='67890',handle='changed'))):
     attest_provider(**change)
     denied(lambda:reserve(),'observation required')
     denied(lambda:sql("update content_calendar set status='approved' where id=%s",(rid,)),'observation required')
     assert sql('select status,generated_authority_pins from content_calendar where id=%s',(rid,))[0]==('pending',None)
     attest_provider()
   fresh_snap=rpc(owner,'fixer_generated_snapshot_20261007',rid)
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,fresh_snap['inventory_revision'],True,0,'SYNTHETIC zero local inventory')
   assert reserve()['replayed']
   readback=rpc(service,'fixer_generated_publish_readback_20261007',rid)
   assert readback['copy_derivation_receipt']==derivation and readback['authority_pins']==pins
   sql('delete from '+mapping)
   denied(lambda:sql("update content_calendar set status='approved' where id=%s",(rid,)),'mapping missing')
   put_mapping(other_gym)
   denied(lambda:sql("update content_calendar set status='approved' where id=%s",(rid,)),'tenant bijection')
   sql('update '+mapping+' set gym_id=%s where echo_account_key=%s',(gym,'gym'))
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
   with patch.dict('os.environ', {'AGENT_S3_PUBLIC_BASE_URL':'https://owned.example'}), \
        patch('agent.visual_writer_prepare._own_media_url',lambda url: url.startswith('https://owned.example/')):
    receipt=guard.attest(str(rid),revision,connection_factory=lambda:connect(guard.ROLE),
     original_verifier=lambda snapshot,source:source==pixels,read_bytes=lambda url:pixels)
   row,job,claim=rid,uuid.UUID(cc['job_id']),uuid.uuid4()
   sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",(claim,row))
   assert rpc(service,'fixer_claim_forward_media_20261006',row,claim,receipt['evidence_id'],revision) is True
   attempt=uuid.uuid4()
   send_snap=rpc(owner,'fixer_generated_snapshot_20261007',rid)
   revision_toggle_positive(rid)
   denied(lambda:rpc(service,'generated_send_acquire_20261007',attempt,'gym',row,claim,job,json.dumps(p)),'fresh local depletion')
   assert sql('select count(*) from generated_send_lease_20261007')[0][0]==0
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,send_snap['inventory_revision'],True,1,'SYNTHETIC late local photo before send')
   denied(lambda:rpc(service,'generated_send_acquire_20261007',attempt,'gym',row,claim,job,json.dumps(p)),'fresh local depletion')
   assert sql('select count(*) from generated_send_lease_20261007')[0][0]==0
   rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,send_snap['inventory_revision'],True,0,'SYNTHETIC confirmed zero local before send')
   assert rpc(service,'generated_send_acquire_20261007',attempt,'gym',row,claim,job,json.dumps(p))['state']=='reserved'
   denied(lambda:observe(),'freezes portal')
   if source_mode:
    denied(lambda:attest_provider(lookup_status='unavailable',authenticated=False),'freezes portal')
    denied(lambda:sql('truncate echo_source_brand_provider_status'),'freezes portal')
   for statement,parameters in (
      ('update '+mapping+' set gym_id=%s where echo_account_key=%s',(other_gym,'gym')),
      ('update '+mapping+' set echo_account_key=%s where gym_id=%s',('other',gym)),
      ('update '+mapping+' set approval_evidence_ref=%s where gym_id=%s',('changed',gym)),
      ('delete from '+mapping+' where gym_id=%s',(gym,)),
      ('update echo_intake_tokens set echo_account_key=%s where gym_id=%s',('other',gym)),
      ('delete from echo_intake_tokens where gym_id=%s',(gym,)),
      ('truncate '+mapping,None)):
    denied(lambda statement=statement,parameters=parameters:sql(statement,parameters),'freezes portal')
   denied(lambda:sql("update app_users set role='executive' where clerk_user_id='SYNTHETIC Blake identity'"),'freezes actor')
   denied(lambda:sql('truncate echo_source_captures'),'freezes portal')
   assert rpc(service,'generated_send_begin_20261007',attempt)['authorize_send']
   assert not rpc(service,'generated_send_begin_20261007',attempt)['authorize_send']
   assert rpc(service,'generated_send_validate_20261007',attempt)['authorize_send']
   evidence=json.dumps(dict(actor='SYNTHETIC test publisher',receipt_ref='SYNTHETIC unknown transport'))
   assert rpc(service,'generated_send_outcome_20261007',attempt,'unknown',evidence,False)['state']=='unknown'
   denied(lambda:observe(),'freezes portal')
   denied(lambda:sql('delete from '+mapping+' where gym_id=%s',(gym,)),'freezes portal')
   denied(lambda:sql('truncate '+mapping),'freezes portal')
   assert not rpc(service,'generated_send_validate_20261007',attempt)['authorize_send']
   print('PASS '+str(source_mode or 'legacy_v1')+' latest-authority local census (revision A->B->A and zero->positive/incomplete/isolated stale holds pre-reserve and final send, newest zero restores exhausted fallback) + assembled portal + B/gap + delegated approval/acquire/begin/validate + empty/missing/cross-tenant mapping holds + exact approved UUID bijection + mapping/ACL/concurrency fences + failed/changed v2 provider holds + stale evidence + durable unknown fences; synthetic local only')
  finally:
   for c in connections:c.close()
   subprocess.run([str(PG/'pg_ctl'),'-D',str(root/'data'),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)

if __name__=='__main__':
 for source_mode in (None,'website_only_no_connected_instagram_v2','website_and_social_v2'):
  main(source_mode)
