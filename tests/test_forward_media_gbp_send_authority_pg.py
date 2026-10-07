"""Disposable PG17 GBP final row/media/creative/destination authorization.

Synthetic freshly produced authority and bytes only. No production DSN/network
or provider sends. Run with an existing psycopg runtime; no installs performed.
"""
import concurrent.futures
from copy import deepcopy
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import forward_media_guard as guard, forward_media_attester as attester
from agent import forward_media_owner as owner, forward_media_prepare as prepare
from agent import gbp_worker as worker, media_host


class Bytes(owner.ObjectReader):
    def read(self, _url):
        return b"synthetic freshly produced original GBP still bytes"


def main(*, provider_fence=False):
    import psycopg
    from psycopg.types.json import Jsonb
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    with tempfile.TemporaryDirectory(prefix='fm_gbp_authority_pg_', dir='/tmp') as temp:
        root = Path(temp); sock = root / 'sock'; sock.mkdir(); data = root / 'data'
        port = random.randint(41000, 59000)
        subprocess.run([str(pg / 'initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        started = False
        try:
            subprocess.run([str(pg / 'pg_ctl'), '-D', str(data), '-l', str(root / 'pg.log'),
                            '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                           check=True, capture_output=True, timeout=60)
            started = True
            dsn = f'host={sock} port={port} dbname=postgres user=postgres'
            admin = psycopg.connect(dsn, autocommit=True)
            def sql(query, args=None):
                with admin.cursor() as cur:
                    cur.execute(query, args)
                    return cur.fetchall() if cur.description else None
            def lane(role='service_role'):
                conn = psycopg.connect(dsn)
                conn.execute('set role ' + role)
                return conn
            sql('create role anon; create role authenticated; create role service_role;'
                'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,'
                'format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,'
                'publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,'
                'thumbnail_url text,media_not_ready_reason text,caption text,pillar text,gbp_topic_type text,'
                'gbp_cta_type text,gbp_cta_url text,gbp_event jsonb,gbp_offer jsonb);'
                'create table gym_gbp_connections(portal_gym_key text,gbp_location_id text,'
                'zernio_account_id text,status text,unique(portal_gym_key,gbp_location_id));')
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_media_gbp_send_authority_20261008.sql'):
                sql((ROOT / 'migrations' / name).read_text())
            sql('grant select,insert,update on content_calendar to service_role;'
                'create role gbp_test_owner; grant fixer_forward_media_owner_20261006 to gbp_test_owner;'
                "insert into fixer_forward_media_claim_gate_20261006 values('gym',true);"
                "insert into gym_gbp_connections values('gym','locations/1','native-1','connected');")
            reader = Bytes(); url = 'https://media.example.test/original.jpg'
            original = prepare.register_original('gym', 'asset', url, reader.read(url), 'synthetic-original-proof')
            clearance = prepare.clear_history(original, 'cleared_unused', 'synthetic-independent-history',
                                              production_evidence_ref='synthetic-fresh-production')
            manifest = prepare.build_render_manifest(original, url, reader.read(url), 'same_object', 'synthetic-render')
            saved = {key: value for key, value in os.environ.items() if owner._FORBIDDEN_ENV_NAME.search(key)}
            for key in saved:
                os.environ.pop(key)
            prior_dsn = os.environ.get('FORWARD_MEDIA_OWNER_DSN')
            os.environ['FORWARD_MEDIA_OWNER_DSN'] = dsn
            try:
                with lane('gbp_test_owner') as conn:
                    owner.ForwardMediaOwnerPersistence(conn, 'gbp_test_owner', reader).persist(original, clearance, manifest)
            finally:
                os.environ.update(saved)
                if prior_dsn is None:
                    os.environ.pop('FORWARD_MEDIA_OWNER_DSN', None)
                else:
                    os.environ['FORWARD_MEDIA_OWNER_DSN'] = prior_dsn
            media_host.config.S3_PUBLIC_BASE_URL = 'https://media.example.test'

            def new_row(photo=False, trusted=True):
                rid, token = str(uuid4()), str(uuid4())
                sql("insert into content_calendar(id,gym_id,post_date,account,format,gbp_location_id,status,"
                    "variant_status,image_url,source_media_url,visual_group_key,source_media_asset_id,caption,pillar,"
                    "gbp_topic_type,gbp_cta_type,gbp_cta_url) values(%s,'gym','2026-10-10','googlebusiness',%s,"
                    "'locations/1','approved','active',%s,%s,'group','asset','A current approved gym caption.',"
                    "'Local Update','STANDARD','LEARN_MORE','https://gym.test/start')", (rid, 'photo' if photo else 'feed', url, url))
                with lane() as conn:
                    assert conn.execute('select fixer_bind_forward_media_manifest_20261006(%s)', (rid,)).fetchone()[0] is True
                sql("update content_calendar set status='publishing',publish_claim_token=%s,"
                    "publish_reservation_day='2026-10-10' where id=%s", (token, rid))
                if trusted:
                    revision = sql('select fixer_forward_media_attestation_request_20261006(%s)', (rid,))[0][0]['revision']
                    guard.attest(rid, revision, connection_factory=lambda: lane(guard.ROLE), read_bytes=reader.read,
                                 original_verifier=attester.make_original_verifier(lambda *a: original.row(), expected_revision=revision))
                row = sql('select to_jsonb(r) from content_calendar r where id=%s', (rid,))[0][0]
                expected = {key: row.get(key) for key in worker._GBP_SEND_CREATIVE}
                return rid, token, row, expected

            rpc = 'select fixer_authorize_gbp_forward_send_20261008(%s,%s,%s,%s,%s,%s)'
            def authorize(rid, token, expected, photo=False, native='native-1', role='service_role'):
                with lane(role) as conn:
                    return conn.execute(rpc, (rid, token, Jsonb(expected), native, 'locations/1',
                                              'gallery' if photo else 'post')).fetchone()[0]
            def denied(fn):
                try:
                    fn()
                except psycopg.Error:
                    return
                raise AssertionError('unsafe GBP authorization unexpectedly succeeded')

            for photo in (False, True):
                rid, token, row, expected = new_row(photo)
                assert authorize(rid, token, expected, photo) is True
                assert authorize(rid, token, expected, photo) is True  # exact same-token replay
                denied(lambda: authorize(rid, str(uuid4()), expected, photo))
                denied(lambda: authorize(rid, token, expected, photo, native='foreign-native'))
                denied(lambda: authorize(rid, token, expected, not photo))
                for key in worker._GBP_SEND_CREATIVE:
                    wrong = deepcopy(expected); wrong[key] = 'changed'
                    denied(lambda: authorize(rid, token, wrong, photo))
                # Caption/CTA changes do not alter media attestation revision;
                # the final RPC must still reject the previously approved body.
                sql("update content_calendar set caption='Changed after preflight',gbp_cta_url='https://changed.test' where id=%s", (rid,))
                denied(lambda: authorize(rid, token, expected, photo))
                sql('update content_calendar set caption=%s,gbp_cta_url=%s where id=%s', (row['caption'], row['gbp_cta_url'], rid))
                for field, value in (('status','needs_reconnect'), ('zernio_account_id','changed-native'), ('portal_gym_key','foreign')):
                    before = sql('select ' + field + " from gym_gbp_connections where gbp_location_id='locations/1'")[0][0]
                    sql('update gym_gbp_connections set ' + field + '=%s', (value,))
                    denied(lambda: authorize(rid, token, expected, photo))
                    sql('update gym_gbp_connections set ' + field + '=%s', (before,))
                for role in ('anon', 'authenticated', 'gbp_test_owner', guard.ROLE):
                    denied(lambda: authorize(rid, token, expected, photo, role=role))
            rid, token, row, expected = new_row(trusted=False)
            denied(lambda: authorize(rid, token, expected))
            rid, token, row, expected = new_row()
            sql("update fixer_forward_media_claim_gate_20261006 set enabled=false")
            denied(lambda: authorize(rid, token, expected))
            sql("update fixer_forward_media_claim_gate_20261006 set enabled=true")

            # Concurrent connection update commits while the RPC is blocked on
            # its connection share lock. Calendar stays locked; the fresh locked
            # destination then rejects. No claim receipt is staged for this row.
            blocker = psycopg.connect(dsn)
            blocker.execute("update gym_gbp_connections set zernio_account_id='changed-native'")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(authorize, rid, token, expected)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    waiting = sql("select count(*) from pg_stat_activity where wait_event_type='Lock' "
                                  "and query like 'select fixer_authorize_gbp_forward_send%'")[0][0]
                    if waiting:
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError('RPC did not reach destination lock')
                with psycopg.connect(dsn) as edit:
                    edit.execute("set local lock_timeout='200ms'")
                    denied(lambda: edit.execute("update content_calendar set caption='race' where id=%s", (rid,)))
                blocker.commit(); blocker.close()
                denied(lambda: future.result(timeout=5))
            assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006 where calendar_row_id=%s', (rid,))[0][0] == 0
            sql("update gym_gbp_connections set zernio_account_id='native-1'")
            assert authorize(rid, token, expected) is True

            # Exercise the worker's exact RPC transport with a real service role
            # DB transaction; no HTTP transport and no provider are involved.
            calls = []
            def post(endpoint, *, json, **kwargs):
                assert endpoint == 'rpc/fixer_authorize_gbp_forward_send_20261008'
                calls.append(json)
                result = authorize(json['p_calendar_row_id'], json['p_claim_token'], json['p_expected_creative'],
                                   json['p_send_kind'] == 'gallery', json['p_native_account_id'])
                return SimpleNamespace(status_code=200, json=lambda: result)
            store = SimpleNamespace(_client=lambda: SimpleNamespace(post=post),
                                    _rest=lambda path: path, _headers=lambda *a: {})
            assert worker._atomic_gbp_send_hold(store, row, {'zernio_account_id':'native-1','gbp_location_id':'locations/1'}, token, 'post') is None
            assert len(calls) == 1
            if provider_fence:
                sql((ROOT / 'migrations' / 'DRAFT_fixer_gbp_provider_attempt_fence_20261009.sql').read_text())
                unrelated = str(uuid4())
                sql("insert into content_calendar(id,account,status) values(%s,'instagram','approved')", (unrelated,))
                with psycopg.connect(dsn) as edit:
                    edit.execute('set transaction isolation level repeatable read')
                    edit.execute("update content_calendar set caption='unrelated' where id=%s", (unrelated,))
                    edit.execute('delete from content_calendar where id=%s', (unrelated,))
                rid, token, row, expected = new_row()
                stale = psycopg.connect(dsn)
                stale.execute('set transaction isolation level repeatable read')
                stale.execute('select id from content_calendar where id=%s', (rid,))
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: authorize(rid, token, expected), range(2)))
                assert sorted(results) == [False, True]
                denied(lambda: stale.execute("update content_calendar set status='approved',publish_claim_token=null where id=%s", (rid,)))
                stale.rollback(); stale.close()
                with psycopg.connect(dsn) as edit:
                    edit.execute('set transaction isolation level repeatable read')
                    denied(lambda: edit.execute("update content_calendar set caption='revoked' where id=%s", (rid,)))
                assert authorize(rid, token, expected) is False
                assert sql('select count(*) from fixer_gbp_provider_attempt_20261009 where claim_token=%s', (token,))[0][0] == 1
                denied(lambda: sql("update content_calendar set status='approved',publish_claim_token=null where id=%s", (rid,)))
                denied(lambda: sql("update content_calendar set publish_claim_token=%s where id=%s", (str(uuid4()),rid)))
                denied(lambda: sql("update content_calendar set caption='admin revoked' where id=%s", (rid,)))
                denied(lambda: sql('delete from content_calendar where id=%s', (rid,)))
                denied(lambda: sql('truncate content_calendar'))
                denied(lambda: sql('delete from fixer_gbp_provider_attempt_20261009'))
                gallery_id, gallery_token, gallery_row, gallery_expected = new_row(photo=True)
                assert authorize(gallery_id, gallery_token, gallery_expected, True) is True
                assert authorize(gallery_id, gallery_token, gallery_expected, True) is False
                denied(lambda: sql("update content_calendar set status='approved',publish_claim_token=null where id=%s", (gallery_id,)))
                sql("update content_calendar set status='failed',publish_claim_token=null where id=%s", (gallery_id,))
                # A gate/connection edit controls future attempts. The durable grant
                # still records the exact destination/creative for the committed one.
                sql("update fixer_forward_media_claim_gate_20261006 set enabled=false")
                sql("update gym_gbp_connections set zernio_account_id='revoked'")
                assert sql('select native_account_id from fixer_gbp_provider_attempt_20261009 where claim_token=%s', (token,))[0][0] == 'native-1'
                sql("update content_calendar set status='published',publish_claim_token=null,late_post_id='provider-confirmed',published_at=now() where id=%s", (rid,))
                assert sql('select count(*) from fixer_gbp_provider_attempt_20261009 where claim_token=%s', (token,))[0][0] == 1
                print('PASS: durable provider attempt replay denial, lease/creative/delete/truncate guards and terminal settlement')
            print('PASS: PG17 GBP post/gallery authority; full creative/token/media/destination drift held; '
                  'same-token replay, role/gate isolation, concurrent row+destination locks and worker RPC passed')
            admin.close()
        finally:
            if started:
                subprocess.run([str(pg / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                               check=False, capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
