"""Disposable PG17 adverse cases for the additive DRAFT mutation authority.

Private socket and synthetic rows only. Actual predecessor SQL, no production
DSN, provider calls, activation, grants to service_role, or dependency installs.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')
PORTAL = ROOT.parent / 'portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.sql'
MIGRATION = ROOT / 'migrations/DRAFT_fixer_inventory_mutation_protocol_20261008.sql'
BEGIN = 'fixer_inventory_mutation_begin_20261008'
COMPLETE = 'fixer_inventory_mutation_complete_20261008'
RECEIPT = 'fixer_inventory_mutation_receipt_20261008'
SNAPSHOT = 'fixer_generated_local_census_snapshot_20261008'
GUARDED = 'fixer_generated_snapshot_guarded_20261008'
RAW = 'fixer_generated_snapshot_20261007'
AUTHORITY = 'fixer_generated_local_census_authority_20261008'
RECORD = 'fixer_still_inventory_record_20261007'
DIGEST = 'sha256:' + 'a' * 64
RESULT = 'sha256:' + 'b' * 64


@pytest.mark.parametrize('composed', [False, True])
def test_inventory_protocol_adverse_pg17(composed):
    psycopg = pytest.importorskip('psycopg')
    if not PG.is_dir() or not PORTAL.is_file():
        pytest.skip('PG17 or assembled sibling source-brand SQL unavailable')
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    with tempfile.TemporaryDirectory(prefix='inventory_mutation_pg_', dir='/tmp') as tmp:
        temp = Path(tmp)
        sock = temp / 'sock'
        sock.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run([str(PG/'initdb'), '-D', str(temp/'data'), '-U', 'postgres', '--no-sync'], check=True, capture_output=True, timeout=60)
        subprocess.run([str(PG/'pg_ctl'), '-D', str(temp/'data'), '-l', str(temp/'pg.log'), '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'], check=True, capture_output=True, timeout=60)
        connections = []
        try:
            def connect(role='postgres', autocommit=True):
                c = psycopg.connect(f'host={sock} port={port} dbname=postgres user={role}', autocommit=autocommit)
                c.execute("set statement_timeout='3s'")
                if not autocommit:
                    c.commit()
                connections.append(c)
                return c
            admin = connect()
            def rpc(conn, name, *args):
                return conn.execute('select public.' + name + '(' + ','.join(['%s']*len(args)) + ')', args).fetchone()[0]
            def apply(name):
                admin.execute((ROOT/'migrations'/name).read_text())
            def denied(fn, phrase=None):
                with pytest.raises(psycopg.Error, match=phrase):
                    fn()
            admin.execute('''create role anon;create role authenticated;create role service_role login bypassrls;
                create table content_calendar(id uuid primary key,gym_id text,logical_post_id uuid,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);
                create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);
                create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);
                create table gyms(id uuid primary key);
                create table app_users(id uuid primary key,clerk_user_id text unique,role text,email text);
                create table echo_intake_tokens(gym_id uuid primary key,echo_account_key text unique);''')
            denied(lambda: admin.execute(MIGRATION.read_text()), 'accepted local census producer')
            admin.execute('rollback')
            assert admin.execute("select to_regclass('public.fixer_inventory_mutation_20261008')").fetchone()[0] is None
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql', 'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql', 'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                         'DRAFT_fixer_owner_photo_clearance_20261007.sql', 'DRAFT_fixer_generated_owner_20261007.sql',
                         'DRAFT_fixer_generated_gap_dispatch_20261007.sql', 'DRAFT_generated_source_palette_authority_20261007.sql',
                         'DRAFT_generated_send_lease_20261007.sql'):
                apply(name)
            if composed:
                for name in ('DRAFT_fixer_forward_visual_index_20261008.sql',
                             'DRAFT_fixer_forward_media_owner_transport_20261007.sql',
                             'DRAFT_fixer_forward_schedule_reservation_20261008.sql',
                             'DRAFT_fixer_forward_schedule_stage_20261008.sql',
                             'DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql',
                             'DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql'):
                    apply(name)
            admin.execute(PORTAL.read_text())
            apply('DRAFT_fixer_generated_bundle_bridge_20261008.sql')
            apply('DRAFT_fixer_generated_local_census_producer_20261008.sql')
            if composed:
                for name in ('DRAFT_fixer_photo_historical_clearance_20261008.sql',
                             'DRAFT_fixer_prospective_photo_authority_20261008.sql',
                             'DRAFT_fixer_photo_historical_clearance_20261008.sql',
                             'DRAFT_fixer_prospective_still_v2_20261008.sql',
                             'DRAFT_fixer_still_v2_owner_transport_20261008.sql',
                             'DRAFT_fixer_current_census_reservation_lookup_20261008.sql'):
                    apply(name)
            provenance_before = admin.execute("select oid,proacl::text,proowner,proconfig,prosrc from pg_proc where proname like '%provenance%' order by oid").fetchall()
            admin.execute('create role census_owner login;grant fixer_forward_media_owner_20261006 to census_owner')
            owner = connect('census_owner')
            baseline, row, logical = [uuid.uuid4() for _ in range(3)]
            admin.execute("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) values(%s,'gym',%s,current_date+1,'instagram','feed','pending','active','vg_group','SYNTHETIC caption')", (row, logical))
            admin.execute("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC',true,'complete_fleet_still_photo_history',null,'SYNTHETIC routes','SYNTHETIC reviewer')")
            admin.execute("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,'SYNTHETIC',true,'[]','SYNTHETIC empty complete',0)", (baseline,))
            admin.execute("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'", (baseline,))
            epoch = rpc(owner, 'fixer_still_cutover_control_20261007', True, 'SYNTHETIC disposable authorized cutover')['epoch_id']
            before = admin.execute("select oid,proname,proacl::text,proowner,proconfig from pg_proc where proname=any(%s) order by proname", (['fixer_reserve_generated_bundle_20261007', 'fixer_generated_runtime_check_20261007', 'fixer_still_reservation_check_20261007', 'fixer_still_final_check_20261007'],)).fetchall()
            # Catalog drift must abort additive installation before any object persists.
            original = admin.execute("select pg_get_functiondef(oid) from pg_proc where proname='fixer_reserve_generated_bundle_20261007'").fetchone()[0]
            raw_before=admin.execute("select oid,proowner,proconfig,prosrc from pg_proc where proname=%s",(RAW,)).fetchone()
            admin.execute('create role inherited_raw_reader nologin;grant execute on function fixer_generated_snapshot_20261007(uuid) to inherited_raw_reader;grant inherited_raw_reader to fixer_forward_media_owner_20261006')
            assert admin.execute("select has_function_privilege('census_owner','fixer_generated_snapshot_20261007(uuid)','EXECUTE')").fetchone()[0]
            admin.execute(original.replace('order by ci.observed_at desc,ci.receipt_id desc limit 1', 'order by ci.receipt_id desc,ci.observed_at desc limit 1'))
            denied(lambda: admin.execute(MIGRATION.read_text()), 'exact assembled generated reservation census selector')
            admin.execute('rollback')
            assert admin.execute("select to_regclass('public.fixer_inventory_mutation_20261008')").fetchone()[0] is None
            assert admin.execute("select count(*) from pg_roles where rolname='fixer_inventory_mutator_20261008'").fetchone()[0] == 0
            admin.execute(original)
            admin.execute(MIGRATION.read_text())
            after = admin.execute("select oid,proname,proacl::text,proowner,proconfig from pg_proc where proname=any(%s) order by proname", (['fixer_reserve_generated_bundle_20261007', 'fixer_generated_runtime_check_20261007', 'fixer_still_reservation_check_20261007', 'fixer_still_final_check_20261007'],)).fetchall()
            assert before == after
            assert raw_before==admin.execute("select oid,proowner,proconfig,prosrc from pg_proc where proname=%s",(RAW,)).fetchone()
            assert not admin.execute("select has_function_privilege('census_owner','fixer_generated_snapshot_20261007(uuid)','EXECUTE')").fetchone()[0]
            assert provenance_before == admin.execute("select oid,proacl::text,proowner,proconfig,prosrc from pg_proc where proname like '%provenance%' order by oid").fetchall()
            assert not admin.execute('select enabled from fixer_inventory_protocol_control_20261008').fetchone()[0]
            denied(lambda: admin.execute('update fixer_inventory_protocol_control_20261008 set enabled=true'), 'check constraint')
            admin.execute('create role inventory_writer login;grant fixer_inventory_mutator_20261008 to inventory_writer;create role mixed_writer login;grant fixer_inventory_mutator_20261008,service_role to mixed_writer; create role dual_writer login; grant fixer_inventory_mutator_20261008,fixer_forward_media_owner_20261006 to dual_writer')
            writer, service, mixed, dual = [connect(x) for x in ('inventory_writer', 'service_role', 'mixed_writer', 'dual_writer')]
            mid = uuid.uuid4()
            for c in (service, mixed, dual):
                denied(lambda c=c: rpc(c, BEGIN, mid, 'gym', epoch, 'library_write', DIGEST), 'permission denied|isolated|mixed|forbidden')
            for role in ('anon', 'authenticated', 'service_role', 'fixer_forward_media_owner_20261006', 'fixer_inventory_mutator_20261008', 'fixer_forward_media_attester_20261006', 'generated_authority_owner_20261007', 'generated_authority_publisher_20261007', 'generated_send_reconciler_20261007'):
                assert not admin.execute("select has_table_privilege(%s,'fixer_inventory_mutation_20261008','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')", (role,)).fetchone()[0]
                assert not admin.execute("select has_function_privilege(%s,'fixer_inventory_bump_private_20261008(text)','EXECUTE')", (role,)).fetchone()[0]
            # Authenticate real temporary LOGIN sessions; SET ROLE cannot hide
            # forbidden direct/transitive memberships or the superuser session.
            admin.execute('''create role unprivileged login;
                create role mixed_attester login;grant fixer_inventory_mutator_20261008,fixer_forward_media_attester_20261006 to mixed_attester;
                create role mixed_publisher login;grant fixer_inventory_mutator_20261008,generated_authority_publisher_20261007 to mixed_publisher;
                create role mixed_sender login;grant fixer_inventory_mutator_20261008,generated_send_reconciler_20261007 to mixed_sender;
                create role intermediate_mixed noinherit;grant fixer_inventory_mutator_20261008,service_role to intermediate_mixed;
                create role transitive_writer login noinherit;grant intermediate_mixed to transitive_writer;
                create role mixed_owner login;grant fixer_forward_media_owner_20261006,service_role to mixed_owner;''')
            def immutable_authority_state():
                return tuple(admin.execute(query).fetchall() for query in (
                    'select * from fixer_inventory_generation_20261008 order by gym_id',
                    'select * from fixer_inventory_mutation_20261008 order by mutation_id',
                    'select * from fixer_inventory_mutation_completion_20261008 order by mutation_id',
                    'select * from fixer_still_cutover_20261007'))
            for login,effective in (('mixed_writer','fixer_inventory_mutator_20261008'),
                                    ('dual_writer','fixer_inventory_mutator_20261008'),
                                    ('mixed_attester','fixer_inventory_mutator_20261008'),
                                    ('mixed_publisher','fixer_inventory_mutator_20261008'),
                                    ('mixed_sender','fixer_inventory_mutator_20261008'),
                                    ('transitive_writer','fixer_inventory_mutator_20261008'),
                                    ('unprivileged',None),('postgres','fixer_inventory_mutator_20261008'),
                                    ('mixed_owner','fixer_forward_media_owner_20261006'),
                                    ('dual_writer','fixer_forward_media_owner_20261006'),
                                    ('postgres','fixer_forward_media_owner_20261006')):
                c=connect(login)
                if effective:
                    c.execute('set role '+effective)
                frozen_roles=immutable_authority_state()
                probe=uuid.uuid4()
                for name,args in ((BEGIN,(probe,'gym',epoch,'library_write',DIGEST)),
                                  (COMPLETE,(probe,'gym',epoch,DIGEST,RESULT)),
                                  (RECEIPT,(probe,'gym',epoch,DIGEST)),
                                  (SNAPSHOT,(row,)),
                                  (GUARDED,(row,)),
                                  ('fixer_still_cutover_control_20261007',(False,'SYNTHETIC denied mixed login'))):
                    denied(lambda name=name,args=args:rpc(c,name,*args),'permission denied|isolated|mixed|forbidden')
                    assert immutable_authority_state()==frozen_roles
            # A pure login may SET ROLE to its same lane and use exact RPCs.
            writer.execute('set role fixer_inventory_mutator_20261008')
            role_probe=uuid.uuid4()
            rpc(writer,BEGIN,role_probe,'role-gym',epoch,'library_write',DIGEST)
            rpc(writer,COMPLETE,role_probe,'role-gym',epoch,DIGEST,RESULT)
            assert rpc(writer,RECEIPT,role_probe,'role-gym',epoch,DIGEST)['state']=='complete'
            writer.execute('reset role')
            owner.execute('set role fixer_forward_media_owner_20261006')
            rpc(owner,SNAPSHOT,row)
            guarded=rpc(owner,GUARDED,row)
            raw=rpc(admin,RAW,row)
            assert all(guarded[k]==value for k,value in raw.items())
            assert not guarded['local_census_current']
            rpc(owner,'fixer_still_cutover_control_20261007',True,'SYNTHETIC pure owner role entry')
            owner.execute('reset role')
            for role in ('anon','authenticated','service_role','fixer_forward_media_owner_20261006',
                         'fixer_forward_media_attester_20261006','fixer_inventory_mutator_20261008',
                         'generated_authority_owner_20261007','generated_authority_publisher_20261007',
                         'generated_send_reconciler_20261007','inherited_raw_reader','census_owner','inventory_writer'):
                assert not admin.execute("select has_function_privilege(%s,'fixer_generated_snapshot_20261007(uuid)','EXECUTE')",(role,)).fetchone()[0]
            for login,effective in (('census_owner',None),('inventory_writer',None),('service_role',None),
                                    ('mixed_owner','fixer_forward_media_owner_20261006'),
                                    ('mixed_writer','fixer_inventory_mutator_20261008')):
                c=connect(login)
                if effective:c.execute('set role '+effective)
                denied(lambda c=c:rpc(c,RAW,row),'permission denied')
                if login!='census_owner':
                    denied(lambda c=c:rpc(c,GUARDED,row),'permission denied|isolated|mixed|forbidden')
            for role in ('anon','authenticated','fixer_forward_media_attester_20261006'):
                # Disposable LOGIN identities; no superuser SET ROLE proof.
                login='raw_probe_'+role
                admin.execute('create role '+login+' login;grant '+role+' to '+login)
                c=connect(login)
                denied(lambda c=c:rpc(c,RAW,row),'permission denied')
                denied(lambda c=c:rpc(c,GUARDED,row),'permission denied')
            def snapshot():
                return rpc(owner, SNAPSHOT, row)
            def authority():
                s = snapshot()
                return rpc(owner, AUTHORITY, 'gym', s['snapshot']['inventory_revision'])
            def record(s=None, available=0, complete=True, receipt=None):
                s = s or snapshot()
                rid = receipt or uuid.uuid4()
                return rpc(owner, RECORD, rid, row, s['snapshot']['inventory_revision'], complete, available,
                           'local-census:' + DIGEST, epoch, psycopg.types.json.Jsonb(s), datetime.now(timezone.utc))
            snap_a = snapshot()
            assert snap_a['inventory_generation'] == 0 and snap_a['pending_mutation_count'] == 0
            denied(lambda: record(snap_a), 'complete fresh epoch-bound census')
            # Even a newest fresh legacy zero/positive row lacks generation proof.
            admin.execute("insert into fixer_still_inventory_20261007(receipt_id,epoch_id,gym_id,inventory_revision,local_complete,local_available,evidence_ref) values(%s,%s,'gym',%s,true,0,'SYNTHETIC legacy receipt without generation')", (uuid.uuid4(),epoch,snap_a['snapshot']['inventory_revision']))
            assert authority()['receipt_id'] is None
            # Positive photo observations retain their path while zero gate is OFF.
            positive = record(available=1)
            assert str(authority()['receipt_id']) == str(positive)
            assert not rpc(owner,GUARDED,row)['local_census_current']
            # Rolled-back begin leaves no durable pending receipt or generation.
            tx = connect('inventory_writer', False)
            rolled = uuid.uuid4()
            rpc(tx, BEGIN, rolled, 'gym', epoch, 'library_write', DIGEST)
            tx.rollback()
            assert snapshot()['inventory_generation'] == 0
            denied(lambda: rpc(writer, RECEIPT, rolled, 'gym', epoch, DIGEST), 'exact immutable')
            begun = rpc(writer, BEGIN, mid, 'gym', epoch, 'library_write', DIGEST)
            assert begun['state'] == 'pending' and begun['generation'] == 1
            assert rpc(writer, BEGIN, mid, 'gym', epoch, 'library_write', DIGEST) == begun
            assert rpc(writer, RECEIPT, mid, 'gym', epoch, DIGEST) == begun  # lost begin ack
            assert authority()['receipt_id'] is None
            assert snapshot()['pending_mutation_count'] == 1
            assert not rpc(owner,GUARDED,row)['local_census_current']
            for args in ((mid, 'other', epoch, 'library_write', DIGEST), (mid, 'gym', epoch, 'other_kind', DIGEST), (mid, 'gym', epoch, 'library_write', RESULT), (uuid.uuid4(), 'Gym', epoch, 'library_write', DIGEST), (uuid.uuid4(), ' gym', epoch, 'library_write', DIGEST)):
                denied(lambda args=args: rpc(writer, BEGIN, *args))
            denied(lambda: rpc(writer, COMPLETE, mid, 'gym', uuid.uuid4(), DIGEST, RESULT), 'exact immutable')
            denied(lambda: record(available=1), 'complete fresh epoch-bound census')
            # Completion rollback remains pending and generation doesn't advance.
            rpc(tx, COMPLETE, mid, 'gym', epoch, DIGEST, RESULT)
            tx.rollback()
            assert rpc(writer, RECEIPT, mid, 'gym', epoch, DIGEST)['state'] == 'pending'
            assert snapshot()['inventory_generation'] == 1
            done = rpc(writer, COMPLETE, mid, 'gym', epoch, DIGEST, RESULT)
            assert done['state'] == 'complete' and done['result_digest'] == RESULT
            assert rpc(writer, COMPLETE, mid, 'gym', epoch, DIGEST, RESULT) == done
            assert rpc(writer, RECEIPT, mid, 'gym', epoch, DIGEST) == done  # lost complete ack
            denied(lambda: rpc(writer, COMPLETE, mid, 'gym', epoch, DIGEST, DIGEST), 'immutable mutation completion')
            assert snapshot()['inventory_generation'] == 2 and snapshot()['pending_mutation_count'] == 0
            for table in ('fixer_inventory_mutation_20261008', 'fixer_inventory_mutation_completion_20261008'):
                denied(lambda table=table: admin.execute('delete from ' + table), 'immutable')
                denied(lambda table=table: admin.execute('truncate ' + table + ' cascade'), 'immutable')
            denied(lambda: admin.execute("update fixer_inventory_mutation_20261008 set kind='changed'"), 'immutable')
            denied(lambda: admin.execute("update fixer_inventory_generation_20261008 set generation=1"), 'strictly advance')
            denied(lambda: admin.execute('truncate fixer_inventory_generation_20261008'), 'cannot be removed')
            # SYNTHETIC all-writer release evidence only; no real release represented.
            admin.execute("update fixer_inventory_protocol_control_20261008 set enabled=true,all_writers_verified_ref='SYNTHETIC isolated all-writer fixture coverage'")
            zero = record()
            assert str(authority()['receipt_id']) == str(zero)
            guarded=rpc(owner,GUARDED,row)
            assert guarded['local_census_current'] and str(guarded['local_census_receipt_id'])==str(zero)
            admin.execute('update fixer_inventory_protocol_control_20261008 set enabled=false')
            assert authority()['receipt_id'] is None
            admin.execute('update fixer_inventory_protocol_control_20261008 set enabled=true')
            assert authority()['receipt_id'] is None  # never revive pre-disable zero
            zero = record()
            # Actual still reserve/final consumers accept this exact current
            # census, then reject it after begin even at unchanged DB revision.
            original_tuple = dict(gym_id='gym',source_asset_id='SYNTHETIC graphic',source_url='https://owned.example/graphic.png',
                                  sha256='sha256:'+'c'*64,md5='md5:'+'d'*32,phash='scene:phash64:'+'0'*16,length=1024)
            clear, reserved = uuid.uuid4(), uuid.uuid4()
            obj = psycopg.types.json.Jsonb(original_tuple)
            rpc(owner,'fixer_still_known_record_20261007',clear,'cleared_fresh',obj,'authenticated-post-epoch:SYNTHETIC original')
            rpc(owner,'fixer_still_reserve_20261007',reserved,row,obj,'graphic',zero,clear)
            rpc(owner,'fixer_still_final_check_20261007',row,reserved)
            # Historical-census-only gyms must invalidate too. RPC and direct
            # disable/re-enable retain epoch/receipt identity without old revival.
            historical=uuid.uuid4()
            admin.execute("insert into fixer_still_inventory_20261007(receipt_id,epoch_id,gym_id,inventory_revision,local_complete,local_available,evidence_ref,inventory_generation) values(%s,%s,'historical-only',%s,true,0,'SYNTHETIC historical-only census',0)",(historical,epoch,DIGEST))
            for direct in (False,True):
                previous=snapshot()['inventory_generation']
                if direct:
                    admin.execute('update fixer_still_cutover_20261007 set enabled=false')
                    admin.execute('update fixer_still_cutover_20261007 set enabled=true')
                else:
                    rpc(owner,'fixer_still_cutover_control_20261007',False,'SYNTHETIC disabled boundary')
                    rpc(owner,'fixer_still_cutover_control_20261007',True,'SYNTHETIC re-enabled boundary')
                assert snapshot()['epoch_id']==epoch
                assert snapshot()['inventory_generation']==previous+2
                assert authority()['receipt_id'] is None
                assert admin.execute("select generation from fixer_inventory_generation_20261008 where gym_id='historical-only'").fetchone()[0]>0
                denied(lambda:rpc(owner,'fixer_still_final_check_20261007',row,reserved),'inventory authority')
                denied(lambda:rpc(owner,'fixer_still_reserve_20261007',uuid.uuid4(),row,obj,'graphic',zero,clear),'inventory authority')
                assert str(admin.execute('select inventory_receipt from fixer_still_reservation_20261007 where receipt_id=%s',(reserved,)).fetchone()[0])==str(zero)
                zero=record()
                reserved=uuid.uuid4()
                rpc(owner,'fixer_still_reserve_20261007',reserved,row,obj,'graphic',zero,clear)
                rpc(owner,'fixer_still_final_check_20261007',row,reserved)
            # Direct identity change also invalidates, with rollback preserving
            # the entire generation/cutover/receipt state in the same transaction.
            saved=immutable_authority_state()
            admin.execute('begin')
            admin.execute("update fixer_still_cutover_20261007 set activation_ref='SYNTHETIC rolled ruling',cutover_at=clock_timestamp()")
            assert immutable_authority_state()!=saved
            admin.execute('rollback')
            assert immutable_authority_state()==saved
            for op in ('delete from','truncate'):
                denied(lambda op=op:admin.execute(op+' fixer_still_cutover_20261007'),'cannot be removed')
            # Trusted migration-owner synthetic retained generated tuple.
            # Exercise the ACTUAL private final-runtime validator without
            # claiming provider, bundle, approval, or publication proof.
            generated_row,generated_logical,job=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
            admin.execute("insert into content_calendar(id,gym_id,logical_post_id,post_date,account,format,status,variant_status,visual_group_key,caption) values(%s,'generated-gym',%s,current_date+1,'instagram','feed','pending','active','vg_generated','SYNTHETIC generated caption')",(generated_row,generated_logical))
            generated_snapshot=rpc(owner,SNAPSHOT,generated_row)['snapshot']
            candidate=dict(job_id=str(job),gym_id='generated-gym',local_date=generated_snapshot['local_date'],
                           logical_post_id=generated_snapshot['logical_post_id'],copy_revision=generated_snapshot['copy_revision'],
                           inventory_revision=generated_snapshot['inventory_revision'],original_url='https://owned.example/generated.png',
                           original_sha256='e'*64,original_md5='f'*32,original_phash='scene:phash64:'+'f'*16)
            manifest=dict(manifest_digest='SYNTHETIC generated runtime manifest')
            admin.execute("insert into fixer_generated_reservation_20261007(job_id,calendar_row_id,group_key,candidate_json,manifest_json,receipt_ref,history_epoch,approved_source_revision) values(%s,%s,'vg_generated',%s,%s,'SYNTHETIC trusted retained generated runtime',%s,%s)",
                          (job,generated_row,psycopg.types.json.Jsonb(candidate),psycopg.types.json.Jsonb(manifest),
                           psycopg.types.json.Jsonb(generated_snapshot['history']['epoch']),'client-source:'+DIGEST))
            admin.execute("update content_calendar set source_media_asset_id=%s,source_media_url=%s,image_url=%s,thumbnail_url=null,render_manifest_digest=%s where id=%s",
                          ('generated-astra:'+str(job),candidate['original_url'],candidate['original_url'],manifest['manifest_digest'],generated_row))
            def generated_census():
                current=rpc(owner,SNAPSHOT,generated_row)
                return rpc(owner,RECORD,uuid.uuid4(),generated_row,current['snapshot']['inventory_revision'],True,0,
                           'local-census:'+DIGEST,epoch,psycopg.types.json.Jsonb(current),datetime.now(timezone.utc))
            # Fixture-only private-validator adapter. The real authenticated
            # owner session must pass the new role gate; no superuser runtime
            # session or production EXECUTE privilege is introduced.
            admin.execute("""create function inventory_generated_runtime_fixture(p_row uuid) returns boolean
                language plpgsql security definer set search_path=pg_catalog,public as $$
                begin
                 perform public.fixer_inventory_runtime_caller_private_20261008('owner');
                 return public.fixer_generated_runtime_check_20261007(p_row);
                end $$;
                revoke all on function inventory_generated_runtime_fixture(uuid) from public;
                grant execute on function inventory_generated_runtime_fixture(uuid) to fixer_forward_media_owner_20261006;""")
            admin.execute("""create function inventory_publisher_runtime_fixture(p_row uuid) returns boolean
                language plpgsql security definer set search_path=pg_catalog,public as $$
                begin
                 if not pg_has_role(session_user,'service_role','member') then raise exception 'fixture publisher required'; end if;
                 return public.fixer_generated_runtime_check_20261007(p_row);
                end $$;
                revoke all on function inventory_publisher_runtime_fixture(uuid) from public;
                grant execute on function inventory_publisher_runtime_fixture(uuid) to service_role;""")
            generated_census()
            assert rpc(owner,'inventory_generated_runtime_fixture',generated_row)
            assert rpc(service,'inventory_publisher_runtime_fixture',generated_row)
            assert rpc(service,'fixer_generated_publish_readback_20261007',generated_row) is None  # retained non-v2 tuple is not publishable
            for direct in (False,True):
                if direct:
                    admin.execute('update fixer_still_cutover_20261007 set enabled=false')
                    admin.execute('update fixer_still_cutover_20261007 set enabled=true')
                else:
                    rpc(owner,'fixer_still_cutover_control_20261007',False,'SYNTHETIC generated disabled')
                    rpc(owner,'fixer_still_cutover_control_20261007',True,'SYNTHETIC generated re-enabled')
                denied(lambda:rpc(owner,'inventory_generated_runtime_fixture',generated_row),'fresh local depletion authority')
                denied(lambda:rpc(service,'inventory_publisher_runtime_fixture',generated_row),'fresh local depletion authority')
                generated_census()
                assert rpc(owner,'inventory_generated_runtime_fixture',generated_row)
            # Both cutover cycles invalidated gym's original still binding too.
            zero=record();reserved=uuid.uuid4()
            rpc(owner,'fixer_still_reserve_20261007',reserved,row,obj,'graphic',zero,clear)
            rpc(owner,'fixer_still_final_check_20261007',row,reserved)
            late = uuid.uuid4()
            rpc(writer,BEGIN,late,'gym',epoch,'rotation_write',DIGEST)
            denied(lambda: rpc(owner,'fixer_still_reserve_20261007',uuid.uuid4(),row,obj,'graphic',zero,clear),'inventory authority')
            denied(lambda: rpc(owner,'fixer_still_final_check_20261007',row,reserved),'inventory authority')
            rpc(writer,COMPLETE,late,'gym',epoch,DIGEST,RESULT)
            assert authority()['receipt_id'] is None
            # Restaging/reobservation is required after completion; generation
            # does not restore the immutable old reservation's census receipt.
            zero = record()
            denied(lambda: rpc(owner,'fixer_still_final_check_20261007',row,reserved),'inventory authority')
            incomplete=record(complete=False)
            assert not rpc(owner,GUARDED,row)['local_census_current']
            zero=record()
            assert rpc(owner,GUARDED,row)['local_census_current']
            frozen = snapshot()
            # Shared direct source DB mutation invalidates census, including A->B->A.
            admin.execute("insert into media_source values('src','gym','gym_drive','folder',true,'ready',clock_timestamp())")
            assert snapshot()['inventory_generation'] == frozen['inventory_generation']+1 and authority()['receipt_id'] is None
            assert not rpc(owner,GUARDED,row)['local_census_current']
            admin.execute("delete from media_source where id='src'")
            restored = snapshot()
            assert restored['snapshot']['inventory_revision'] == frozen['snapshot']['inventory_revision']
            assert restored['inventory_generation'] == frozen['inventory_generation']+2 and authority()['receipt_id'] is None
            denied(lambda: record(frozen), 'complete fresh epoch-bound census')
            # Generic source/asset inserts, tenant-changing UPDATE and DELETE
            # invalidate BOTH affected gyms, including linked inconsistent tenant.
            admin.execute("insert into media_source values('src','gym','gym_drive','folder',true,'ready',clock_timestamp())")
            admin.execute("insert into media_asset(id,source_id,gym_id,kind,eligible) values('asset','src','linked','image',false)")
            generations = dict(admin.execute('select gym_id,generation from fixer_inventory_generation_20261008').fetchall())
            admin.execute("update media_source set gym_id='other' where id='src'")
            changed = dict(admin.execute('select gym_id,generation from fixer_inventory_generation_20261008').fetchall())
            assert changed['gym'] > generations['gym'] and changed['linked'] > generations['linked'] and changed['other'] > 0
            admin.execute("update media_asset set gym_id='newgym' where id='asset'")
            changed2 = dict(admin.execute('select gym_id,generation from fixer_inventory_generation_20261008').fetchall())
            assert changed2['linked'] > changed['linked'] and changed2['other'] > changed['other'] and changed2['newgym'] > 0
            admin.execute("delete from media_asset where id='asset';delete from media_source where id='src'")
            # DB writer rollback preserves generation and inventory revision.
            before_rollback = snapshot()
            admin.execute('begin')
            admin.execute("insert into media_source values('rolled','gym','gym_drive','folder',true,'ready',clock_timestamp())")
            admin.execute('rollback')
            assert snapshot() == before_rollback
            # Consumer holds locks before observation; contenders cannot write.
            observer = connect('census_owner', False)
            rpc(observer, SNAPSHOT, row)
            competitor = connect()
            denied(lambda: competitor.execute("insert into media_source values('raced','gym','gym_drive','folder',true,'ready',clock_timestamp())"), 'busy')
            denied(lambda: competitor.execute('update fixer_inventory_protocol_control_20261008 set enabled=false'), 'busy')
            denied(lambda: competitor.execute("update fixer_inventory_generation_20261008 set generation=generation+1 where gym_id='gym'"), 'busy')
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: rpc(writer, BEGIN, uuid.uuid4(), 'gym', epoch, 'library_write', DIGEST))
                denied(lambda: future.result(timeout=5), 'statement timeout')
            denied(lambda: competitor.execute('update fixer_still_cutover_20261007 set enabled=false'), 'busy')
            contender=connect('census_owner')
            denied(lambda:rpc(contender,'fixer_still_cutover_control_20261007',False,'SYNTHETIC contended control'),'busy')
            observer.rollback()
            assert snapshot() == before_rollback
            # Existing inherited caller owns shared graph authority. Another
            # shared holder makes an exclusive upgrade unavailable; control
            # must raise immediately rather than wait/deadlock.
            shared_a,shared_b=connect('census_owner',False),connect(autocommit=False)
            for c in (shared_a,shared_b):
                c.execute("select pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))")
            denied(lambda:rpc(shared_a,'fixer_still_cutover_control_20261007',False,'SYNTHETIC inherited shared caller'),'busy')
            shared_a.rollback();shared_b.rollback()
            assert snapshot() == before_rollback
            # Old pending receipts cannot time out clean, even across cutovers.
            abandoned = uuid.uuid4()
            g = snapshot()['inventory_generation'] + 1
            admin.execute("update fixer_inventory_generation_20261008 set generation=%s where gym_id='gym'", (g,))
            admin.execute("insert into fixer_inventory_mutation_20261008(mutation_id,gym_id,epoch_id,generation,kind,request_digest,begun_at) values(%s,'gym',%s,%s,'library_write',%s,clock_timestamp()-interval '30 days')", (abandoned, epoch, g, DIGEST))
            assert rpc(writer, RECEIPT, abandoned, 'gym', epoch, DIGEST)['state'] == 'pending'
            assert authority()['pending_mutation_count'] == 1 and authority()['receipt_id'] is None
            rpc(owner, 'fixer_still_cutover_control_20261007', False, 'SYNTHETIC retired epoch')
            # Existing enable/disable preserves epoch; this administrator-only
            # synthetic retirement models an independently reconciled new epoch.
            new_epoch = str(uuid.uuid4())
            admin.execute("update fixer_still_cutover_20261007 set epoch_id=%s where singleton", (new_epoch,))
            rpc(owner, 'fixer_still_cutover_control_20261007', True, 'SYNTHETIC replacement epoch')
            assert new_epoch != epoch
            denied(lambda: rpc(writer, COMPLETE, abandoned, 'gym', epoch, DIGEST, RESULT), 'current durable')
            assert snapshot()['pending_mutation_count'] == 1
            # No old-epoch receipt or obsolete null generation grants authority.
            assert authority()['receipt_id'] is None
            assert all('fixer_local_census_latest_private_20261008' in admin.execute('select prosrc from pg_proc where proname=%s', (name,)).fetchone()[0]
                       for name in ('fixer_reserve_generated_bundle_20261007', 'fixer_generated_runtime_check_20261007', 'fixer_still_reservation_check_20261007', 'fixer_still_final_check_20261007'))
            print('PASS '+('composed' if composed else 'producer')+' PG17 mutation protocol: default OFF zero hold, isolated ACLs, immutable receipts, rollback/crash/lost-ack fencing, ABA generation invalidation, old/new/linked tenant triggers, contention holds, cutover RPC/direct ABA and rollback invalidation, authenticated/effective lane matrix, raw ACL closed and guarded owner snapshot exact fields, retained publisher validators, retired pending never expires; synthetic only')
        finally:
            for conn in connections:
                conn.close()
            subprocess.run([str(PG/'pg_ctl'), '-D', str(temp/'data'), '-m', 'immediate', '-w', 'stop'], check=True, capture_output=True, timeout=60)
