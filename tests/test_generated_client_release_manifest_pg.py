"""Actual full default-OFF release stack in disposable PG17, no production DSN.

Whole migrations/preflights are executed unchanged. Synthetic base schema and
trusted fixture facts remain the accepted install/seed evidence; no replacement
portal version/approval trigger or census/inventory authority is fabricated.
The accepted seed artifact and portal release files are hash-pinned fixtures.
The four upstream entry tranche bodies are the accepted corpus test's actual
source extracts; that corpus's full unchanged preflight verifies their hashes.
"""
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/generated_client_release_manifest'
P0611 = ROOT / 'tests/fixtures'
P0625 = FIXTURES
P0611_APPLY = P0611 / 'portal_0611_echo_source_brand_bundle_held_20261008.sql'
P0611_VERIFY = FIXTURES / '0611_echo_source_brand_bundle.verify.sql'
SEED = FIXTURES / 'generated_client_seed_20261009.py'
SEED_SHA = 'd00b1a98c4fe413f84c859ac664682aa61fddb2a32c3d5d5a59f77b55969a4e8'
PORTAL_SHA = 'd54a5b9e1f857d31912540f38f626b59c891296a4aebd7b3c2d55a1f4abdc705'
HASHES = {
 '0611_echo_source_brand_bundle.verify.sql': 'c57bd51f871e1a3eef142c9f69b5a474830b47c946d449a35fdd8f135b833c54',
 'DRAFT_0625_generated_client_approval.sql': 'd54a5b9e1f857d31912540f38f626b59c891296a4aebd7b3c2d55a1f4abdc705',
 'DRAFT_0625_generated_client_approval.verify.sql': '19fd8aa6b031bd9fd80aa70a64b34b737daf1ae02d03d0e04bfa6d3bf4953ee7',
 'generated_client_seed_20261009.py': 'd00b1a98c4fe413f84c859ac664682aa61fddb2a32c3d5d5a59f77b55969a4e8',
 'portal_0611_echo_source_brand_bundle_held_20261008.sql': 'd3a3b22d62f2ebc0376f89552a994d785947eb5309dff6e3d655ebf4c0f71178',
}
FOUNDATIONS = (
 'DRAFT_fixer_forward_media_claim_20261006.sql',
 'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
 'DRAFT_fixer_forward_media_source_history_20261007.sql',
 'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
 'DRAFT_fixer_owner_photo_clearance_20261007.sql',
 'lasso_bounded_catchup_capacity_20261005.sql',
 'lasso_immediate_backlog_capacity_20261005.sql',
 'calendar_approval_provenance_20261005.sql')
GENERATED = (
 'DRAFT_fixer_generated_owner_20261007.sql', 'DRAFT_fixer_generated_gap_dispatch_20261007.sql',
 'DRAFT_generated_source_palette_authority_20261007.sql', 'DRAFT_generated_send_lease_20261007.sql')
STAGED = (
 'DRAFT_fixer_forward_media_owner_transport_20261007.sql',
 'DRAFT_fixer_generated_bundle_bridge_20261008.sql',
 'DRAFT_fixer_forward_visual_index_20261008.sql',
 'DRAFT_fixer_forward_schedule_reservation_20261008.sql',
 'DRAFT_fixer_forward_schedule_stage_20261008.sql',
 'DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql',
 'DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql',
 'DRAFT_fixer_generated_local_census_producer_20261008.sql',
 'DRAFT_fixer_photo_historical_clearance_20261008.sql',
 'DRAFT_fixer_prospective_photo_authority_20261008.sql',
 'DRAFT_fixer_photo_historical_clearance_20261008.sql',
 'DRAFT_fixer_prospective_still_v2_20261008.sql',
 'DRAFT_fixer_still_v2_owner_transport_20261008.sql',
 'DRAFT_fixer_current_census_reservation_lookup_20261008.sql',
 'DRAFT_fixer_inventory_mutation_protocol_20261008.sql',
 'DRAFT_fixer_remote_drive_use_cas_20261008.sql',
 'DRAFT_fixer_calendar_admission_guard_20261009.sql')
CONSUMER = (
 'DRAFT_generated_hosted_byte_authority_20261009.sql',
 'DRAFT_generated_client_admission_20261009.sql',
 'DRAFT_generated_client_staged_adapter_20261009.sql',
 'DRAFT_generated_client_admission_20261009.verify.sql',
 'DRAFT_generated_client_staged_adapter_20261009.verify.sql')
CHAIN_RECEIPTS = {}
SIGNATURES = (
 'public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)',
 'public.fixer_generated_runtime_check_20261007(uuid)',
 'public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)')


def catalog(db):
 result = {}
 for signature in SIGNATURES:
  row = db.execute('''select oid,pg_get_userbyid(proowner),proacl::text,proconfig,
   encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc
   where oid=to_regprocedure(%s)''', (signature,)).fetchone()
  if row: result[signature] = row
 return result


def apply(db, path):
 raw = path.read_bytes()
 print('INSTALL', path.name, hashlib.sha256(raw).hexdigest(), flush=True)
 before = catalog(db)
 try: db.execute(raw.decode('utf-8'))
 except Exception as error:
  db.execute('rollback')
  raise AssertionError(f'FIRST RELEASE INCOMPATIBILITY at {path.name}: {error}\nCatalog: {before!r}') from error
 after = catalog(db)
 for signature, prior in before.items():
  if signature not in after: continue
  assert prior[:4] == after[signature][:4], (path.name, signature, prior, after[signature])
  if prior[4] != after[signature][4]:
   print('BODY OVERLAY', path.name, signature, prior[4], '->', after[signature][4], flush=True)


def install(db):
 from tests.test_forward_corpus_atomic_cutover_pg import entry_definitions
 tree = ast.parse((ROOT/'tests/test_generated_bundle_bridge_pg.py').read_text())
 bootstrap = next(n.args[0].value for n in ast.walk(tree)
  if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='sql'
  and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str)
  and 'create role anon;create role authenticated' in n.args[0].value)
 db.execute(bootstrap)
 # Match Supabase's production extension placement before any migration runs.
 db.execute('create schema extensions; create extension pgcrypto with schema extensions')
 db.execute('''alter table content_calendar add column pillar text,add column slot_index integer,
  add column scheduled_at timestamptz,add column byte_hash text,add column gbp_topic_type text,
  add column gbp_cta_type text,add column gbp_cta_url text,add column gbp_event jsonb,add column gbp_offer jsonb;
  create table gym_assignments(app_user_id uuid,gym_id uuid,relationship text,primary key(app_user_id,gym_id));
  create table echo_gym_settings(gym_id uuid primary key,autonomous boolean,autonomy_updated_by text);
  alter table media_asset add column review_note text,add column consent_status text,
   add column release_ref text,add column consent_member_ref text,add column consent_expires_at timestamptz;
  create table media_asset_review_event(gym_id text,asset_id text,content_hash text,prior_status text,
   decision text,reviewed_by text,reviewed_at timestamptz,review_note text);
  create table echo_infographic_artifacts(tenant text,image_url text,image_sha256 text,evidence jsonb,source_identity jsonb);
  set check_function_bodies=off;''')
 for name in FOUNDATIONS: apply(db,ROOT/'migrations'/name)
 receipt = (ROOT/'migrations/portal_action_receipt_draft_20261004.sql').read_text()
 # Only unrelated receipt foundation is sliced, as in the accepted installer.
 db.execute(receipt[receipt.index('CREATE TABLE IF NOT EXISTS public.portal_action_receipt ('):
  receipt.index('-- Trusted public media origin:')])
 for name, definition in entry_definitions().items():
  if name.startswith('fixer_') and name!='fixer_forward_calendar_entry_lock_20261008': continue
  db.execute(definition)
 apply(db,ROOT/'migrations/DRAFT_fixer_forward_corpus_atomic_cutover_20261008.sql')
 for name in GENERATED: apply(db,ROOT/'migrations'/name)
 apply(db,P0611_APPLY)
 for name in STAGED: apply(db,ROOT/'migrations'/name)
 apply(db,P0625/'DRAFT_0625_generated_client_approval.sql')
 for name in CONSUMER: apply(db,ROOT/'migrations'/name)
 apply(db,P0611_VERIFY)
 apply(db,P0625/'DRAFT_0625_generated_client_approval.verify.sql')
 for table in ('fixer_inventory_protocol_control_20261008','fixer_remote_drive_use_control_20261008',
  'fixer_calendar_admission_gate_20261009'):
  assert db.execute(f'select enabled from public.{table}').fetchall()==[(False,)]
 for name in ('fixer_local_census_latest_private_20261008','fixer_inventory_protocol_lock_private_20261008'):
  assert not db.execute('''select exists(select 1 from pg_proc p cross join lateral
   aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
   where p.proname=%s and a.grantee=0)''',(name,)).fetchone()[0]


def chain_portability_and_private_acl(db,sock,port,ordinary):
 import psycopg
 import uuid
 pin=db.execute("select original_oid,body_sha256,normalized_sha256,admission_body_oid,admission_body_sha256 from generated_client_install_pin_20261009").fetchone()
 assert pin[2]=='c658ed1e8e4194eb961e32445890f734d26d156d79011b658149a77c06caf5bc'
 assert pin[4]=='7eaacf4f87922e0f4948fc56a40a9da44b6bdc77cb90b553c6e413d0a7827f29'
 CHAIN_RECEIPTS[ordinary]=pin
 if len(CHAIN_RECEIPTS)==2:
  left,right=CHAIN_RECEIPTS[False],CHAIN_RECEIPTS[True]
  assert left[0]!=right[0] and left[1]!=right[1]
  assert left[2]==right[2] and left[4]==right[4]
  print('PASS two clusters different public OIDs/raw wrapper hashes; same normalized/underlying pins',left[0],right[0],flush=True)
 db.execute('create role release_service_probe login;grant service_role to release_service_probe; create role release_owner_probe login;grant fixer_forward_media_owner_20261006 to release_owner_probe')
 private=('generated_client_original_finalizer_20261009',f'fixer_admission_body_{pin[0]}')
 for login in ('release_service_probe','release_owner_probe'):
  with psycopg.connect(host=str(sock),port=port,user=login,dbname='postgres',autocommit=True) as probe:
   for name in private:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
     probe.execute(f'select public.{name}(%s,%s,%s::jsonb,%s::jsonb)',('gym',uuid.uuid4(),'[]','[]'))
   with pytest.raises(psycopg.errors.InsufficientPrivilege):
    probe.execute("insert into fixer_calendar_admission_capability_20261009 values(%s,pg_backend_pid(),pg_current_xact_id(),'finalize',array[]::uuid[])",(uuid.uuid4(),))
 assert db.execute('select count(*) from fixer_calendar_admission_capability_20261009').fetchone()[0]==0
 print('PASS direct private finalizer/capability invocation denied for owner and service logins',flush=True)


def accepted_seed():
 # The frozen predecessor flow itself is unchanged. These fixture-only call
 # upgrades use the actual new authority RPC and preserve producer identity.
 # No raw snapshot grant or removed six-arg API is restored.
 source=SEED.read_text()
 source=source.replace("'fixer_generated_snapshot_20261007'", "'fixer_generated_snapshot_guarded_20261008'")
 calls={
  "rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),rid,snap['inventory_revision'],True,0,'SYNTHETIC current zero')": "record_census(rid,0)",
  "rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),new_id,snap['inventory_revision'],True,1,'SYNTHETIC late photo')": "record_census(rid,1)",
  "rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),new_id,snap['inventory_revision'],True,0,'SYNTHETIC restored zero')": "record_census(rid,0)",
 }
 for old,new in calls.items():
  assert source.count(old)==1
  source=source.replace(old,new)
 needle="  owner=connect('generated_owner');service=connect('service_role');baseline=uuid.uuid4()"
 assert source.count(needle)==1
 source=source.replace(needle,needle+"""
  # Only this disposable fixture attests complete local writer coverage.
  sql("update fixer_inventory_protocol_control_20261008 set enabled=true,all_writers_verified_ref='SYNTHETIC disposable all writer coverage'")
  def record_census(row,available):
   expected=rpc(owner,'fixer_generated_local_census_snapshot_20261008',row)
   before=sql('select count(*) from fixer_still_inventory_20261007')[0][0]
   denied(lambda:rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),row,
    expected['snapshot']['inventory_revision'],True,available,'local-census:sha256:'+'b'*64,
    uuid.UUID(expected['epoch_id']),Jsonb(expected),sql('select clock_timestamp()')[0][0]-timedelta(minutes=6)),
    'stale epoch/snapshot-bound census producer refuses')
   assert sql('select count(*) from fixer_still_inventory_20261007')[0][0]==before
   return rpc(owner,'fixer_still_inventory_record_20261007',uuid.uuid4(),row,
    expected['snapshot']['inventory_revision'],True,available,'local-census:sha256:'+'a'*64,
    uuid.UUID(expected['epoch_id']),Jsonb(expected),sql('select clock_timestamp()')[0][0])
""")
 # Exercise the preserved capability wrapper with its real admission gate
 # armed ONLY in this synthetic database after all initial default-OFF checks.
 old=" ordinary_receipt,ordinary_via=finalworker.finalize_batch(ordinary_store,ordinary_status)"
 assert source.count(old)==1
 source=source.replace(old," db.execute('update fixer_calendar_admission_gate_20261009 set enabled=true')\n"+old)
 old="   print('PASS reservation refusal rolls back gap rebind, private decision and activation',flush=True)"
 assert source.count(old)==1
 source=source.replace(old,old+"""
   sql('update fixer_calendar_admission_gate_20261009 set enabled=true')
   # Real inventory transaction wins G; finalizer waits before acquiring C or
   # any calendar row. Rollback cancels the mutation, then the bad proof still
   # refuses the finalizer atomically; no blind worker retry is involved.
   import time
   from concurrent.futures import ThreadPoolExecutor
   epoch=sql('select epoch_id from fixer_still_cutover_20261007')[0][0]
   mutation=uuid.uuid4();probe=connect('service_role')
   owner.execute('begin')
   rpc(owner,'fixer_inventory_mutation_begin_20261008',mutation,'gym',epoch,'library_write','sha256:'+'c'*64)
   with ThreadPoolExecutor(max_workers=1) as inventory_pool:
    pending=inventory_pool.submit(denied,lambda:rpc(probe,'finalize_forward_schedule_staged_batch_20261008',
     'gym',batch,Jsonb(bad_candidates),Jsonb([plan['old_snapshot']])),'inventory rollback then reservation refusal')
    try:
     deadline=time.monotonic()+3
     while time.monotonic()<deadline:
      waiting=sql('select wait_event from pg_stat_activity where pid=%s',(probe.info.backend_pid,))[0][0]
      if waiting=='advisory':break
      time.sleep(.01)
     assert waiting=='advisory' and not pending.done()
    finally:owner.execute('rollback')
    pending.result(timeout=3)
   assert sql('select count(*) from fixer_inventory_mutation_20261008 where mutation_id=%s',(mutation,))[0][0]==0
   assert sql("select pg_try_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0))")[0][0]
   assert_rollback()
   print('PASS inventory mutation race waits at G; rollback releases authority and finalizer refusal preserves all state',flush=True)
""")
 old="   print('PASS actual separate finalizer worker function + service SQL + exact terminal receipt retry',flush=True)"
 assert source.count(old)==1
 source=source.replace(old,old+"""
   changed=[{**candidates[0],'expected_revision':'changed-terminal-args'}]
   denied(lambda:rpc(service,'finalize_forward_schedule_staged_batch_20261008','gym',batch,
    Jsonb(changed),Jsonb([plan['old_snapshot']])),'changed terminal arguments refuse')
   assert sql('select count(*) from forward_schedule_reservation')[0][0]==1
   assert sql('select count(*) from fixer_calendar_admission_capability_20261009')[0][0]==0
""")
 module=importlib.util.module_from_spec(importlib.util.spec_from_file_location('accepted_generated_release_seed',SEED))
 exec(compile(source,str(SEED)+' [release fixture adapters]','exec'),module.__dict__)
 return module


def pg17_bin_dir():
 """Find a complete PostgreSQL 17 server installation without accepting other majors."""
 candidates=[]
 for env_name in ('POSTGRESQL_17_BIN', 'PG17_BIN'):
  value=os.environ.get(env_name)
  if value: candidates.append(Path(value))
 pg_config=shutil.which('pg_config')
 if pg_config:
  try:
   bindir=subprocess.run([pg_config,'--bindir'],check=True,capture_output=True,text=True,
    timeout=5).stdout.strip()
   if bindir: candidates.append(Path(bindir))
  except (OSError,subprocess.SubprocessError):
   pass
 for path in ('/opt/homebrew/opt/postgresql@17/bin','/usr/local/opt/postgresql@17/bin',
  '/usr/lib/postgresql/17/bin','/usr/pgsql-17/bin'):
  candidates.append(Path(path))
 for directory in candidates:
  initdb,pg_ctl=directory/'initdb',directory/'pg_ctl'
  if not initdb.is_file() or not pg_ctl.is_file(): continue
  try:
   version=subprocess.run([str(initdb),'--version'],check=True,capture_output=True,text=True,
    timeout=5).stdout
  except (OSError,subprocess.SubprocessError):
   continue
  if '(PostgreSQL) 17.' in version or 'PostgreSQL 17.' in version:
   return directory
 return None


@pytest.mark.parametrize('ordinary',[False,True],ids=['generated','ordinary'])
def test_generated_client_full_release_manifest_pg17(ordinary,monkeypatch):
 psycopg=pytest.importorskip('psycopg')
 pg17=pg17_bin_dir()
 if pg17 is None: pytest.skip('Full manifest requires PostgreSQL 17 server binaries (initdb and pg_ctl); set PG17_BIN')
 required=[SEED,P0611_APPLY,
  P0611_VERIFY,P0625/'DRAFT_0625_generated_client_approval.sql',
  P0625/'DRAFT_0625_generated_client_approval.verify.sql']
 missing=[str(path) for path in required if not path.is_file()]
 if missing: pytest.fail('Required checked-in release fixtures are missing: '+', '.join(missing))
 for name,expected in HASHES.items():
  path=ROOT/'tests/fixtures'/name if name.startswith('portal_0611_') else FIXTURES/name
  assert hashlib.sha256(path.read_bytes()).hexdigest()==expected, f'fixture SHA-256 mismatch: {path}'
 assert hashlib.sha256(SEED.read_bytes()).hexdigest()==SEED_SHA
 assert hashlib.sha256((P0625/'DRAFT_0625_generated_client_approval.sql').read_bytes()).hexdigest()==PORTAL_SHA
 assert shutil.disk_usage('/tmp').free>5*1024**3
 if ordinary: monkeypatch.setenv('ONLY_ORDINARY','1')
 else: monkeypatch.delenv('ONLY_ORDINARY',raising=False)
 with tempfile.TemporaryDirectory(prefix='generated_release_pg_',dir='/tmp') as directory:
  temp=Path(directory);sock=temp/'sock';sock.mkdir();port=random.randint(41000,59000)
  subprocess.run([str(pg17/'initdb'),'-D',str(temp/'data'),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
  subprocess.run([str(pg17/'pg_ctl'),'-D',str(temp/'data'),'-l',str(temp/'pg.log'),
   '-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
  try:
   with psycopg.connect(host=str(sock),port=port,user='postgres',dbname='postgres',autocommit=True) as db:
    if ordinary:
     # Harmless extra catalog objects make second cluster function OIDs differ.
     for offset in range(37):
      db.execute(f'create function public.release_fixture_oid_pad_{offset}() returns integer language sql as $$select 0$$')
    install(db)
    chain_portability_and_private_acl(db,sock,port,ordinary)
    seed=accepted_seed()
    seed.run(db,sock,port)
  finally:
   subprocess.run([str(pg17/'pg_ctl'),'-D',str(temp/'data'),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)
