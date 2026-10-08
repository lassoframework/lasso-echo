"""Standalone PG17 proof using the actual P3 candidate and frozen B RPC bodies.

Private socket, synthetic rows, no production DSN/network/install. Run with the
existing psycopg runtime. Only the schema/data are fixtures; functions under test
come from reviewed SQL files, including the owner-photo and generated guards.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from pathlib import Path
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PG = Path('/opt/homebrew/opt/postgresql@17/bin')
GRAPH = 'fixer_forward_graph_20261006'
CENSUS = 'fixer_forward_photo_census_20261007'
TRIGGER = '000_fixer_forward_corpus_entry_20261008'
TABLES = ['content_calendar', 'media_asset', 'media_source',
          'fixer_forward_media_claim_receipt_20261006',
          'fixer_forward_media_photo_state_20261007',
          'fixer_forward_media_photo_key_revocation_20261007',
          'fixer_owner_photo_revocation_20261007']


def function(text, name, delimiter='function'):
    marker = '$function$' if delimiter == 'function' else '$$'
    match = re.search(r'create(?: or replace)? function public\.' + name +
                      r'\(.*?' + re.escape(marker) + r'.*?' + re.escape(marker) + r'\s*;',
                      text, re.I | re.S)
    assert match, name
    return match[0]


def lasso_sql():
    path = ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_lasso_20261008.sql'
    if path.exists():
        return path.read_text()
    # P3 worker started at 61773b3b before P2b landed. Frozen shared-repo object
    # is the only fallback; no branch movement or network lookup is performed.
    return subprocess.check_output(['git', 'show', 'f68d2375:migrations/DRAFT_fixer_forward_lock_entry_lasso_20261008.sql'],
                                   cwd=ROOT, text=True)


# Exact #345 guard, captured from its reviewed draft. Keep its whitespace so
# md5(prosrc) remains the catalog hash pinned by P3. Tested before/after cutover.
GENERATED = """create function public.fixer_generated_inventory_lock_20261007()
returns trigger language plpgsql set search_path=pg_catalog,public as $$
begin
 if current_setting('transaction_isolation')<>'read committed' then
  raise exception 'generated inventory requires read committed' using errcode='25000'; end if;
 if not pg_try_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))
  or not pg_try_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0)) then
  raise exception 'generated inventory authority busy; retry database mutation only' using errcode='40001'; end if;
 return null;
end; $$;
create trigger generated_inventory_lock before insert or update or delete or truncate on public.media_asset
 for each statement execute function public.fixer_generated_inventory_lock_20261007();
create trigger generated_inventory_lock before insert or update or delete or truncate on public.media_source
 for each statement execute function public.fixer_generated_inventory_lock_20261007();
"""


def test_cutover_hashes_pin_the_actual_entry_drafts():
    candidate = (ROOT / 'migrations/DRAFT_fixer_forward_corpus_atomic_cutover_20261008.sql').read_text()
    sources = [
        ('portal_action_receipt_apply', (ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_media_20261008.sql').read_text(), 'function'),
        ('stage_lasso_campaign_row', lasso_sql(), 'function'),
        ('fixer_forward_calendar_entry_lock_20261008', (ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql').read_text(), 'dollar'),
        ('fixer_owner_photo_corpus_write_lock_20261007', (ROOT / 'migrations/DRAFT_fixer_owner_photo_clearance_20261007.sql').read_text(), 'dollar'),
        ('fixer_generated_inventory_lock_20261007', GENERATED, 'dollar'),
    ]
    for name, source, delimiter in sources:
        marker = '$function$' if delimiter == 'function' else '$$'
        body = function(source, name, delimiter).split(marker)[1]
        assert hashlib.md5(body.encode()).hexdigest() in candidate, name
        if name in ('portal_action_receipt_apply', 'stage_lasso_campaign_row'):
            after = re.sub(r'(?im)^  lock table public\.content_calendar in share row exclusive mode;\n', '', body)
            assert after != body
            assert hashlib.md5(after.encode()).hexdigest() in candidate, name


def main():
    import psycopg
    from psycopg.types.json import Jsonb
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    temp = Path(tempfile.mkdtemp(prefix='forward_corpus_actual_pg_', dir='/tmp'))
    data, sock = temp / 'data', temp / 'sock'
    sock.mkdir()
    port = random.randint(41000, 59000)
    start_attempted = False
    try:
        subprocess.run([str(PG / 'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        start_attempted = True
        subprocess.run([str(PG / 'pg_ctl'), '-D', str(data), '-l', str(temp / 'pg.log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        dsn = f'host={sock} port={port} user=postgres dbname=postgres'
        with psycopg.connect(dsn, autocommit=True) as admin:
            def sql(q, args=None):
                cur = admin.execute(q, args)
                return cur.fetchall() if cur.description else None

            @contextmanager
            def lane(name):
                with psycopg.connect(dsn, application_name=name) as conn:
                    conn.execute("set lock_timeout='4s'; set statement_timeout='8s'; set deadlock_timeout='100ms'")
                    yield conn

            def run(conn, q, args=None):
                try:
                    result = conn.execute(q, args).fetchone()
                    conn.commit()
                    return None, result
                except psycopg.Error as exc:
                    conn.rollback()
                    return exc.sqlstate, str(exc)

            def locks(name, key):
                return sql("select l.mode,l.granted from pg_locks l join pg_stat_activity a on a.pid=l.pid "
                           "where a.application_name=%s and l.locktype='advisory' "
                           "and l.classid::bigint=((hashtextextended(%s,0)>>32)&4294967295) "
                           "and l.objid::bigint=(hashtextextended(%s,0)&4294967295)", (name, key, key))

            def wait_c(name):
                end = time.monotonic() + 3
                while ('ExclusiveLock', False) not in locks(name, CENSUS):
                    assert time.monotonic() < end, name + ' did not wait on C'
                    time.sleep(.01)

            def no_sre(name):
                assert not sql("select 1 from pg_locks l join pg_stat_activity a on a.pid=l.pid "
                               "where a.application_name=%s and l.relation='content_calendar'::regclass "
                               "and l.mode='ShareRowExclusiveLock'", (name,))

            assert int(sql('show server_version_num')[0][0]) // 10000 == 17
            sql('create role anon; create role authenticated; create role service_role;'
                'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,'
                'format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,'
                'publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,'
                'thumbnail_url text,media_not_ready_reason text,caption text,logical_post_id uuid,'
                'pillar text,slot_index integer,scheduled_at timestamptz);'
                'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
                'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,'
                'rendition_url text,eligible boolean,excluded_by_coach boolean);'
                'create table echo_infographic_artifacts(tenant text,image_url text,image_sha256 text,evidence jsonb,source_identity jsonb);')
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql',
                         'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                         'DRAFT_fixer_owner_photo_clearance_20261007.sql'):
                sql((ROOT / 'migrations' / name).read_text())
            receipt_sql = (ROOT / 'migrations/portal_action_receipt_draft_20261004.sql').read_text()
            sql(receipt_sql[receipt_sql.index('CREATE TABLE IF NOT EXISTS public.portal_action_receipt ('):
                            receipt_sql.index('-- Trusted public media origin:')])
            helper = function((ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_calendar_20261008.sql').read_text(),
                              'fixer_forward_calendar_entry_lock_20261008', 'dollar')
            receipt = function((ROOT / 'migrations/DRAFT_fixer_forward_lock_entry_media_20261008.sql').read_text(),
                               'portal_action_receipt_apply')
            stage = function(lasso_sql(), 'stage_lasso_campaign_row')
            sql(helper + receipt + stage)
            sql('revoke all on function fixer_forward_calendar_entry_lock_20261008() from public,anon,authenticated,service_role;'
                'revoke all on function portal_action_receipt_apply(text,text,text,jsonb),'
                'stage_lasso_campaign_row(jsonb,text,text,text,text) from public,anon,authenticated;'
                'grant execute on function portal_action_receipt_apply(text,text,text,jsonb),'
                'stage_lasso_campaign_row(jsonb,text,text,text,text) to service_role;'
                'grant select,insert,update,delete,truncate on content_calendar to anon,service_role;'
                'grant select,insert,update,delete,truncate on media_asset,media_source to service_role;')
            sql(GENERATED)
            candidate = (ROOT / 'migrations/DRAFT_fixer_forward_corpus_atomic_cutover_20261008.sql').read_text()
            metadata = sql("select oid,proname,proowner,proacl,prosecdef,provolatile,proconfig,"
                           "pg_get_function_identity_arguments(oid) from pg_proc where proname in "
                           "('portal_action_receipt_apply','stage_lasso_campaign_row') order by proname")
            before = {name: sql('select prosrc from pg_proc where proname=%s', (name,))[0][0]
                      for name in ('portal_action_receipt_apply', 'stage_lasso_campaign_row')}
            # Overload drift rejects ALL sections; no early statement guard and
            # no lock removal/privilege change may escape the failed transaction.
            sql('create function stage_lasso_campaign_row(integer) returns integer language sql as $$select $1$$;')
            try:
                sql(candidate)
                raise AssertionError('overload drift accepted')
            except psycopg.Error as exc:
                assert exc.sqlstate == '23514'
                sql('rollback')
            assert sql('select count(*) from pg_trigger where tgname=%s', (TRIGGER,)) == [(0,)]
            assert sql("select has_table_privilege('anon','content_calendar','truncate')") == [(True,)]
            assert sql("select prosrc from pg_proc where proname='portal_action_receipt_apply'")[0][0] == before['portal_action_receipt_apply']
            sql('drop function stage_lasso_campaign_row(integer)')
            # A remaining future visual RPC with SRE also rejects the cutover.
            sql('create function future_visual_activation() returns void language plpgsql as $$begin lock table public.content_calendar in share row exclusive mode; end$$;')
            try:
                sql(candidate)
                raise AssertionError('additional SRE accepted')
            except psycopg.Error as exc:
                assert exc.sqlstate == '23514' and 'additional calendar SRE' in str(exc)
                sql('rollback')
            sql('drop function future_visual_activation()')
            # Force failure in the LAST privilege guard, after trigger creation
            # and both rewrites. Inherited privilege is not removed by a direct
            # REVOKE: the candidate must reject it and roll every mutation back.
            sql('create role inherited_truncate; grant truncate on content_calendar to inherited_truncate; grant inherited_truncate to service_role')
            try:
                sql(candidate)
                raise AssertionError('inherited truncate accepted')
            except psycopg.Error as exc:
                assert exc.sqlstate == '23514' and 'inherited TRUNCATE' in str(exc)
                sql('rollback')
            assert sql('select count(*) from pg_trigger where tgname=%s', (TRIGGER,)) == [(0,)]
            for name, body in before.items():
                assert sql('select prosrc from pg_proc where proname=%s', (name,))[0][0] == body
            assert sql("select has_table_privilege('anon','content_calendar','truncate')") == [(True,)]
            sql('revoke inherited_truncate from service_role; drop owned by inherited_truncate; drop role inherited_truncate')
            # Exercise success with the optional generated function present in
            # a rollback-only rehearsal, then genuinely absent at B cutover.
            sql(candidate.rsplit('commit;', 1)[0] + 'rollback;')
            sql('drop trigger generated_inventory_lock on media_asset; drop trigger generated_inventory_lock on media_source; drop function fixer_generated_inventory_lock_20261007()')
            sql(candidate)
            sql(GENERATED)
            after_metadata = sql("select oid,proname,proowner,proacl,prosecdef,provolatile,proconfig,"
                                 "pg_get_function_identity_arguments(oid) from pg_proc where proname in "
                                 "('portal_action_receipt_apply','stage_lasso_campaign_row') order by proname")
            assert metadata == after_metadata
            for name, body in before.items():
                after = sql('select prosrc from pg_proc where proname=%s', (name,))[0][0]
                assert after == re.sub(r'(?im)^  lock table public\.content_calendar in share row exclusive mode;\n', '', body)
            assert sql('select count(*) from pg_trigger where tgname=%s', (TRIGGER,)) == [(7,)]
            for role in ('anon', 'authenticated', 'service_role'):
                assert sql('select has_function_privilege(%s,\'fixer_forward_corpus_entry_20261008()\',\'execute\')', (role,)) == [(False,)]
                for table in TABLES:
                    assert sql('select has_table_privilege(%s,%s,\'truncate\')', (role, table)) == [(False,)]
                    with lane('truncate_' + role) as conn:
                        conn.execute('set role ' + role)
                        assert run(conn, 'truncate ' + table + ' cascade')[0] == '42501'
            print('PASS: actual atomic candidate drift rollback, exact two-line body delta, metadata/ACL and TRUNCATE', flush=True)

            def stage_args(row=None):
                row = row or {'id': str(uuid.uuid4()), 'gym_id': 'lasso', 'account': 'instagram',
                              'post_date': '2026-10-10', 'pillar': 'teaching', 'format': 'feed',
                              'caption': str(uuid.uuid4()), 'image_url': 'https://media.test/' + str(uuid.uuid4()),
                              'status': 'pending', 'slot_index': 0, 'variant_status': 'active'}
                sha = 'a' * 64
                sql('insert into echo_infographic_artifacts values(%s,%s,%s,%s,%s)',
                    ('lasso', row['image_url'], sha, Jsonb({'grade_status': 'PASS', 'image_sha256': sha, 'policy_version': 'synthetic'}),
                     Jsonb({'source_hash': sha})))
                return row, (Jsonb(row), 'lasso', sha, 'synthetic', sha)

            stage_call = 'select stage_lasso_campaign_row(%s,%s,%s,%s,%s)'
            sql("insert into content_calendar(id,gym_id,caption) values(gen_random_uuid(),'unrelated','synthetic')")
            # RPC-first actual candidate, direct DML is waiting with relation
            # RowExclusive already held. Candidate completes without SRE.
            _, args = stage_args()
            with lane('rpc_first_stage') as rpc, lane('direct_after_stage') as direct, ThreadPoolExecutor(max_workers=1) as pool:
                rpc.execute('select fixer_forward_calendar_entry_lock_20261008()')
                future = pool.submit(run, direct, "update content_calendar set caption='changed' where gym_id='unrelated' returning id")
                wait_c('direct_after_stage')
                assert locks('direct_after_stage', GRAPH) == [('ShareLock', True)]
                assert rpc.execute(stage_call, args).fetchone()[0]['result'] == 'inserted'
                no_sre('rpc_first_stage')
                rpc.commit()
                assert future.result(timeout=5)[0] is None
            sql("delete from content_calendar where gym_id='lasso'")
            # DML-first new occupant. Both real RPC outer SELECTs start before
            # commit, then their fresh internal reads see the same occupied slot.
            row, args = stage_args()
            with lane('direct_first_stage') as direct, lane('waiting_stage_a') as a, lane('waiting_stage_b') as b, ThreadPoolExecutor(max_workers=2) as pool:
                direct.execute("insert into content_calendar(id,gym_id,account,post_date,format,status,variant_status,slot_index,caption,image_url) values(gen_random_uuid(),'lasso','instagram','2026-10-10','feed','approved','active',0,'existing','https://media.test/existing')")
                futures = [pool.submit(run, conn, stage_call, args) for conn in (a, b)]
                for name in ('waiting_stage_a', 'waiting_stage_b'):
                    wait_c(name)
                direct.commit()
                results = [f.result(timeout=5) for f in futures]
                assert all(error is None and result[0]['reason'] == 'occupied_logical_slot' for error, result in results), results
            assert sql("select count(*) from content_calendar where gym_id='lasso'") == [(1,)]
            sql("delete from content_calendar where gym_id='lasso'")
            # No slot uniqueness hides a duplicate decision: concurrent stage
            # calls choose one insert and one idempotent replay under C.
            row, args = stage_args()
            with lane('stage_a') as a, lane('stage_b') as b, ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = [f.result(timeout=5) for f in [pool.submit(run, c, stage_call, args) for c in (a, b)]]
                assert sorted(v[1][0]['result'] for v in outcomes) == ['idempotent', 'inserted'], outcomes
            assert sql("select count(*) from content_calendar where id=%s", (row['id'],)) == [(1,)]
            print('PASS: actual stage RPC-first and DML-first, fresh occupied-slot reads, one insert/idempotent replay', flush=True)

            def selected(action):
                rid, logical = uuid.uuid4(), uuid.uuid4()
                sql("insert into content_calendar(id,gym_id,logical_post_id,account,format,post_date,status,variant_status,caption,image_url) values(%s,'gym',%s,'instagram','feed','2026-10-11','pending','active','synthetic','https://media.test/before')", (rid, logical))
                member = sql('select to_jsonb(c) from content_calendar c where id=%s', (rid,))[0][0]
                member['calendar_row_id'] = str(rid)
                media = {'image_url': 'https://media.test/after', 'source_media_url': None,
                         'thumbnail_url': None, 'source_media_asset_id': None}
                sql('insert into portal_action_receipt(gym_id,action_id,action,row_id,request_fingerprint,status,selected_asset,planned_siblings,member_manifest) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    ('gym', action, 'swap', rid, 'b' * 64, 'selected', Jsonb({'image_url': media['image_url']}), Jsonb({}),
                     Jsonb({'logical_post_id': str(logical), 'members': [member]})))
                return rid, logical, ('gym', action, 'b' * 64, Jsonb({'rows': [{'calendar_row_id': str(rid), 'media': media}]}))

            receipt_call = 'select (portal_action_receipt_apply(%s,%s,%s,%s)).status'
            rid, _, args = selected('rpc-first')
            with lane('rpc_first_receipt') as rpc, lane('direct_after_receipt') as direct, ThreadPoolExecutor(max_workers=1) as pool:
                rpc.execute('select fixer_forward_calendar_entry_lock_20261008()')
                future = pool.submit(run, direct, 'update content_calendar set caption=caption where id=%s returning id', (rid,))
                wait_c('direct_after_receipt')
                assert rpc.execute(receipt_call, args).fetchone()[0] == 'succeeded'
                no_sre('rpc_first_receipt')
                rpc.commit()
                assert future.result(timeout=5)[0] is None
            for change in ('caption', 'sibling'):
                rid, logical, args = selected('dml-first-' + change)
                with lane('direct_first_receipt') as direct, lane('waiting_receipt') as rpc, ThreadPoolExecutor(max_workers=1) as pool:
                    if change == 'caption':
                        direct.execute("update content_calendar set caption='changed by owner' where id=%s", (rid,))
                    else:
                        direct.execute("insert into content_calendar(id,gym_id,logical_post_id,variant_status) values(gen_random_uuid(),'gym',%s,'active')", (logical,))
                    future = pool.submit(run, rpc, receipt_call, args)
                    wait_c('waiting_receipt')
                    direct.commit()
                    assert future.result(timeout=5)[0] == '23514'
                assert sql('select status from portal_action_receipt where action_id=%s', ('dml-first-' + change,)) == [('selected',)]
                assert sql('select image_url from content_calendar where id=%s', (rid,)) == [('https://media.test/before',)]
            print('PASS: actual receipt RPC-first completes; DML-first stale caption/sibling abort entire swap and preserve selected receipt', flush=True)

            # Existing try guards run after our blocking guard, including when
            # generated draft is installed later. A waiting direct inventory
            # write must WAIT on C, not raise its old 40001 busy outcome.
            for installed_when in ('before', 'after'):
                if installed_when == 'after':
                    sql('drop trigger generated_inventory_lock on media_asset; drop trigger generated_inventory_lock on media_source; drop function fixer_generated_inventory_lock_20261007()')
                    sql(GENERATED)
                for table in ('media_asset', 'media_source'):
                    with lane('inventory_holder') as holder, lane('inventory_waiter') as writer, ThreadPoolExecutor(max_workers=1) as pool:
                        holder.execute('select fixer_forward_calendar_entry_lock_20261008()')
                        future = pool.submit(run, writer, f"insert into {table}(id,gym_id) values(%s,'gym') returning id", (str(uuid.uuid4()),))
                        wait_c('inventory_waiter')
                        assert locks('inventory_waiter', GRAPH) == [('ShareLock', True)]
                        holder.commit()
                        assert future.result(timeout=5)[0] is None
            # Real authority holders may outlast five seconds. With the caller
            # deliberately permitting that wait, the candidate trigger must not
            # replace its deadline with a function-local five-second timeout.
            config = sql("select proconfig from pg_proc where proname='fixer_forward_corpus_entry_20261008'")[0][0]
            assert not any(item.startswith('lock_timeout=') for item in config)
            for key in (GRAPH, CENSUS):
                name = 'long_' + ('graph' if key == GRAPH else 'census')
                asset_id = str(uuid.uuid4())
                with lane(name + '_holder') as holder, lane(name + '_writer') as writer, ThreadPoolExecutor(max_workers=1) as pool:
                    writer.execute("set lock_timeout='0'; set statement_timeout='12s'")
                    holder.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))
                    future = pool.submit(run, writer, "insert into media_source(id,gym_id) values(%s,'gym') returning id", (asset_id,))
                    wait_end = time.monotonic() + 3
                    waiting_mode = 'ShareLock' if key == GRAPH else 'ExclusiveLock'
                    while (waiting_mode, False) not in locks(name + '_writer', key):
                        assert time.monotonic() < wait_end, name + ' did not wait on authority'
                        assert not future.done(), name + ' prematurely completed'
                        time.sleep(.01)
                    if key == GRAPH:
                        assert locks(name + '_writer', CENSUS) == []
                    else:
                        assert locks(name + '_writer', GRAPH) == [('ShareLock', True)]
                    started_wait = time.monotonic()
                    while time.monotonic() - started_wait < 5.5:
                        assert not future.done(), name + ' failed before caller deadline: ' + str(future.result())
                        time.sleep(.05)
                    assert (waiting_mode, False) in locks(name + '_writer', key)
                    holder.commit()
                    assert future.result(timeout=5) == (None, (asset_id,))
                    assert writer.execute("select current_setting('lock_timeout'),current_setting('statement_timeout')").fetchone() == ('0', '12s')
                assert sql('select count(*) from media_source where id=%s', (asset_id,)) == [(1,)]
                print('PASS: actual candidate media_source INSERT waits >5s on ' + key + ' then commits under caller deadlines', flush=True)

            # Negative authority takes final G exclusive before waiting on C;
            # Read Committed guard rejects repeatable-read even for zero rows.
            with lane('negative_holder') as holder, lane('negative_waiter') as writer, ThreadPoolExecutor(max_workers=1) as pool:
                holder.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))', (CENSUS,))
                future = pool.submit(run, writer, 'update fixer_forward_media_photo_state_20261007 set generation=generation where false returning generation')
                wait_c('negative_waiter')
                assert locks('negative_waiter', GRAPH) == [('ExclusiveLock', True)]
                holder.rollback()
                assert future.result(timeout=5)[0] is None
            with lane('no_upgrade') as writer:
                writer.execute('update media_source set id=id where false')
                assert run(writer, 'update fixer_forward_media_photo_state_20261007 set generation=generation where false returning generation')[0] == '25000'
            with lane('repeatable') as writer:
                writer.commit()
                writer.execute('set transaction isolation level repeatable read')
                assert run(writer, 'update media_source set id=id where false returning id')[0] == '25000'
            print('PASS: generated guard before/after P3 waits normally; negative authority final G-exclusive -> C; isolation gate', flush=True)
            print('LIMIT: synthetic schema/data and installed actual candidate SQL; no production DDL, provider I/O, RLS integration, rollout or release acceptance', flush=True)
    finally:
        if start_attempted and (data / 'postmaster.pid').exists():
            subprocess.run([str(PG / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=30)
        if (data / 'postmaster.pid').exists():
            raise RuntimeError('live disposable cluster preserved: ' + str(temp))
        shutil.rmtree(temp)


if __name__ == '__main__':
    main()
