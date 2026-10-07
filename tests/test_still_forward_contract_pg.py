"""Disposable PG17 incremental owner contract. No production or provider I/O.

This does not prove a deployed forward admission adapter. Owner fresh-original
and local-library observations below are explicitly synthetic trust fixtures.
Runnable both as a script and as a focused pytest test (skips when no
PostgreSQL 17 binaries or psycopg are available on this host).
"""
import concurrent.futures
import json
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]


def find_pg17():
 candidates=[Path('/opt/homebrew/opt/postgresql@17/bin')]
 try:
  out=subprocess.run(['pg_config','--bindir'],capture_output=True,text=True)
  if out.returncode==0 and out.stdout.strip(): candidates.append(Path(out.stdout.strip()))
 except OSError: pass
 for cand in candidates+[None]:
  initdb=shutil.which('initdb',path=str(cand)) if cand else shutil.which('initdb')
  if not initdb: continue
  bindir=Path(initdb).resolve().parent
  try: ver=subprocess.run([str(bindir/'postgres'),'--version'],capture_output=True,text=True).stdout
  except OSError: continue
  if '(PostgreSQL) 17' in ver: return bindir
 return None

PG17=find_pg17()


def run_contract(pg):
 import psycopg
 assert shutil.disk_usage('/tmp').free > 5 * 1024 ** 3
 with tempfile.TemporaryDirectory(prefix='still_contract_pg_', dir='/tmp') as tmp:
  root = Path(tmp); sock = root / 'sock'; sock.mkdir(); data = root / 'data'
  port = random.randint(41000, 59000)
  subprocess.run([str(pg/'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'], check=True, capture_output=True)
  subprocess.run([str(pg/'pg_ctl'), '-D', str(data), '-l', str(root/'pg.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'], check=True, capture_output=True)
  try:
   def connect(role='postgres'):
    c = psycopg.connect(f'host={sock} port={port} dbname=postgres user={role}')
    c.execute("set statement_timeout='5s'"); c.commit(); return c
   admin = connect(); admin.autocommit = True
   def sql(q, args=None):
    cur = admin.execute(q, args); return cur.fetchall() if cur.description else None
   def uid(): return str(uuid.uuid4())
   def denied(fn, text=None):
    try: fn()
    except psycopg.Error as e:
     if text: assert text in str(e), str(e)
    else: raise AssertionError('unsafe contract unexpectedly accepted')
   sql('create role anon;create role authenticated;create role service_role login;'
       'create table content_calendar(id uuid primary key,gym_id text,logical_post_id uuid,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
       'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);'
       'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);')
   for name in ('DRAFT_fixer_forward_media_claim_20261006.sql','DRAFT_fixer_forward_media_observation_bridge_20261007.sql','DRAFT_fixer_forward_media_source_history_20261007.sql','DRAFT_fixer_forward_media_photo_certificate_20261007.sql','DRAFT_fixer_owner_photo_clearance_20261007.sql','DRAFT_fixer_generated_owner_20261007.sql'):
    sql((ROOT/'migrations'/name).read_text())
   sql('create role still_owner login;grant fixer_forward_media_owner_20261006 to still_owner;'
       'alter role fixer_forward_media_attester_20261006 login;')
   owner = connect('still_owner')
   def rpc(q, args): return owner.execute(q, args).fetchone()[0]
   def row(gym='gym', day='2026-10-10', logical=None, fmt='feed'):
    rid=uid(); sql("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) values(%s,%s,%s,%s,'instagram',%s,'approved','active','vg-synthetic','SYNTHETIC')", (rid,gym,logical or uid(),day,fmt)); return rid
   def original(gym='gym', asset='synthetic-fresh', n=1, ph='0000000000000000'):
    return {'gym_id':gym,'source_asset_id':asset,'source_url':f'https://owned.example/{asset}.png',
      'sha256':'sha256:'+f'{n:064x}','md5':'md5:'+f'{n:032x}','phash':'scene:phash64:'+ph,'length':100}
   def known(o, decision='cleared_fresh', ref='authenticated-post-epoch:SYNTHETIC byte/provider receipt'):
    k=uid(); rpc('select fixer_still_known_record_20261007(%s,%s,%s::jsonb,%s)',(k,decision,json.dumps(o),ref)); owner.commit(); return k
   def census(rid, complete=True, available=0):
    snap=rpc('select fixer_generated_snapshot_20261007(%s)',(rid,)); owner.rollback()
    i=uid(); rpc('select fixer_still_inventory_record_20261007(%s,%s,%s,%s,%s,%s)',(i,rid,snap['inventory_revision'],complete,available,'SYNTHETIC freshly observed local library/rotation')); owner.commit(); return i
   def reserve(rid,o,k,i,kind='graphic',receipt=None,conn=None):
    c=conn or owner
    return c.execute('select fixer_still_reserve_20261007(%s,%s,%s::jsonb,%s,%s,%s)',(receipt or uid(),rid,json.dumps(o),kind,i,k)).fetchone()[0]
   def final(rid,receipt): sql('select fixer_still_final_check_20261007(%s,%s)',(rid,receipt))
   assert sql('select enabled,epoch_id from fixer_still_cutover_20261007')==[(False,None)]
   r=row(); a=original()
   denied(lambda:known(a), 'active epoch'); owner.rollback()
   rpc('select fixer_still_cutover_control_20261007(%s,%s)',(True,'SYNTHETIC explicit fixture cutover'));owner.commit()
   epoch=sql('select epoch_id from fixer_still_cutover_20261007')[0][0]
   k=known(a); i=census(r)
   incomplete=census(r,complete=False)
   denied(lambda:reserve(r,a,k,incomplete),'inventory authority');owner.rollback()
   local_supply=census(r,available=1)
   denied(lambda:reserve(r,a,k,local_supply),'inventory authority');owner.rollback()
   denied(lambda:reserve(r,a,uid(),i),'inventory authority');owner.rollback()
   # Rollback leaves no committed claim, deletion cannot erase committed use.
   token=reserve(r,a,k,i);owner.rollback()
   assert sql('select count(*) from fixer_still_reservation_20261007')[0][0]==0
   token=reserve(r,a,k,i);owner.commit()
   assert reserve(r,a,k,i,receipt=token)==token;owner.commit()
   logical=sql('select logical_post_id from content_calendar where id=%s',(r,))[0][0]
   sibling=row(logical=logical,fmt='story');si=census(sibling)
   # Cross-format siblings of the same logical post share the original.
   sibres=reserve(sibling,a,k,si);owner.commit()
   final(sibling,sibres)
   for foreign in (row(),row(day='2026-10-11',logical=logical),row(gym='other',logical=logical)):
    fo={**a,'gym_id':sql('select gym_id from content_calendar where id=%s',(foreign,))[0][0]}
    fk=known(fo);fi=census(foreign)
    denied(lambda:reserve(foreign,fo,fk,fi),'already reserved');owner.rollback()
   # A generated sibling rebind can never silently replace an approved visual.
   sql("insert into fixer_forward_media_photo_policy_20261007 values('policy-syn',true,'complete_fleet_still_photo_history',null,'SYNTHETIC ruling','SYNTHETIC auditor')")
   sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values('11111111-1111-1111-1111-111111111111','policy-syn',true,'[]','SYNTHETIC',0)")
   sql("update fixer_forward_media_photo_state_20261007 set enabled=true,routes_reconciled_ref='SYNTHETIC',baseline_id='11111111-1111-1111-1111-111111111111'")
   rl=uid(); rx=row(gym='rebind-gym',logical=rl); ry=row(gym='rebind-gym',logical=rl)
   sql("update content_calendar set source_media_asset_id='other-old',source_media_url='https://owned.example/old.png',image_url='https://owned.example/old.png',render_manifest_digest=%s where id=%s",('sha256:'+'0'*64,ry))
   job=uid()
   csnap=rpc('select fixer_generated_snapshot_20261007(%s)',(ry,)); owner.rollback()
   assert csnap['history_complete'] is True and csnap['eligible_photo_count']==0
   cand={'schema_version':1,'source_type':'generated_astra_infographic','provider':'astra','model':'gpt-6-astra','job_id':job,
    'gym_id':'rebind-gym','local_date':'2026-10-10','logical_post_id':rl,
    'copy_revision':csnap['copy_revision'],'inventory_revision':csnap['inventory_revision'],'history_revision':csnap['history_revision'],
    'original_sha256':'8'*64,'original_md5':'8'*32,'original_phash':'scene:phash64:bbbbbbbbbbbbbbbb','original_length':100,
    'original_url':'https://owned.example/rebind.png','storage_readback_sha256':'8'*64,
    'palette_revision':'SYNTHETIC','copy_digest':'SYNTHETIC','palette_digest':'SYNTHETIC','provider_response_id':'SYNTHETIC',
    'provider_output_id':'SYNTHETIC','storage_key':'SYNTHETIC','review_response_id':'SYNTHETIC','review_policy_id':'SYNTHETIC'}
   mreb={'manifest_digest':'sha256:'+'8'*64}
   sql("insert into fixer_generated_reservation_20261007 select %s,%s,'vg-synthetic',%s::jsonb,%s::jsonb,'SYNTHETIC rebind grant',fixer_generated_snapshot_20261007(%s)#>'{history,epoch}',now()",(job,rx,json.dumps(cand),json.dumps(mreb),rx))
   denied(lambda: rpc('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb)',(ry,json.dumps(cand),'[]',json.dumps(mreb))),'approved visual'); owner.rollback()
   sql("update content_calendar set source_media_asset_id=null,source_media_url=null,image_url=null,render_manifest_digest=null where id=%s",(ry,))
   census(ry)
   rpc('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb)',(ry,json.dumps(cand),'[]',json.dumps(mreb))); owner.commit()
   assert sql("select status,image_url,source_media_asset_id from content_calendar where id=%s",(ry,))[0]==('approved',cand['original_url'],'generated-astra:'+job)
   # A NEW generated reservation likewise cannot overwrite an approved visual:
   # (a) a first reserve and (b) a sibling replay both refuse, and (c) neither
   # denial mutates the approved visual or approval status.
   gf=row(gym='first-gym'); gl=sql('select logical_post_id from content_calendar where id=%s',(gf,))[0][0]
   gos=row(gym='first-gym',logical=gl); gappr=row(gym='first-gym',logical=gl)
   old_visual=('photo-old','https://owned.example/old-photo.png','sha256:'+'9'*64)
   sql("update content_calendar set source_media_asset_id=%s,source_media_url=%s,image_url=%s,render_manifest_digest=%s where id in (%s,%s)",(old_visual[0],old_visual[1],old_visual[1],old_visual[2],gappr,gos))
   sql("update content_calendar set status='draft' where id=%s",(gos,))
   owner.commit()
   def gq():
    return {'schema_version':1,'source_type':'generated_astra_infographic','provider':'astra','model':'gpt-6-astra','job_id':uid(),
     'gym_id':'first-gym','local_date':'2026-10-10','logical_post_id':str(gl),
     'copy_revision':None,'inventory_revision':None,'history_revision':None,
     'original_sha256':'c'*64,'original_md5':'c'*32,'original_phash':'scene:phash64:cccccccccccccccc','original_length':100,
     'original_url':'https://owned.example/gen-c.png','storage_readback_sha256':'c'*64,
     'palette_revision':'SYNTHETIC','copy_digest':'SYNTHETIC','palette_digest':'SYNTHETIC','provider_response_id':'SYNTHETIC',
     'provider_output_id':'SYNTHETIC','storage_key':'SYNTHETIC','review_response_id':'SYNTHETIC','review_policy_id':'SYNTHETIC'}
   def gman(candidate):
    body={'tenant_id':candidate['gym_id'],'source_asset_id':'generated-astra:'+candidate['job_id'],
     'image_url':candidate['original_url'],'image_fingerprint':'md5:'+candidate['original_md5'],
     'image_length':candidate['original_length'],'thumbnail_url':None,'thumbnail_fingerprint':None,
     'thumbnail_length':None,'operation':'same_object','render_recipe':None,
     'render_evidence_ref':'generated-astra:'+candidate['job_id']}
    body['manifest_digest']=sql("select 'sha256:'||encode(sha256(convert_to(fixer_owner_photo_canonical_20261007(%s::jsonb),'UTF8')),'hex')",(json.dumps(body),))[0][0]
    return body
   def gassert_unchanged(target):
    assert sql("select status,image_url,source_media_asset_id,source_media_url,render_manifest_digest from content_calendar where id=%s",(target,))[0]==('approved',old_visual[1],old_visual[0],old_visual[1],old_visual[2])
   def greserve(target):
    snap=rpc('select fixer_generated_snapshot_20261007(%s)',(target,)); owner.rollback()
    cand=gq(); cand['copy_revision']=snap['copy_revision']; cand['inventory_revision']=snap['inventory_revision']; cand['history_revision']=snap['history_revision']
    rpc('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb)',(target,json.dumps(cand),'[]',json.dumps(gman(cand))))
   # (a) first generated reserve on an approved row already bearing a different visual.
   denied(lambda:greserve(gappr),'approved visual'); owner.rollback()
   gassert_unchanged(gappr)
   # Bind a fresh job to the un-approved sibling, then (b) replay it onto the
   # approved sibling with the different pre-approved visual.
   gsnap=rpc('select fixer_generated_snapshot_20261007(%s)',(gos,)); owner.rollback()
   gcand=gq(); gcand['copy_revision']=gsnap['copy_revision']; gcand['inventory_revision']=gsnap['inventory_revision']; gcand['history_revision']=gsnap['history_revision']
   gjob=gcand['job_id']; gmanifest=gman(gcand)
   ghistory=[{'history_key':h['history_key'],'published_binding_ref':h['published_binding_ref'],'visual_sha256':'sha256:'+cand['original_sha256'],'phash':cand['original_phash'],'visual_url':h['visual_url']} for h in gsnap['history']['rows']]
   census(gos)
   rpc('select fixer_reserve_generated_20261007(%s,%s::jsonb,%s::jsonb,%s::jsonb)',(gos,json.dumps(gcand),json.dumps(ghistory),json.dumps(gmanifest))); owner.commit()
   denied(lambda:greserve(gappr),'approved visual'); owner.rollback()
   gassert_unchanged(gappr)
   sql('delete from content_calendar where id=%s',(r,))
   later=row(day='2026-10-12');li=census(later)
   denied(lambda:reserve(later,a,k,li),'already reserved');owner.rollback()
   # Two different SHA objects with close pHash serialize into one winner.
   rs=[row(gym='race-a'),row(gym='race-b')]
   objects=[original('race-a','race-a',2,'ffffffffffffffff'),original('race-b','race-b',3,'fffffffffffffffe')]
   keys=[known(o) for o in objects]; inventories=[census(x) for x in rs]
   def race(n):
    c=connect('still_owner')
    try: reserve(rs[n],objects[n],keys[n],inventories[n],conn=c);c.commit();return 'reserved'
    except psycopg.Error as e: c.rollback();assert 'already reserved' in str(e),str(e);return 'held'
    finally:c.close()
   with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    assert sorted(pool.map(race,range(2)))==['held','reserved']
   # Approved photo and graphic share the same perceptual reservation authority.
   photo=original('photo-gym','photo',4,'5555555555555555');pr=row(gym='photo-gym')
   sql("insert into media_source values('photo-src','photo-gym','gym_drive','folder',true,'ready',now())")
   proof={'verdict':'clean','provider':'SYNTHETIC','content_hash':photo['md5'][4:],'asset_id':'photo','gym_id':'photo-gym','people_detected':False,'observed_at':'2026-10-07T00:00:00Z','sha256':photo['sha256'][7:]}
   sql("insert into media_asset values('photo','photo-src','photo-gym',%s,null,'photo',true,false,'approved','clean',%s,'SYNTHETIC',now(),%s::jsonb,false,0)",(photo['md5'][4:],photo['md5'][4:],json.dumps(proof)))
   pk=known(photo);pi=census(pr);pres=reserve(pr,photo,pk,pi,kind='photo');owner.commit()
   graphic=original('graphic-gym','near-photo',5,'5555555555555554');gr=row(gym='graphic-gym');gk=known(graphic);gi=census(gr)
   denied(lambda:reserve(gr,graphic,gk,gi),'already reserved');owner.rollback()
   # Final trusted check must hold late arrivals and stale sync independently.
   def finalcheck(): final(pr,pres)
   finalcheck()
   sql("update media_source set sync_finished_at=now()-interval '31 minutes' where id='photo-src'")
   denied(finalcheck,'inventory authority')
   sql("update media_source set sync_finished_at=now() where id='photo-src'")
   pi=census(pr)
   sql("update media_asset set moderation_json=moderation_json-'sha256' where id='photo'")
   denied(finalcheck,'inventory authority')
   sql("update media_asset set moderation_json=%s::jsonb where id='photo'",(json.dumps(proof),));pi=census(pr)
   finalcheck()
   # >10 minute reserve-to-final: the stored receipt is never re-trusted for
   # freshness, so an aged reservation holds WITHOUT a fresh census yet stays
   # sendable once the owner records one; reservation identity is unchanged.
   aged=original('aged-gym','aged-photo',6,'6666666666666666');ar=row(gym='aged-gym')
   sql("insert into media_source values('aged-src','aged-gym','gym_drive','folder',true,'ready',now())")
   aproof={'verdict':'clean','provider':'SYNTHETIC','content_hash':aged['md5'][4:],'asset_id':'aged-photo','gym_id':'aged-gym','people_detected':False,'observed_at':'2026-10-07T00:00:00Z','sha256':aged['sha256'][7:]}
   sql("insert into media_asset values('aged-photo','aged-src','aged-gym',%s,null,'photo',true,false,'approved','clean',%s,'SYNTHETIC',now(),%s::jsonb,false,0)",(aged['md5'][4:],aged['md5'][4:],json.dumps(aproof)))
   ak=known(aged)
   alog=sql('select logical_post_id from content_calendar where id=%s',(ar,))[0][0]
   asnap=rpc('select fixer_generated_snapshot_20261007(%s)',(ar,)); owner.rollback()
   oldi=uid(); oldr=uid()
   sql("insert into fixer_still_inventory_20261007 values(%s,%s,'aged-gym',%s,true,1,'SYNTHETIC aged reserve-time census',now()-interval '20 minutes')",(oldi,epoch,asnap['inventory_revision']))
   sql("insert into fixer_still_reservation_20261007 values(%s,%s,%s,'aged-gym','2026-10-10',%s,'vg-synthetic','photo',%s::jsonb,%s,%s,now()-interval '20 minutes')",(oldr,epoch,ar,alog,json.dumps(aged),oldi,ak))
   denied(lambda: final(ar,oldr),'fresh inventory authority')
   census(ar,available=1)
   final(ar,oldr)
   # Redating or retargeting the row holds the final send even with a fresh census.
   sql("update content_calendar set post_date='2026-10-11' where id=%s",(ar,))
   denied(lambda: final(ar,oldr),'current binding')
   sql("update content_calendar set post_date='2026-10-10' where id=%s",(ar,))
   final(ar,oldr)
   # A committed local supply observation of one cannot prove depletion.
   localgraphic=original('local','local-art',7,'aaaaaaaaaaaaaaaa');lr=row(gym='local');lk=known(localgraphic);linv=census(lr,available=1)
   denied(lambda:reserve(lr,localgraphic,lk,linv),'inventory authority');owner.rollback()
   # Asset-bound quarantine is permanent even when hashes/URL change.
   known({'gym_id':'local','source_asset_id':'local-art'},'quarantine','SYNTHETIC unknown older local source')
   linv=census(lr)
   denied(lambda:reserve(lr,localgraphic,lk,linv),'quarantined');owner.rollback()
   known(photo,'deny','SYNTHETIC later known historical use')
   denied(finalcheck,'quarantined')
   # A pending client photo is supply awaiting moderation, never depletion.
   prow=row(gym='pend-gym')
   sql("insert into media_source values('pend-src','pend-gym','gym_drive','folder',true,'ready',now())")
   sql("insert into media_asset values('pend-photo','pend-src','pend-gym','%s',null,'photo',true,false,'pending_review','pending',null,null,null,'{}',false,0)" % ('e'*32,))
   pa2=original('pend-gym','pend-art',8,'eeeeeeeeeeeeeeee');pk2=known(pa2);pi2=census(prow,complete=False)
   denied(lambda:reserve(prow,pa2,pk2,pi2),'inventory authority');owner.rollback()
   # An inactive Drive source holds inventory; it never proves depletion, and
   # an active source must be genuinely fresh (same rule at reserve and final).
   ia=original('inact-gym','inact-art',9,'dddddddddddddddd');ir=row(gym='inact-gym')
   sql("insert into media_source values('inact-src','inact-gym','gym_drive','folder',true,'ready',now())")
   sql("insert into media_source values('inact-old','inact-gym','gym_drive','folder-old',false,'ready',now()-interval '90 days')")
   ik=known(ia);ii=census(ir,complete=False)
   denied(lambda:reserve(ir,ia,ik,ii),'inventory authority');owner.rollback()
   sql("delete from media_source where id='inact-old'")
   ii=census(ir);reserve(ir,ia,ik,ii);owner.commit()
   sql("update media_source set sync_finished_at=now()+interval '5 minutes' where id='inact-src'")
   ii2=census(ir)
   denied(lambda:reserve(ir,ia,ik,ii2),'inventory authority');owner.rollback()
   sql("update media_source set sync_finished_at=now() where id='inact-src'")
   # A late photo arrival after a graphic reservation holds the final send.
   lg=original('late-gym','late-art',10,'9999999999999999');lr2=row(gym='late-gym')
   sql("insert into media_source values('late-src','late-gym','gym_drive','folder',true,'ready',now())")
   lk2=known(lg);li2=census(lr2);lres=reserve(lr2,lg,lk2,li2);owner.commit()
   sql("insert into media_asset values('late-photo','late-src','late-gym','%s',null,'photo',true,false,'pending_review','pending',null,null,null,'{}',false,0)" % ('c'*32,))
   denied(lambda: final(lr2,lres),'inventory authority')
   # Near-pHash denial against the retained published/delivered baseline.
   sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'policy-syn',true,%s::jsonb,'SYNTHETIC',1)",(uid(),json.dumps([{'history_key':'calendar-image:delivered-old','resolved':True,'media_kind':'still_photo','visual_sha256':'sha256:'+'9'*64,'phash':'scene:phash64:7777777777777770','visual_url':'https://owned.example/delivered-old.png','published_binding_ref':'SYNTHETIC delivered receipt'}])))
   dup=original('dup-gym','dup-art',11,'7777777777777777');dr=row(gym='dup-gym');dk=known(dup);di=census(dr)
   denied(lambda:reserve(dr,dup,dk,di),'already reserved');owner.rollback()
   # Existing staged B original is held by negative knowledge at provenance.
   bid=uid();br=row(gym='staged');bo=original('staged','generated-astra:'+bid,12,'3333333333333333')
   c={'job_id':bid,'gym_id':'staged','local_date':'2026-10-10','original_url':bo['source_url'],'original_md5':bo['md5'][4:],'original_sha256':bo['sha256'][7:],'original_length':100,'original_phash':bo['phash']}
   sql("insert into fixer_generated_reservation_20261007 values(%s,%s,'vg-synthetic',%s::jsonb,'{}','SYNTHETIC staged grant','{}',now())",(bid,br,json.dumps(c)))
   sql("insert into fixer_forward_media_original_registry_20261006 values(%s,%s,%s,%s,100,%s,now())",('staged',bo['source_asset_id'],bo['source_url'],bo['md5'],'astra-job:'+bid))
   sql("insert into fixer_forward_media_history_clearance_20261006 values(%s,%s,%s,%s,100,%s,'cleared_unused','SYNTHETIC staged grant',now())",('staged',bo['source_asset_id'],bo['source_url'],bo['md5'],'astra-job:'+bid))
   manifest='sha256:'+'7'*64
   sql("insert into fixer_forward_media_render_manifest_20261006(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,operation,render_evidence_ref) values(%s,'staged',%s,%s,%s,100,'same_object','SYNTHETIC')",(manifest,bo['source_asset_id'],bo['source_url'],bo['md5']))
   sql('update content_calendar set source_media_asset_id=%s,source_media_url=%s,image_url=%s,render_manifest_digest=%s where id=%s',(bo['source_asset_id'],bo['source_url'],bo['source_url'],manifest,br))
   known(bo,'deny','SYNTHETIC staged known denial')
   denied(lambda:rpc('select fixer_forward_media_provenance_lookup_20261006(%s)',(br,)),'quarantined');owner.rollback()
   sql("insert into fixer_forward_media_claim_gate_20261006 values('staged',true)")
   claim=uid();sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",(claim,br))
   revision=sql('select fixer_forward_media_attestation_request_20261006(%s)',(br,))[0][0]['revision']
   # A forged/foreign certificate reference is not a clearance route.
   denied(lambda:known(original('forge-gym','forge-art',13,'abcdefabcdefabcd'),'cleared_certificate','photo-audit:sha256:'+'f'*64),'older asset requires exact signed photo authority');owner.rollback()
   # A resolved delivered row with neither exact bytes nor a valid pHash fails
   # closed for every later candidate; novelty is never inferred.
   sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'policy-syn',true,%s::jsonb,'SYNTHETIC',1)",(uid(),json.dumps([{'history_key':'calendar-image:delivered-unknown','resolved':True,'media_kind':'still_photo','visual_sha256':None,'visual_url':'https://owned.example/unknown.png','published_binding_ref':'SYNTHETIC unknown receipt'}])))
   unk=original('unk-gym','unk-art',14,'1234567890abcdef');ur=row(gym='unk-gym');uk=known(unk);ui2=census(ur)
   denied(lambda:reserve(ur,unk,uk,ui2),'already reserved');owner.rollback()
   # A delivered baseline row with valid bytes but a missing or invalid pHash
   # is uncomparable: every later candidate holds; novelty is never inferred
   # and exact identity constraints for same-logical siblings are unchanged.
   for missing in (True,False):
    sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'policy-syn',true,%s::jsonb,'SYNTHETIC',1)",(uid(),json.dumps([{'history_key':'calendar-image:delivered-nophash-'+str(missing),'resolved':True,'media_kind':'still_photo','visual_sha256':'sha256:'+'b'*64,'phash':None if missing else 'not-a-valid-phash','visual_url':'https://owned.example/delivered-nophash.png','published_binding_ref':'SYNTHETIC nophash receipt'}])))
   nph=original('nph-gym','nph-art',15,'7777777777777777');nr=row(gym='nph-gym');nk=known(nph);ni=census(nr)
   denied(lambda:reserve(nr,nph,nk,ni),'already reserved');owner.rollback()
   # Generated final send re-observes local depletion: no census, or only an
   # aged one, holds at this gate. A fresh zero-supply observation advances to
   # the later history gate, which this synthetic fixture leaves unresolved.
   gj=uid();gr2=row(gym='gen-gym');glog=sql('select logical_post_id from content_calendar where id=%s',(gr2,))[0][0]
   gsnap2=rpc('select fixer_generated_snapshot_20261007(%s)',(gr2,));owner.rollback()
   gc={'job_id':gj,'gym_id':'gen-gym','local_date':'2026-10-10','logical_post_id':str(glog),
    'copy_revision':gsnap2['copy_revision'],'inventory_revision':gsnap2['inventory_revision'],
    'original_sha256':'d'*64,'original_md5':'d'*32,'original_phash':'scene:phash64:0f0f0f0f0f0f0f0f','original_length':100,
    'original_url':'https://owned.example/gen-d.png'}
   gm={'manifest_digest':'sha256:'+'d'*64}
   sql("insert into fixer_generated_reservation_20261007 values(%s,%s,'vg-synthetic',%s::jsonb,%s::jsonb,'SYNTHETIC gen final grant',%s::jsonb,now())",(gj,gr2,json.dumps(gc),json.dumps(gm),json.dumps(gsnap2['history']['epoch'])))
   sql('update content_calendar set source_media_asset_id=%s,source_media_url=%s,image_url=%s,render_manifest_digest=%s where id=%s',('generated-astra:'+gj,gc['original_url'],gc['original_url'],gm['manifest_digest'],gr2))
   gepoch=sql('select epoch_id from fixer_still_cutover_20261007')[0][0]
   # Exercise the generated final-send guard directly; the wrapper also needs
   # unrelated base provenance fixtures before it reaches this guard.
   def genfinal(): sql('select fixer_generated_runtime_check_20261007(%s)',(gr2,))
   denied(genfinal,'fresh local depletion');owner.rollback()
   sql("insert into fixer_still_inventory_20261007 values(%s,%s,'gen-gym',%s,true,0,'SYNTHETIC gen final aged census',now()-interval '20 minutes')",(uid(),gepoch,gsnap2['inventory_revision']))
   denied(genfinal,'fresh local depletion');owner.rollback()
   sql("insert into fixer_still_inventory_20261007 values(%s,%s,'gen-gym',%s,true,0,'SYNTHETIC gen final stale-epoch census',now())",(uid(),uid(),gsnap2['inventory_revision']))
   denied(genfinal,'fresh local depletion');owner.rollback()
   sql("insert into fixer_still_inventory_20261007 values(%s,%s,'gen-gym',%s,true,0,'SYNTHETIC gen final fresh census',now())",(uid(),gepoch,gsnap2['inventory_revision']))
   denied(genfinal,'generated historical perceptual identity unresolved or repeated');owner.rollback()
   # Service cannot invoke owner functions or insert/control raw authority.
   service=connect('service_role')
   denied(lambda:service.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',(br,claim,uid(),revision)),'quarantined');service.rollback()
   for q,args in [('select fixer_still_cutover_control_20261007(true,%s)',('forged',)),
                  ('select fixer_still_known_record_20261007(%s,%s,%s::jsonb,%s)',(uid(),'cleared_fresh',json.dumps(a),'forged')),
                  ('select fixer_still_reserve_20261007(%s,%s,%s::jsonb,%s,%s,%s)',(uid(),sibling,json.dumps(a),'graphic',si,k)),
                  ('select fixer_still_final_check_20261007(%s,%s)',(sibling,sibres)),
                  ('update fixer_still_cutover_20261007 set enabled=true',None),
                  ('delete from fixer_still_reservation_20261007',None)]:
    denied(lambda:service.execute(q,args),'permission denied');service.rollback()
   service.close()
   denied(lambda:sql('delete from fixer_still_reservation_20261007'),'immutable')
   rpc('select fixer_still_cutover_control_20261007(%s,%s)',(False,'SYNTHETIC disabled'));owner.commit()
   assert sql('select epoch_id from fixer_still_cutover_20261007')[0][0]==epoch
   denied(lambda:reserve(sibling,a,k,si),'inventory authority');owner.rollback()
   owner.close();admin.close()
   print('PASS: PG17 default-OFF epoch; owner-only asset deny/quarantine; immutable common photo/graphic exact+near-pHash concurrency; date/tenant/logical isolation+sibling; rollback/delete retention; local supply, stale source and moderation holds; aged reserve-to-final with fresh census; redating/retargeting hold; baseline near-pHash and fail-closed unknowns/uncomparable delivered stills; generated final-send fresh depletion gate; inactive-source hold; pending/late photo holds; approved-visual rebind guard; forged certificate denial. Incremental contract only, no positive generated staging adapter.')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True)


@pytest.mark.skipif(PG17 is None, reason='PostgreSQL 17 binaries not found on this host')
def test_still_forward_contract_pg():
 run_contract(PG17)


def main():
 run_contract(PG17 or Path('/opt/homebrew/opt/postgresql@17/bin'))


if __name__=='__main__': main()
