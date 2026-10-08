"""Disposable PostgreSQL 17 tests of the DRAFT Swift one-asset fence.

Uses existing server binaries, Unix socket only, no DSN or production access.
Fixture keys/types come from repository migrations, frozen live RPCs and a
2026-10-08 read-only catalog check of project ooqcvmcjspeltuuhcvlh. All six
tables are ordinary tables owned postgres; service_role is not owner/member,
superuser, createrole or replication. Full production constraints/defaults
and existing calendar guards are not recreated by this focused harness.
SQLite counter-ledger coordination, provider queues/history clearance and
prospective calendar admission are deliberately outside this SQL proof.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'migrations/DRAFT_swift_single_asset_maintenance_fence_20261008.sql'
GYM = 'swiftrivercrossfite5c9db'
GYM_UUID = 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'
ASSET = '1PhOFVHpEm8_ye2a546WT2VGwpRtCXcFr'
OTHER_ASSET = '1esVxnCv-EU-6N5R_-QYUNxCg9oypjk4s'


def pg(name):
    candidates = []
    if os.environ.get('PG17_BIN'):
        candidates.append(Path(os.environ['PG17_BIN']))
    candidates.extend((Path('/opt/homebrew/opt/postgresql@17/bin'),
                       Path('/usr/local/opt/postgresql@17/bin'),
                       Path('/usr/lib/postgresql/17/bin')))
    found = shutil.which(name)
    if found:
        candidates.append(Path(found).resolve().parent)
    for candidate in candidates:
        binary = candidate / name
        server = candidate / 'postgres'
        if binary.is_file() and server.is_file():
            version = subprocess.run([str(server), '--version'], capture_output=True,
                                     text=True, check=False)
            if version.returncode == 0 and ' 17.' in version.stdout:
                return str(binary)
    return None


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    if any(not pg(name) for name in ('initdb', 'pg_ctl', 'psql', 'postgres')):
        raise RuntimeError('Existing PostgreSQL 17 server binaries unavailable')
    assert shutil.disk_usage('/tmp').free >= 5 * 1024 ** 3, '5 GiB disk guard'
    assert subprocess.check_output([pg('postgres'), '--version'], text=True).split()[2].startswith('17.')
    with tempfile.TemporaryDirectory(prefix='swift_fence_pg17_', dir='/tmp') as temp:
        work = Path(temp)
        sock = work / 'sock'
        sock.mkdir()
        data = work / 'data'
        subprocess.run([pg('initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([pg('pg_ctl'), '-D', str(data), '-l', str(work / 'server.log'),
                        '-o', f"-k {sock} -p 55479 -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        base = [pg('psql'), '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-h', str(sock),
                '-p', '55479', '-d', 'postgres']

        def run(command, login='postgres'):
            return subprocess.run(base + ['-U', login], input=command, text=True,
                                  capture_output=True, timeout=20)

        def sql(command, login='postgres', error=None):
            result = run(command, login)
            if error:
                assert result.returncode != 0 and error in result.stderr, (command, result.stderr)
                return result.stderr
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        def await_sleep(name):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if sql(f"select count(*) from pg_stat_activity where application_name={quote(name)} and wait_event='PgSleep';") == '1':
                    return
                time.sleep(.01)
            raise AssertionError('transaction did not reach wait: ' + name)

        def snapshot(table, where):
            return json.loads(sql(f'select to_jsonb(t) from public.{table} t where {where};'))

        def manifest():
            before = snapshot('media_asset', f'id={quote(ASSET)}')
            return {'asset_before': before, 'asset_after': dict(before, used_count=0, last_used_at=None),
                    'source_before': snapshot('media_source', "id='swift-source'"),
                    'calendar_before': json.loads(sql("select coalesce(jsonb_agg(to_jsonb(c) order by id),'[]'::jsonb) from content_calendar c where swift_maintenance_20261008.row_is_swift('content_calendar',to_jsonb(c));")),
                    'ledger_sha256': 'a' * 64}

        def literal(value):
            return quote(json.dumps(value)) + '::jsonb'

        def repair(operation, value):
            return f'select swift_maintenance_20261008.repair({quote(operation)},{literal(value)});'

        try:
            sql('create role anon; create role authenticated; create role service_role login bypassrls; '
                'create role swift_operator login; create role attacker login; grant swift_operator to attacker;')
            sql((ROOT / 'migrations/media_source_media_asset_20260827.sql').read_text())
            sql((ROOT / 'migrations/media_asset_review_20260918.sql').read_text())
            sql((ROOT / 'migrations/media_asset_review_binding_20260918.sql').read_text())
            # Extra deployed columns absent from the older base migrations.
            # Full-row CAS/receipt must preserve these too. Defaults here are
            # fixture conveniences, not claimed production default evidence.
            sql('alter table media_asset add column first_indexed_at timestamptz; '
                "alter table media_source add column sync_status text not null default 'idle',"
                'add column sync_requested_at timestamptz,add column sync_started_at timestamptz,'
                'add column sync_finished_at timestamptz,add column sync_error text,add column sync_claim_token text;')
            calendar_columns = {
                'id': 'uuid primary key', 'gym_id': 'text', 'account': 'text', 'post_date': 'date',
                'pillar': 'text', 'format': 'text', 'caption': 'text', 'image_url': 'text',
                'status': 'text not null', 'created_at': 'timestamptz not null default now()',
                'published_at': 'timestamptz', 'late_post_id': 'text', 'scheduled_at': 'timestamptz',
                'thumbnail_url': 'text', 'gbp_topic_type': 'text', 'gbp_cta_type': 'text',
                'gbp_cta_url': 'text', 'gbp_event': 'jsonb', 'gbp_offer': 'jsonb', 'gbp_location_id': 'text',
                'reject_reason': 'text', 'source_media_url': 'text', "mentions": "jsonb not null default '[]'::jsonb",
                'hook_family': 'text', 'ask_type': 'text', 'time_slot': 'text', 'caption_len_band': 'text',
                'has_member_face': 'boolean', 'experiment_label': 'text', 'slot_index': 'integer',
                'source_media_asset_id': 'text', 'media_not_ready_reason': 'text', 'event_id': 'text',
                'variant_of': 'uuid', 'variant_status': 'text not null', 'publish_reservation_day': 'date',
                'publish_claim_token': 'uuid', 'logical_post_id': 'uuid', 'approval_kind': 'text',
                'approved_by': 'text', 'approved_at': 'timestamptz', 'approval_digest': 'text'}
            assert len(calendar_columns) == 42
            sql('create table content_calendar(' + ','.join(k + ' ' + t for k, t in calendar_columns.items()) + ');')
            sql('create table portal_swap_guard(id uuid primary key default gen_random_uuid(),'
                'gym_id uuid not null,account_key text not null,post_id uuid not null,'
                'action_id uuid not null unique,actor_id text not null,baseline_image_url text,baseline_caption text,'
                'baseline_status text,status text not null,created_at timestamptz not null default now(),'
                'updated_at timestamptz not null default now());')
            receipt_sql = (ROOT / 'migrations/portal_action_receipt_draft_20261004.sql').read_text()
            receipt_ddl = receipt_sql.split('CREATE TABLE IF NOT EXISTS public.portal_action_receipt (', 1)[1].split('\n);', 1)[0]
            sql('CREATE TABLE public.portal_action_receipt (' + receipt_ddl + '\n);')
            sql(f"insert into media_source(id,gym_id,folder_id,connected_at) values('swift-source','{GYM}','swift-folder',now()),('other-source','other','other-folder',now());")
            for asset, tenant, source, stamp in ((ASSET, GYM, 'swift-source', '2026-10-07T01:05:48.303394Z'),
                    (OTHER_ASSET, GYM, 'swift-source', '2026-10-07T02:02:42.327208Z'),
                    ('other-asset', 'other', 'other-source', '2026-10-07T01:00:00Z')):
                sql(f"insert into media_asset(id,gym_id,source_id,kind,title,indexed_at,used_count,last_used_at,content_hash,drive_modified,review_status,eligible) values({quote(asset)},{quote(tenant)},{quote(source)},'photo','photo',now(),1,{quote(stamp)},'version-1',now(),'approved',true);")
            swift_row, other_row = str(uuid.uuid4()), str(uuid.uuid4())
            sql(f"insert into content_calendar(id,gym_id,status,variant_status,image_url,caption) values('{swift_row}','{GYM}','pending','active','https://example/photo.jpg','caption'),('{other_row}','other','pending','active','https://example/other.jpg','caption');")
            # Preexisting tenant-corrupt relationships reproduced by independent
            # review: a foreign-keyed asset still belongs to a Swift source.
            sql(f"insert into media_asset(id,gym_id,source_id,kind,title,indexed_at) values('crosslinked-asset','other','swift-source','photo','corrupt fixture',now());")
            linked_row = str(uuid.uuid4())
            sql(f"insert into content_calendar(id,gym_id,source_media_asset_id,status,variant_status,image_url,caption) values('{linked_row}','other','crosslinked-asset','pending','active','https://example/crosslinked.jpg','linked caption');")
            # The reverse relationship also needs closure: a foreign-keyed
            # source already owning a Swift-keyed asset stays inside the fence.
            sql(f"insert into media_source(id,gym_id,folder_id,connected_at) values('linked-source','other','linked-folder',now()); insert into media_asset(id,gym_id,source_id,kind,title,indexed_at) values('swift-linked-asset','{GYM}','linked-source','photo','reverse corrupt fixture',now());")
            sql('grant usage on schema public to service_role,swift_operator,attacker; '
                'grant select,insert,update,delete,truncate on all tables in schema public to service_role; '
                'grant usage,select on all sequences in schema public to service_role;')
            # Use the actual frozen SECURITY DEFINER portal reservation RPC:
            # a direct legacy call must hit the row trigger on portal_swap_guard.
            frozen = (ROOT / 'tests/fixtures/forward_lock_entry/portal-function-definitions-20261008.sql').read_text()
            rpc = frozen.split('-- portal_swap_reserve\n', 1)[1].split('\n-- record_gym_media_review', 1)[0]
            sql(rpc)
            sql('alter table content_calendar alter column source_media_asset_id type varchar;')
            sql(MIGRATION.read_text(), error='schema mismatch')
            assert sql("select to_regnamespace('swift_maintenance_20261008') is null;") == 't'
            sql('alter table content_calendar alter column source_media_asset_id type text;')

            # An older writer predates the triggers. Atomic ACCESS EXCLUSIVE
            # installation must WAIT for its transaction before closing writes.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(run, "begin; set application_name='old_writer'; set role service_role; update media_asset set title='older writer committed' where id=" + quote(ASSET) + "; select pg_sleep(1); commit;")
                await_sleep('old_writer')
                installer = pool.submit(run, MIGRATION.read_text())
                time.sleep(.15)
                assert not installer.done(), 'installation skipped in-flight writer'
                assert writer.result().returncode == 0
                installed = installer.result()
                assert installed.returncode == 0, installed.stderr
            assert sql('select active from swift_maintenance_20261008.fence;') == 't'
            assert sql('select operation_id is null from swift_maintenance_20261008.fence;') == 't'
            assert sql("select count(*) from pg_trigger where tgname='aa_swift_maintenance_fence' and tgenabled='A';") == '6'
            sql("set role service_role; update media_asset set title='blocked' where id=" + quote(ASSET) + ';', error='write denied')
            sql("set role service_role; update media_asset set title='blocked' where id=" + quote(OTHER_ASSET) + ';', error='write denied')
            sql("set role service_role; update media_asset set title='other succeeds' where id='other-asset';")
            sql(f"set role service_role; update content_calendar set caption='other succeeds' where id='{other_row}';")
            # OLD tenant, NEW tenant, and related source/asset/row identity.
            for command in ["update media_asset set gym_id='other' where id=" + quote(ASSET),
                            "update media_asset set gym_id='" + GYM + "' where id='other-asset'",
                            "update media_asset set source_id='swift-source' where id='other-asset'",
                            "update media_source set gym_id='other' where id='swift-source'",
                            "update content_calendar set gym_id='other' where id='" + swift_row + "'",
                            "update content_calendar set source_media_asset_id=" + quote(ASSET) + " where id='" + other_row + "'",
                            "insert into media_asset_review_event(gym_id,asset_id,content_hash,decision,reviewed_by,reviewed_at) values('other'," + quote(ASSET) + ",'h','approved','tester',now())",
                            "insert into portal_action_receipt(gym_id,action_id,action,row_id,request_fingerprint) values('other','a','swap','" + swift_row + "','" + 'a' * 64 + "')"]:
                sql('set role service_role; ' + command + ';', error='write denied')
            direct_rpc = f"set role service_role; select portal_swap_reserve('{GYM_UUID}','{swift_row}','{uuid.uuid4()}','actor','{GYM}','https://example/photo.jpg','caption');"
            sql(direct_rpc, error='write denied')
            for command in [
                    "update media_asset set title='blocked corrupt asset' where id='crosslinked-asset'",
                    "update media_source set active=false where id='linked-source'",
                    "update content_calendar set source_media_asset_id='crosslinked-asset' where id='" + other_row + "'",
                    "update content_calendar set caption='blocked corrupt row' where id='" + linked_row + "'",
                    "insert into media_asset_review_event(gym_id,asset_id,content_hash,decision,reviewed_by,reviewed_at) values('other','crosslinked-asset','h','approved','tester',now())",
                    "insert into portal_action_receipt(gym_id,action_id,action,row_id,request_fingerprint,selected_asset) values('other','corrupt-selection','swap','" + other_row + "','" + 'a' * 64 + "','{\"asset_id\":\"crosslinked-asset\"}')",
                    "insert into portal_action_receipt(gym_id,action_id,action,row_id,request_fingerprint) values('other','corrupt-row','swap','" + linked_row + "','" + 'a' * 64 + "')",
                    "insert into portal_swap_guard(gym_id,account_key,post_id,action_id,actor_id,status) values('" + str(uuid.uuid4()) + "','other','" + linked_row + "','" + str(uuid.uuid4()) + "','actor','active')"]:
                # These are the SQL mutations reached by direct service REST.
                sql('set role service_role; ' + command + ';', error='write denied')
            corrupt_rpc = f"set role service_role; select portal_swap_reserve('{uuid.uuid4()}','{linked_row}','{uuid.uuid4()}','actor','other','https://example/crosslinked.jpg','linked caption');"
            sql(corrupt_rpc, error='write denied')
            sql(f"set role service_role; select portal_swap_reserve('{uuid.uuid4()}','{other_row}','{uuid.uuid4()}','actor','other','https://example/other.jpg','other succeeds');")
            for command in ['update swift_maintenance_20261008.fence set operation_id=gen_random_uuid()',
                            "select swift_maintenance_20261008.bind(null,null,null)",
                            "select swift_maintenance_20261008.repair(null,null)",
                            'insert into swift_maintenance_20261008.permit values(null,null,null,null,null,null,null,false)']:
                sql('set role service_role; ' + command + ';', error='permission denied')
            sql("set role service_role; set session_replication_role='replica';", error='permission denied')
            sql('set role service_role; alter table media_asset disable trigger aa_swift_maintenance_fence;', error='must be owner')
            sql('set role service_role; truncate media_asset cascade;', error='truncate denied')
            sql('delete from swift_maintenance_20261008.fence;', error='immutable state')

            value = manifest()
            operation = str(uuid.uuid4())
            digest = sql(f"select encode(sha256(convert_to(({literal(value)})::text,'UTF8')),'hex');")
            bind = f"select swift_maintenance_20261008.bind('{operation}','swift_operator','{digest}');"
            # Reject every owner lane, not only media_asset/fence. Otherwise an
            # accepted operator can disable that table's trigger while active.
            guarded = ['public.media_source', 'public.media_asset', 'public.content_calendar',
                       'public.portal_action_receipt', 'public.portal_swap_guard', 'public.media_asset_review_event',
                       'swift_maintenance_20261008.fence', 'swift_maintenance_20261008.permit',
                       'swift_maintenance_20261008.receipt']
            for table in guarded:
                sql(f'alter table {table} owner to swift_operator;')
                sql(bind, error='unsafe operator')
                assert sql('select operation_id is null from swift_maintenance_20261008.fence;') == 't'
                sql(f'alter table {table} owner to postgres;')
            sql('create role guarded_table_owner; grant guarded_table_owner to swift_operator; '
                'alter table content_calendar owner to guarded_table_owner;')
            sql(bind, error='unsafe operator')
            sql('alter table content_calendar owner to postgres; revoke guarded_table_owner from swift_operator;')
            sql('alter schema swift_maintenance_20261008 owner to swift_operator;')
            sql(bind, error='unsafe operator')
            sql('alter schema swift_maintenance_20261008 owner to postgres; '
                'alter function swift_maintenance_20261008.guard() owner to swift_operator;')
            sql(bind, error='unsafe operator')
            sql('alter function swift_maintenance_20261008.guard() owner to postgres;')
            # Fence binding uses FOR UPDATE. An existing shared writer/read lock
            # survives until transaction end; no NOWAIT/TTL escape is used.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                holder = pool.submit(run, "begin; set application_name='share_holder'; select active from swift_maintenance_20261008.fence for share; select pg_sleep(.8); commit;")
                await_sleep('share_holder')
                binder = pool.submit(run, bind)
                time.sleep(.15)
                assert not binder.done(), 'binding ignored writer fence lock'
                assert holder.result().returncode == 0
                assert binder.result().returncode == 0
            sql(bind, error='binding unavailable')
            sql(repair(operation, value), login='attacker', error='operator/binding/replay denied')
            sql('set role swift_operator; ' + repair(operation, value), login='attacker', error='operator/binding/replay denied')
            sql("set role service_role; set app.swift_maintenance_permit='true'; update media_asset set used_count=0 where id=" + quote(ASSET) + ';', error='write denied')
            changed = dict(value, ledger_sha256='b' * 64)
            sql(repair(operation, changed), login='swift_operator', error='operator/binding/replay denied')
            sql(repair(str(uuid.uuid4()), value), login='swift_operator', error='operator/binding/replay denied')
            release = f"select swift_maintenance_20261008.release('{operation}',null);"
            sql(release, login='swift_operator', error='receipt readback denied')

            # Transaction abort after successful repair rolls back counters,
            # private permit and receipt; persistent active binding survives.
            command = repair(operation, value)
            sql('begin; ' + command + ' rollback;', login='swift_operator')
            assert snapshot('media_asset', f'id={quote(ASSET)}') == value['asset_before']
            assert sql('select count(*) from swift_maintenance_20261008.receipt;') == '0'
            assert sql('select count(*) from swift_maintenance_20261008.permit;') == '0'
            # Connection crash while repair is uncommitted gives the same result.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                crashing = pool.submit(run, "begin; set application_name='crash_repair'; " + command + ' select pg_sleep(10); commit;', 'swift_operator')
                await_sleep('crash_repair')
                assert sql("select pg_terminate_backend(pid) from pg_stat_activity where application_name='crash_repair';") == 't'
                assert crashing.result().returncode != 0
            assert snapshot('media_asset', f'id={quote(ASSET)}') == value['asset_before']
            assert sql('select count(*) from swift_maintenance_20261008.receipt;') == '0'
            # Later BEFORE triggers cannot silently alter an otherwise permitted
            # reset. The post-write full-row readback must abort the transaction.
            sql("create function soft_drift() returns trigger language plpgsql as $$ begin new.title='soft drift'; return new; end $$; create trigger zz_soft_drift before update on media_asset for each row execute function soft_drift();")
            sql(command, login='swift_operator', error='post-write drift')
            assert snapshot('media_asset', f'id={quote(ASSET)}') == value['asset_before']
            sql('drop trigger zz_soft_drift on media_asset; drop function soft_drift();')
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                racing = list(pool.map(lambda _: run(command, 'swift_operator'), range(2)))
            winners = [r for r in racing if r.returncode == 0]
            assert len(winners) == 1, [r.stderr for r in racing]
            result = json.loads(winners[0].stdout)
            assert result['after'] == value['asset_after'] and result['fence_retained']
            assert sql('select count(*) from swift_maintenance_20261008.permit;') == '0'
            assert sql('select count(*) from swift_maintenance_20261008.receipt;') == '1'
            sql(command, login='swift_operator', error='operator/binding/replay denied')
            sql('update swift_maintenance_20261008.receipt set after_row=before_row;', error='immutable state')
            sql('set role service_role; update media_asset set used_count=1 where id=' + quote(ASSET) + ';', error='write denied')
            sql('set role service_role; update media_asset set used_count=0 where id=' + quote(OTHER_ASSET) + ';', error='write denied')
            sql("set role service_role; update media_asset set used_count=2 where id='other-asset';")
            assert sql('select active from swift_maintenance_20261008.fence;') == 't'
            # A real server crash/recovery also retains the committed binding,
            # active fence and receipt, rather than relying on process state.
            subprocess.run([pg('pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=60)
            subprocess.run([pg('pg_ctl'), '-D', str(data), '-l', str(work / 'server.log'),
                            '-o', f"-k {sock} -p 55479 -c listen_addresses=''", '-w', 'start'],
                           check=True, capture_output=True, timeout=60)
            assert sql('select active from swift_maintenance_20261008.fence;') == 't'
            assert sql('select count(*) from swift_maintenance_20261008.receipt;') == '1'
            sql(command, login='swift_operator', error='operator/binding/replay denied')
            receipt_digest = sql("select encode(sha256(convert_to(to_jsonb(r)::text,'UTF8')),'hex') from swift_maintenance_20261008.receipt r;")
            sql(release, login='swift_operator', error='receipt readback denied')
            wrong_release = f"select swift_maintenance_20261008.release('{operation}','{'b' * 64}');"
            sql(wrong_release, login='swift_operator', error='receipt readback denied')
            correct_release = f"select swift_maintenance_20261008.release('{operation}','{receipt_digest}');"
            sql(correct_release, login='attacker', error='receipt readback denied')
            sql('set role swift_operator; ' + correct_release, login='attacker', error='receipt readback denied')
            sql("set role service_role; " + correct_release, error='permission denied')
            assert sql('select active from swift_maintenance_20261008.fence;') == 't'
            assert json.loads(sql(correct_release, login='swift_operator'))['released']
            assert sql('select active from swift_maintenance_20261008.fence;') == 'f'
            assert sql('select release_receipt_sha256 from swift_maintenance_20261008.fence;') == receipt_digest
            sql(correct_release, login='swift_operator', error='receipt readback denied')
            sql("set role service_role; update media_asset set title='after attended release' where id=" + quote(ASSET) + ';')
            assert sql('select count(*) from swift_maintenance_20261008.receipt;') == '1'
            print('PASS PG17: schema mismatch atomic rollback; atomic installation waits old writer; active unbound default; shared fence binding; '
                  'six ALWAYS BEFORE ROW tables; direct portal SECURITY DEFINER RPC denied; '
                  'corrupt source/asset/calendar relation closure across service REST/RPC writes; '
                  'all guarded/private ownership and owner membership binding denied; OLD/NEW and related '
                  'Swift identity; same/different Swift asset denied; unrelated tenant succeeds; role/GUC/replication/'
                  'DDL/private-permit bypass denied; exact one-candidate counter repair; rollback/crash/replay; '
                  'soft BEFORE-trigger drift rollback; two-operator one-winner race; server crash persistence; '
                  'immutable receipt; wrong/missing release digest HOLD; '
                  'exact attended release/readback/release replay denial. SQL ONLY: no SQLite/provider/history/admission proof.')
        finally:
            subprocess.run([pg('pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           capture_output=True, timeout=60)


def test_swift_single_asset_maintenance_fence_pg():
    main()


if __name__ == '__main__':
    main()
