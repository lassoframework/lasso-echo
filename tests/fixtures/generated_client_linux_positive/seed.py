"""Synthetic runtime seed through real source approval/gap/census SQL authority.

No generated candidate or journal is supplied by this fixture. All generated
bytes, review, upload, queue, preparation and staging must follow the real CLI.
"""
import json
from psycopg.types.json import Jsonb
import uuid
from datetime import timedelta
from agent import generated_infographic_preparation as prep, generated_infographic_runtime as runtime


def seed(admin, connect):
  owner=connect('positive_owner');service=connect('positive_service');baseline=uuid.uuid4()
  def sql(q,args=None):
    cur=admin.execute(q,args)
    return cur.fetchall() if cur.description else None
  def rpc(con,name,*args):
    return con.execute('select public.'+name+'('+','.join(['%s']*len(args))+')',args).fetchone()[0]
  sql("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC policy',true,'complete_fleet_still_photo_history',null,'SYNTHETIC reconciliation','SYNTHETIC admin')")
  sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC policy',true,'[]','SYNTHETIC empty full fleet',0)",(baseline,))
  sql("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'",(baseline,))
  sql("insert into fixer_forward_media_claim_gate_20261006 values('fixture-gym',true)")
  rpc(owner,'fixer_still_cutover_control_20261007',True,'SYNTHETIC cutover')
  gym,web,social=[uuid.uuid4() for _ in range(3)]
  sql('alter table gyms add column slug text,add column name text');sql("insert into gyms(id,slug,name) values(%s,'synthetic','Synthetic Gym')",(gym,));sql("insert into app_users values(%s,'SYNTHETIC Blake identity','owner','blake@lassoframework.com')",(uuid.uuid4(),))
  sql("insert into echo_intake_tokens values(%s,'fixture-gym')",(gym,));sql("insert into echo_gym_settings values(%s,false,'synthetic')",(gym,))
  sql("insert into fixer_generated_portal_tenant_map_20261008(echo_account_key,gym_id,approval_evidence_ref,approved_by) values('fixture-gym',%s,'SYNTHETIC map approval','SYNTHETIC owner')",(gym,))
  text='SYNTHETIC training fact';raw=(text+' #112233 #aabbcc').encode()
  for ident,kind,url,locator,data in ((web,'website','https://synthetic.test/',None,raw),(social,'social','https://api.apify.com/v2/synthetic','https://www.instagram.com/synthetic/',b'SYNTHETIC social bytes')):
   sql('''insert into echo_source_captures(id,gym_id,echo_account_key,source_kind,source_url,provider_account_id,source_locator,capture_provider,provider_response_id,source_revision,mapping_revision,mapping_evidence,fetched_at,raw_bytes)
     values(%s,%s,'fixture-gym',%s,%s,%s,%s,%s,%s,'SYNTHETIC source','SYNTHETIC mapping','{"synthetic":true}',clock_timestamp(),%s)''',(ident,gym,kind,url,'12345' if kind=='social' else None,locator,'apify' if kind=='social' else 'direct','SYNTHETIC response' if kind=='social' else None,data))
  spans=json.dumps([dict(key='training',capture_id=str(web),byte_offset=0,byte_length=len(text.encode()))]);primary,secondary=raw.index(b'#112233'),raw.index(b'#aabbcc')
  rpc(service,'echo_source_brand_prepare',gym,'SYNTHETIC Blake identity',[web,social],web,primary,secondary,None,spans)
  b=sql('select to_jsonb(b) from echo_source_brand_bundles b')[0][0]
  rpc(service,'echo_source_brand_decide',gym,'SYNTHETIC Blake identity',b['id'],b['content_sha256'],b['version'],uuid.uuid4(),'approve')
  rpc(service,'echo_source_brand_revalidate',gym,b['id'],b['content_sha256'],[web,social],web,primary,secondary,spans,'SYNTHETIC validator',json.dumps(dict(selected_facts_status='supported_uncontradicted',identity_status='verified')))
  active=rpc(owner,'fixer_generated_source_brand_active_20261007','fixture-gym');authority=runtime.delegated_copy(active['active'],'fixture-gym',caption=text);pins,derivation=authority['authority_pins'],authority['copy_derivation_receipt']
  day=sql('select current_date')[0][0]+timedelta(days=1)
  q=rpc(service,'fixer_generated_gap_dispatch_20261007',uuid.uuid4(),'fixture-gym',day,'instagram','feed')
  rid,logical=uuid.uuid4(),uuid.uuid4();group='vg_generated_'+logical.hex
  args=(q['request_id'],rid,logical,group,text,authority['source_revision'],authority['palette_revision'],prep.digest(authority['palette']),authority['palette']['evidence_ref'],json.dumps(pins),json.dumps(derivation))
  assert rpc(owner,'fixer_generated_gap_bind_bundle_20261007',*args)['bound']
  sql("update fixer_inventory_protocol_control_20261008 set enabled=true,all_writers_verified_ref='SYNTHETIC isolated no-writer fixture coverage'")
  observed=sql('select clock_timestamp()')[0][0]
  census=rpc(owner,'fixer_generated_local_census_snapshot_20261008',rid)
  rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,census['snapshot']['inventory_revision'],True,0,'local-census:sha256:'+prep.digest(census),uuid.UUID(census['epoch_id']),Jsonb(census),observed)
  sql("update generated_client_control_20261009 set enabled=true where owner_principal='positive_owner'")
  sql("update forward_schedule_reservation_gate_20261008 set enabled=true where singleton")
  owner.close();service.close()
  return str(rid)
