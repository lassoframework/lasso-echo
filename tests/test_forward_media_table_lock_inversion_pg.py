"""Standalone, disposable PostgreSQL 17 relation-lock inversion proof.

Run with the existing psycopg runtime, for example:
  /tmp/echo-forward-media-pg-20261007/bin/python this_file.py

Synthetic schema only; no production DSN, migrations, external connection or
dependency installation. The fixture models the real G-shared -> C statement
trigger and RPC entry ordering, then compares an RPC with and without SHARE ROW
EXCLUSIVE. It deliberately has no slot/receipt uniqueness constraint: the
corrected decision path must serialize through C and re-read after its wait.
This is a minimal lock/visibility proof, not production function parity proof.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import time


PG = Path('/opt/homebrew/opt/postgresql@17/bin')
GRAPH = 'fixer_forward_graph_20261006'
CENSUS = 'fixer_forward_photo_census_20261007'


@contextmanager
def disposable_directory():
    root = Path(tempfile.mkdtemp(prefix='forward_table_inversion_pg_', dir='/tmp'))
    try:
        yield root
    finally:
        # A failed shutdown must preserve files for operator recovery rather
        # than allow TemporaryDirectory to erase a possibly live cluster.
        if (root / 'data' / 'postmaster.pid').exists():
            raise RuntimeError(f'disposable cluster shutdown failed; preserved at {root}')
        shutil.rmtree(root)


SCHEMA = """
create table content_calendar (
 id bigint generated always as identity primary key,
 gym_id text not null, slot integer not null, revision integer not null
);
create table claim_receipt (
 id bigint generated always as identity primary key,
 calendar_row_id bigint not null references content_calendar(id)
);
create function calendar_entry() returns void language plpgsql volatile as $$
begin
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
end $$;
create function calendar_statement_entry() returns trigger language plpgsql as $$
begin
 perform calendar_entry();
 return null;
end $$;
create trigger calendar_entry before insert or update on content_calendar
 for each statement execute function calendar_statement_entry();
create function decide_slot(p_gym text, p_slot integer, p_table_lock boolean)
returns jsonb language plpgsql volatile as $$
declare r content_calendar%rowtype; receipt_id bigint; existed boolean;
begin
 perform calendar_entry();
 if p_table_lock then
  lock table content_calendar in share row exclusive mode;
 end if;
 -- VOLATILE PL/pgSQL statements get fresh Read Committed snapshots after C.
 select * into r from content_calendar where gym_id=p_gym and slot=p_slot;
 existed:=found;
 if not existed then
  insert into content_calendar(gym_id,slot,revision) values(p_gym,p_slot,1)
   returning * into r;
 end if;
 select id into receipt_id from claim_receipt where calendar_row_id=r.id;
 if found then
  return jsonb_build_object('row_id',r.id,'revision',r.revision,
   'existing_slot',existed,'receipt_created',false);
 end if;
 insert into claim_receipt(calendar_row_id) values(r.id);
 return jsonb_build_object('row_id',r.id,'revision',r.revision,
  'existing_slot',existed,'receipt_created',true);
end $$;
"""


def main():
    import psycopg

    for name in ('initdb', 'pg_ctl'):
        assert (PG / name).is_file(), f'BLOCKED: installed PG17 {name} required'
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3, 'BLOCKED: 5 GiB disk guard'
    with disposable_directory() as root:
        sock, data = root / 'sock', root / 'data'
        sock.mkdir()
        # TCP is disabled and each cluster has a private socket directory, so
        # concurrent disposable clusters do not contend for a TCP port.
        port = random.randint(41000, 59000)
        subprocess.run([str(PG / 'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        start_attempted = False
        try:
            start_attempted = True
            subprocess.run([str(PG / 'pg_ctl'), '-D', str(data), '-l', str(root / 'pg.log'),
                            '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                           check=True, capture_output=True, timeout=60)
            dsn = f'host={sock} port={port} user=postgres dbname=postgres'
            with psycopg.connect(dsn, autocommit=True) as admin:
                def sql(query, params=None):
                    cur = admin.execute(query, params)
                    return cur.fetchall() if cur.description else None

                assert int(sql('show server_version_num')[0][0]) // 10000 == 17
                sql(SCHEMA)

                def connection(name, deadlock='5s'):
                    conn = psycopg.connect(dsn, application_name=name)
                    conn.execute("set lock_timeout='8s'; set statement_timeout='12s'")
                    conn.execute('set deadlock_timeout = ' + "'" + deadlock + "'")
                    assert conn.execute('show transaction_isolation').fetchone() == ('read committed',)
                    return conn

                def advisory(name, key):
                    return sql("select l.mode,l.granted from pg_locks l join pg_stat_activity a on a.pid=l.pid "
                               "where a.application_name=%s and l.locktype='advisory' "
                               "and l.classid::bigint=((hashtextextended(%s,0)>>32)&4294967295) "
                               "and l.objid::bigint=(hashtextextended(%s,0)&4294967295)", (name, key, key))

                def relation(name):
                    return sql("select l.mode,l.granted from pg_locks l join pg_stat_activity a on a.pid=l.pid "
                               "where a.application_name=%s and l.locktype='relation' "
                               "and l.relation='content_calendar'::regclass", (name,))

                def wait_for(predicate, label):
                    deadline = time.monotonic() + 3
                    while not predicate():
                        assert time.monotonic() < deadline, f'did not observe {label}'
                        # Poll observable lock state; this is not a timing-based
                        # assumption about when either transaction has started.
                        time.sleep(.005)

                def execute(conn, command, params=None):
                    try:
                        cur = conn.execute(command, params)
                        value = cur.fetchone()[0] if cur.description else None
                        conn.commit()
                        return None, value
                    except psycopg.Error as exc:
                        conn.rollback()
                        return exc.sqlstate, None

                def reset():
                    sql('truncate claim_receipt, content_calendar restart identity')
                    sql("insert into content_calendar(gym_id,slot,revision) values('gym',0,0)")

                statements = {
                    'UPDATE': "update content_calendar set revision=revision+1 where gym_id='gym' and slot=0",
                    'INSERT': "insert into content_calendar(gym_id,slot,revision) values('gym',1,1)",
                }

                # RPC already owns G-shared and C. Ordinary DML takes its
                # implicit RowExclusive relation lock before BEFORE STATEMENT
                # tries G-shared -> C. SRE conflicts with that relation lock.
                # Different deadlock deadlines make the RPC the deterministic
                # detector/victim; lock_timeout is longer than deadlock_timeout.
                for kind, statement in statements.items():
                    reset()
                    direct_name, rpc_name = 'broken_direct_' + kind, 'broken_rpc_' + kind
                    with connection(direct_name) as direct, connection(rpc_name, '1s') as rpc:
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            rpc.execute('select calendar_entry()')
                            direct_future = pool.submit(execute, direct, statement)
                            wait_for(lambda: ('ExclusiveLock', False) in advisory(direct_name, CENSUS),
                                     kind + ' statement trigger waiting on C')
                            assert advisory(direct_name, GRAPH) == [('ShareLock', True)]
                            assert ('RowExclusiveLock', True) in relation(direct_name)
                            assert advisory(rpc_name, GRAPH) == [('ShareLock', True)]
                            assert advisory(rpc_name, CENSUS) == [('ExclusiveLock', True)]
                            rpc_future = pool.submit(execute, rpc, "select decide_slot('gym',0,true)")
                            wait_for(lambda: ('ShareRowExclusiveLock', False) in relation(rpc_name),
                                     kind + ' RPC waiting for conflicting relation lock')
                            # Complete cycle: RPC waits on DML relation; DML
                            # waits on the same RPC's transaction-owned C.
                            assert ('ExclusiveLock', False) in advisory(direct_name, CENSUS)
                            assert rpc_future.result(timeout=5)[0] == '40P01'
                            assert direct_future.result(timeout=5)[0] is None
                    assert sql('select count(*) from claim_receipt') == [(0,)]
                    print(f'PASS: {kind} relation -> G-shared -> C versus RPC G-shared -> C -> SRE: SQLSTATE 40P01',
                          flush=True)

                # Same RPC-first schedule, now no SRE. Compatible relation
                # locks allow the RPC to finish while DML waits on C.
                for kind, statement in statements.items():
                    reset()
                    direct_name, rpc_name = 'fixed_direct_' + kind, 'fixed_rpc_' + kind
                    with connection(direct_name) as direct, connection(rpc_name) as rpc:
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            rpc.execute('select calendar_entry()')
                            direct_future = pool.submit(execute, direct, statement)
                            wait_for(lambda: ('ExclusiveLock', False) in advisory(direct_name, CENSUS),
                                     kind + ' corrected statement waiting on C')
                            assert ('RowExclusiveLock', True) in relation(direct_name)
                            result = rpc.execute("select decide_slot('gym',0,false)").fetchone()[0]
                            assert result['existing_slot'] and result['receipt_created']
                            assert not any(mode == 'ShareRowExclusiveLock' for mode, _ in relation(rpc_name))
                            rpc.commit()
                            assert direct_future.result(timeout=5)[0] is None
                    assert sql('select count(*) from claim_receipt') == [(1,)]
                    print(f'PASS: corrected RPC-first {kind}, no SRE and both transactions commit', flush=True)

                # DML-first: both RPCs start while the direct change is invisible
                # and wait on C. Their outer calls began before DML committed;
                # the volatile function's subsequent read must see fresh state.
                # Neither slots nor receipts have uniqueness constraints beyond
                # surrogate IDs, so duplicated decisions cannot hide as 23505.
                for kind, statement in statements.items():
                    reset()
                    slot = 0 if kind == 'UPDATE' else 1
                    with connection('fresh_direct_' + kind) as direct:
                        direct.execute(statement)
                        names = ['fresh_rpc_' + kind + '_' + str(i) for i in range(2)]
                        with connection(names[0]) as rpc_a, connection(names[1]) as rpc_b:
                            # Prove both sessions already saw the old committed
                            # state before entering the waiting RPC SELECT.
                            old = [(0,)] if kind == 'UPDATE' else []
                            for rpc in (rpc_a, rpc_b):
                                assert rpc.execute('select revision from content_calendar where gym_id=%s and slot=%s',
                                                   ('gym', slot)).fetchall() == old
                                # Release the probe's AccessShare relation lock.
                                # The following RPC SELECT still starts before
                                # direct.commit(), so its initial snapshot is old.
                                rpc.commit()
                            with ThreadPoolExecutor(max_workers=2) as pool:
                                futures = [pool.submit(execute, rpc, 'select decide_slot(%s,%s,false)', ('gym', slot))
                                           for rpc in (rpc_a, rpc_b)]
                                for name in names:
                                    wait_for(lambda n=name: ('ExclusiveLock', False) in advisory(n, CENSUS),
                                             name + ' waiting on C')
                                    assert advisory(name, GRAPH) == [('ShareLock', True)]
                                    assert relation(name) == [], 'RPC read must follow its C acquisition'
                                direct.commit()
                                values = [future.result(timeout=5) for future in futures]
                            assert all(error is None for error, _ in values), values
                            decisions = [value for _, value in values]
                            assert all(d['existing_slot'] and d['revision'] == 1 for d in decisions), decisions
                            assert len({d['row_id'] for d in decisions}) == 1, decisions
                            assert sorted(d['receipt_created'] for d in decisions) == [False, True], decisions
                    assert sql('select count(*) from content_calendar where gym_id=%s and slot=%s',
                               ('gym', slot)) == [(1,)]
                    assert sql('select count(*) from claim_receipt') == [(1,)]
                    print(f'PASS: corrected DML-first {kind}, fresh Read Committed visibility, '
                          'two RPCs choose one calendar slot and one receipt', flush=True)
                # Mirror the catalog-discovered TRUNCATE grants: TRUNCATE does
                # not fire the INSERT/UPDATE statement trigger, so removing
                # tenant privileges is a required separate guard. This fixture
                # tests the revoke pattern, not the release migration itself.
                sql('create role anon; create role authenticated; create role service_role; '
                    'create table media_asset(id integer); create table media_source(id integer); '
                    'insert into media_asset values(1); insert into media_source values(1); '
                    'grant truncate on content_calendar to anon; '
                    'grant truncate on content_calendar,media_asset,media_source to service_role')
                assert sql("select has_table_privilege('anon','content_calendar','truncate')") == [(True,)]
                assert sql("select has_table_privilege('service_role','media_asset','truncate')") == [(True,)]
                sql('revoke truncate on content_calendar,media_asset,media_source '
                    'from public,anon,authenticated,service_role')
                for role in ('anon', 'authenticated', 'service_role'):
                    for table in ('content_calendar', 'media_asset', 'media_source'):
                        before = sql('select count(*) from ' + table)
                        with connection('truncate_' + role + '_' + table) as runtime:
                            runtime.execute('set role ' + role)
                            # CASCADE avoids unrelated FK errors masking the
                            # privilege guard on content_calendar.
                            assert execute(runtime, 'truncate ' + table + ' cascade')[0] == '42501'
                        assert sql('select count(*) from ' + table) == before
                print('PASS: tenant-role TRUNCATE attempts reject with SQLSTATE 42501; rows preserved', flush=True)
                print('LIMIT: minimal fixture proves the relation-lock inversion and C serialization only; '
                      'production function coverage, privileges, tenant/safety contracts and deployment '
                      'require separate verification.', flush=True)
        finally:
            # Also stop a cluster if pg_ctl start succeeded but its client call
            # timed out. Never remove live cluster files without shutdown.
            if start_attempted and (data / 'postmaster.pid').exists():
                subprocess.run([str(PG / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                               check=True, capture_output=True, timeout=30)


if __name__ == '__main__':
    main()
