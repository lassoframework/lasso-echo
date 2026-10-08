"""Real PG17 source/history authority tests. Disposable local socket, no live writes."""
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import forward_media_owner as owner
from agent.forward_media_source_history import SourceHistoryStore
from agent.forward_media_source_verifier import verify_source
from tests.test_forward_media_source_verifier import Drive, Hosted, FILE, FOLDER, DATA, URL


def main():
    import psycopg
    pg = Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free > 5*1024**3
    with tempfile.TemporaryDirectory(prefix='fm_source_pg_', dir='/tmp') as tmp:
        root = Path(tmp)
        sock = root/'sock'
        sock.mkdir()
        data = root/'data'
        port = random.randint(41000,59000)
        subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],
                       check=True,capture_output=True,timeout=60)
        started = False
        try:
            subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),
                '-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],
                check=True,capture_output=True,timeout=60)
            started = True
            dsn = f'host={sock} port={port} dbname=postgres'
            admin = psycopg.connect(dsn+' user=postgres',autocommit=True)
            def sql(query, values=None):
                with admin.cursor() as cur:
                    cur.execute(query, values)
                    return cur.fetchall() if cur.description else None
            sql('create role anon; create role authenticated; create role service_role;'
                'create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,'
                'format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,'
                'publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,'
                'thumbnail_url text,media_not_ready_reason text,caption text);'
                'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
                'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
            for name in ('DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql'):
                sql((ROOT/'migrations'/name).read_text())
            sql('create role source_test_owner login; grant fixer_forward_media_owner_20261006 to source_test_owner;')
            for name in list(os.environ):
                if name in owner.forbidden_credential_names(os.environ):
                    os.environ.pop(name)
            os.environ.update(FORWARD_MEDIA_OWNER_DSN=dsn+' user=source_test_owner',
                              FORWARD_MEDIA_OWNER_ROLE='source_test_owner')
            rid = str(uuid.uuid4())
            sql('insert into media_source values(%s,%s,%s,%s,true)', ('source','gym','gym_drive',FOLDER))
            sql('insert into media_asset values(%s,%s,%s,%s,%s)', (FILE,'source','gym','metadata-only','https://example.test/rendition'))
            sql("insert into content_calendar(id,gym_id,post_date,status,source_media_asset_id,source_media_url,image_url) values(%s,'gym','2026-10-10','pending',%s,%s,%s)", (rid,FILE,URL,URL))
            revision = sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(rid,))[0][0]
            conn = psycopg.connect(dsn+' user=source_test_owner')
            persistence = owner.ForwardMediaOwnerPersistence(conn,'source_test_owner',Hosted())
            store = SourceHistoryStore(persistence)
            snap = store.snapshot(rid,revision)
            assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            verified = verify_source(snap,Drive(),Hosted())

            # Snapshot leaves no row or graph lock across slow external reads.
            sql("set lock_timeout='300ms'")
            sql("select pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0))")
            sql("update media_source set active=false where id='source'")
            try:
                store.stage_source(verified)
                raise AssertionError('inactive source admitted')
            except psycopg.errors.CheckViolation:
                conn.rollback()
            assert sql('select count(*) from fixer_forward_media_source_receipt_20261007')[0][0] == 0
            sql("update media_source set active=true where id='source'")
            sql("update media_source set folder_id='ChangedFolder12345678' where id='source'")
            try:
                store.stage_source(verified)
                raise AssertionError('changed binding admitted')
            except psycopg.errors.CheckViolation:
                conn.rollback()
            sql('update media_source set folder_id=%s where id=%s',(FOLDER,'source'))
            sql("update media_source set gym_id='foreign' where id='source'")
            try:
                store.stage_source(verified)
                raise AssertionError('cross-gym source admitted')
            except psycopg.errors.CheckViolation:
                conn.rollback()
            sql("update media_source set gym_id='gym' where id='source'")
            sql("update media_asset set rendition_url='https://example.test/changed' where id=%s",(FILE,))
            try:
                store.stage_source(verified)
                raise AssertionError('changed asset snapshot admitted')
            except psycopg.errors.CheckViolation:
                conn.rollback()
            sql("update media_asset set rendition_url='https://example.test/rendition' where id=%s",(FILE,))
            # Even the owner RPC cannot rebind a byte receipt to a normalized or
            # producer-substituted URL when the hash of its JSON is recomputed.
            altered = dict(verified.evidence)
            altered['exact_source_url'] = URL.replace('%2B','+')
            encoded = json.dumps(altered,sort_keys=True,separators=(',',':'))
            altered_ref = 'source-receipt:sha256:'+hashlib.sha256(encoded.encode()).hexdigest()
            try:
                conn.execute('select fixer_forward_media_source_record_20261007(%s,%s,%s,%s,%s)',
                             (rid,revision,snap['binding_revision'],altered_ref,encoded))
                raise AssertionError('altered exact source URL admitted')
            except psycopg.errors.CheckViolation:
                conn.rollback()
            store.stage_source(verified)
            h = store.history(verified.original)
            assert h['decision']=='hold_uncertain' and h['reason']=='preexisting_original_has_no_fresh_production_proof'
            assert sql('select count(*) from fixer_forward_media_source_receipt_20261007')[0][0]==0
            conn.commit()
            assert sql('select count(*) from fixer_forward_media_source_receipt_20261007')[0][0]==1
            assert sql('select result_json from fixer_forward_media_history_query_receipt_20261007')[0][0]==h
            store.stage_source(verified)  # exact replay
            conn.commit()

            # Fleet scan accounts for unknown cross-gym publication and published
            # receipts whose current status changed. No missing lineage clears.
            old = str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,status,published_at,image_url,source_media_asset_id,source_media_url) values(%s,'other','failed',now(),'https://example.test/rendered',%s,%s)",(old,FILE,URL))
            h = store.history(verified.original)
            assert h['decision']=='hold_uncertain' and h['unknown_rows_in_bound']==1
            assert h['reason']=='historical_original_bytes_unknown'
            conn.commit()
            for _ in range(2):
                sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'third','published','https://example.test/old')",(str(uuid.uuid4()),))
            h = store.history(verified.original,1)
            assert h['decision']=='hold_uncertain' and h['limit_exceeded'] is True
            conn.commit()

            # Audited original-byte USED evidence is an independent role import,
            # not today's Drive metadata. Fixture tests authority mechanics only.
            old_revision = sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(old,))[0][0]
            sql('set role fixer_forward_media_history_auditor_20261007')
            sql('insert into fixer_forward_media_historical_original_20261007(calendar_row_id,calendar_revision,tenant_id,source_asset_id,exact_source_url,source_fingerprint,source_sha256,source_length,historical_evidence_ref) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (old,old_revision,'other',FILE,URL,verified.original.source_fingerprint,
                 'sha256:'+hashlib.sha256(DATA).hexdigest(),len(DATA),'SYNTHETIC independently reviewed publication original'))
            sql('reset role')
            h = store.history(verified.original)
            assert h['decision']=='hold_used' and h['known_matches']==1
            conn.commit()
            sql('delete from content_calendar where id=%s',(old,))
            assert store.history(verified.original)['decision']=='hold_used'
            conn.commit()

            # Source+history query receipt rollback is atomic, and a missing
            # durable source tuple cannot be replaced by caller hash metadata.
            altered_original = verified.original.row()
            altered_original['source_length'] += 1
            try:
                conn.execute('select fixer_forward_media_source_history_20261007(%s::jsonb,2500)',
                             (json.dumps(altered_original),))
                raise AssertionError('forged original tuple admitted')
            except psycopg.errors.CheckViolation:
                conn.rollback()

            # Row mutation after remote verification prevents source append.
            sql('update content_calendar set caption=%s where id=%s',('edited',rid))
            try:
                store.stage_source(verified)
                raise AssertionError('stale calendar admitted')
            except psycopg.errors.CheckViolation:
                conn.rollback()
            for role in ('service_role','anon','authenticated','fixer_forward_media_attester_20261006',
                         'fixer_forward_media_history_auditor_20261007'):
                sql('set role '+role)
                try:
                    sql('select fixer_forward_media_source_snapshot_20261007(%s,%s)',(rid,revision))
                    raise AssertionError('untrusted role accessed source')
                except psycopg.errors.InsufficientPrivilege:
                    pass
                finally:
                    sql('reset role')
            for table in ('fixer_forward_media_source_receipt_20261007',
                          'fixer_forward_media_history_query_receipt_20261007',
                          'fixer_forward_media_historical_original_20261007'):
                try:
                    sql('truncate '+table+' cascade')
                    raise AssertionError('immutable receipt truncated')
                except psycopg.errors.CheckViolation:
                    pass
            sql('set role fixer_forward_media_owner_20261006')
            try:
                sql('insert into fixer_forward_media_historical_original_20261007(calendar_row_id) values(%s)',(rid,))
                raise AssertionError('source owner auto-imported independent historical proof')
            except psycopg.errors.InsufficientPrivilege:
                pass
            finally:
                sql('reset role')
            conn.close()
            admin.close()
            print('PASS PG17: independent byte source receipt, active same-gym binding, stale calendar/source holds, no remote graph locks, atomic durable receipts, bounded fleet unknowns, trusted used history, least privilege, immutability')
        finally:
            if started:
                subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],
                               check=True,capture_output=True,timeout=60)


if __name__ == '__main__':
    main()
