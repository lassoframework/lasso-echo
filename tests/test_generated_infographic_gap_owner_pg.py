"""Run as a script: disposable PG17 gap dispatch/bind/reserve/claim proof.

No production credentials, remote providers, or persistent databases are used.
"""
import concurrent.futures
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
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import forward_media_guard as guard, forward_media_owner as owner
from agent import generated_infographic_gap_owner as gap, generated_infographic_preparation as prep
from agent import generated_infographic_runtime as runtime
from tests.test_generated_owner_guard import candidate, trusted, image_bytes


def main():
 import psycopg
 pg=Path('/opt/homebrew/opt/postgresql@17/bin')
 assert shutil.disk_usage('/tmp').free>5*1024**3
 with tempfile.TemporaryDirectory(prefix='generated_gap_pg_',dir='/tmp') as tmp:
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
   def denied(fn,reason=None):
    try:fn()
    except psycopg.Error as e:
     if reason:assert reason in str(e),str(e)
    else:raise AssertionError('unsafe gap mutation unexpectedly accepted')
   sql('create role anon;create role authenticated;create role service_role login;'
       'create table content_calendar(id uuid primary key,gym_id text,logical_post_id uuid,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
       'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);'
       'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);')
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql','DRAFT_fixer_generated_owner_20261007.sql','DRAFT_fixer_generated_gap_dispatch_20261007.sql'):
    sql((ROOT/'migrations'/name).read_text())
   sql('create role generated_owner login;grant fixer_forward_media_owner_20261006 to generated_owner;'
       'create role mixed_owner login;grant fixer_forward_media_owner_20261006,service_role to mixed_owner;'
       'alter role fixer_forward_media_attester_20261006 login;'
       'grant select,insert,update,delete on content_calendar to service_role;')
   baseline=str(uuid.uuid4())
   sql("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC policy',true,'complete_fleet_still_photo_history',null,'SYNTHETIC reconciliation','SYNTHETIC admin')")
   sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC policy',true,'[]','SYNTHETIC empty full fleet',0)",(baseline,))
   sql("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'",(baseline,))
   sql("insert into fixer_forward_media_claim_gate_20261006 values('gym',true),('other',true)")
   # The discovery RPC explicitly uses UTC; match that date at UTC/local midnight.
   today=sql("select (clock_timestamp() at time zone 'UTC')::date")[0][0];day=today+timedelta(days=1)
   service=lane('service_role');conn=lane('generated_owner')
   def windows(tenants,first=day):return {t:[first.isoformat(),(first+timedelta(days=1)).isoformat()] for t in tenants}
   def dispatch(gym='gym',date_=day,account='instagram',request=None):
    request=request or str(uuid.uuid4())
    result=service.execute('select fixer_generated_gap_dispatch_20261007(%s,%s,%s,%s,%s)',
                           (request,gym,date_,account,'feed')).fetchone()[0]
    service.commit();return result
   source=SimpleNamespace(id=1,account_key='gym_ig',text='SYNTHETIC approved words',
     status='approved',category='educational',citation='fixture approved source',created_at='2026-10-07')
   # Legacy SQL fixture source reference only; production owner no longer reads SQLite.
   graphic_copy=dict(headline=source.text,facts=[source.text],cta='',footer='')
   source_revision='client-source:sha256:'+prep.digest({k:getattr(source,k,None) for k in ('id','account_key','category','text','citation','status','created_at')})
   palette=dict(evidence_ref='brand-colors:sha256:'+'c'*64)
   refs=(source.text,source_revision,'sha256:'+'b'*64,prep.digest(palette),palette['evidence_ref'])
   logical=str(uuid.uuid4());group='vg_generated_'+uuid.UUID(logical).hex
   def args(q,row=None):return (q['request_id'],row or str(uuid.uuid4()),logical,group,*refs)
   def bind(c,a,commit=True):
    result=c.execute('select fixer_generated_gap_bind_20261007('+','.join(['%s']*9)+')',a).fetchone()[0]
    if commit:c.commit()
    return result
   q=dispatch();a=args(q)
   assert sql('select count(*) from content_calendar')[0][0]==0
   assert dispatch(request=q['request_id'])==q
   denied(lambda:dispatch(request=str(uuid.uuid4())),'identity conflict');service.rollback()
   denied(lambda:dispatch(date_=today+timedelta(days=10)),'bounded exact');service.rollback()
   denied(lambda:service.execute('select fixer_generated_gap_pending_20261007(%s,10,%s::jsonb)',(['gym'],json.dumps(windows(['gym'])))),'permission denied');service.rollback()
   denied(lambda:service.execute('insert into fixer_generated_gap_request_20261007(request_id,gym_id,local_date,account,format) values(%s,%s,%s,%s,%s)',(str(uuid.uuid4()),'other',day,'instagram','feed')),'permission denied');service.rollback()
   denied(lambda:bind(service,a),'permission denied');service.rollback()
   mixed=lane('mixed_owner');denied(lambda:bind(mixed,a),'isolated existing owner');mixed.rollback();mixed.close()
   assert conn.execute('select fixer_generated_gap_pending_20261007(%s,10,%s::jsonb)',(['other'],json.dumps(windows(['other'])))).fetchone()[0]==[];conn.rollback()
   assert len(conn.execute('select fixer_generated_gap_pending_20261007(%s,10,%s::jsonb)',(['gym'],json.dumps(windows(['gym'])))).fetchone()[0])==1;conn.rollback()
   out=bind(conn,a);assert out['bound'] and not out['replayed']
   assert bind(conn,a)['replayed']
   assert sql('select count(*) from content_calendar')[0][0]==1
   r=sql('select status,variant_status,image_url,thumbnail_url,source_media_asset_id,caption,media_not_ready_reason from content_calendar where id=%s',(a[1],))[0]
   assert r==('pending','active',None,None,None,refs[0],None)
   denied(lambda:bind(conn,(*a[:4],'Changed words',*a[5:])),'immutable identity');conn.rollback()
   denied(lambda:service.execute("insert into content_calendar(id,gym_id,post_date,account,format,status,variant_status) values(%s,'gym',%s,'instagram','feed','pending','active')",(str(uuid.uuid4()),day)),'already bound');service.rollback()
   denied(lambda:service.execute("insert into content_calendar(id,gym_id,post_date,account,format,status,variant_status) values(%s,'gym',%s,'ig','feed','pending','active')",(str(uuid.uuid4()),day)),'already bound');service.rollback()
   # Human approval remains required and existing holds are neither cleared nor
   # overwritten. A changed caption/denied slot is held on owner replay.
   sql("update content_calendar set status='approved',media_not_ready_reason='existing hold' where id=%s",(a[1],))
   assert bind(conn,a)['replayed']
   assert sql('select media_not_ready_reason from content_calendar where id=%s',(a[1],))[0][0]=='existing hold'
   sql('update content_calendar set media_not_ready_reason=null where id=%s',(a[1],))
   denied(lambda:conn.execute('select fixer_generated_gap_record_20261007(%s,%s,true,null)',(q['request_id'],a[1])),'committed generated reservation');conn.rollback()
   assert conn.execute('select fixer_generated_gap_record_20261007(%s,%s,false,%s)',(q['request_id'],a[1],'generated_photo_available')).fetchone()[0];conn.commit()
   assert sql('select state,last_hold from fixer_generated_gap_request_20261007')[0]==('bound','generated_photo_available')
   # Atomic replay under two independent owner transactions creates one row.
   q2=dispatch(date_=today+timedelta(days=2));a2=args(q2)
   def concurrent_bind():
    c=lane('generated_owner')
    try:return bind(c,a2)
    finally:c.close()
   with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results=list(pool.map(lambda _:concurrent_bind(),range(2)))
   assert sorted(x['replayed'] for x in results)==[False,True]
   assert sql('select count(*) from content_calendar where id=%s',(a2[1],))[0][0]==1
   # Occupied legacy alias dates and arbitrary active holds cannot be replaced.
   occupied=dispatch(gym='other')
   sql("insert into content_calendar(id,gym_id,post_date,account,format,status,variant_status) values(%s,'other',%s,'ig','feed','pending','active')",(str(uuid.uuid4()),day))
   denied(lambda:bind(conn,args(occupied)),'already occupied');conn.rollback()
   assert sql("select calendar_row_id from fixer_generated_gap_request_20261007 where gym_id='other'")[0][0] is None
   # Late eligible photo prevents ANY placeholder commit, irrespective of
   # stale scheduler depletion. Unknown readiness SHA fails closed too.
   photoq=dispatch(gym='photo-gym');photoa=args(photoq)
   sql("insert into media_source values('source','photo-gym','gym_drive','folder',true,'ready',now())")
   pixels=image_bytes();fp=hashlib.md5(pixels).hexdigest()
   proof=dict(verdict='clean',provider='SYNTHETIC scanner',content_hash=fp,asset_id='photo',gym_id='photo-gym',people_detected=False,observed_at='2026-10-07T00:00:00Z',sha256=hashlib.sha256(pixels).hexdigest())
   sql("insert into media_asset values('photo','source','photo-gym',%s,null,'photo',true,false,'approved','clean',%s,'SYNTHETIC scanner',now(),%s::jsonb,false,999)",(fp,fp,json.dumps(proof)))
   denied(lambda:bind(conn,photoa),'depletion or sealed');conn.rollback()
   assert sql('select count(*) from content_calendar where id=%s',(photoa[1],))[0][0]==0
   sql("update media_asset set moderation_json=moderation_json-'sha256' where id='photo'")
   denied(lambda:bind(conn,photoa),'depletion or sealed');conn.rollback()
   sql("delete from media_asset where id='photo';delete from media_source where id='source'")
   sql('update fixer_forward_media_photo_state_20261007 set baseline_id=null')
   denied(lambda:bind(conn,photoa),'depletion or sealed');conn.rollback()
   sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s',(baseline,))
   # SQL bind -> B verified original reserve -> queue completion -> normal
   # attester -> owned publisher claim, using synthetic original bytes only.
   persistence=owner.ForwardMediaOwnerPersistence(conn,'generated_owner',None)
   # Disposable synthetic epoch/census only; no live cutover or adapter proof.
   conn.execute('select fixer_still_cutover_control_20261007(true,%s)',
     ('SYNTHETIC gap fixture cutover',));conn.commit()
   snap={**trusted(guard.generated_snapshot(persistence,a[1])),
     'approved_source_revision':source_revision,'copy_digest':prep.digest(graphic_copy),
     'palette_revision':refs[2],'palette_digest':prep.digest(palette)}
   conn.rollback();original=candidate(snap,pixels)
   with patch('agent.visual_writer_prepare._own_media_url',lambda u:isinstance(u,str) and u.startswith('https://owned.example/')):
    reserved=guard.reserve_generated(persistence,a[1],original,snap,history_visuals=[],read_bytes=lambda u:pixels);conn.commit()
    assert reserved['reserved']
    # The owner cannot replay the same job (including a sibling) with a
    # different source approval revision; omitted legacy RPC input fails closed.
    denied(lambda:conn.execute('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb)',
      (a[1],json.dumps(original),'[]',json.dumps(reserved['manifest']))),
      'verified approved source revision required');conn.rollback()
    denied(lambda:conn.execute('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)',
      (a[1],json.dumps(original),'[]',json.dumps(reserved['manifest']),'client-source:sha256:'+'d'*64)),
      'immutable identity conflict');conn.rollback()
    assert conn.execute('select fixer_generated_gap_record_20261007(%s,%s,true,null)',(q['request_id'],a[1])).fetchone()[0];conn.commit()
    assert sql('select state,last_hold from fixer_generated_gap_request_20261007 where request_id=%s',(q['request_id'],))[0]==('complete',None)
    assert sql('select status from content_calendar where id=%s',(a[1],))[0][0]=='approved'
    census=guard.generated_snapshot(persistence,a[1]);conn.rollback()
    assert census['photo_inventory_complete'] and census['eligible_photo_count']==0
    conn.execute('select fixer_still_inventory_record_20261007(%s,%s,%s,true,0,%s)',
      (str(uuid.uuid4()),a[1],census['inventory_revision'],
       'SYNTHETIC freshly observed empty local library/rotation'));conn.commit()
    revision=sql('select fixer_forward_media_attestation_request_20261006(%s)',(a[1],))[0][0]['revision']
    receipt=guard.attest(a[1],revision,connection_factory=lambda:lane(guard.ROLE),original_verifier=lambda s,b:b==pixels,read_bytes=lambda u:pixels)
    token=str(uuid.uuid4())
    sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",(token,a[1]))
    assert service.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',(a[1],token,receipt['evidence_id'],revision)).fetchone()[0] is True;service.commit()
    # Sibling shares exact logical identity/original; no queue-generated authority.
    fbq=dispatch(account='facebook');fba=args(fbq);fbrow=bind(conn,fba)
    fresh={**trusted(guard.generated_snapshot(persistence,fba[1])),
     'approved_source_revision':source_revision,'copy_digest':prep.digest(graphic_copy),
     'palette_revision':refs[2],'palette_digest':prep.digest(palette)};conn.rollback()
    assert guard.reserve_generated(persistence,fba[1],original,fresh,history_visuals=[{**h,'visual_sha256':h.get('visual_sha256') or 'sha256:'+hashlib.sha256(pixels).hexdigest()} for h in fresh['history']['rows']],read_bytes=lambda u:pixels)['replayed'];conn.commit()
    assert fbrow['logical_post_id']==out['logical_post_id']
    denied(lambda:conn.execute('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)',
      (fba[1],json.dumps(original),'[]',json.dumps(reserved['manifest']),'client-source:sha256:'+'d'*64)),
      'immutable identity conflict');conn.rollback()
    # Cross-volume publish readback: the immutable minimal SQL binding is the
    # publisher's only prepared-job authority (the owner's local journal is on
    # another /data volume). Only service_role may execute it; the owner,
    # attester and anon roles are denied, reservation rows stay immutable, and
    # no provider response/output/storage internals are exposed.
    binding=service.execute('select fixer_generated_publish_readback_20261007(%s)',(a[1],)).fetchone()[0];service.commit()
    assert set(binding)=={'job_id','calendar_row_id','gym_id','account','local_date','logical_post_id','group_key','original_url','manifest_digest','source_revision','copy_digest','palette_revision','palette_digest','receipt_ref','authority_pins'}
    assert binding['authority_pins'] is None
    assert binding['job_id']==original['job_id'] and binding['calendar_row_id']==a[1]
    assert binding['gym_id']=='gym' and binding['account']=='instagram'
    assert binding['local_date']==original['local_date'] and binding['logical_post_id']==original['logical_post_id']
    assert binding['group_key']==group and binding['original_url']==original['original_url']
    assert binding['manifest_digest']==reserved['manifest']['manifest_digest'] and binding['receipt_ref']==reserved['receipt_ref']
    assert binding['source_revision']==source_revision and binding['source_revision']!=original['copy_revision']
    assert binding['copy_digest']==original['copy_digest']
    # Actual SQL readback must satisfy the Python boundary without the owner's journal.
    leased=sql('select row_to_json(r) from content_calendar r where id=%s',(a[1],))[0][0]
    with patch.object(runtime,'enabled',lambda:True),patch.object(guard,'enabled',lambda:True):
     try:runtime.validate_publish_palette(leased,readback=lambda rid:binding)
     except runtime.RuntimeHold as exc:assert str(exc)=='generated_publish_binding_unavailable'
     else:raise AssertionError('legacy SQL binding authorized canonical publisher')
    assert binding['palette_revision']==original['palette_revision'] and binding['palette_digest']==original['palette_digest']
    fbind=service.execute('select fixer_generated_publish_readback_20261007(%s)',(fba[1],)).fetchone()[0];service.commit()
    assert fbind['job_id']==original['job_id'] and fbind['account']=='facebook' and fbind['calendar_row_id']==fba[1]
    # Format drift is authoritative in SQL even when Python retains an old feed row.
    sql("update content_calendar set format='story' where id=%s",(fba[1],))
    assert service.execute('select fixer_generated_publish_readback_20261007(%s)',(fba[1],)).fetchone()[0] is None;service.commit()
    sql("update content_calendar set format='feed' where id=%s",(fba[1],))
    # Identity drift or an unknown/non-generated row reads back NULL (fail closed).
    assert service.execute('select fixer_generated_publish_readback_20261007(%s)',(str(uuid.uuid4()),)).fetchone()[0] is None;service.commit()
    assert service.execute('select fixer_generated_publish_readback_20261007(%s)',(a2[1],)).fetchone()[0] is None;service.commit()
    sql("update content_calendar set thumbnail_url='https://owned.example/drift.png' where id=%s",(fba[1],))
    assert service.execute('select fixer_generated_publish_readback_20261007(%s)',(fba[1],)).fetchone()[0] is None;service.commit()
    sql("update content_calendar set thumbnail_url=null where id=%s",(fba[1],))
    denied(lambda:conn.execute('select fixer_generated_publish_readback_20261007(%s)',(a[1],)),'permission denied');conn.rollback()
    attlane=lane(guard.ROLE);denied(lambda:attlane.execute('select fixer_generated_publish_readback_20261007(%s)',(a[1],)),'permission denied');attlane.rollback();attlane.close()
    anonlane=lane('postgres');anonlane.execute('set role anon')
    denied(lambda:anonlane.execute('select fixer_generated_publish_readback_20261007(%s)',(a[1],)),'permission denied');anonlane.rollback();anonlane.close()
    denied(lambda:service.execute("update fixer_generated_reservation_20261007 set group_key='drift'"),'permission denied');service.rollback()
   # Exercise the actual idle-transaction owner RPC adapter and durable phase,
   # without any provider execution or publisher credential fallback.
   adapter=gap.GapOwnerTransport(persistence,prep.SQLiteGenerationJobs(root/'jobs.sqlite'))
   discovered=adapter.pending(('photo-gym',),10,windows(['photo-gym']))
   assert len(discovered)==1 and discovered[0]['request_id']==photoq['request_id']
   palette=dict(evidence_ref=refs[-1])
   # Canonical owner adapter must hold on this legacy-only SQL installation.
   # The legacy raw SQL contract remains exercised above, while the separate
   # bundle bridge PG fixture establishes the new adapter's bound phase.
   pins=dict(mode='delegated_policy',gym_id=str(uuid.uuid4()),echo_account_key='photo-gym',
    bundle_id=str(uuid.uuid4()),bundle_version=1,configuration_sha256='a'*64,
    configuration_receipt_sha256='b'*64,observation_id=1,observation_sha256='c'*64,
    validator_revision='SYNTHETIC validator',derivation_sha256='d'*64)
   authority=dict(authority_pins=pins,copy_derivation_receipt={})
   for attempt in range(2):
    try:adapter.bind(discovered[0],caption=refs[0],source_revision=refs[1],palette=palette,palette_revision=refs[2],authority=authority)
    except runtime.RuntimeHold as exc:assert str(exc)=='generated_gap_binding_unavailable'
    else:raise AssertionError('new adapter bound without canonical SQL bridge')
    assert adapter.phase(photoq['request_id'])=='ready'
   bound=bind(conn,args(discovered[0]))
   assert bound['bound']
   assert bind(conn,args(discovered[0],row=bound['calendar_row_id']))['replayed']
   adapter.record(discovered[0],bound['calendar_row_id'],dict(ok=False,reason='generated_astra_unavailable'))
   # More expired requests than the batch cannot hide a fresh eligible date.
   # Include a bound old job whose uncertain outcome must remain untouched.
   expired=[dispatch(gym='expired-'+str(i),date_=today-timedelta(days=1)) for i in range(26)]
   old_bound=bind(conn,args(expired[0]))
   freshq=dispatch(gym='fresh-gym')
   tenants=['expired-'+str(i) for i in range(26)]+['fresh-gym']
   frozen_queue=sql("select request_id,state,calendar_row_id from fixer_generated_gap_request_20261007 where gym_id like 'expired-%' order by gym_id")
   pending=adapter.pending(tenants,25,windows(tenants))
   assert [r['request_id'] for r in pending]==[freshq['request_id']]
   assert sql("select request_id,state,calendar_row_id from fixer_generated_gap_request_20261007 where gym_id like 'expired-%' order by gym_id")==frozen_queue
   assert sql('select status from content_calendar where id=%s',(old_bound['calendar_row_id'],))[0][0]=='pending'
   # An explicit local-tomorrow window may start on UTC today, but callers
   # cannot enlarge it, omit gyms, or discover historical queue entries.
   assert adapter.pending(('expired-0',),25,windows(['expired-0'],today))==[]
   denied(lambda:conn.execute('select fixer_generated_gap_pending_20261007(%s,25,%s::jsonb)',
          (tenants,json.dumps(windows(['fresh-gym'])))),'exact local');conn.rollback()
   denied(lambda:conn.execute('select fixer_generated_gap_pending_20261007(%s,25,%s::jsonb)',
          (['expired-0'],json.dumps(windows(['expired-0'],today-timedelta(days=1))))),'bounded local');conn.rollback()
   # Competing calendar writer cannot bypass an in-flight graph/slot bind.
   lock=lane('generated_owner');lock.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',('fixer_forward_graph_20261006',))
   denied(lambda:service.execute("insert into content_calendar(id,gym_id,post_date,account,format,status) values(%s,'free',%s,'instagram','feed','pending')",(str(uuid.uuid4()),day)),'authority busy');service.rollback();lock.rollback();lock.close()
   service.close();conn.close();admin.close()
   print('PASS: PG17 queue-only dispatch, role isolation, bounded gym-local discovery before limit, 26-expired starvation denial with bound-job preservation, atomic/idempotent row bind, concurrency, exact refs, occupied/held slots, late-photo and sealed-history rollback, no coach marker, client approval, B reservation/completion and normal attester/forward claim, canonical Python legacy-binding rejection')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)

if __name__=='__main__':main()
