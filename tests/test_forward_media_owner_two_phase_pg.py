"""Real PG17 slow-read concurrency and source→held authority owner integration.

Only disposable socket DB and synthetic byte readers. No live writes/sends.
"""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
from agent import forward_media_owner as owner
from agent import forward_media_owner_worker as worker
from agent import forward_media_owner_packet as packet
from agent import forward_media_attester as attester
from agent.forward_media_owner_transport import DedicatedOwnerTransport, FrozenObjectReader
from agent.forward_media_observation_bridge import prepare
from agent.gym_media_index import materialization_observation
from tests.test_forward_media_owner_transport_pg import LostCommit

OWNER = 'two_phase_test_owner'
BASE = 'https://media.example.test/'
FOLDER = 'FolderOriginal1234567'


def png(color):
    buf = io.BytesIO()
    Image.new('RGB',(1200,400),color).save(buf,'PNG')
    return buf.getvalue()


class Drive:
    def __init__(self, asset, data):
        self.asset, self.data = asset, data

    def metadata(self, file_id):
        if file_id == FOLDER:
            return {'id':FOLDER,'mimeType':'application/vnd.google-apps.folder',
                    'trashed':False,'version':'1'}
        assert file_id==self.asset
        return {'id':file_id,'mimeType':'image/png','trashed':False,'version':'3',
                'parents':[FOLDER],'size':str(len(self.data)),
                'md5Checksum':hashlib.md5(self.data).hexdigest()}

    def original_bytes(self, file_id):
        assert file_id==self.asset
        return self.data


class SlowReader(owner.ObjectReader):
    def __init__(self, objects, first_url, entered, release):
        self.objects, self.first_url = objects, first_url
        self.entered, self.release = entered, release
        self.conn = None
        self.reads = []

    def read(self, url):
        from psycopg.pq import TransactionStatus
        # This catches hidden persistence rereads during the final graph tx.
        assert self.conn.info.transaction_status==TransactionStatus.IDLE, 'remote I/O during DB transaction'
        self.reads.append(url)
        if url==self.first_url:
            self.entered.set()
            assert self.release.wait(10), 'slow reader was not released'
        return self.objects[url]


def main():
    import psycopg
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free > 5*1024**3
    source_bytes = png('blue')
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    image_bytes = attester.replay_still_recipe(source_bytes,recipe)['image_bytes']
    with tempfile.TemporaryDirectory(prefix='fm_two_phase_pg_',dir='/tmp') as temp:
        root=Path(temp)
        sock=root/'sock'; sock.mkdir()
        data=root/'data'
        port=random.randint(41000,59000)
        subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
        started=False
        try:
            subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),
                '-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
            started=True
            def dsn(role=OWNER): return f'host={sock} port={port} dbname=postgres user={role}'
            admin=psycopg.connect(dsn('postgres'),autocommit=True)
            def sql(query, values=None):
                with admin.cursor() as cur:
                    cur.execute(query,values)
                    return cur.fetchall() if cur.description else None
            sql('create role anon; create role authenticated; create role service_role;'
                'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,'
                'format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,'
                'publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,'
                'thumbnail_url text,media_not_ready_reason text,caption text);'
                'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
                'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql',
                         'DRAFT_fixer_forward_media_owner_transport_20261007.sql'):
                sql((ROOT/'migrations'/name).read_text())
            sql(f'create role {OWNER} login; grant fixer_forward_media_owner_20261006 to {OWNER};')
            for name in list(os.environ):
                if owner._FORBIDDEN_ENV_NAME.search(name) or name in worker._FORBIDDEN:
                    os.environ.pop(name)
            os.environ.update(FORWARD_MEDIA_OWNER_DSN=dsn(),FORWARD_MEDIA_OWNER_ROLE=OWNER,
                              AGENT_FORWARD_MEDIA_OWNER_WORKER='true',AGENT_FORWARD_MEDIA_OWNER_TENANTS='gym')
            sql('insert into media_source values(%s,%s,%s,%s,true)',('source','gym','gym_drive',FOLDER))
            def candidate():
                rid,asset=str(uuid.uuid4()),uuid.uuid4().hex
                source_url,image_url=BASE+'source/'+asset,BASE+'render/'+asset
                sql('insert into media_asset values(%s,%s,%s,%s,%s)',(asset,'source','gym','metadata-only',image_url))
                sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,visual_group_key,source_media_asset_id,source_media_url,image_url) values(%s,'gym','2026-10-10','pending','active','group',%s,%s,%s)",(rid,asset,source_url,image_url))
                row=sql('select to_jsonb(r) from content_calendar r where id=%s',(rid,))[0][0]
                observation=materialization_observation(source_bytes,image_bytes,image_url,
                    tenant='gym',source_asset_id=asset,source_url=source_url,recipe=recipe,
                    bytes_fn=lambda url:source_bytes if url==source_url else image_bytes)
                item=prepare(row,[observation])
                result=sql('select fixer_record_forward_media_observation_20261007(%s,%s::jsonb,%s,%s)',
                    (rid,json.dumps(row),item['observation_json'],item['digest_input']))[0][0]
                return {k:result[k] for k in ('calendar_row_id','revision','observation_digest')},asset,source_url,image_url

            def worker_future(pool,c,asset,source_url,image_url,uncertain=None):
                entered,release=threading.Event(),threading.Event()
                reader=SlowReader({source_url:source_bytes,image_url:image_bytes},source_url,entered,release)
                def run():
                    with psycopg.connect(dsn()) as conn:
                        reader.conn=conn
                        p=owner.ForwardMediaOwnerPersistence(LostCommit(conn,uncertain) if uncertain else conn,OWNER,reader)
                        return worker.run_adapter(transport=DedicatedOwnerTransport(p),persistence=p,
                                                  reader=reader,drive_reader=Drive(asset,source_bytes))
                future=pool.submit(run)
                assert entered.wait(5), 'worker never entered remote read'
                return future,release,reader

            # An unrelated valid owner→binder→attest→claim chain completes while
            # source read is intentionally stalled. These are real PG functions.
            c,asset,source_url,image_url=candidate()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future,release,reader=worker_future(pool,c,asset,source_url,image_url)
                try:
                    assert not future.done()
                    sql("set lock_timeout='500ms'; set statement_timeout='2s'")
                    other_bytes,other_url=png('red'),BASE+'unrelated-original.png'
                    local=FrozenObjectReader({other_url:other_bytes})
                    tuples=packet.build_tuples({'schema_version':1,'tenant_id':'unrelated',
                        'source_asset_id':'unrelated-asset','source_url':other_url,
                        'registry_evidence_ref':'SYNTHETIC unrelated original',
                        'render_evidence_ref':'SYNTHETIC unrelated replay',
                        'decision':'cleared_unused','history_evidence_ref':'SYNTHETIC unrelated audit',
                        'production_evidence_ref':'SYNTHETIC fresh production','operation':'same_object'},local)
                    with psycopg.connect(dsn()) as cc:
                        cc.execute("set lock_timeout='500ms'; set statement_timeout='2s'")
                        owner.ForwardMediaOwnerPersistence(cc,OWNER,local).persist(*tuples)
                    other,token=str(uuid.uuid4()),str(uuid.uuid4())
                    sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,visual_group_key,source_media_asset_id,source_media_url,image_url) values(%s,'unrelated','2026-10-10','approved','active','other-group','unrelated-asset',%s,%s)",(other,other_url,other_url))
                    sql('set role service_role')
                    assert sql('select fixer_bind_forward_media_manifest_20261006(%s)',(other,))[0][0] is True
                    sql('reset role')
                    sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day='2026-10-10' where id=%s",(token,other))
                    revision=sql('select fixer_forward_media_attestation_request_20261006(%s)->>\'revision\'',(other,))[0][0]
                    evidence=str(uuid.uuid4())
                    fp='md5:'+hashlib.md5(other_bytes).hexdigest()
                    sql('set role fixer_forward_media_attester_20261006')
                    sql('select fixer_attest_forward_media_20261006(%s,%s,%s,%s,%s,%s,%s,null,null,%s,%s)',
                        (other,revision,evidence,fp,len(other_bytes),fp,len(other_bytes),'same_object','SYNTHETIC attestation'))
                    sql('reset role')
                    sql("insert into fixer_forward_media_claim_gate_20261006 values('unrelated',true)")
                    sql('set role service_role')
                    assert sql('select fixer_claim_forward_media_20261006(%s,%s,%s,%s)',(other,token,evidence,revision))[0][0] is True
                    sql('reset role')
                    assert not future.done(), 'remote read finished before concurrency proof'
                finally:
                    release.set()
                report=future.result(timeout=10)
            assert report['status']=='partial_hold',report
            assert report['rows'][0]['status']=='hold' and report['rows'][0]['decision']=='hold_uncertain',report
            assert reader.reads==[source_url,image_url],reader.reads
            assert sql('select count(*) from fixer_forward_media_source_receipt_20261007')[0][0]==1
            assert sql('select count(*) from fixer_forward_media_history_query_receipt_20261007')[0][0]==1
            assert sql('select count(*) from fixer_forward_media_original_registry_20261006 where source_asset_id=%s',(asset,))[0][0]==0
            assert sql('select count(*) from fixer_forward_media_history_clearance_20261006 where source_asset_id=%s',(asset,))[0][0]==0
            assert sql('select count(*) from fixer_forward_media_render_manifest_20261006 where source_asset_id=%s',(asset,))[0][0]==0
            progress=sql('select state,outcome from fixer_forward_media_owner_progress_20261007 where calendar_row_id=%s',(c['calendar_row_id'],))[0]
            assert progress[0]=='final' and progress[1]['status']=='hold' and progress[1]['decision']=='hold_uncertain'
            assert progress[1]['source_receipt_ref']==report['rows'][0]['source_receipt_ref']
            assert progress[1]['history_evidence_ref']==report['rows'][0]['history_evidence_ref']
            sql('set role service_role')
            try:
                sql('select fixer_bind_forward_media_manifest_20261006(%s)',(c['calendar_row_id'],))
                raise AssertionError('hold_uncertain released for binding')
            except psycopg.errors.CheckViolation:
                pass
            finally:
                sql('reset role')

            # Canonical row/source mutation is allowed during remote I/O and
            # becomes a durable exact final hold, with no stale authority append.
            for mutation,reason in [('calendar','canonical_revision_changed'),('source','owner_source_binding_changed')]:
                c,asset,source_url,image_url=candidate()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future,release,reader=worker_future(pool,c,asset,source_url,image_url)
                    try:
                        if mutation=='calendar':
                            sql('update content_calendar set caption=%s where id=%s',('changed',c['calendar_row_id']))
                        else:
                            sql("update media_source set active=false where id='source'")
                    finally:
                        release.set()
                    report=future.result(timeout=10)
                assert report['rows'][0]['reason']==reason,report
                assert sql('select count(*) from fixer_forward_media_source_receipt_20261007 where source_asset_id=%s',(asset,))[0][0]==0
                assert sql('select count(*) from fixer_forward_media_original_registry_20261006 where source_asset_id=%s',(asset,))[0][0]==0
                assert sql('select state from fixer_forward_media_owner_progress_20261007 where calendar_row_id=%s',(c['calendar_row_id'],))[0][0]=='final'
                sql("update media_source set active=true where id='source'")
            # A lost final COMMIT acknowledgment never reports success. Both
            # realities preserve all-or-none source/history/progress receipts.
            for when in ('before','after'):
                c,asset,source_url,image_url=candidate()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future,release,reader=worker_future(pool,c,asset,source_url,image_url,uncertain=when)
                    release.set()
                    report=future.result(timeout=10)
                assert report=={'status':'hold','reason':'uncertain_authority_commit','rows':[]},report
                expected=1 if when=='after' else 0
                assert sql('select count(*) from fixer_forward_media_source_receipt_20261007 where source_asset_id=%s',(asset,))[0][0]==expected
                assert sql('select count(*) from fixer_forward_media_history_query_receipt_20261007 q join fixer_forward_media_source_receipt_20261007 s on s.receipt_ref=q.source_receipt_ref where s.source_asset_id=%s',(asset,))[0][0]==expected
                assert sql('select state from fixer_forward_media_owner_progress_20261007 where calendar_row_id=%s',(c['calendar_row_id'],))[0][0]==('final' if expected else 'quarantine')
                assert sql('select count(*) from fixer_forward_media_history_clearance_20261006 where source_asset_id=%s',(asset,))[0][0]==0
            # Operational one-pass factory uses the REAL environment-based owner
            # connection. Only object transports are synthetic fixture readers.
            # It must close that connection on normal HOLD and uncertain COMMIT.
            real_connect = owner.ForwardMediaOwnerPersistence.connect_from_environment
            opened = []
            class ClosingLostCommit(LostCommit):
                def close(self):
                    return self.conn.close()
            for when in (None, 'before', 'after'):
                c,asset,source_url,image_url=candidate()
                reads=[]
                class RuntimeReader(owner.ObjectReader):
                    def read(self, url):
                        assert opened[-1].info.transaction_status==psycopg.pq.TransactionStatus.IDLE
                        reads.append(url)
                        return {source_url:source_bytes,image_url:image_bytes}[url]
                reader=RuntimeReader()
                def connect(*, reader):
                    persistence=real_connect(reader=reader)
                    opened.append(persistence._conn)
                    if when:
                        persistence._conn=ClosingLostCommit(persistence._conn,when)
                    return persistence
                with patch.object(owner,'HostedObjectReader',return_value=reader), \
                     patch.object(owner.ForwardMediaOwnerPersistence,'connect_from_environment',side_effect=connect), \
                     patch('agent.forward_media_source_verifier.OriginalDriveReader',return_value=Drive(asset,source_bytes)):
                    report=worker.run_once()
                assert opened[-1].closed, 'runtime leaked its owner connection'
                assert reads==[source_url,image_url]
                if when:
                    assert report=={'status':'hold','reason':'uncertain_authority_commit','rows':[]},report
                else:
                    assert report['status']=='partial_hold' and len(report['rows'])==1,report
                    assert report['rows'][0]['decision']=='hold_uncertain',report
                expected=0 if when=='before' else 1
                assert sql('select count(*) from fixer_forward_media_source_receipt_20261007 where source_asset_id=%s',(asset,))[0][0]==expected
                assert sql('select count(*) from fixer_forward_media_history_query_receipt_20261007 q join fixer_forward_media_source_receipt_20261007 s on s.receipt_ref=q.source_receipt_ref where s.source_asset_id=%s',(asset,))[0][0]==expected
                assert sql('select state from fixer_forward_media_owner_progress_20261007 where calendar_row_id=%s',(c['calendar_row_id'],))[0][0]==('final' if expected else 'quarantine')
                outcome=sql('select outcome from fixer_forward_media_owner_progress_20261007 where calendar_row_id=%s',(c['calendar_row_id'],))[0][0]
                if expected:
                    assert outcome['status']=='hold' and outcome['decision']=='hold_uncertain'
                    assert outcome['source_receipt_ref']==sql('select receipt_ref from fixer_forward_media_source_receipt_20261007 where source_asset_id=%s',(asset,))[0][0]
                    assert sql('select count(*) from fixer_forward_media_history_query_receipt_20261007 where source_receipt_ref=%s',(outcome['source_receipt_ref'],))[0][0]==1
                else:
                    assert outcome is None
                for table in ('original_registry_20261006','history_clearance_20261006','render_manifest_20261006'):
                    assert sql('select count(*) from fixer_forward_media_'+table+' where source_asset_id=%s',(asset,))[0][0]==0
            # Every previous revision has a final outcome or quarantine, so a
            # subsequent real factory pass is empty and still closes its lane.
            with patch.object(owner.ForwardMediaOwnerPersistence,'connect_from_environment',side_effect=connect):
                report=worker.run_once()
            assert report=={'status':'complete','rows':[]},report
            assert opened[-1].closed
            # An actual role mismatch must close even the failed identity read
            # transaction and leave every candidate untouched.
            c,asset,source_url,image_url=candidate()
            os.environ['FORWARD_MEDIA_OWNER_ROLE']='not_the_authenticated_owner'
            with patch.object(owner.ForwardMediaOwnerPersistence,'connect_from_environment',side_effect=connect):
                report=worker.run_once()
            os.environ['FORWARD_MEDIA_OWNER_ROLE']=OWNER
            assert report=={'status':'hold','reason':'owner_transport_unavailable','rows':[]},report
            assert opened[-1].closed
            assert sql('select count(*) from fixer_forward_media_owner_progress_20261007 where calendar_row_id=%s',(c['calendar_row_id'],))[0][0]==0
            admin.close()
            print('PASS PG17: stalled remote reader permits unrelated real binder/attester/claim; remote reads see IDLE; atomic source/history/HOLD; stale row/source and uncertain commits held; operational owner factory closes real connections; zero held-asset authority')
        finally:
            if started:
                subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)


if __name__=='__main__':
    main()
