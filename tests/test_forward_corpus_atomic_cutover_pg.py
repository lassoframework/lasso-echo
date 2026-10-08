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


def entry_sql(tranche):
    # Every prerequisite is tracked in the integrated B branch. No local task
    # evidence directory, git history or network fallback is permitted.
    return (ROOT / f'migrations/DRAFT_fixer_forward_lock_entry_{tranche}_20261008.sql').read_text()


def lasso_sql():
    return entry_sql('lasso')


def entry_definitions():
    definitions = {}
    for tranche in ('calendar', 'media', 'publish_caption', 'lasso'):
        text = entry_sql(tranche)
        for name in re.findall(r'CREATE OR REPLACE FUNCTION public\.(\w+)\(', text):
            definitions[name] = function(text, name)
    assert len(definitions) == 22
    definitions['fixer_forward_calendar_entry_lock_20261008'] = function(
        entry_sql('calendar'), 'fixer_forward_calendar_entry_lock_20261008', 'dollar')
    for filename, names in (
        ('DRAFT_fixer_forward_media_claim_20261006.sql', ('fixer_attest_forward_media_20261006',)),
        ('DRAFT_fixer_forward_media_observation_bridge_20261007.sql', ('fixer_record_forward_media_observation_20261007',)),
        ('DRAFT_fixer_owner_photo_clearance_20261007.sql', ('fixer_prepare_owner_photo_20261007', 'fixer_forward_media_provenance_lookup_20261006', 'fixer_owner_photo_corpus_write_lock_20261007')),
    ):
        text = (ROOT / 'migrations' / filename).read_text()
        for name in names:
            definitions[name] = function(text, name, 'dollar')
    # These wrappers intentionally retain their frozen production bodies; the
    # cutover pins them because adding a pre-delegation row lock would invert C.
    wrappers = (ROOT / 'migrations/calendar_approval_provenance_20261005.sql').read_text()
    for name in ('claim_calendar_gbp_publish_with_mode_owned', 'claim_calendar_publish_slot_proven_owned'):
        definitions[name] = function(wrappers, name, 'dollar')
    assert len(definitions) == 30
    return definitions


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
    for name, definition in entry_definitions().items():
        marker = '$function$' if '$function$' in definition else '$$'
        body = definition.split(marker)[1]
        assert hashlib.md5(body.encode()).hexdigest() in candidate, name
        if name in ('portal_action_receipt_apply', 'stage_lasso_campaign_row'):
            after = re.sub(r'(?im)^  lock table public\.content_calendar in share row exclusive mode;\n', '', body)
            assert after != body
            assert hashlib.md5(after.encode()).hexdigest() in candidate, name
    assert hashlib.md5(GENERATED.split('$$')[1].encode()).hexdigest() in candidate


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
                'rendition_url text,eligible boolean,excluded_by_coach boolean,review_status text,'
                'reviewed_at timestamptz,reviewed_by text,review_note text,review_content_hash text,'
                'consent_status text,release_ref text,consent_member_ref text,consent_expires_at timestamptz);'
                'create table media_asset_review_event(gym_id text,asset_id text,content_hash text,prior_status text,'
                'decision text,reviewed_by text,reviewed_at timestamptz,review_note text);'
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
            definitions = entry_definitions()
            # Install the complete real B entry stack, including the unchanged
            # wrappers. Validation is deferred solely because this synthetic
            # schema omits production tables used by unrelated business paths.
            sql("set check_function_bodies=off")
            for name, definition in definitions.items():
                if name.startswith('fixer_') and name != 'fixer_forward_calendar_entry_lock_20261008':
                    continue  # Already installed by the full actual P1 drafts.
                sql(definition)
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
            # Regress every P2 entry and delegating wrapper independently.
            # Legacy entries retain their tracked business bodies but omit G/C;
            # each missing/overloaded/legacy prerequisite must reject the whole
            # candidate before guards, lock removals or privileges change.
            entry_names = [name for name in definitions if not name.startswith('fixer_')]

            def assert_prerequisite_rejected(name):
                try:
                    sql(candidate)
                    raise AssertionError('unsafe prerequisite accepted: ' + name)
                except psycopg.Error as exc:
                    assert exc.sqlstate == '23514' and name in str(exc), (name, exc)
                    sql('rollback')
                assert sql('select count(*) from pg_trigger where tgname=%s', (TRIGGER,)) == [(0,)]
                assert sql("select has_table_privilege('anon','content_calendar','truncate')") == [(True,)]

            for name in entry_names:
                signature = sql("select oid::regprocedure::text from pg_proc where proname=%s", (name,))[0][0]
                sql('drop function ' + signature)
                assert_prerequisite_rejected(name)
                sql(definitions[name])
                sql('create function public.' + name + '(integer) returns integer language sql as $$select $1$$;')
                assert_prerequisite_rejected(name)
                sql('drop function public.' + name + '(integer)')
                if name == 'record_gym_media_review':
                    # Exact catalog-reported legacy body is already tracked.
                    legacy = function((ROOT / 'migrations/media_asset_review_binding_20260918.sql').read_text(), name, 'dollar')
                    assert hashlib.md5(legacy.split('$$')[1].encode()).hexdigest() == '626aedcebdd94728ead0475897246004'
                    sql(legacy)
                elif name not in ('claim_calendar_gbp_publish_with_mode_owned', 'claim_calendar_publish_slot_proven_owned'):
                    legacy = definitions[name].replace('perform public.fixer_forward_calendar_entry_lock_20261008();', '', 1)
                    assert legacy != definitions[name]
                    sql(legacy)
                else:
                    sql(definitions[name].replace('begin\n', 'begin\n  perform 1 from public.media_asset for update;\n', 1))
                assert_prerequisite_rejected(name)
                sql(definitions[name])
            # P1/helper/body flags are equally required. A same-body owner,
            # security/language/config alteration cannot pass a hash-only check.
            for name in (name for name in definitions if name.startswith('fixer_') and name != 'fixer_owner_photo_corpus_write_lock_20261007'):
                definition = definitions[name]
                replaceable = re.sub(r'create function', 'create or replace function', definition, count=1, flags=re.I)
                signature = sql("select oid::regprocedure::text from pg_proc where proname=%s", (name,))[0][0]
                sql('drop function ' + signature)
                assert_prerequisite_rejected(name)
                sql(replaceable)
                sql('create function public.' + name + '(integer) returns integer language sql as $$select $1$$;')
                assert_prerequisite_rejected(name)
                sql('drop function public.' + name + '(integer)')
                legacy = re.sub(r"(?m)^.*perform pg_advisory_xact_lock\(hashtextextended\('fixer_forward_photo_census_20261007', *0\)\);\n", '', replaceable, count=1)
                if name == 'fixer_record_forward_media_observation_20261007':
                    legacy = re.sub(r"(?m)^.*perform pg_advisory_xact_lock_shared\(hashtextextended\('fixer_forward_graph_20261006', *0\)\);\n", '', legacy, count=1)
                assert legacy != replaceable, name
                sql(legacy)
                assert_prerequisite_rejected(name)
                sql(replaceable)
            helper_sig = 'fixer_forward_calendar_entry_lock_20261008()'
            sql('alter function ' + helper_sig + ' owner to service_role')
            assert_prerequisite_rejected('fixer_forward_calendar_entry_lock_20261008')
            sql('alter function ' + helper_sig + ' owner to postgres')
            sql('alter function ' + helper_sig + ' set search_path=public')
            assert_prerequisite_rejected('fixer_forward_calendar_entry_lock_20261008')
            sql('alter function ' + helper_sig + ' set search_path=pg_catalog,public')
            sql('alter function ' + helper_sig + ' security invoker')
            assert_prerequisite_rejected('fixer_forward_calendar_entry_lock_20261008')
            sql('alter function ' + helper_sig + ' security definer')
            sql('alter function ' + helper_sig + ' stable')
            assert_prerequisite_rejected('fixer_forward_calendar_entry_lock_20261008')
            sql('alter function ' + helper_sig + ' volatile')
            sql('revoke all on function fixer_forward_calendar_entry_lock_20261008() from public,anon,authenticated,service_role;'
                'revoke all on function portal_action_receipt_apply(text,text,text,jsonb),'
                'stage_lasso_campaign_row(jsonb,text,text,text,text) from public,anon,authenticated;'
                'grant execute on function portal_action_receipt_apply(text,text,text,jsonb),'
                'stage_lasso_campaign_row(jsonb,text,text,text,text) to service_role;')
            # DROP/recreate drift probes naturally allocate new OIDs. Freeze
            # metadata only after all prerequisites have been restored, before
            # applying the candidate; the cutover itself must preserve it.
            metadata = sql("select oid,proname,proowner,proacl,prosecdef,provolatile,proconfig,"
                           "pg_get_function_identity_arguments(oid) from pg_proc where proname in "
                           "('portal_action_receipt_apply','stage_lasso_campaign_row') order by proname")
            print('PASS: complete B prerequisite inventory rejects legacy/missing/overloaded entries and helper owner/config/security/volatility drift before atomic cutover', flush=True)

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
            # Exercise the exact media-review entry implicated by the legacy
            # row-first reproduction. The real repaired function waits on C
            # without owning its target row, so a C holder can update that asset
            # and commit while the review waits; no C/row cycle can form.
            asset = str(uuid.uuid4())
            sql("insert into media_asset(id,gym_id,content_hash,review_status) values(%s,'gym','synthetic-hash','pending_review')", (asset,))
            review_call = 'select record_gym_media_review(%s,%s,%s,%s,%s,%s)'
            review_fields = Jsonb({'review_content_hash': 'synthetic-hash', 'review_status': 'approved',
                                  'reviewed_by': 'synthetic reviewer', 'reviewed_at': '2026-10-08T00:00:00Z'})
            review_args = ('gym', asset, 'synthetic-hash', 'pending_review', None, review_fields)
            with lane('review_holder') as holder, lane('review_waiter') as reviewer, ThreadPoolExecutor(max_workers=1) as pool:
                holder.execute('select fixer_forward_calendar_entry_lock_20261008()')
                future = pool.submit(run, reviewer, review_call, review_args)
                wait_c('review_waiter')
                with lane('review_row_probe') as probe:
                    probe.execute('select id from media_asset where id=%s for update nowait', (asset,))
                    probe.rollback()
                holder.execute('update media_asset set eligible=true where id=%s', (asset,))
                holder.commit()
                assert future.result(timeout=5) == (None, (True,))
            assert sql('select count(*) from media_asset_review_event where asset_id=%s', (asset,)) == [(1,)]
            # The waiting function also observes a coach edit committed before
            # its C acquisition and refuses the stale expected review state.
            with lane('review_direct') as direct, lane('review_stale') as reviewer, ThreadPoolExecutor(max_workers=1) as pool:
                direct.execute("update media_asset set review_status='rejected' where id=%s", (asset,))
                current_at = sql('select reviewed_at from media_asset where id=%s', (asset,))[0][0]
                future = pool.submit(run, reviewer, review_call, ('gym', asset, 'synthetic-hash', 'approved', current_at, review_fields))
                wait_c('review_stale')
                direct.commit()
                assert future.result(timeout=5) == (None, (False,))
            assert sql('select count(*) from media_asset_review_event where asset_id=%s', (asset,)) == [(1,)]
            print('PASS: actual full-B media-review RPC waits before asset row; C-holder edit completes and stale review refuses without new event', flush=True)

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
