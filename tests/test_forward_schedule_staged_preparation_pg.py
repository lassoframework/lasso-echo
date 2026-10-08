"""Standalone disposable PG17 acceptance for DRAFT_fixer_forward_schedule_staged_preparation_20261008.

Run `python3 tests/test_forward_schedule_staged_preparation_pg.py` (also under
pytest; module-level main() runs in test_staged_preparation_pg). Uses a real
disposable PostgreSQL 17 cluster on a Unix socket; no DSN, network, production
connection, installs or persisted cluster. Requires the stage draft SQL from
the sibling reservation checkout (read-only; override with FORWARD_STAGE_SQL).
Skips cleanly when PG17 binaries or the stage draft are unavailable.

All bytes, Drive metadata, signing keys, corpus judgments and roles SYNTHETIC.
Proves: genuine positive staged preparation (owner evidence-only hold, then
signed-photo grant, then pre-finalization manifest bind), alias success with
canonical tenant agreement between discovery and the owner-locked transport
while raw source/asset ownership is still checked, wrong tenant/asset
rejection, stale row, unknown commit, and absence of unauthorized mutation.
"""
import copy
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STAGE_SQL = Path(os.environ.get(
    'FORWARD_STAGE_SQL',
    ROOT.parent / 'echo-schedule-reservation-20261008'
    / 'migrations/DRAFT_fixer_forward_schedule_stage_20261008.sql'))

from agent import forward_media_owner as owner
from agent import forward_media_owner_worker as worker
from agent import forward_media_attester as attester
from agent import forward_media_guard as guard
from agent import forward_media_visual_index as visual_index
from agent.forward_media_owner_transport import DedicatedOwnerTransport
from agent.forward_media_owner_photo_prepare import (
    run_staged_photo_pass, stage_prepared_photo, stage_prepared_staged_photo)
from agent.forward_media_photo_certificate import IndependentPhotoAuditor, digest
from tests.test_forward_media_photo_certificate import fixtures
from tests.test_forward_media_owner_two_phase_pg import png

FOLDER = 'FolderOriginal1234567'


def _pg(name):
    bundled = Path('/opt/homebrew/opt/postgresql@17/bin') / name
    return str(bundled) if bundled.is_file() else shutil.which(name)


def _skipped():
    if any(not _pg(n) for n in ('initdb', 'pg_ctl', 'postgres')):
        return 'existing initdb/pg_ctl unavailable; disposable PostgreSQL not provisioned'
    if not STAGE_SQL.is_file():
        return f'stage draft unavailable at {STAGE_SQL}; set FORWARD_STAGE_SQL'
    return None


class FakeDrive:
    """SYNTHETIC Drive reader for exactly one file; never touches a network."""

    def __init__(self, file_id, data):
        self.file_id, self.data = file_id, data
        self.meta = {'id': file_id, 'mimeType': 'image/png', 'trashed': False,
                     'version': '3', 'parents': [FOLDER], 'size': str(len(data)),
                     'md5Checksum': hashlib.md5(data).hexdigest()}

    def metadata(self, file_id):
        if file_id == FOLDER:
            return {'id': FOLDER, 'mimeType': 'application/vnd.google-apps.folder',
                    'trashed': False, 'version': '2'}
        assert file_id == self.file_id
        return copy.deepcopy(self.meta)

    def original_bytes(self, file_id):
        assert file_id == self.file_id
        return self.data

    def proves_parent(self, file_id, folder_id):
        return file_id == self.file_id and folder_id == FOLDER


class FakeHosted:
    def __init__(self, data):
        self.data = data

    def read(self, url):
        return self.data


class LostCommitResponse:
    """COMMIT applies server-side but the client response is lost."""

    def __init__(self, raw, fail_on=2):
        self.raw, self.commits, self.fail_on = raw, 0, fail_on

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def commit(self):
        self.commits += 1
        self.raw.commit()
        if self.commits == self.fail_on:
            raise RuntimeError('SYNTHETIC COMMIT response lost')


def main():
    skip = _skipped()
    if skip:
        print('SKIP: ' + skip)
        return
    import psycopg
    assert ' 17.' in subprocess.check_output([_pg('postgres'), '--version'], text=True)
    with tempfile.TemporaryDirectory(prefix='fwd_staged_prep_pg_', dir='/tmp') as tmp:
        work = Path(tmp)
        sock = work / 'sock'
        sock.mkdir()
        data = work / 'data'
        port = random.randint(41000, 59000)
        subprocess.run([_pg('initdb'), '-D', str(data), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([_pg('pg_ctl'), '-D', str(data), '-l', str(work / 'pg.log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        try:
            def dsn(role='postgres'):
                return f'host={sock} port={port} dbname=postgres user={role}'

            admin = psycopg.connect(dsn(), autocommit=True)

            def sql(query, args=None, role=None):
                conn = admin
                if role:
                    conn = psycopg.connect(dsn(), autocommit=True)
                    conn.execute('set role ' + role)
                try:
                    with conn.cursor() as cur:
                        cur.execute(query, args)
                        return cur.fetchall() if cur.description else None
                finally:
                    if role:
                        conn.close()

            def denied(query, args=None, role='anon', fragment='permission denied'):
                try:
                    sql(query, args, role=role)
                    raise AssertionError(f'unsafe grant unexpectedly accepted for {role}')
                except psycopg.Error as exc:
                    assert fragment in str(exc), str(exc)

            sql('create role anon; create role authenticated; create role service_role;'
                "create table content_calendar(id uuid primary key,gym_id text,post_date date,"
                "account text,format text,gbp_location_id text,"
                "status text not null check(status in ('draft','pending','approved','published','denied','killed','failed','publishing','deleted','coach_review')),"
                "variant_status text not null check(variant_status in ('active','candidate','archived')),"
                "published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,"
                "late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);"
                'create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean);'
                'create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text);')
            sql("alter table media_asset add column eligible boolean default true,"
                "add column excluded_by_coach boolean default false,"
                "add column review_status text default 'approved',"
                "add column moderation_status text default 'clean',"
                "add column review_content_hash text,"
                "add column reviewed_by text default 'SYNTHETIC reviewer',"
                "add column reviewed_at timestamptz default now(),"
                "add column moderation_json jsonb,"
                "add column people_detected boolean default false,"
                "add column used_count integer default 0")
            for name in ('logical_post_id_20261004.sql',
                         'DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_visual_index_20261008.sql',
                         'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql',
                         'DRAFT_fixer_forward_media_owner_transport_20261007.sql',
                         'DRAFT_fixer_forward_media_photo_certificate_20261007.sql',
                         'DRAFT_fixer_owner_photo_clearance_20261007.sql',
                         'DRAFT_fixer_forward_schedule_reservation_20261008.sql'):
                sql((ROOT / 'migrations' / name).read_text())
            sql(STAGE_SQL.read_text())
            sql((ROOT / 'migrations'
                 / 'DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql').read_text())
            sql((ROOT / 'migrations'
                 / 'DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql').read_text())
            sql('grant select,insert,update,delete on public.content_calendar to service_role;')
            sql('create role staged_owner login; grant fixer_forward_media_owner_20261006 to staged_owner;')
            sql('create role staged_auditor login; grant fixer_forward_media_photo_auditor_20261007 to staged_auditor;')
            sql('create role staged_attester login; grant fixer_forward_media_attester_20261006 to staged_attester;')
            sql('update forward_media_visual_gate_20261008 set enabled=true;')
            sql('update forward_schedule_reservation_gate_20261008 set enabled=true;')

            for name in list(os.environ):
                if owner._FORBIDDEN_ENV_NAME.search(name):
                    os.environ.pop(name)
            os.environ['FORWARD_MEDIA_OWNER_DSN'] = dsn('staged_owner')
            os.environ['FORWARD_MEDIA_OWNER_ROLE'] = 'staged_owner'
            os.environ[worker.WORKER_ENV] = 'true'
            os.environ[worker.STAGED_ENV] = 'true'

            recipe = attester.make_still_recipe('identity')

            def approved_asset(asset_id, data_bytes, gym):
                md5 = hashlib.md5(data_bytes).hexdigest()
                proof = {'verdict': 'clean', 'provider': 'SYNTHETIC scanner',
                         'content_hash': md5, 'asset_id': asset_id, 'gym_id': gym,
                         'people_detected': False, 'observed_at': '2026-10-08T00:00:00Z',
                         'sha256': hashlib.sha256(data_bytes).hexdigest()}
                sql('update media_asset set content_hash=%s,review_content_hash=%s,'
                    'moderation_json=%s::jsonb where id=%s', (md5, md5, json.dumps(proof), asset_id))

            def new_media(gym, color):
                data_bytes = png(color)
                asset_id = 'File' + uuid.uuid4().hex
                source_id = 'source-' + uuid.uuid4().hex
                url = 'https://media.example.test/' + uuid.uuid4().hex + '.png'
                sql('insert into media_source values(%s,%s,%s,%s,true)',
                    (source_id, gym, 'gym_drive', FOLDER))
                sql('insert into media_asset(id,source_id,gym_id) values(%s,%s,%s)',
                    (asset_id, source_id, gym))
                approved_asset(asset_id, data_bytes, gym)
                return asset_id, url, data_bytes, FakeDrive(asset_id, data_bytes), FakeHosted(data_bytes)

            def observation(canonical_tenant, row, *, recipe_override=None):
                digest_input_obj = {
                    'schema_version': 1, 'provenance_status': 'unverified',
                    'tenant': canonical_tenant, 'source_asset_id': row['source_media_asset_id'],
                    'source_exact_url': row['source_media_url'],
                    'delivered_exact_url': row['image_url'],
                    'source_sha256': uuid.uuid4().hex + uuid.uuid4().hex,
                    'delivered_sha256': uuid.uuid4().hex + uuid.uuid4().hex,
                    'source_byte_length': 10, 'delivered_byte_length': 10,
                    'recipe': recipe_override or recipe, 'hold_reasons': []}
                digest_input = json.dumps(digest_input_obj, sort_keys=True, separators=(',', ':'))
                obs_digest = hashlib.sha256(digest_input.encode()).hexdigest()
                return {'observation_json': json.dumps(
                            dict(digest_input_obj, observation_digest=obs_digest),
                            sort_keys=True, separators=(',', ':')),
                        'digest_input': digest_input}

            def member(canonical_tenant, gym, asset_id, url):
                row = {'id': str(uuid.uuid4()), 'gym_id': gym, 'post_date': '2026-10-10',
                       'status': 'pending', 'account': 'instagram', 'format': 'feed',
                       'logical_post_id': str(uuid.uuid4()),
                       'source_media_url': url, 'image_url': url,
                       'source_media_asset_id': asset_id,
                       'visual_group_key': 'vg_' + uuid.uuid4().hex}
                return {'row': row, 'observation': observation(canonical_tenant, row)}

            def stage(tenant, members, batch_id=None):
                batch_id = batch_id or str(uuid.uuid4())
                request = json.dumps({'members': members, 'old_rows': []},
                                     sort_keys=True, separators=(',', ':'))
                req_digest = hashlib.sha256(request.encode()).hexdigest()
                receipt = sql('select public.stage_forward_schedule_batch_20261008(%s,%s,%s,%s)',
                              (tenant, batch_id, request, req_digest), role='service_role')[0][0]
                return batch_id, receipt

            def eligible(rid):
                return sql('select public.forward_schedule_preparation_eligible_20261008(%s)',
                           (rid,), role='staged_owner')[0][0]

            def authority_counts():
                return sql('select (select count(*) from fixer_owner_photo_reservation_20261007),'
                           '(select count(*) from fixer_forward_media_original_registry_20261006),'
                           '(select count(*) from fixer_forward_media_history_clearance_20261006),'
                           '(select count(*) from fixer_forward_media_render_manifest_20261006)')[0]

            def owner_pass(tenants, drive, hosted):
                os.environ[worker.TENANTS_ENV] = ','.join(tenants)
                conn = psycopg.connect(dsn('staged_owner'))
                try:
                    persistence = owner.ForwardMediaOwnerPersistence(conn, 'staged_owner', hosted)
                    transport = DedicatedOwnerTransport(persistence)
                    return worker.run_staged_pass(transport=transport, persistence=persistence,
                                                  reader=hosted, drive_reader=drive)
                finally:
                    conn.close()

            def owner_photo_conn():
                conn = psycopg.connect(dsn('staged_owner'))
                return conn, owner.ForwardMediaOwnerPersistence(conn, 'staged_owner', FakeHosted(b'x'))

            # Photo policy/key/baseline with a complete SYNTHETIC corpus.
            _, key, _, private = fixtures()
            sql("insert into fixer_forward_media_photo_policy_20261007 values(%s,true,"
                "'complete_fleet_still_photo_history',null,'SYNTHETIC cutover','SYNTHETIC admin')",
                (key['policy_id'],))
            sql('insert into fixer_forward_media_photo_key_20261007 values(%s,%s,%s,%s,%s,true)',
                (key['key_id'], key['auditor_id'], 'staged_auditor', key['policy_id'],
                 key['public_key_hex']))
            baseline = str(uuid.uuid4())
            history_row = {'history_key': 'SYNTHETIC full historical photo', 'resolved': True,
                           'media_kind': 'still_photo',
                           'visual_sha256': digest('SYNTHETIC unrelated historic visual'),
                           'published_binding_ref': 'SYNTHETIC preserved complete fleet history'}
            sql('insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,'
                'scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count)'
                ' values(%s,%s,true,%s::jsonb,%s,1)',
                (baseline, key['policy_id'], json.dumps([history_row]), 'SYNTHETIC complete corpus'))
            sql('update fixer_forward_media_photo_state_20261007 set baseline_id=%s where singleton',
                (baseline,))

            def sign_candidate(rid, gym, asset_id, url, receipt, *, image_url=None,
                               image_bytes=None, thumbnail_url=None, recipe_override=None):
                delivered = image_bytes
                candidate = {
                    'calendar_row_id': rid, 'tenant_id': gym, 'group_key':
                        sql('select visual_group_key from content_calendar where id=%s', (rid,))[0][0],
                    'post_date': '2026-10-10', 'source_asset_id': asset_id, 'source_url': url,
                    'source_fingerprint': receipt['source_fingerprint'],
                    'source_sha256': receipt['source_sha256'],
                    'source_length': receipt['source_length'],
                    'source_receipt_ref': receipt['receipt_ref'],
                    'image_url': image_url or url,
                    'image_fingerprint': ('md5:' + hashlib.md5(delivered).hexdigest()
                                          if image_bytes is not None else receipt['source_fingerprint']),
                    'image_sha256': ('sha256:' + hashlib.sha256(delivered).hexdigest()
                                     if image_bytes is not None else receipt['source_sha256']),
                    'image_length': (len(delivered) if image_bytes is not None else receipt['source_length']),
                    'render_recipe_digest': digest(recipe_override or recipe),
                    'content_digest': sql("select 'sha256:'||encode(sha256(convert_to("
                        'public.fixer_forward_media_photo_content_20261007(%s)::text,'
                        "'UTF8')),'hex')", (rid,))[0][0]}
                if thumbnail_url:
                    candidate.update(thumbnail_url=thumbnail_url,
                                     thumbnail_fingerprint=candidate['image_fingerprint'],
                                     thumbnail_sha256=candidate['image_sha256'],
                                     thumbnail_length=candidate['image_length'])
                packet, _, _, _ = fixtures(
                    candidate=candidate,
                    snapshot=sql('select public.fixer_forward_media_photo_snapshot_20261007()')[0][0],
                    private=private)
                auditor_conn = psycopg.connect(dsn('staged_auditor'))
                try:
                    IndependentPhotoAuditor(auditor_conn, 'staged_auditor').submit(packet)
                    auditor_conn.commit()
                finally:
                    auditor_conn.close()
                return packet

            def receipt_for(rid):
                cols = ('receipt_ref', 'source_fingerprint', 'source_sha256', 'source_length',
                        'tenant_id')
                row = sql('select receipt_ref,source_fingerprint,source_sha256,source_length,tenant_id'
                          ' from fixer_forward_media_source_receipt_20261007'
                          ' where calendar_row_id=%s', (rid,))[0]
                return dict(zip(cols, row))

            # ============ A. genuine positive staged preparation ============
            tenant = 'gym_' + uuid.uuid4().hex
            asset, url, source_bytes, drive, hosted = new_media(tenant, 'blue')
            batch, receipt = stage(tenant, [member(tenant, tenant, asset, url)])
            rid = receipt['member_row_ids'][0]
            assert eligible(rid) == {'eligible': True, 'mode': 'staged', 'tenant_id': tenant,
                                     'batch_id': batch, 'reason': None}

            # Owner staged pass: exact provenance is staged but unsigned history
            # stays an uncertain HOLD; no positive authority is ever invented.
            before = authority_counts()
            report = owner_pass((tenant,), drive, hosted)
            assert report['status'] == 'partial_hold', report
            assert report['rows'][0]['status'] == 'hold'
            assert report['rows'][0]['decision'] == 'hold_uncertain', report
            assert report['rows'][0]['reason'] == 'preexisting_original_has_no_fresh_production_proof'
            assert authority_counts() == before
            src = receipt_for(rid)
            assert src['tenant_id'] == tenant
            assert sql('select state,outcome->>\'status\' from fixer_forward_media_owner_progress_20261007'
                       ' where calendar_row_id=%s', (rid,))[0] == ('final', 'hold')

            # Signed independent certificate. The ACTIVE-only grant RPC refuses
            # the staged row before any reservation exists for this audit.
            packet = sign_candidate(rid, tenant, asset, url, src)
            sql("update fixer_forward_media_photo_state_20261007 set enabled=true,"
                "routes_reconciled_ref='SYNTHETIC all routes reconciled'")
            cert_ref = sql('select receipt_ref from fixer_forward_media_photo_certificate_20261007'
                           ' where audit_id=%s', (packet['payload']['audit_id'],))[0][0]
            exact_original = {'tenant_id': tenant, 'source_asset_id': asset, 'source_url': url,
                              'source_fingerprint': src['source_fingerprint'],
                              'source_length': src['source_length'],
                              'registry_evidence_ref': src['receipt_ref']}
            exact_manifest = {'manifest_digest': 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex,
                              'tenant_id': tenant, 'source_asset_id': asset, 'image_url': url,
                              'image_fingerprint': src['source_fingerprint'],
                              'image_length': src['source_length'], 'thumbnail_url': None,
                              'thumbnail_fingerprint': None, 'thumbnail_length': None,
                              'operation': 'same_object', 'render_recipe': recipe,
                              'render_evidence_ref': cert_ref}
            denied('select public.fixer_prepare_owner_photo_20261007(%s,%s::jsonb,%s::jsonb)',
                   (packet['payload']['audit_id'], json.dumps(exact_original),
                    json.dumps(exact_manifest)),
                   role='staged_owner', fragment='unsent canonical candidate')
            # The staged photo pass grants positive authority through the
            # staged-specific RPC only.
            conn, persistence = owner_photo_conn()
            try:
                photo_report = run_staged_photo_pass(persistence=persistence, reader=hosted,
                                                     drive_reader=drive, tenants=(tenant,), limit=25)
            finally:
                conn.close()
            assert photo_report['status'] == 'complete', photo_report
            assert photo_report['rows'][0]['status'] == 'persisted'
            assert photo_report['rows'][0]['decision'] == 'cleared_unused'
            assert authority_counts() == (1, 1, 1, 1)
            manifest_digest = photo_report['rows'][0]['manifest_digest']
            assert sql("select state,outcome->>'status' from fixer_owner_photo_progress_20261007"
                       ' where audit_id=%s', (packet['payload']['audit_id'],))[0] == ('final', 'persisted')
            # Active-lane discovery never sees the staged row.
            assert sql('select public.fixer_forward_media_owner_pending_20261007(%s,%s)',
                       ([tenant], 25), role='staged_owner')[0][0] == []
            assert sql('select public.fixer_owner_photo_pending_20261007(%s,%s)',
                       ([tenant], 25), role='staged_owner')[0][0] == []

            # Staged binder: SQL-authorized discovery then bind of the exact
            # persisted manifest digest to the exact staged row before finalize.
            found = sql('select public.fixer_forward_schedule_staged_binder_pending_20261008(%s,%s)',
                        ([tenant], 25), role='service_role')[0][0]
            assert [c['calendar_row_id'] for c in found] == [rid], found
            assert found[0]['manifest_digest'] == manifest_digest
            # The active-only bind RPC refuses staged rows.
            denied('select public.fixer_bind_forward_media_manifest_20261006(%s)', (rid,),
                   role='service_role', fragment='unsent active row')
            assert sql('select public.fixer_bind_forward_schedule_staged_manifest_20261008(%s)',
                       (rid,), role='service_role')[0][0] is True
            assert sql('select render_manifest_digest from content_calendar where id=%s',
                       (rid,))[0][0] == manifest_digest
            # Binding is a trusted preparation output field: the predicate still
            # authorizes and the finalizer membership checks still hold.
            assert eligible(rid)['eligible'] is True
            # Idempotent re-bind and empty discovery afterwards.
            assert sql('select public.fixer_bind_forward_schedule_staged_manifest_20261008(%s)',
                       (rid,), role='service_role')[0][0] is True
            assert sql('select public.fixer_forward_schedule_staged_binder_pending_20261008(%s,%s)',
                       ([tenant], 25), role='service_role')[0][0] == []

            # ============ B. alias success with canonical agreement ============
            canonical = 'gym_' + uuid.uuid4().hex
            alias_gym = 'alias_' + uuid.uuid4().hex
            sql('insert into fixer_forward_media_tenant_alias_20261006(alias_key,tenant_id)'
                ' values(%s,%s)', (alias_gym, canonical))
            a_asset, a_url, a_bytes, a_drive, a_hosted = new_media(alias_gym, 'green')
            # Exercise the real controlled renderer, including three visual
            # roles (the NULL thumbnail aliases the delivered object).
            identity_recipe = recipe
            recipe = attester.make_still_recipe('feed_autofit_4x5')
            a_image_bytes = attester.replay_still_recipe(
                a_bytes, recipe, has_thumbnail=False)['image_bytes']
            assert a_image_bytes != a_bytes
            a_image_url = 'https://media.example.test/' + uuid.uuid4().hex + '.jpg'
            a_thumb_url = None
            class AliasHosted:
                objects = {a_url: a_bytes, a_image_url: a_image_bytes}
                def read(self, url): return self.objects[url]
            a_hosted = AliasHosted()
            a_member = member(canonical, alias_gym, a_asset, a_url)
            a_member['row'].update(image_url=a_image_url, thumbnail_url=a_thumb_url)
            a_member['observation'] = observation(canonical, a_member['row'])
            a_batch, a_receipt = stage(canonical, [a_member])
            a_rid = a_receipt['member_row_ids'][0]
            # Discovery under the raw alias is empty; canonical tenant admits.
            assert sql('select public.fixer_forward_schedule_staged_owner_pending_20261008(%s,%s)',
                       ([alias_gym], 25), role='staged_owner')[0][0] == []
            # Owner-locked transport agrees with discovery on the canonical
            # tenant while raw asset/source ownership binds the alias gym key.
            a_report = owner_pass((canonical,), a_drive, a_hosted)
            assert a_report['status'] == 'partial_hold', a_report
            assert a_report['rows'][0]['decision'] == 'hold_uncertain'
            a_src = receipt_for(a_rid)
            # The source receipt binds the RAW gym (certificate tenant contract)
            # while membership/batch bind the canonical tenant.
            assert a_src['tenant_id'] == alias_gym
            a_packet = sign_candidate(a_rid, alias_gym, a_asset, a_url, a_src,
                                      image_url=a_image_url, image_bytes=a_image_bytes,
                                      thumbnail_url=a_thumb_url)
            conn, persistence = owner_photo_conn()
            try:
                a_photo = run_staged_photo_pass(persistence=persistence, reader=a_hosted,
                                                drive_reader=a_drive, tenants=(canonical,), limit=25)
            finally:
                conn.close()
            assert a_photo['status'] == 'complete' and a_photo['rows'][0]['status'] == 'persisted', a_photo
            assert authority_counts() == (2, 2, 2, 2)
            assert sql('select public.fixer_bind_forward_schedule_staged_manifest_20261008(%s)',
                       (a_rid,), role='service_role')[0][0] is True
            # Wrong tenant allowlist sees and mutates nothing.
            conn, persistence = owner_photo_conn()
            try:
                foreign = run_staged_photo_pass(persistence=persistence, reader=a_hosted,
                                                drive_reader=a_drive,
                                                tenants=('other_' + uuid.uuid4().hex,), limit=25)
            finally:
                conn.close()
            assert foreign == {'status': 'complete', 'rows': []}
            assert authority_counts() == (2, 2, 2, 2)

            # --- P1 alias authority handoff: provenance -> attestation ->
            # reservation -> atomic finalization with genuine evidence. Owner
            # authority rows are keyed under the RAW alias gym; the prepared
            # snapshot, attestation and reservation bind the canonical tenant.
            a_digest = sql('select render_manifest_digest from content_calendar where id=%s',
                           (a_rid,))[0][0]
            assert a_digest is not None
            prov = sql('select public.fixer_forward_media_provenance_lookup_20261006(%s)',
                       (a_rid,), role='staged_attester')[0][0]
            assert prov['original']['tenant_id'] == alias_gym
            assert prov['original']['source_asset_id'] == a_asset
            assert prov['manifest']['manifest_digest'] == a_digest
            assert prov['clearance']['decision'] == 'cleared_unused'
            # Provenance ACLs still fail closed for non-attester roles.
            for role in ('anon', 'authenticated', 'service_role', 'staged_owner'):
                denied('select public.fixer_forward_media_provenance_lookup_20261006(%s)',
                       (a_rid,), role=role)

            def trusted_lane():
                conn = psycopg.connect(dsn('staged_attester'))
                conn.execute('set role ' + guard.ROLE)
                return conn

            # Actual production branch: no verifier, renderer or connection
            # factory passed to guard.attest. Only the hosted network read and
            # dedicated disposable connection are replaced. The narrow SQL RPC
            # supplies callbacks; the production code replays the real recipe,
            # commits genuine object-read/lineage receipts, and the visual
            # attester fetches/hashes bytes for all three roles itself.
            from agent import visual_writer_prepare
            from agent import media_host
            with patch.object(media_host.config, 'S3_PUBLIC_BASE_URL', 'https://media.example.test'), \
                 patch.object(guard, '_connect', side_effect=trusted_lane), \
                 patch.object(visual_writer_prepare, '_bytes_for_url', side_effect=a_hosted.read):
                a_rev = sql('select public.fixer_forward_media_attestation_request_20261006(%s)',
                            (a_rid,))[0][0]['revision']
                genuine = guard.attest(a_rid, a_rev)
                visual = visual_index.attest(a_rid, a_rev, genuine['evidence_id'],
                                             tenant_key=canonical, gym_key=alias_gym,
                                             content_date='2026-10-10')
            assert visual['tenant_key'] == canonical
            assert set(visual['attestation_ids']) == set(visual_index.ROLES)
            a_attestations = list(visual['attestation_ids'].values())
            a_logical = sql('select logical_post_id from content_calendar where id=%s', (a_rid,))[0][0]
            assert sql('select tenant_id,operation,source_asset_id,manifest_digest from '
                       'fixer_forward_media_lineage_20261006 where evidence_id=%s',
                       (genuine['evidence_id'],))[0] == (canonical, 'render', a_asset, a_digest)
            assert sql('select count(*) from forward_media_visual_attestation '
                       'where lineage_receipt_id=%s', (genuine['evidence_id'],))[0][0] == 3
            assert sql('select count(*) from fixer_forward_media_object_read_20261006 '
                       'where tenant_id=%s', (canonical,))[0][0] == 2
            # Wrong canonical tenant and stale revisions cannot reuse the RPC
            # bridge, and failed callback verification creates no evidence.
            with trusted_lane() as conn:
                verify, render = attester.production_callbacks(conn, a_rid,
                                                               expected_revision=a_rev)
                captured = sql('select public.fixer_forward_media_attestation_request_20261006(%s)',
                               (a_rid,))[0][0]
                for field, value in [('tenant_id', tenant), ('gym_id', tenant),
                                     ('source_asset_id', asset), ('revision', 'wrong')]:
                    changed = dict(captured, **{field: value})
                    try:
                        verify(changed, a_bytes)
                        raise AssertionError('wrong-tenant or stale alias snapshot accepted')
                    except guard.ForwardMediaVerificationHold:
                        pass
                    try:
                        render(a_bytes, changed)
                        raise AssertionError('wrong-tenant or stale alias render accepted')
                    except guard.ForwardMediaVerificationHold:
                        pass

            # Atomic finalization: activation + reservation happen in ONE
            # transaction through the batch-ID finalizer; the reservation's
            # provenance re-check crosses the same alias handoff.
            a_candidate = [{'calendar_row_id': a_rid, 'logical_post_id': a_logical,
                            'expected_revision': a_rev, 'attestation_ids': a_attestations,
                            'expected_reservation_id': None}]
            fin = sql('select public.finalize_forward_schedule_staged_batch_20261008'
                      '(%s,%s,%s::jsonb,%s::jsonb)',
                      (canonical, a_batch, json.dumps(a_candidate, default=str), '[]'),
                      role='service_role')[0][0]
            assert fin['state'] == 'finalized' and fin['row_ids'] == [a_rid], fin
            assert len(fin['reservation_ids']) == 1 and fin['archived_old_row_ids'] == []
            # Exact retry returns the persisted receipt; no second reservation.
            assert sql('select public.finalize_forward_schedule_staged_batch_20261008'
                       '(%s,%s,%s::jsonb,%s::jsonb)',
                       (canonical, a_batch, json.dumps(a_candidate, default=str), '[]'),
                       role='service_role')[0][0] == fin
            assert sql('select tenant_id,source_asset_id,source_url,state from'
                       ' forward_schedule_reservation where calendar_row_id=%s',
                       (a_rid,))[0] == (canonical, a_asset, a_url, 'active')
            assert sql('select variant_status,media_not_ready_reason from content_calendar'
                       ' where id=%s', (a_rid,))[0] == ('active', None)
            status = sql('select public.forward_schedule_batch_status_20261008(%s)',
                         (a_batch,), role='service_role')[0][0]
            assert status['state'] == 'finalized'
            assert status['finalize_receipt']['reservation_ids'] == fin['reservation_ids']
            # Durable post-finalization callback readback must still prove
            # raw signed authority for this canonical active reservation.
            with trusted_lane() as conn:
                verify, render = attester.production_callbacks(conn, a_rid,
                                                               expected_revision=a_rev)
                assert verify(captured, a_bytes) is True
                assert render(a_bytes, captured)['image_bytes'] == a_image_bytes
            assert sql('select public.fixer_forward_schedule_staged_owner_pending_20261008(%s,%s)',
                       ([canonical], 25), role='staged_owner')[0][0] == []
            assert sql('select public.fixer_forward_schedule_staged_photo_pending_20261008(%s,%s)',
                       ([canonical], 25), role='staged_owner')[0][0] == []
            assert sql('select public.fixer_forward_schedule_staged_binder_pending_20261008(%s,%s)',
                       ([canonical], 25), role='service_role')[0][0] == []
            # Immutable aliases cannot drift; a wrong raw gym on the live row
            # must invalidate finalized provenance immediately.
            sql('update content_calendar set gym_id=%s where id=%s', (tenant, a_rid))
            denied('select public.fixer_forward_media_provenance_lookup_20261006(%s)', (a_rid,),
                   role='staged_attester', fragment='authoritative original registry binding unavailable')
            sql('update content_calendar set gym_id=%s where id=%s', (alias_gym, a_rid))
            # The durable receipt cannot attest a changed immutable revision,
            # a released reservation or an unapproved/foreign source.
            original_group = captured['group_key']
            sql('update content_calendar set visual_group_key=%s where id=%s',
                ('foreign-group', a_rid))
            denied('select public.fixer_forward_media_provenance_lookup_20261006(%s)', (a_rid,),
                   role='staged_attester', fragment='exact active reservation receipt')
            sql('update content_calendar set visual_group_key=%s where id=%s',
                (original_group, a_rid))
            for column, changed, restored in [('gym_id', tenant, alias_gym),
                                               ('review_status', 'pending', 'approved')]:
                sql('update media_asset set ' + column + '=%s where id=%s', (changed, a_asset))
                denied('select public.fixer_forward_media_provenance_lookup_20261006(%s)', (a_rid,),
                       role='staged_attester', fragment='current approved byte-bound same-gym source')
                sql('update media_asset set ' + column + '=%s where id=%s', (restored, a_asset))
            try:
                with psycopg.connect(dsn()) as conn:
                    conn.execute('set role service_role')
                    conn.execute('select public.release_forward_slot_20261008(%s,%s)',
                                 (fin['reservation_ids'][0], 'SYNTHETIC released readback'))
                    conn.execute('set role ' + guard.ROLE)
                    conn.execute('select public.fixer_forward_media_provenance_lookup_20261006(%s)',
                                 (a_rid,))
                    raise AssertionError('released finalized alias reservation admitted')
            except psycopg.errors.CheckViolation as exc:
                assert 'exact active reservation receipt' in str(exc), str(exc)
            # Failed transaction rolls back the synthetic release.
            assert sql('select state from forward_schedule_reservation where reservation_id=%s',
                       (fin['reservation_ids'][0],))[0][0] == 'active'
            # Terminal batch membership cannot reopen staged preparation.
            sql("update content_calendar set variant_status='candidate',"
                "media_not_ready_reason='forward_reservation_staged' where id=%s", (a_rid,))
            assert eligible(a_rid)['eligible'] is False
            denied('select public.fixer_forward_media_provenance_lookup_20261006(%s)', (a_rid,),
                   role='staged_attester', fragment='exact active reservation receipt')
            sql("update content_calendar set variant_status='active',media_not_ready_reason=null where id=%s",
                (a_rid,))
            # Ordinary canonical active rows continue through inherited lookup
            # and callbacks with no alias bridge or new staged membership.
            sql("update content_calendar set variant_status='active',media_not_ready_reason=null where id=%s",
                (rid,))
            normal = sql('select public.fixer_forward_media_provenance_lookup_20261006(%s)',
                         (rid,), role='staged_attester')[0][0]
            assert 'staged_alias_binding' not in normal
            normal_rev = sql('select public.fixer_forward_media_attestation_request_20261006(%s)',
                             (rid,))[0][0]['revision']
            with patch.object(media_host.config, 'S3_PUBLIC_BASE_URL', 'https://media.example.test'), \
                 patch.object(guard, '_connect', side_effect=trusted_lane), \
                 patch.object(visual_writer_prepare, '_bytes_for_url', side_effect=hosted.read):
                active_proof = guard.attest(rid, normal_rev)
            assert active_proof['revision'] == normal_rev
            recipe = identity_recipe
            # Authority tables were not touched by the handoff path.
            assert authority_counts() == (2, 2, 2, 2)

            # ============ C. rejections / stale / unknown commit ============
            # WRONG ASSET OWNERSHIP: the media asset belongs to another gym;
            # raw ownership fails closed before any byte trust or authority.
            t2 = 'gym_' + uuid.uuid4().hex
            w_asset, w_url, w_bytes, w_drive, w_hosted = new_media(t2, 'red')
            _, w_receipt = stage(t2, [member(t2, t2, w_asset, w_url)])
            w_rid = w_receipt['member_row_ids'][0]
            sql("insert into media_source values('rogue-source',%s,'gym_drive',%s,true)",
                ('rogue_' + uuid.uuid4().hex[:8], FOLDER))
            sql("update media_asset set gym_id='rogue-gym' where id=%s", (w_asset,))
            w_report = owner_pass((t2,), w_drive, w_hosted)
            assert w_report['rows'][0]['status'] == 'hold', w_report
            # Both the pre-read snapshot and the final locked recheck fail
            # closed on raw asset ownership; the durable outcome is the lock.
            assert w_report['rows'][0]['reason'] in (
                'owner_source_snapshot_invalid', 'canonical_tenant_asset_mismatch'), w_report
            assert authority_counts() == (2, 2, 2, 2)
            assert sql('select count(*) from fixer_forward_media_source_receipt_20261007'
                       ' where calendar_row_id=%s', (w_rid,))[0][0] == 0

            # STALE ROW: reserved under one revision, then the row changes
            # before the final locked phase; authority is never staged.
            t3 = 'gym_' + uuid.uuid4().hex
            s_asset, s_url, s_bytes, s_drive, s_hosted = new_media(t3, 'yellow')
            _, s_receipt = stage(t3, [member(t3, t3, s_asset, s_url)])
            s_rid = s_receipt['member_row_ids'][0]
            stale = sql('select o.row_revision,o.observation_digest from '
                        'fixer_forward_media_observation_20261007 o where o.calendar_row_id=%s',
                        (s_rid,))[0]
            token = str(uuid.uuid4())
            assert sql('select public.fixer_forward_media_owner_reserve_20261007(%s,%s,%s,%s)',
                       (s_rid, stale[0], stale[1], token), role='staged_owner')[0][0] is True
            sql("update content_calendar set caption='SYNTHETIC stale edit' where id=%s", (s_rid,))
            stale_lock = sql('select public.fixer_forward_schedule_staged_owner_locked_20261008'
                             '(%s,%s,%s,%s)', (s_rid, stale[0], stale[1], token),
                             role='staged_owner')[0][0]
            assert stale_lock['hold_reason'] == 'canonical_revision_changed', stale_lock
            assert authority_counts() == (2, 2, 2, 2)
            # The staged snapshot RPC also refuses the stale revision.
            stale_snap = sql('select public.fixer_forward_schedule_staged_owner_snapshot_20261008'
                             '(%s,%s,%s,%s)', (s_rid, stale[0], stale[1], token),
                             role='staged_owner')[0][0]
            assert stale_snap['hold_reason'] == 'owner_source_snapshot_invalid', stale_snap

            # UNKNOWN COMMIT: the final authority COMMIT response is lost; the
            # pass reports an uncertain hold, never retries, and the durable
            # progress readback (not a mutable row) resolves the truth.
            t4 = 'gym_' + uuid.uuid4().hex
            l_asset, l_url, l_bytes, l_drive, l_hosted = new_media(t4, 'black')
            _, l_receipt = stage(t4, [member(t4, t4, l_asset, l_url)])
            l_rid = l_receipt['member_row_ids'][0]
            l_report = owner_pass((t4,), l_drive, l_hosted)
            assert l_report['rows'][0]['decision'] == 'hold_uncertain'
            l_packet = sign_candidate(l_rid, t4, l_asset, l_url, receipt_for(l_rid))
            wrapped = LostCommitResponse(psycopg.connect(dsn('staged_owner')), fail_on=2)
            lost_persistence = owner.ForwardMediaOwnerPersistence(wrapped, 'staged_owner', l_hosted)
            lost_report = run_staged_photo_pass(persistence=lost_persistence, reader=l_hosted,
                                                drive_reader=l_drive, tenants=(t4,), limit=25)
            assert lost_report == {'status': 'hold', 'reason': 'uncertain_authority_commit',
                                   'rows': []}, lost_report
            assert wrapped.commits == 2
            assert sql("select state,outcome->>'status' from fixer_owner_photo_progress_20261007"
                       ' where audit_id=%s', (l_packet['payload']['audit_id'],))[0] == ('final', 'persisted')
            # The committed grant is durable exactly once; no replay created a
            # second authority tuple.
            assert authority_counts() == (3, 3, 3, 3)
            # A fresh pass never re-admits the committed audit.
            conn, persistence = owner_photo_conn()
            try:
                again = run_staged_photo_pass(persistence=persistence, reader=l_hosted,
                                              drive_reader=l_drive, tenants=(t4,), limit=25)
            finally:
                conn.close()
            assert again == {'status': 'complete', 'rows': []}
            assert authority_counts() == (3, 3, 3, 3)

            # UNCERTAIN HISTORY PRESERVED: a signed certificate can never clear
            # an asset whose exact bytes already carry a non-cleared fleet
            # clearance; uncertainty stays a durable hold, never a grant.
            t5 = 'gym_' + uuid.uuid4().hex
            h_asset, h_url, h_bytes, h_drive, h_hosted = new_media(t5, 'purple')
            _, h_receipt = stage(t5, [member(t5, t5, h_asset, h_url)])
            h_rid = h_receipt['member_row_ids'][0]
            assert owner_pass((t5,), h_drive, h_hosted)['rows'][0]['decision'] == 'hold_uncertain'
            h_receipt_row = receipt_for(h_rid)
            sql("insert into fixer_forward_media_original_registry_20261006"
                '(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,'
                "registry_evidence_ref) values(%s,%s,%s,%s,10,'SYNTHETIC earlier registry')",
                (t5, h_asset, h_url, h_receipt_row['source_fingerprint']))
            sql("insert into fixer_forward_media_history_clearance_20261006"
                '(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,'
                "registry_evidence_ref,decision,history_evidence_ref) values"
                "(%s,%s,%s,%s,10,'SYNTHETIC earlier registry','hold_uncertain','SYNTHETIC earlier doubt')",
                (t5, h_asset, h_url, h_receipt_row['source_fingerprint']))
            assert authority_counts() == (3, 4, 4, 3)
            h_packet = sign_candidate(h_rid, t5, h_asset, h_url, h_receipt_row)
            conn, persistence = owner_photo_conn()
            try:
                held_report = run_staged_photo_pass(persistence=persistence, reader=h_hosted,
                                                    drive_reader=h_drive, tenants=(t5,), limit=25)
            finally:
                conn.close()
            # The prior uncertain clearance for this exact fingerprint holds
            # the grant; nothing mutates.
            assert held_report['rows'][0]['status'] == 'hold', held_report
            assert authority_counts() == (3, 4, 4, 3)
            assert sql('select count(*) from fixer_owner_photo_reservation_20261007'
                       ' where audit_id=%s', (h_packet['payload']['audit_id'],))[0][0] == 0

            # ============ D. run_once wiring (staged + photo flags) ============
            t6 = 'gym_' + uuid.uuid4().hex
            r_asset, r_url, r_bytes, r_drive, r_hosted = new_media(t6, 'orange')
            _, r_receipt = stage(t6, [member(t6, t6, r_asset, r_url)])
            r_rid = r_receipt['member_row_ids'][0]
            assert owner_pass((t6,), r_drive, r_hosted)['rows'][0]['decision'] == 'hold_uncertain'
            r_packet = sign_candidate(r_rid, t6, r_asset, r_url, receipt_for(r_rid))
            os.environ[worker.TENANTS_ENV] = t6
            os.environ[worker.PHOTO_CLEARANCE_ENV] = 'true'
            with patch.object(owner, 'HostedObjectReader', return_value=r_hosted), \
                    patch('agent.forward_media_source_verifier.OriginalDriveReader',
                          return_value=r_drive):
                live = worker.run_once()
            assert live['status'] == 'complete', live
            assert live['rows'][0]['status'] == 'persisted'
            assert live['rows'][0]['audit_id'] == r_packet['payload']['audit_id']
            assert authority_counts()[:2] == (4, 5)
            # The loop entry point honors the same default-OFF gate.
            os.environ.pop(worker.WORKER_ENV)
            assert worker.run_once() == {'status': 'disabled', 'rows': []}
            os.environ[worker.WORKER_ENV] = 'true'

            # ============ E. ACLs fail closed ============
            obs = sql('select row_revision,observation_digest from '
                      'fixer_forward_media_observation_20261007 where calendar_row_id=%s',
                      (r_rid,))[0]
            for role in ('anon', 'authenticated', 'service_role', 'staged_auditor'):
                denied('select public.fixer_forward_schedule_staged_owner_snapshot_20261008(%s,%s,%s,%s)',
                       (r_rid, obs[0], obs[1], str(uuid.uuid4())), role=role)
                denied('select public.fixer_forward_schedule_staged_owner_locked_20261008(%s,%s,%s,%s)',
                       (r_rid, obs[0], obs[1], str(uuid.uuid4())), role=role)
                denied('select public.fixer_prepare_owner_staged_photo_20261008(%s,%s::jsonb,%s::jsonb)',
                       (r_packet['payload']['audit_id'], '{}', '{}'), role=role)
            denied('select public.fixer_forward_schedule_canonical_tenant_20261008(%s)',
                   ('gym',), role='service_role')
            for role in ('anon', 'authenticated', 'staged_owner'):
                denied('select public.fixer_bind_forward_schedule_staged_manifest_20261008(%s)',
                       (r_rid,), role=role)
            # The staged grant is isolated: service_role membership on the
            # caller is refused even when the owner role is present.
            sql('grant service_role to staged_owner')
            try:
                denied('select public.fixer_prepare_owner_staged_photo_20261008(%s,%s::jsonb,%s::jsonb)',
                       (r_packet['payload']['audit_id'], '{}', '{}'), role='staged_owner',
                       fragment='isolated dedicated owner required')
            finally:
                sql('revoke service_role from staged_owner')

            # No unauthorized mutation anywhere: only the four genuine grants
            # and the one synthetic uncertain clearance exist.
            final = authority_counts()
            assert final == (4, 5, 5, 4), final
            assert sql('select count(*) from fixer_forward_media_history_clearance_20261006'
                       " where decision='cleared_unused'")[0][0] == 4
            assert sql('select count(*) from content_calendar where render_manifest_digest'
                       ' is not null')[0][0] == 2

            # A second platform rendition can reuse the exact logical slot
            # while keeping its own signed manifest, revision and evidence.
            # This late batch ensures the non-primary binding survives its
            # own terminal receipt independently of the incumbent's proof.
            b_recipe = attester.make_still_recipe('gbp_crop_4x3')
            b_image = attester.replay_still_recipe(a_bytes, b_recipe)['image_bytes']
            assert b_image != a_image_bytes
            b_url = 'https://media.example.test/' + uuid.uuid4().hex + '.jpg'
            a_hosted.objects[b_url] = b_image
            b_member = member(canonical, alias_gym, a_asset, a_url)
            b_member['row'].update(account='facebook', image_url=b_url,
                                   logical_post_id=str(a_logical),
                                   visual_group_key=captured['group_key'])
            b_member['observation'] = observation(canonical, b_member['row'],
                                                  recipe_override=b_recipe)
            b_batch, b_stage = stage(canonical, [b_member])
            b_rid = b_stage['member_row_ids'][0]
            b_owner = owner_pass((canonical,), a_drive, a_hosted)
            assert b_owner['rows'][0]['status'] == 'hold', b_owner
            b_src = receipt_for(b_rid)
            b_packet = sign_candidate(b_rid, alias_gym, a_asset, a_url, b_src,
                                      image_url=b_url, image_bytes=b_image,
                                      recipe_override=b_recipe)
            conn, persistence = owner_photo_conn()
            try:
                b_photo = run_staged_photo_pass(persistence=persistence, reader=a_hosted,
                                                drive_reader=a_drive, tenants=(canonical,), limit=25)
            finally:
                conn.close()
            assert b_photo['status'] == 'complete' and b_photo['rows'][0]['status'] == 'persisted', b_photo
            assert sql('select public.fixer_bind_forward_schedule_staged_manifest_20261008(%s)',
                       (b_rid,), role='service_role')[0][0] is True
            with patch.object(media_host.config, 'S3_PUBLIC_BASE_URL', 'https://media.example.test'), \
                 patch.object(guard, '_connect', side_effect=trusted_lane), \
                 patch.object(visual_writer_prepare, '_bytes_for_url', side_effect=a_hosted.read):
                b_snapshot = sql('select public.fixer_forward_media_attestation_request_20261006(%s)',
                                 (b_rid,))[0][0]
                b_rev = b_snapshot['revision']
                b_lineage = guard.attest(b_rid, b_rev)
                b_visual = visual_index.attest(b_rid, b_rev, b_lineage['evidence_id'],
                                              tenant_key=canonical, gym_key=alias_gym,
                                              content_date='2026-10-10')
            b_ids = list(b_visual['attestation_ids'].values())
            assert b_rev != a_rev and b_lineage['evidence_id'] != genuine['evidence_id']
            assert set(b_ids).isdisjoint(a_attestations)
            b_candidate = [{'calendar_row_id': b_rid, 'logical_post_id': str(a_logical),
                            'expected_revision': b_rev, 'attestation_ids': b_ids,
                            'expected_reservation_id': None}]
            # Primary platform proof cannot stand in for the sibling proof.
            for wrong_field, primary in [('expected_revision', a_rev),
                                          ('attestation_ids', a_attestations)]:
                wrong = [dict(b_candidate[0], **{wrong_field: primary})]
                denied('select public.finalize_forward_schedule_staged_batch_20261008'
                       '(%s,%s,%s::jsonb,%s::jsonb)',
                       (canonical, b_batch, json.dumps(wrong), '[]'), role='service_role',
                       fragment='revision' if wrong_field == 'expected_revision' else 'evidence')
            b_fin = sql('select public.finalize_forward_schedule_staged_batch_20261008'
                        '(%s,%s,%s::jsonb,%s::jsonb)',
                        (canonical, b_batch, json.dumps(b_candidate), '[]'), role='service_role')[0][0]
            assert b_fin['reservation_ids'] == fin['reservation_ids']
            assert sql('select calendar_row_id,row_revision,lineage_evidence_id,attestation_ids '
                       'from forward_schedule_reservation where reservation_id=%s',
                       (fin['reservation_ids'][0],))[0] == (
                           uuid.UUID(a_rid), a_rev, uuid.UUID(genuine['evidence_id']),
                           [uuid.UUID(i) for i in a_attestations])
            assert sql('select row_revision,lineage_evidence_id,attestation_ids from '
                       'forward_schedule_reservation_binding_20261008 where reservation_id=%s '
                       'and calendar_row_id=%s', (fin['reservation_ids'][0], b_rid))[0] == (
                           b_rev, uuid.UUID(b_lineage['evidence_id']), [uuid.UUID(i) for i in b_ids])
            # Both platform callbacks still independently verify their own
            # signed rendition after the sibling has finalized.
            for row_id, revision, snap_row, expected_image in (
                    (a_rid, a_rev, captured, a_image_bytes),
                    (b_rid, b_rev, b_snapshot, b_image)):
                with trusted_lane() as conn:
                    verify, render = attester.production_callbacks(conn, row_id,
                                                                   expected_revision=revision)
                    assert verify(snap_row, a_bytes) is True
                    assert render(a_bytes, snap_row)['image_bytes'] == expected_image
                    other = b_snapshot if row_id == a_rid else captured
                    for callback in (lambda: verify(other, a_bytes),
                                     lambda: render(a_bytes, other)):
                        try:
                            callback()
                            raise AssertionError('platform sibling snapshot replay accepted')
                        except guard.ForwardMediaVerificationHold:
                            pass
            assert sql('select public.finalize_forward_schedule_staged_batch_20261008'
                       '(%s,%s,%s::jsonb,%s::jsonb)',
                       (canonical, b_batch, json.dumps(b_candidate), '[]'), role='service_role')[0][0] == b_fin
            # Swapping the sibling's account changes its revision even though
            # tenant, logical slot and source remain identical.
            sql("update content_calendar set account='instagram' where id=%s", (b_rid,))
            denied('select public.fixer_forward_media_provenance_lookup_20261006(%s)', (b_rid,),
                   role='staged_attester', fragment='exact active reservation receipt')
            sql("update content_calendar set account='facebook' where id=%s", (b_rid,))
            # The new sibling adds one signed rendition, with no duplicate
            # original/clearance or schedule reservation.
            assert authority_counts() == (final[0] + 1, final[1], final[2], final[3] + 1)
            assert sql('select count(*) from forward_schedule_reservation where tenant_id=%s',
                       (canonical,))[0][0] == 1

            print('PASS: PG17 staged preparation; owner staged pass stages exact provenance with '
                  'durable uncertain hold and no invented authority; signed staged photo grant '
                  'creates exactly one positive reservation/registry/clearance/manifest through '
                  'the staged-specific RPC; staged binder binds the exact manifest digest before '
                  'finalization and stays eligible; alias success with canonical tenant agreement '
                  'and raw asset/source ownership; actual production callbacks replay a transformed feed, '
                  'commit lineage and all three visual roles, reserve and finalize, then prove durable '
                  'finalized alias readback and non-primary Facebook sibling with own bound proof; '
                  'primary-proof/snapshot replay and sibling account drift denied; released reservation, terminal prep, revision drift and '
                  'unapproved source holds; active canonical regression; wrong tenant/asset rejection; stale revision '
                  'hold; lost COMMIT never retried and resolved only by durable progress readback; '
                  'prior used/uncertain history stays held; run_once wiring; role ACLs fail closed; '
                  'active-only RPCs refuse staged rows')
        finally:
            subprocess.run([_pg('pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           capture_output=True, timeout=60)


def test_staged_preparation_pg():
    skip = _skipped()
    if skip:
        import pytest
        pytest.skip(skip)
    main()


if __name__ == '__main__':
    main()
