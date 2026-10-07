"""Disposable PG17 incremental owner contract. No production or provider I/O.

This does not prove a deployed forward admission adapter. Owner fresh-original
and local-library observations below are explicitly synthetic trust fixtures.
"""
import concurrent.futures
import json
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
 import psycopg
 pg = Path('/opt/homebrew/opt/postgresql@17/bin')
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
   def row(gym='gym', day='2026-10-10', logical=None):
    rid=uid(); sql("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) values(%s,%s,%s,%s,'instagram','feed','approved','active','vg-synthetic','SYNTHETIC')", (rid,gym,logical or uid(),day)); return rid
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
   sibling=row(logical=logical);si=census(sibling)
   reserve(sibling,a,k,si);owner.commit()
   for foreign in (row(),row(day='2026-10-11',logical=logical),row(gym='other',logical=logical)):
    fo={**a,'gym_id':sql('select gym_id from content_calendar where id=%s',(foreign,))[0][0]}
    fk=known(fo);fi=census(foreign)
    denied(lambda:reserve(foreign,fo,fk,fi),'already reserved');owner.rollback()
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
   pk=known(photo);pi=census(pr);reserve(pr,photo,pk,pi,kind='photo');owner.commit()
   graphic=original('graphic-gym','near-photo',5,'5555555555555554');gr=row(gym='graphic-gym');gk=known(graphic);gi=census(gr)
   denied(lambda:reserve(gr,graphic,gk,gi),'already reserved');owner.rollback()
   # Final trusted check must hold late arrivals and stale sync independently.
   def finalcheck(): sql('select fixer_still_reservation_check_20261007(%s,%s::jsonb,%s,%s,%s)',(pr,json.dumps(photo),'photo',pi,pk))
   finalcheck()
   sql("update media_source set sync_finished_at=now()-interval '31 minutes' where id='photo-src'")
   denied(finalcheck,'inventory authority')
   sql("update media_source set sync_finished_at=now() where id='photo-src'")
   pi=census(pr)
   sql("update media_asset set moderation_json=moderation_json-'sha256' where id='photo'")
   denied(finalcheck,'inventory authority')
   sql("update media_asset set moderation_json=%s::jsonb where id='photo'",(json.dumps(proof),));pi=census(pr)
   finalcheck()
   # A committed local supply observation of one cannot prove depletion.
   localgraphic=original('local','local-art',6,'aaaaaaaaaaaaaaaa');lr=row(gym='local');lk=known(localgraphic);linv=census(lr,available=1)
   denied(lambda:reserve(lr,localgraphic,lk,linv),'inventory authority');owner.rollback()
   # Asset-bound quarantine is permanent even when hashes/URL change.
   known({'gym_id':'local','source_asset_id':'local-art'},'quarantine','SYNTHETIC unknown older local source')
   linv=census(lr)
   denied(lambda:reserve(lr,localgraphic,lk,linv),'quarantined');owner.rollback()
   known(photo,'deny','SYNTHETIC later known historical use')
   denied(finalcheck,'quarantined')
   # Existing staged B original is held by negative knowledge at provenance.
   bid=uid();br=row(gym='staged');bo=original('staged','generated-astra:'+bid,7,'3333333333333333')
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
   # Service cannot invoke owner functions or insert/control raw authority.
   service=connect('service_role')
   denied(lambda:service.execute('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',(br,claim,uid(),revision)),'quarantined');service.rollback()
   for q,args in [('select fixer_still_cutover_control_20261007(true,%s)',('forged',)),
                  ('select fixer_still_known_record_20261007(%s,%s,%s::jsonb,%s)',(uid(),'cleared_fresh',json.dumps(a),'forged')),
                  ('select fixer_still_reserve_20261007(%s,%s,%s::jsonb,%s,%s,%s)',(uid(),sibling,json.dumps(a),'graphic',si,k)),
                  ('update fixer_still_cutover_20261007 set enabled=true',None),
                  ('delete from fixer_still_reservation_20261007',None)]:
    denied(lambda:service.execute(q,args),'permission denied');service.rollback()
   service.close()
   denied(lambda:sql('delete from fixer_still_reservation_20261007'),'immutable')
   rpc('select fixer_still_cutover_control_20261007(%s,%s)',(False,'SYNTHETIC disabled'));owner.commit()
   assert sql('select epoch_id from fixer_still_cutover_20261007')[0][0]==epoch
   denied(lambda:reserve(sibling,a,k,si),'inventory authority');owner.rollback()
   owner.close();admin.close()
   print('PASS: PG17 default-OFF epoch; owner-only asset deny/quarantine; immutable common photo/graphic exact+near-pHash concurrency; date/tenant/logical isolation+sibling; rollback/delete retention; local supply, stale source and moderation holds. Incremental contract only, no positive staging adapter.')
  finally:
   subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True)


if __name__=='__main__': main()
