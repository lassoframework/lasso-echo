"""Disposable PG17 availability regression; run as a standalone script.

Uses installed PostgreSQL tools and psycopg only. No existing DB or production
DSN is read. The blocked row stays locked while an unrelated tenant succeeds.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import time
import uuid

from agent import forward_media_guard as guard
from agent import forward_media_owner as owner_module
from agent.forward_media_lane import RUNTIME_NAMES
from agent.forward_media_prepare import prepare_generated_original, build_render_manifest

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'deadline_test_owner'


def main():
    import psycopg

    for tool in ('initdb', 'pg_ctl', 'psql'):
        assert shutil.which(tool), f'BLOCKED: installed {tool} required'
    with tempfile.TemporaryDirectory(prefix='forward_deadlines_pg_') as tmp:
        root = Path(tmp)
        sock = root / 'sock'
        sock.mkdir()
        data = root / 'data'
        port = random.randint(41000, 59000)
        subprocess.run(['initdb', '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(['pg_ctl', '-D', str(data), '-l', str(root / 'pg.log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        dsn = f'host={sock} port={port} user=postgres dbname=postgres'
        try:
            with psycopg.connect(dsn, autocommit=True) as admin:
                def sql(query, params=None):
                    cur = admin.execute(query, params)
                    return cur.fetchone()[0] if cur.description else None

                assert int(sql('show server_version_num')) // 10000 == 17
                sql('create role anon; create role authenticated; create role service_role; '
                    'create table content_calendar(id uuid primary key,gym_id text,post_date date,'
                    'account text,format text,gbp_location_id text,status text,variant_status text,'
                    'published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,'
                    'late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text);')
                sql((ROOT / 'migrations/DRAFT_fixer_forward_media_claim_20261006.sql').read_text())
                sql(f'create role {OWNER} login; grant fixer_forward_media_owner_20261006 to {OWNER};')

                def seed(*, bind=False, attested=True):
                    row_id, token, evidence = map(str, (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()))
                    tenant, asset = 'gym_' + uuid.uuid4().hex, 'asset_' + uuid.uuid4().hex
                    url, fp = 'https://owned.example/' + uuid.uuid4().hex, 'md5:' + uuid.uuid4().hex
                    digest = 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
                    sql('insert into fixer_forward_media_original_registry_20261006 '
                        'values(%s,%s,%s,%s,10,%s,now())', (tenant, asset, url, fp, 'registry'))
                    sql('insert into fixer_forward_media_history_clearance_20261006 '
                        'values(%s,%s,%s,%s,10,%s,%s,%s,now())',
                        (tenant, asset, url, fp, 'registry', 'cleared_unused', 'history'))
                    sql('insert into fixer_forward_media_render_manifest_20261006 '
                        '(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,'
                        'operation,render_evidence_ref) values(%s,%s,%s,%s,%s,10,%s,%s)',
                        (digest, tenant, asset, url, fp, 'same_object', 'render'))
                    sql('insert into content_calendar(id,gym_id,post_date,status,variant_status,'
                        'publish_claim_token,publish_reservation_day,image_url,source_media_url,'
                        'visual_group_key,source_media_asset_id,render_manifest_digest) '
                        "values(%s,%s,'2026-10-10',%s,'active',%s,'2026-10-10',%s,%s,%s,%s,%s)",
                        (row_id, tenant, 'draft' if bind else 'publishing', None if bind else token,
                         url, url, 'group_' + uuid.uuid4().hex, asset, None if bind else digest))
                    sql('insert into fixer_forward_media_claim_gate_20261006 values(%s,true)', (tenant,))
                    revision = None if bind else sql(
                        'select fixer_forward_media_attestation_request_20261006(%s)->>\'revision\'', (row_id,))
                    attestation = ('select fixer_attest_forward_media_20261006(%s,%s,%s,%s,10,%s,10,null,null,%s,%s)',
                                   (row_id, revision, evidence, fp, fp, 'same_object', 'test-evidence'))
                    if attested and not bind:
                        sql(*attestation)
                    claim = ('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',
                             (row_id, token, evidence, revision))
                    return row_id, claim, attestation, evidence

                def run(role, command, name):
                    started = time.monotonic()
                    with psycopg.connect(dsn, application_name=name) as conn:
                        # Deliberately remove caller deadlines: SQL functions
                        # themselves must bound lock acquisition for REST callers.
                        conn.execute('set lock_timeout=0; set statement_timeout=0')
                        conn.execute('set role ' + role)
                        try:
                            result = conn.execute(*command).fetchone()[0]
                            conn.commit()
                            return result, time.monotonic() - started
                        except psycopg.Error as exc:
                            conn.rollback()
                            return exc.sqlstate, time.monotonic() - started

                def wait_for_lock(name):
                    end = time.monotonic() + 3
                    while not sql("select exists(select 1 from pg_stat_activity "
                                  "where application_name=%s and wait_event_type='Lock')", (name,)):
                        assert time.monotonic() < end, f'{name} never reached its blocked row'
                        time.sleep(.02)
                    assert sql("select exists(select 1 from pg_locks l join pg_stat_activity a "
                               "on a.pid=l.pid where a.application_name=%s "
                               "and l.locktype='advisory' and l.granted)", (name,))

                for mode in ('attester', 'claim', 'binder'):
                    row_id, claim_a, attest_a, evidence = seed(bind=mode == 'binder', attested=mode == 'claim')
                    _, claim_b, _, _ = seed()
                    command = (attest_a if mode == 'attester' else claim_a if mode == 'claim' else
                               ('select fixer_bind_forward_media_manifest_20261006(%s)', (row_id,)))
                    role = guard.ROLE if mode == 'attester' else 'service_role'
                    with psycopg.connect(dsn) as holder, ThreadPoolExecutor(max_workers=2) as pool:
                        holder.execute('select id from content_calendar where id=%s for update', (row_id,))
                        blocked = pool.submit(run, role, command, 'blocked_' + mode)
                        wait_for_lock('blocked_' + mode)
                        unrelated = pool.submit(run, 'service_role', claim_b, 'unrelated_' + mode)
                        error, elapsed = blocked.result(timeout=8)
                        assert error == '55P03' and 4.5 <= elapsed < 8, (mode, error, elapsed)
                        result, other_elapsed = unrelated.result(timeout=8)
                        assert result is True and other_elapsed < 8, (mode, result, other_elapsed)
                        # Holder still owns A. No failed attempt forged durable proof.
                        assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006 '
                                   'where calendar_row_id=%s', (row_id,)) == 0
                        if mode == 'attester':
                            assert sql('select count(*) from fixer_forward_media_lineage_20261006 '
                                       'where evidence_id=%s', (evidence,)) == 0
                        if mode == 'binder':
                            assert sql('select render_manifest_digest is null from content_calendar where id=%s', (row_id,))
                        holder.rollback()
                    retry, _ = run(role, command, 'retry_' + mode)
                    assert retry == (uuid.UUID(evidence) if mode == 'attester' else True)
                    if mode == 'claim':
                        assert run(role, command, 'exact_retry_claim')[0] is True
                        assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006 '
                                   'where calendar_row_id=%s', (row_id,)) == 1
                    print(f'PASS: {mode} SQL lock timeout {elapsed:.2f}s, tenant B progress, rollback/retry', flush=True)

                # Even a raw owner INSERT with no connection deadline must
                # bound its trigger's acquisition of the global graph lock.
                trigger_tenant, trigger_asset = 'trigger_tenant', 'trigger_asset'
                sql('insert into fixer_forward_media_original_registry_20261006 '
                    "values(%s,%s,'https://owned.example/trigger',%s,10,'registry',now())",
                    (trigger_tenant, trigger_asset, 'md5:' + 'f' * 32))
                trigger_command = (
                    'insert into fixer_forward_media_history_clearance_20261006 '
                    "values(%s,%s,'https://owned.example/trigger',%s,10,'registry',"
                    "'cleared_unused','history',now()) returning true",
                    (trigger_tenant, trigger_asset, 'md5:' + 'f' * 32))
                with psycopg.connect(dsn) as holder, ThreadPoolExecutor(max_workers=1) as pool:
                    holder.execute("select pg_advisory_xact_lock(hashtextextended('fixer_forward_graph_20261006',0))")
                    error, elapsed = pool.submit(run, OWNER, trigger_command, 'blocked_trigger').result(timeout=8)
                    assert error == '55P03' and 4.5 <= elapsed < 8
                    assert sql('select count(*) from fixer_forward_media_history_clearance_20261006 '
                               'where tenant_id=%s', (trigger_tenant,)) == 0
                    holder.rollback()
                assert run(OWNER, trigger_command, 'trigger_retry')[0] is True
                print(f'PASS: owner graph trigger acquisition timeout {elapsed:.2f}s, rollback/retry', flush=True)

                # Outer owner INSERT takes its trigger's graph lock, returns
                # from that trigger, then waits on the original FK key. This
                # wait must be bounded by the connection/transaction settings.
                original_bytes = b'synthetic unique original ' + uuid.uuid4().bytes
                original, clearance = prepare_generated_original(
                    'owner_tenant', 'owner_asset', 'https://owned.example/owner-original',
                    original_bytes, 'generated', 'registry', 'history')
                first_manifest = build_render_manifest(original, original.source_url, original_bytes,
                                                       'same_object', 'render')
                extension = build_render_manifest(original, 'https://owned.example/owner-extension',
                                                  original_bytes, 'rehost', 'extension')
                class Reader(owner_module.ObjectReader):
                    def read(self, url):
                        assert url in (original.source_url, extension.image_url)
                        return original_bytes
                old_env = dict(os.environ)
                os.environ.clear()
                os.environ.update({k: v for k, v in old_env.items() if k in RUNTIME_NAMES})
                os.environ.update(FORWARD_MEDIA_OWNER_DSN=dsn.replace('user=postgres', f'user={OWNER}'),
                                  FORWARD_MEDIA_OWNER_ROLE=OWNER)
                try:
                    owner = owner_module.ForwardMediaOwnerPersistence.connect_from_environment(reader=Reader())
                    assert owner._conn.execute('show lock_timeout').fetchone() == ('5s',)
                    assert owner._conn.execute('show statement_timeout').fetchone() == ('20s',)
                    owner.persist(original, clearance, first_manifest)
                    _, claim_b, _, _ = seed()
                    with psycopg.connect(dsn) as holder, ThreadPoolExecutor(max_workers=2) as pool:
                        holder.execute('select source_asset_id from fixer_forward_media_original_registry_20261006 '
                                       'where tenant_id=%s for update', (original.tenant_id,))
                        owner._conn.execute("set application_name='blocked_owner'")
                        owner._conn.execute('set local lock_timeout=0; set local statement_timeout=0')
                        start = time.monotonic()
                        blocked = pool.submit(owner.persist, original, clearance, extension)
                        wait_for_lock('blocked_owner')
                        unrelated = pool.submit(run, 'service_role', claim_b, 'unrelated_owner')
                        try:
                            blocked.result(timeout=8)
                        except psycopg.Error as exc:
                            assert exc.sqlstate == '55P03'
                        else:
                            raise AssertionError('outer owner INSERT did not time out')
                        elapsed = time.monotonic() - start
                        assert 4.5 <= elapsed < 8
                        assert unrelated.result(timeout=8)[0] is True
                        assert sql('select count(*) from fixer_forward_media_render_manifest_20261006 '
                                   'where manifest_digest=%s', (extension.manifest_digest,)) == 0
                        holder.rollback()
                    assert owner.persist(original, clearance, extension)['replayed'] is False
                    assert owner.persist(original, clearance, extension)['replayed'] is True
                    owner._conn.close()
                    print(f'PASS: outer owner INSERT timeout {elapsed:.2f}s, tenant B progress, rollback/replay', flush=True)
                    # Attester startup deadlines are active before its invoking
                    # SELECT, not installed inside the server function.
                    os.environ.pop('FORWARD_MEDIA_OWNER_DSN')
                    os.environ.pop('FORWARD_MEDIA_OWNER_ROLE')
                    os.environ.update(AGENT_FORWARD_MEDIA_GUARD='true',
                                      AGENT_FORWARD_MEDIA_ATTESTER_DSN=dsn,
                                      AGENT_FORWARD_MEDIA_ATTESTER_ROLE=guard.ROLE)
                    # Enable LOGIN only in this disposable cluster, so the
                    # production constructor's exact-role check runs unchanged.
                    sql('alter role ' + guard.ROLE + ' login')
                    os.environ['AGENT_FORWARD_MEDIA_ATTESTER_DSN'] = dsn.replace('user=postgres', 'user=' + guard.ROLE)
                    conn = guard._connect()
                    assert conn.execute('show lock_timeout').fetchone() == ('5s',)
                    assert conn.execute('show statement_timeout').fetchone() == ('20s',)
                    conn.rollback()
                    start = time.monotonic()
                    try:
                        conn.execute('select pg_sleep(21)')
                    except psycopg.Error as exc:
                        assert exc.sqlstate == '57014'
                    else:
                        raise AssertionError('invoking SELECT statement deadline missing')
                    assert 19.5 <= time.monotonic() - start < 23
                    conn.rollback()
                    conn.close()
                    print('PASS: dedicated attester invoking SELECT cancels at 20s; owner/attester startup deadlines', flush=True)
                finally:
                    os.environ.clear()
                    os.environ.update(old_env)
        finally:
            subprocess.run(['pg_ctl', '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
