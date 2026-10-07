"""Disposable PG17 role/revision tests. All records/auth are synthetic."""
from dataclasses import asdict
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.forward_media_generated_owner import OwnerEvidenceReadStore, OwnerEvidenceReadAPI, ReadTokenBinding
from agent.forward_media_generated_prepare import GeneratedReceiptHold
from tests.test_forward_media_generated_owner import make_request, body, TOKEN


def main():
    import psycopg
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    with tempfile.TemporaryDirectory(prefix='generated_owner_read_pg_', dir='/tmp') as tmp:
        root = Path(tmp); sock = root/'sock'; sock.mkdir(); data = root/'data'
        port = random.randint(41000, 59000)
        subprocess.run([str(pg/'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        started = False
        connections = []
        try:
            subprocess.run([str(pg/'pg_ctl'), '-D', str(data), '-l', str(root/'pg.log'),
                '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                check=True, capture_output=True, timeout=60)
            started = True
            def dsn(role='postgres'): return f'host={sock} port={port} dbname=postgres user={role}'
            admin = psycopg.connect(dsn(), autocommit=True); connections.append(admin)
            def sql(query, args=None):
                with admin.cursor() as cur:
                    cur.execute(query, args)
                    return cur.fetchall() if cur.description else None
            sql('create role anon;create role authenticated;create role service_role;'
                'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,'
                'format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,'
                'publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,'
                'thumbnail_url text,media_not_ready_reason text,caption text);'
                'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
                'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql',
                'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                'DRAFT_fixer_forward_media_source_history_20261007.sql',
                'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                'DRAFT_fixer_owner_photo_clearance_20261007.sql',
                'DRAFT_fixer_generated_owner_read_20261007.sql'):
                sql((ROOT/'migrations'/name).read_text())
            sql('create role generated_test_reader login;grant fixer_generated_owner_reader_20261007 to generated_test_reader;'
                'create role generated_test_writer login;grant fixer_forward_media_owner_20261006 to generated_test_writer;'
                'create role generated_test_mixed login;grant fixer_generated_owner_reader_20261007,fixer_forward_media_owner_20261006 to generated_test_mixed;'
                'create role generated_test_history_mixed login;grant fixer_generated_owner_reader_20261007,fixer_forward_media_history_auditor_20261007 to generated_test_history_mixed;')
            reader = psycopg.connect(dsn('generated_test_reader')); connections.append(reader)
            store = OwnerEvidenceReadStore(reader, 'generated_test_reader')
            req = make_request()
            first = store.diagnostics(req)
            assert reader.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            assert first['database_calendar_rows'] == first['database_tenant_assets'] == 0
            assert first['history_complete'] is False and first['photo_inventory_complete'] is False
            assert first['eligible_photo_count'] is None  # empty DB is not depletion
            api = OwnerEvidenceReadAPI(store, [ReadTokenBinding(TOKEN, 'verifier', frozenset({'gym'}))], enabled=True)
            code, result = api.handle('POST', '/snapshot', 'Bearer '+TOKEN, body(req))
            assert code == 200 and result['status'] == 'hold' and result['request'] == asdict(req)
            assert result['copy'] is None and result['palette'] is None
            assert reader.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

            def denied(conn, query):
                try:
                    conn.execute(query); raise AssertionError('untrusted permission admitted')
                except psycopg.errors.InsufficientPrivilege:
                    conn.rollback()
            # The reader has one aggregate RPC, no raw tables or write grants.
            denied(reader, 'select * from content_calendar')
            denied(reader, "insert into media_asset(id,gym_id) values('bad','gym')")
            denied(reader, 'select * from fixer_forward_media_original_registry_20261006')
            for role in ('service_role', 'anon', 'authenticated', 'generated_test_writer'):
                # NOLOGIN fixture roles use admin SET ROLE, like existing tests.
                if role in ('service_role', 'anon', 'authenticated'):
                    conn = psycopg.connect(dsn()); connections.append(conn)
                    conn.execute('set role '+role); conn.commit()
                else:
                    conn = psycopg.connect(dsn(role)); connections.append(conn)
                denied(conn, "select fixer_generated_owner_diagnostics_20261007('gym')")
            for role in ('generated_test_mixed', 'generated_test_history_mixed', 'postgres'):
                conn = psycopg.connect(dsn(role)); connections.append(conn)
                if role == 'generated_test_history_mixed':
                    # Reproduce the reviewed authority leak: this role really
                    # has raw historical SELECT/INSERT privileges and therefore
                    # cannot be the isolated generated read credential.
                    assert conn.execute("select has_table_privilege(current_user,"
                        "'fixer_forward_media_historical_original_20261007','SELECT'),"
                        "has_table_privilege(current_user,"
                        "'fixer_forward_media_historical_original_20261007','INSERT')").fetchone() == (True, True)
                    conn.rollback()
                try:
                    OwnerEvidenceReadStore(conn, role).diagnostics(req)
                    raise AssertionError('mixed/super owner accepted as reader')
                except GeneratedReceiptHold as exc:
                    assert str(exc) == 'generated_owner_read_identity_required'
                    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            # SET ROLE cannot hide a privileged connection behind reader identity.
            elevated = psycopg.connect(dsn()); connections.append(elevated)
            elevated.execute('set role generated_test_reader'); elevated.commit()
            try:
                OwnerEvidenceReadStore(elevated, 'generated_test_reader').diagnostics(req)
                raise AssertionError('privileged session masked with SET ROLE')
            except GeneratedReceiptHold as exc:
                assert str(exc) == 'generated_owner_read_identity_required'

            # Every current fleet calendar row is in the diagnostic revision,
            # including a cross-tenant undated unpublished/unknown record.
            rid = str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','failed','https://synthetic.invalid/unknown')", (rid,))
            after = store.diagnostics(req)
            assert after['history_revision'] != first['history_revision']
            assert after['database_calendar_rows'] == after['database_undated_rows'] == 1
            assert after['database_published_rows'] == 0 and after['history_complete'] is False
            sql("update content_calendar set published_at=now() where id=%s", (rid,))
            published = store.diagnostics(req)
            assert published['database_published_rows'] == 1
            assert published['history_revision'] != after['history_revision']
            sql("insert into media_source values('s','gym','gym_drive','SYNTHETIC',true)")
            sql("insert into media_asset values('a','s','gym','metadata only','https://synthetic.invalid/photo')")
            inventory = store.diagnostics(req)
            assert inventory['photo_inventory_revision'] != first['photo_inventory_revision']
            assert inventory['database_tenant_assets'] == 1 and inventory['database_tenant_sources'] == 1
            assert inventory['eligible_photo_count'] is None and inventory['photo_inventory_complete'] is False
            assert inventory['history_complete'] is False
            # A query releases both its snapshot and all advisory/row locks.
            assert reader.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            assert sql('select count(*) from pg_locks where pid=%s and locktype=\'advisory\'', (reader.info.backend_pid,))[0][0] == 0
            # Read transaction ownership is never stolen from the caller.
            reader.execute('select 1')
            try:
                store.diagnostics(req); raise AssertionError('active transaction replaced')
            except GeneratedReceiptHold as exc:
                assert str(exc) == 'generated_owner_read_transaction_required'
            assert reader.info.transaction_status != psycopg.pq.TransactionStatus.IDLE
            reader.rollback()
            assert sql('select count(*) from fixer_owner_photo_reservation_20261007')[0][0] == 0
            assert sql('select count(*) from fixer_forward_media_original_registry_20261006')[0][0] == 0
            print('PASS: PG17 read role isolation, default HOLD, fleet/undated/source revisions, no locks or grants')
        finally:
            for conn in connections: conn.close()
            if started:
                subprocess.run([str(pg/'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'],
                               check=True, capture_output=True, timeout=60)


if __name__ == '__main__': main()
