"""Disposable PG17 proof of G -> C -> row at the P1 authority entries.

Synthetic schema and rows only; no production DSN, installs or remote I/O.
Run standalone with the existing PostgreSQL 17 tools and psycopg runtime.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import time
import uuid

from psycopg.types.json import Jsonb

from agent.forward_media_observation_bridge import prepare
from agent.gym_media_index import materialization_observation

ROOT = Path(__file__).resolve().parents[1]
GRAPH = 'fixer_forward_graph_20261006'
CENSUS = 'fixer_forward_photo_census_20261007'


def main():
    import psycopg

    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    with tempfile.TemporaryDirectory(prefix='forward_entry_order_pg_', dir='/tmp') as tmp:
        root = Path(tmp)
        sock, data = root / 'sock', root / 'data'
        sock.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run([str(pg / 'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        started = False
        try:
            subprocess.run([str(pg / 'pg_ctl'), '-D', str(data), '-l', str(root / 'pg.log'),
                            '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                           check=True, capture_output=True, timeout=60)
            started = True
            dsn = f'host={sock} port={port} user=postgres dbname=postgres'
            with psycopg.connect(dsn, autocommit=True) as admin:
                def sql(query, params=None):
                    cur = admin.execute(query, params)
                    return cur.fetchall() if cur.description else None

                assert int(sql('show server_version_num')[0][0]) // 10000 == 17
                sql('create role anon; create role authenticated; create role service_role;'
                    'create table content_calendar(id uuid primary key,gym_id text,post_date date,'
                    'account text,format text,gbp_location_id text,status text,variant_status text,'
                    'published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,'
                    'late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
                    'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
                    'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
                for name in ('DRAFT_fixer_forward_media_claim_20261006.sql',
                             'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                             'DRAFT_fixer_forward_media_source_history_20261007.sql',
                             'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                             'DRAFT_fixer_owner_photo_clearance_20261007.sql'):
                    sql((ROOT / 'migrations' / name).read_text())

                rid = uuid.uuid4()
                sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                    "source_media_asset_id,source_media_url,image_url) values(%s,'gym','2026-10-10',"
                    "'pending','active','asset','https://media.test/source','https://media.test/render')", (rid,))
                row = sql('select to_jsonb(r) from content_calendar r where id=%s', (rid,))[0][0]
                observation = materialization_observation(
                    b'source', b'render', row['image_url'], tenant='gym', source_asset_id='asset',
                    source_url=row['source_media_url'], recipe={'runtime_verified': False},
                    bytes_fn=lambda url: b'source' if url == row['source_media_url'] else b'render')
                packet = prepare(row, [observation])
                cases = (
                    ('attester', 'fixer_forward_media_attester_20261006', 'ExclusiveLock',
                     'select fixer_attest_forward_media_20261006(%s,%s,%s,%s,10,%s,10,null,null,%s,%s)',
                     (rid, 'stale revision', uuid.uuid4(), 'md5:' + 'a' * 32,
                      'md5:' + 'a' * 32, 'same_object', 'synthetic'), '23514'),
                    ('observation', 'service_role', 'ShareLock',
                     'select fixer_record_forward_media_observation_20261007(%s,%s,%s,%s)',
                     (rid, Jsonb(row), packet['observation_json'], packet['digest_input']), None),
                    ('owner_prepare', 'fixer_forward_media_owner_20261006', 'ExclusiveLock',
                     'select fixer_prepare_owner_photo_20261007(%s,%s,%s)',
                     (uuid.uuid4(), Jsonb({}), Jsonb({})), '55000'),
                )

                def locks(name, key):
                    return sql("select l.mode,l.granted from pg_locks l join pg_stat_activity a on a.pid=l.pid "
                               "where a.application_name=%s and l.locktype='advisory' "
                               "and l.classid::bigint=((hashtextextended(%s,0)>>32)&4294967295) "
                               "and l.objid::bigint=(hashtextextended(%s,0)&4294967295)", (name, key, key))

                def wait(name, key):
                    end = time.monotonic() + 3
                    while not any(not granted for _, granted in locks(name, key)):
                        assert time.monotonic() < end, f'{name} did not wait on {key}'
                        time.sleep(.01)

                def run(case, name):
                    _, role, _, command, args, _ = case
                    with psycopg.connect(dsn, application_name=name) as conn:
                        conn.execute("set lock_timeout='4s'; set statement_timeout='8s'")
                        conn.execute('set role ' + role)
                        try:
                            return None, conn.execute(command, args).fetchone()[0]
                        except psycopg.Error as exc:
                            conn.rollback()
                            return exc.sqlstate, None

                for case in cases:
                    label, _, graph_mode, _, _, expected_error = case
                    # First hold G: C must still be available to an independent
                    # transaction while this entry waits for its final G mode.
                    with psycopg.connect(dsn) as holder, ThreadPoolExecutor(max_workers=1) as pool:
                        holder.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))', (GRAPH,))
                        future = pool.submit(run, case, label + '_graph')
                        wait(label + '_graph', GRAPH)
                        assert locks(label + '_graph', CENSUS) == []
                        assert sql('select pg_try_advisory_xact_lock(hashtextextended(%s,0))', (CENSUS,)) == [(True,)]
                        holder.rollback()
                        assert future.result(timeout=5)[0] == expected_error
                    # Then hold C: this entry must already own G in its final
                    # mode, without having acquired its first row lock.
                    with psycopg.connect(dsn) as holder, ThreadPoolExecutor(max_workers=1) as pool:
                        holder.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))', (CENSUS,))
                        future = pool.submit(run, case, label + '_census')
                        wait(label + '_census', CENSUS)
                        assert locks(label + '_census', GRAPH) == [(graph_mode, True)]
                        assert locks(label + '_census', CENSUS) == [('ExclusiveLock', False)]
                        probe = ('select singleton from fixer_forward_media_photo_state_20261007 for update nowait'
                                 if label == 'owner_prepare' else
                                 'select id from content_calendar where id=%s for update nowait')
                        with psycopg.connect(dsn) as row_probe:
                            row_probe.execute(probe, None if label == 'owner_prepare' else (rid,))
                            row_probe.rollback()
                        holder.rollback()
                        error, receipt = future.result(timeout=5)
                        assert error == expected_error
                        if label == 'observation':
                            assert receipt['calendar_row_id'] == str(rid)
                            assert receipt['provenance_status'] == 'unverified'
                    print(f'PASS: {label} final {graph_mode} G -> exclusive C -> row', flush=True)
                assert sql('select count(*) from fixer_forward_media_observation_20261007') == [(1,)]
                assert sql('select count(*) from fixer_forward_media_lineage_20261006') == [(0,)]
                assert sql('select count(*) from fixer_owner_photo_reservation_20261007') == [(0,)]
                print('PASS: observation replay and invalid authority rollback preserved', flush=True)
        finally:
            if started:
                subprocess.run([str(pg / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                               check=True, capture_output=True, timeout=30)


if __name__ == '__main__':
    main()
