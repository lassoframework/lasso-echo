"""Real PG17 transport tests on disposable Unix-socket cluster; no live writes.

Run with existing /tmp/echo-forward-media-pg-20261007/bin/python. Synthetic
prepared tuples test transaction mechanics only, not historical trust readiness.
"""
import concurrent.futures
import io
import json
import multiprocessing
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
from agent import forward_media_attester as attester
from agent import forward_media_owner as owner
from agent import forward_media_owner_packet as packet
from agent import forward_media_owner_worker as worker
from agent.forward_media_owner_transport import DedicatedOwnerTransport
from agent.forward_media_observation_bridge import prepare
from agent.gym_media_index import materialization_observation

OWNER = 'transport_test_owner'
SOURCE = 'https://media.example.test/source.png'
IMAGE = 'https://media.example.test/image.png'


class Reader(owner.ObjectReader):
    def __init__(self):
        buf = io.BytesIO()
        Image.new('RGB', (1200, 400), 'blue').save(buf, 'PNG')
        self.source = buf.getvalue()
        self.recipe = attester.make_still_recipe('gbp_crop_4x3')
        self.image = attester.replay_still_recipe(self.source, self.recipe)['image_bytes']

    def read(self, url):
        if url == SOURCE or url.startswith(SOURCE + '/'):
            return self.source
        if url == IMAGE or url.startswith(IMAGE + '/'):
            return self.image
        raise KeyError('unknown synthetic URL')


def prepared(reader, asset):
    return packet.build_tuples({'schema_version': 2, 'tenant_id': 'gym',
        'source_asset_id': asset, 'source_url': SOURCE+'/'+asset, 'image_url': IMAGE+'/'+asset,
        'registry_evidence_ref': 'SYNTHETIC independent original receipt',
        'render_evidence_ref': 'SYNTHETIC render receipt', 'decision': 'hold_uncertain',
        'history_evidence_ref': 'SYNTHETIC independent audit',
        'production_evidence_ref': 'SYNTHETIC production receipt',
        'operation': 'render', 'render_recipe': reader.recipe}, reader)


def crash_after_stage(dsn, candidate, asset):
    import psycopg
    reader = Reader()
    conn = psycopg.connect(dsn)
    persistence = owner.ForwardMediaOwnerPersistence(conn, OWNER, reader)
    transport = DedicatedOwnerTransport(persistence)
    with transport.locked_current(candidate):
        tuples = prepared(reader, asset)
        transport.stage_authority(candidate, persistence, tuples)
        transport.record(candidate, {'status': 'persisted', 'decision': tuples[1].decision,
                                     'manifest_digest': tuples[2].manifest_digest})
        os._exit(19)  # Actual process death before final COMMIT, not a mocked exception.


class LostCommit:
    """Real connection with lost acknowledgment before/after selected COMMIT."""
    def __init__(self, conn, when, number=2):
        self.conn, self.when, self.number, self.calls = conn, when, number, 0

    @property
    def autocommit(self):
        return self.conn.autocommit

    @property
    def info(self):
        return self.conn.info

    def cursor(self):
        return self.conn.cursor()

    def rollback(self):
        return self.conn.rollback()

    def commit(self):
        self.calls += 1
        if self.calls == self.number and self.when == 'before':
            raise OSError('synthetic lost COMMIT response')
        self.conn.commit()
        if self.calls == self.number and self.when == 'after':
            raise OSError('synthetic lost COMMIT response')


def main():
    import psycopg
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    if not all((pg / t).exists() for t in ('postgres','initdb','pg_ctl')):
        raise SystemExit('BLOCKED: existing PG17 unavailable; no install attempted')
    assert ' 17.' in subprocess.check_output([str(pg / 'postgres'), '--version'], text=True)
    assert shutil.disk_usage('/tmp').free >= 5 * 1024**3
    reader = Reader()
    with tempfile.TemporaryDirectory(prefix='fm_owner_pg_',dir='/tmp') as temp:
        root = Path(temp)
        sock = root / 'socket'
        sock.mkdir()
        data = root / 'data'
        port = random.randint(41000,59000)
        subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],
                       check=True,capture_output=True,timeout=60)
        started = False
        try:
            subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),
                '-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],
                check=True,capture_output=True,timeout=60)
            started = True
            def dsn(role=OWNER):
                return f'host={sock} port={port} user={role} dbname=postgres'
            admin = psycopg.connect(dsn('postgres'), autocommit=True)
            def sql(query, params=()):
                with admin.cursor() as cur:
                    cur.execute(query, params if params else None)
                    return cur.fetchall() if cur.description else None
            sql('create role anon; create role authenticated; create role service_role;'
                'create table public.content_calendar(id uuid primary key,gym_id text,'
                'post_date date,account text,format text,gbp_location_id text,status text,'
                "variant_status text default 'active',published_at timestamptz,"
                'publish_claim_token uuid,publish_reservation_day date,late_post_id text,'
                'image_url text,thumbnail_url text,media_not_ready_reason text,caption text);'
                'create table public.media_asset(id text primary key,gym_id text,'
                'rendition_url text,content_hash text,used_count integer default 0);')
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_owner_transport_20261007.sql'):
                sql((ROOT/'migrations'/name).read_text())
            sql(f'create role {OWNER} login; grant fixer_forward_media_owner_20261006 to {OWNER};')
            for name in list(os.environ):
                if owner._FORBIDDEN_ENV_NAME.search(name) or name in worker._FORBIDDEN:
                    os.environ.pop(name)
            os.environ.update(FORWARD_MEDIA_OWNER_DSN=dsn(),FORWARD_MEDIA_OWNER_ROLE=OWNER,
                AGENT_FORWARD_MEDIA_OWNER_WORKER='true',AGENT_FORWARD_MEDIA_OWNER_TENANTS='gym')

            def lane(proxy=None):
                conn = psycopg.connect(dsn())
                persistence = owner.ForwardMediaOwnerPersistence(proxy(conn) if proxy else conn, OWNER, reader)
                return conn, persistence, DedicatedOwnerTransport(persistence)

            def candidate(tenant='gym'):
                rid, asset = str(uuid.uuid4()), 'asset-'+uuid.uuid4().hex
                sql('insert into media_asset(id,gym_id) values(%s,%s)', (asset,tenant))
                sql('insert into content_calendar(id,gym_id,post_date,status,variant_status,'
                    'source_media_asset_id,source_media_url,image_url,visual_group_key)'
                    " values(%s,%s,'2026-10-10','pending','active',%s,%s,%s,'group')",
                    (rid,tenant,asset,SOURCE+'/'+asset,IMAGE+'/'+asset))
                row = sql('select to_jsonb(r) from content_calendar r where id=%s',(rid,))[0][0]
                observation = materialization_observation(reader.source,reader.image,IMAGE+'/'+asset,
                    tenant=tenant,source_asset_id=asset,source_url=SOURCE+'/'+asset,recipe=reader.recipe,
                    bytes_fn=reader.read)
                item = prepare(row,[observation])
                result = sql('select fixer_record_forward_media_observation_20261007(%s,%s::jsonb,%s,%s)',
                    (rid,json.dumps(row),item['observation_json'],item['digest_input']))[0][0]
                return {k:result[k] for k in ('calendar_row_id','revision','observation_digest')}, asset

            def progress(c):
                return sql('select state,outcome from fixer_forward_media_owner_progress_20261007 '
                           'where calendar_row_id=%s',(c['calendar_row_id'],))
            def authority(asset):
                return sql('select count(*) from fixer_forward_media_original_registry_20261006 '
                           'where source_asset_id=%s',(asset,))[0][0]
            def stage(c,asset,p,t):
                tuples = prepared(reader,asset)
                result = t.stage_authority(c,p,tuples)
                assert result['replayed'] is False
                assert t.record(c,{'status':'persisted','decision':tuples[1].decision,
                                  'manifest_digest':tuples[2].manifest_digest}) is True

            c,asset = candidate()
            conn,p,t = lane()
            assert t.pending(('gym',),25) == [c]
            with t.locked_current(c) as current:
                assert current['revision'] == c['revision']
                assert current['asset']['id'] == asset and current['asset']['used_count'] == 0
                assert current['observation']['provenance_status'] == 'unverified'
                stage(c,asset,p,t)
                assert authority(asset) == 0 and progress(c) == [('quarantine',None)]
                with psycopg.connect(dsn('postgres'),autocommit=True) as editor:
                    editor.execute("set lock_timeout='200ms'")
                    for query,params in (
                        ('update content_calendar set caption=%s where id=%s',('changed',c['calendar_row_id'])),
                        ('update media_asset set gym_id=%s where id=%s',('foreign',asset))):
                        try:
                            editor.execute(query,params)
                            raise AssertionError('canonical row/asset changed while authority staged')
                        except psycopg.errors.LockNotAvailable:
                            pass
            assert authority(asset) == 1 and progress(c)[0][0] == 'final'
            assert t.pending(('gym',),25) == []
            # Final exact replay accepted; mutable final outcomes rejected.
            with conn.cursor() as cur:
                token = sql('select reservation_token from fixer_forward_media_owner_progress_20261007 '
                            'where calendar_row_id=%s',(c['calendar_row_id'],))[0][0]
                outcome = progress(c)[0][1]
                cur.execute('select fixer_forward_media_owner_record_20261007(%s,%s,%s,%s,%s::jsonb)',
                            (*worker._identity(c),token,json.dumps(outcome)))
                assert cur.fetchone()[0] is True
            conn.rollback()
            conn.close()

            # Existing caller transactions cannot be committed with a reservation.
            c,asset = candidate()
            conn,p,t = lane()
            conn.execute('select 1')
            try:
                with t.locked_current(c):
                    raise AssertionError('ambient transaction admitted')
            except worker.OwnerWorkerHold as exc:
                assert str(exc)=='owner_transaction_contract_required'
            assert progress(c)==[] and authority(asset)==0
            conn.rollback()
            # SQL failure after authority staging rolls back all three authority
            # tables and leaves the durable pre-work quarantine intact.
            try:
                with t.locked_current(c):
                    tuples = prepared(reader,asset)
                    t.stage_authority(c,p,tuples)
                    t.record(c,{'status':'persisted','decision':tuples[1].decision,
                                'manifest_digest':'sha256:'+'0'*64})
                raise AssertionError('invalid exact outcome accepted')
            except psycopg.errors.CheckViolation:
                pass
            assert authority(asset)==0 and progress(c)==[('quarantine',None)]
            conn.close()

            # Real concrete worker holds honest missing source binding; never clears zero-use.
            c,asset = candidate()
            conn,p,t = lane()
            report = worker.run_adapter(transport=t,persistence=p,reader=reader)
            assert report['status']=='partial_hold', report
            assert report['rows'][0]['reason']=='owner_asset_source_binding_missing', report
            assert authority(asset)==0 and progress(c)[0][0]=='final'
            assert t.pending(('gym',),25)==[]
            try:
                t.verified_history(prepared(reader,asset)[0])
                raise AssertionError('missing history unexpectedly accepted')
            except worker.OwnerWorkerHold as exc:
                assert str(exc)=='verified_history_transport_missing'
            conn.close()

            # A calendar race after discovery receives a static stale-revision hold.
            c,asset = candidate()
            conn,p,t = lane()
            sql('update content_calendar set caption=%s where id=%s',('new revision',c['calendar_row_id']))
            with t.locked_current(c) as current:
                assert current == {'hold_reason':'canonical_revision_changed'}
                t.record(c,{'status':'hold','reason':'canonical_revision_changed'})
            assert progress(c)[0][0]=='final' and authority(asset)==0
            conn.close()

            # Duplicate worker reservations yield one durable owner, no expiry retry.
            c,asset = candidate()
            def reserve():
                cc,pp,tt = lane()
                try:
                    with tt.locked_current(c):
                        tt.record(c,{'status':'hold','reason':'verified_history_transport_missing'})
                    return 'won'
                except worker.OwnerWorkerHold as exc:
                    return str(exc)
                finally:
                    cc.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:reserve(),range(2)))
            assert sorted(results)==['owner_manual_reconciliation_required','won'],results

            # Authority and staged outcome roll back together on abrupt process death.
            c,asset = candidate()
            child = multiprocessing.get_context('fork').Process(target=crash_after_stage,args=(dsn(),c,asset))
            child.start(); child.join(15)
            assert child.exitcode==19,child.exitcode
            assert authority(asset)==0 and progress(c)==[('quarantine',None)]
            conn,p,t = lane()
            assert c not in t.pending(('gym',),100)
            conn.close()

            # Both possible unknown COMMIT realities remain excluded; same transport stops.
            for when in ('before','after'):
                c,asset = candidate()
                conn,p,t=lane(lambda cc:LostCommit(cc,when))
                try:
                    with t.locked_current(c):
                        stage(c,asset,p,t)
                    raise AssertionError('uncertain final commit reported success')
                except owner.UncertainCommitError:
                    pass
                assert authority(asset)==(1 if when=='after' else 0)
                assert progress(c)[0][0]==('final' if when=='after' else 'quarantine')
                try:
                    t.pending(('gym',),25)
                    raise AssertionError('broken transport retried')
                except worker.OwnerWorkerHold as exc:
                    assert str(exc)=='owner_manual_reconciliation_required'
                conn.close()
                cc,pp,tt=lane()
                assert c not in tt.pending(('gym',),100)
                cc.close()

            # Unknown reservation COMMIT also never retries on that connection.
            c,asset=candidate()
            conn,p,t=lane(lambda cc:LostCommit(cc,'after',number=1))
            try:
                with t.locked_current(c):
                    raise AssertionError('unknown reservation entered work')
            except owner.UncertainCommitError:
                pass
            assert progress(c)==[('quarantine',None)] and authority(asset)==0
            conn.close()

            # First-write uncertainty BEFORE reservation commit leaves no
            # authority or object-read side effect. Originating transport stops;
            # only an actually aborted reservation permits safe fresh admission.
            c,asset=candidate()
            conn,p,t=lane(lambda cc:LostCommit(cc,'before',number=1))
            try:
                with t.locked_current(c):
                    raise AssertionError('unknown reservation entered work')
            except owner.UncertainCommitError:
                pass
            assert progress(c)==[] and authority(asset)==0
            try:
                t.pending(('gym',),100)
                raise AssertionError('uncertain initial writer retried')
            except worker.OwnerWorkerHold as exc:
                assert str(exc)=='owner_manual_reconciliation_required'
            conn.close()
            cc,pp,tt=lane()
            assert c in tt.pending(('gym',),100)  # Safe fresh admission: no authority attempted.
            cc.close()

            # An unresolved first reservation blocks a fresh worker on the
            # UNIQUE key. If first COMMIT eventually succeeds, fresh work holds;
            # if it aborts, fresh admission has no prior authority to replay.
            class UnresolvedReservation(LostCommit):
                def rollback(self):
                    raise OSError('synthetic rollback acknowledgment unavailable')
            for resolution in ('commit','rollback'):
                c,asset=candidate()
                conn,p,t=lane(lambda cc:UnresolvedReservation(cc,'before',number=1))
                try:
                    with t.locked_current(c):
                        raise AssertionError('uncertain first writer entered authority')
                except owner.UncertainCommitError:
                    pass
                assert t._active is None and t._broken is True and authority(asset)==0
                entered=threading.Event()
                def fresh_admission():
                    cc,pp,tt=lane()
                    try:
                        assert c in tt.pending(('gym',),100)
                        entered.set()
                        with tt.locked_current(c):
                            assert authority(asset)==0
                            tt.record(c,{'status':'hold','reason':'verified_history_transport_missing'})
                        return 'fresh_hold'
                    except worker.OwnerWorkerHold as exc:
                        return str(exc)
                    finally:
                        cc.close()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(fresh_admission)
                    assert entered.wait(3)
                    time.sleep(0.15)
                    assert not future.done(), 'fresh worker bypassed unresolved unique reservation'
                    getattr(conn,resolution)()
                    result=future.result(timeout=5)
                assert result==('owner_manual_reconciliation_required' if resolution=='commit' else 'fresh_hold')
                assert authority(asset)==0
                assert progress(c)[0][0]==('quarantine' if resolution=='commit' else 'final')
                conn.close()

            # Fair tenant interleaving, bounds, and no cross-tenant discovery.
            for _ in range(3): candidate('tenant-a')
            candidate('tenant-b')
            candidate('outside')
            conn,p,t=lane()
            discovered=t.pending(('tenant-a','tenant-b'),2)
            found={sql('select gym_id from content_calendar where id=%s',(r['calendar_row_id'],))[0][0]
                   for r in discovered}
            assert found=={'tenant-a','tenant-b'},found
            conn.close()
            for role in ('service_role','fixer_forward_media_attester_20261006','anon','authenticated'):
                for query in (
                    "select fixer_forward_media_owner_pending_20261007(array['gym'],1)",
                    'select * from fixer_forward_media_owner_progress_20261007'):
                    try:
                        sql(f'set role {role}')
                        sql(query)
                        raise AssertionError(f'{role} accessed owner transport')
                    except psycopg.errors.InsufficientPrivilege:
                        pass
                    finally:
                        sql('reset role')
            admin.close()
            print('PASS PG17: dedicated discovery, exact calendar/asset locks, authority+outcome atomicity, '
                  'real crash quarantine, before/after uncertain commit, duplicate workers, tenant fairness, '
                  'static source/history holds, least privilege')
        finally:
            if started:
                subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],
                    check=True,capture_output=True,timeout=60)


if __name__=='__main__':
    main()
