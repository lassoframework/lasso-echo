"""Real PG17 + actual Ed25519 certificate receipt slice, all data SYNTHETIC.

No live writes, provider sends, production key provisioning or claim activation.
"""
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import forward_media_owner as owner
from agent.forward_media_source_history import SourceHistoryStore
from agent.forward_media_source_verifier import verify_source
from agent.forward_media_photo_certificate import IndependentPhotoAuditor,PhotoCertificateHold,digest
from tests.test_forward_media_source_verifier import Drive,Hosted,FILE,FOLDER,DATA,URL
from tests.test_forward_media_photo_certificate import fixtures
from tests.test_forward_media_owner_two_phase_pg import png
from agent.forward_media_attester import make_still_recipe

OWNER='photo_test_owner'
AUDITOR='photo_test_auditor'


def main():
    import psycopg
    pg=Path('/opt/homebrew/opt/postgresql@17/bin')
    assert shutil.disk_usage('/tmp').free>5*1024**3
    with tempfile.TemporaryDirectory(prefix='fm_photo_cert_pg_',dir='/tmp') as temp:
        root=Path(temp); sock=root/'sock'; sock.mkdir(); data=root/'data'
        port=random.randint(41000,59000)
        subprocess.run([str(pg/'initdb'),'-D',str(data),'-U','postgres','--no-sync'],check=True,capture_output=True,timeout=60)
        started=False
        try:
            subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-l',str(root/'pg.log'),
                '-o',f"-k {sock} -p {port} -c listen_addresses=''",'-w','start'],check=True,capture_output=True,timeout=60)
            started=True
            def dsn(role): return f'host={sock} port={port} dbname=postgres user={role}'
            admin=psycopg.connect(dsn('postgres'),autocommit=True)
            def sql(query,values=None):
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
                         'DRAFT_fixer_forward_media_source_history_20261007.sql',
                         'DRAFT_fixer_forward_media_photo_certificate_20261007.sql'):
                sql((ROOT/'migrations'/name).read_text())
            sql(f'create role {OWNER} login; grant fixer_forward_media_owner_20261006 to {OWNER};'
                f'create role {AUDITOR} login; grant fixer_forward_media_photo_auditor_20261007 to {AUDITOR};')
            for name in list(os.environ):
                if owner._FORBIDDEN_ENV_NAME.search(name): os.environ.pop(name)
            os.environ.update(FORWARD_MEDIA_OWNER_DSN=dsn(OWNER),FORWARD_MEDIA_OWNER_ROLE=OWNER)
            rid,history_id=str(uuid.uuid4()),str(uuid.uuid4())
            sql('insert into media_source values(%s,%s,%s,%s,true)',('source','gym','gym_drive',FOLDER))
            sql('insert into media_asset values(%s,%s,%s,null,null)',(FILE,'source','gym'))
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,visual_group_key,source_media_asset_id,source_media_url,image_url) values(%s,'gym','2026-10-10','pending','active','group',%s,%s,%s)",(rid,FILE,URL,URL))
            sql("insert into content_calendar(id,gym_id,post_date,status,image_url) values(%s,'historic','2026-09-01','published','https://media.example.test/historic.png')",(history_id,))
            revision=sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',(rid,))[0][0]
            source_conn=psycopg.connect(dsn(OWNER))
            p=owner.ForwardMediaOwnerPersistence(source_conn,OWNER,Hosted())
            store=SourceHistoryStore(p)
            photo_bytes=png('blue')
            drive=Drive(); drive.data=photo_bytes
            drive.meta['size']=str(len(photo_bytes)); drive.meta['md5Checksum']=hashlib.md5(photo_bytes).hexdigest()
            verified_source=verify_source(store.snapshot(rid,revision),drive,Hosted(photo_bytes))
            store.stage_source(verified_source); source_conn.commit()
            assert sql('select enabled from fixer_forward_media_photo_state_20261007')[0][0] is False
            assert sql('select count(*) from fixer_forward_media_photo_key_20261007')[0][0]==0

            candidate={'calendar_row_id':rid,'tenant_id':'gym','group_key':'group','post_date':'2026-10-10',
                'source_asset_id':FILE,'source_url':URL,'image_url':URL,
                'source_fingerprint':verified_source.original.source_fingerprint,
                'source_sha256':verified_source.evidence['source_sha256'],'source_length':len(photo_bytes),
                'source_receipt_ref':verified_source.receipt_ref,
                'image_fingerprint':verified_source.original.source_fingerprint,
                'image_sha256':verified_source.evidence['source_sha256'],'image_length':len(photo_bytes),
                'render_recipe_digest':digest(make_still_recipe('identity')),
                'content_digest':sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(rid,))[0][0]}
            _,key,_,private=fixtures(candidate=candidate)
            sql("insert into fixer_forward_media_photo_policy_20261007 values(%s,true,'complete_fleet_still_photo_history',null,%s,%s)",
                (key['policy_id'],'SYNTHETIC cutover/no outstanding provider requests','SYNTHETIC administrator'))
            sql('insert into fixer_forward_media_photo_key_20261007 values(%s,%s,%s,%s,%s,true)',
                (key['key_id'],key['auditor_id'],AUDITOR,key['policy_id'],key['public_key_hex']))
            history_digest=sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",(history_id,))[0][0]
            row={'history_key':'calendar:'+history_id,'resolved':True,'media_kind':'still_photo',
                 'visual_sha256':'sha256:'+hashlib.sha256(png('red')).hexdigest(),
                 'published_binding_ref':'SYNTHETIC actual publication evidence',
                 'calendar_visual_digest':history_digest}
            baseline=str(uuid.uuid4())
            sql('insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) values(%s,%s,true,%s::jsonb,%s,1)',
                (baseline,key['policy_id'],json.dumps([row]),'SYNTHETIC full preserved corpus'))
            sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s where singleton',(baseline,))
            snapshot=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0]
            assert snapshot['rows']==[row],snapshot['rows']
            packet,_,_,_=fixtures(candidate=candidate,snapshot=snapshot,private=private)
            auditor_conn=psycopg.connect(dsn(AUDITOR))
            client=IndependentPhotoAuditor(auditor_conn,AUDITOR)
            verified=client.submit(packet)
            assert sql('select count(*) from fixer_forward_media_photo_certificate_20261007')[0][0]==0
            auditor_conn.commit()
            assert sql('select count(*) from fixer_forward_media_photo_certificate_20261007')[0][0]==1
            lookup=IndependentPhotoAuditor(source_conn,OWNER)
            assert lookup.lookup_for_owner(packet['payload']['audit_id'],candidate)==verified
            source_conn.rollback()
            # Current slice is immutable verification evidence only, never
            # original clearance or a provider permission, even with valid key.
            for table in ('fixer_forward_media_original_registry_20261006',
                          'fixer_forward_media_history_clearance_20261006',
                          'fixer_forward_media_render_manifest_20261006',
                          'fixer_forward_media_claim_receipt_20261006'):
                assert sql('select count(*) from '+table)[0][0]==0
            assert sql('select enabled from fixer_forward_media_photo_state_20261007')[0][0] is False

            # Wrong signature fails before RPC append, not just SQL shape.
            bad=json.loads(json.dumps(packet)); bad['signature_hex']='00'*64
            try:
                client.submit(bad); raise AssertionError('bad signature accepted')
            except PhotoCertificateHold as exc:
                assert str(exc)=='certificate_signature_invalid'
                auditor_conn.rollback()

            # Versioned replacement of stale audit: append a new signed ID at
            # current generation without modifying the first immutable receipt.
            sql('update fixer_forward_media_photo_state_20261007 set generation=1 where singleton')
            try:
                lookup.lookup_for_owner(packet['payload']['audit_id'],candidate)
                raise AssertionError('stale certificate accepted')
            except PhotoCertificateHold as exc:
                assert str(exc)=='certificate_corpus_stale_or_unapproved'
                source_conn.rollback()
            replacement,_,_,_=fixtures(candidate=candidate,snapshot=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0],private=private)
            client.submit(replacement); auditor_conn.commit()
            assert sql('select count(*) from fixer_forward_media_photo_certificate_20261007')[0][0]==2

            # Missing live historical row becomes an unresolved corpus member.
            new_history=str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,status,image_url) values(%s,'other','published','https://media.example.test/unresolved.png')",(new_history,))
            changed=sql('select fixer_forward_media_photo_snapshot_20261007()')[0][0]
            assert any(r['resolved'] is False for r in changed['rows'])
            unresolved,_,_,_=fixtures(candidate=candidate,snapshot=changed,private=private)
            try:
                client.submit(unresolved); raise AssertionError('unknown still history accepted')
            except PhotoCertificateHold as exc:
                assert str(exc)=='certificate_history_unresolved_or_matching'
                auditor_conn.rollback()
            sql('delete from content_calendar where id=%s',(new_history,))

            # No publisher/source owner can write certificates or approved keys.
            for role in ('service_role','anon','authenticated','fixer_forward_media_owner_20261006'):
                sql('set role '+role)
                try:
                    sql('select fixer_forward_media_photo_record_20261007(%s,%s,%s)',
                        (verified.payload_json,verified.signature_hex,verified.receipt_ref))
                    raise AssertionError('non-auditor appended certificate')
                except psycopg.errors.InsufficientPrivilege:
                    pass
                finally:
                    sql('reset role')
            for table in ('fixer_forward_media_photo_key_20261007','fixer_forward_media_photo_baseline_20261007',
                          'fixer_forward_media_photo_certificate_20261007'):
                try:
                    sql('truncate '+table+' cascade'); raise AssertionError('immutable evidence truncated')
                except psycopg.errors.CheckViolation:
                    pass
            sql('insert into fixer_forward_media_photo_key_revocation_20261007 values(%s,%s)',
                (key['key_id'],'SYNTHETIC key retirement'))
            try:
                lookup.lookup_for_owner(replacement['payload']['audit_id'],candidate)
                raise AssertionError('revoked key accepted')
            except PhotoCertificateHold as exc:
                assert str(exc)=='certificate_signer_unapproved'
                source_conn.rollback()
            auditor_conn.close(); source_conn.close(); admin.close()
            print('PASS PG17+Ed25519: approved independent signer receipt, owner signature re-verification, atomic immutable append, stale version replacement, unknown/live omitted row rejects, role isolation, revocation, zero source clearance/provider claims, default OFF')
        finally:
            if started:
                subprocess.run([str(pg/'pg_ctl'),'-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,timeout=60)


if __name__=='__main__': main()
