"""Standalone disposable PG17 acceptance for DRAFT_fixer_forward_schedule_worker_discovery_20261008.

Run `python3 tests/test_forward_schedule_worker_discovery_pg.py` (also under pytest;
module-level main() runs in test_worker_discovery_pg). Uses a real disposable
PostgreSQL 17 cluster on a Unix socket; no DSN, network, production connection,
installs or persisted cluster. Requires the stage draft SQL from the sibling
reservation checkout (read-only; override with FORWARD_STAGE_SQL). Skips cleanly
when PG17 binaries or the stage draft are unavailable.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STAGE_SQL = Path(os.environ.get(
    'FORWARD_STAGE_SQL',
    ROOT.parent / 'echo-schedule-reservation-20261008'
    / 'migrations/DRAFT_fixer_forward_schedule_stage_20261008.sql'))


def _pg(name):
    bundled = Path('/opt/homebrew/opt/postgresql@17/bin') / name
    return str(bundled) if bundled.is_file() else shutil.which(name)


def _skipped():
    if any(not _pg(n) for n in ('initdb', 'pg_ctl', 'postgres')):
        return 'existing initdb/pg_ctl unavailable; disposable PostgreSQL not provisioned'
    if not STAGE_SQL.is_file():
        return f'stage draft unavailable at {STAGE_SQL}; set FORWARD_STAGE_SQL'
    return None


def main():
    skip = _skipped()
    if skip:
        print('SKIP: ' + skip)
        return
    import psycopg
    assert ' 17.' in subprocess.check_output([_pg('postgres'), '--version'], text=True)
    with tempfile.TemporaryDirectory(prefix='fwd_discovery_pg_', dir='/tmp') as tmp:
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
            sql('grant select,insert,update,delete on public.content_calendar to service_role;')
            sql('create role discovery_owner login; grant fixer_forward_media_owner_20261006 to discovery_owner;')
            sql('update forward_media_visual_gate_20261008 set enabled=true;')
            sql('update forward_schedule_reservation_gate_20261008 set enabled=true;')

            def observation(tenant, row):
                digest_input_obj = {
                    'schema_version': 1, 'provenance_status': 'unverified',
                    'tenant': tenant, 'source_asset_id': row['source_media_asset_id'],
                    'source_exact_url': row['source_media_url'],
                    'delivered_exact_url': row['image_url'],
                    'source_sha256': uuid.uuid4().hex + uuid.uuid4().hex,
                    'delivered_sha256': uuid.uuid4().hex + uuid.uuid4().hex,
                    'source_byte_length': 10, 'delivered_byte_length': 10,
                    'recipe': {'kind': 'fixture'}, 'hold_reasons': []}
                digest_input = json.dumps(digest_input_obj, sort_keys=True, separators=(',', ':'))
                digest = hashlib.sha256(digest_input.encode()).hexdigest()
                return {'observation_json': json.dumps(dict(digest_input_obj, observation_digest=digest),
                                                       sort_keys=True, separators=(',', ':')),
                        'digest_input': digest_input}

            def member(tenant, with_obs=True, gym=None):
                url = 'https://scratch.example/' + uuid.uuid4().hex
                row = {'id': str(uuid.uuid4()), 'gym_id': gym or tenant, 'post_date': '2026-10-10',
                       'status': 'pending', 'account': 'instagram', 'format': 'feed',
                       'logical_post_id': str(uuid.uuid4()),
                       'source_media_url': url, 'image_url': url,
                       'source_media_asset_id': 'staged_' + uuid.uuid4().hex,
                       'visual_group_key': 'vg_' + uuid.uuid4().hex}
                return {'row': row, 'observation': observation(tenant, row) if with_obs else None}

            def stage(tenant, members, batch_id=None, old_rows=None):
                batch_id = batch_id or str(uuid.uuid4())
                request = json.dumps({'members': members, 'old_rows': old_rows or []},
                                     sort_keys=True, separators=(',', ':'))
                digest = hashlib.sha256(request.encode()).hexdigest()
                receipt = sql('select public.stage_forward_schedule_batch_20261008(%s,%s,%s,%s)',
                              (tenant, batch_id, request, digest), role='service_role')[0][0]
                return batch_id, receipt

            def eligible(rid, role='discovery_owner'):
                return sql('select public.forward_schedule_preparation_eligible_20261008(%s)',
                           (rid,), role=role)[0][0]

            def owner_pending(tenants, limit=25, role='discovery_owner'):
                return sql('select public.fixer_forward_schedule_staged_owner_pending_20261008(%s,%s)',
                           (tenants, limit), role=role)[0][0]

            def active_pending(tenants, limit=25, role='discovery_owner'):
                return sql('select public.fixer_forward_media_owner_pending_20261007(%s,%s)',
                           (tenants, limit), role=role)[0][0]

            def binder_pending(tenants, limit=25, role='service_role'):
                return sql('select public.fixer_forward_schedule_staged_binder_pending_20261008(%s,%s)',
                           (tenants, limit), role=role)[0][0]

            tenant = 'gym_' + uuid.uuid4().hex
            batch, receipt = stage(tenant, [member(tenant), member(tenant, with_obs=False)])
            observed_id, plain_id = receipt['member_row_ids']

            # Staged discovery returns the exact observed member with its batch
            # and canonical tenant; the predicate agrees; the active lane stays
            # active-only and never sees staged rows.
            found = owner_pending([tenant])
            assert [c['calendar_row_id'] for c in found] == [observed_id], found
            assert found[0]['batch_id'] == batch and found[0]['tenant_id'] == tenant
            assert len(found[0]['revision']) == 32 and len(found[0]['observation_digest']) == 64
            assert eligible(observed_id)['mode'] == 'staged'
            assert active_pending([tenant]) == []
            # The member WITHOUT an observation is not an owner candidate.
            assert all(c['calendar_row_id'] != plain_id for c in found)

            # A real ACTIVE row with a recorded observation stays in the active
            # lane and never appears in staged discovery (active regression).
            active_id = str(uuid.uuid4())
            asset = 'asset_' + uuid.uuid4().hex
            url = 'https://scratch.example/' + uuid.uuid4().hex
            sql("insert into media_source values(%s,%s,'gym_drive','folder',true)", ('src_' + asset, tenant))
            sql('insert into media_asset(id,source_id,gym_id) values(%s,%s,%s)',
                (asset, 'src_' + asset, tenant))
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "visual_group_key,source_media_asset_id,source_media_url,image_url)"
                " values(%s,%s,'2026-10-11','pending','active','group',%s,%s,%s)",
                (active_id, tenant, asset, url, url))
            row = sql('select to_jsonb(r) from content_calendar r where id=%s', (active_id,))[0][0]
            obs = observation(tenant, {'source_media_asset_id': asset,
                                       'source_media_url': url, 'image_url': url})
            recorded = sql('select public.fixer_record_forward_media_observation_20261007(%s,%s::jsonb,%s,%s)',
                           (active_id, json.dumps(row), obs['observation_json'], obs['digest_input']))[0][0]
            assert [c['calendar_row_id'] for c in active_pending([tenant])] == [active_id]
            assert all(c['calendar_row_id'] != active_id for c in owner_pending([tenant]))

            # FORGED MARKER: candidate + marker but no registered membership is
            # never discovered and the predicate refuses it as unregistered.
            forged_id = str(uuid.uuid4())
            sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                "media_not_ready_reason,visual_group_key,source_media_asset_id,"
                "source_media_url,image_url,logical_post_id)"
                " values(%s,%s,'2026-10-12','pending','candidate','forward_reservation_staged',"
                "'group','staged_forged','https://scratch.example/f','https://scratch.example/f',%s)",
                (forged_id, tenant, str(uuid.uuid4())))
            assert eligible(forged_id)['reason'] == 'unregistered staged row'
            assert all(c['calendar_row_id'] != forged_id for c in owner_pending([tenant]))

            # WRONG TENANT: another tenant's allowlist never sees the row.
            assert owner_pending(['other_' + uuid.uuid4().hex]) == []
            # Canonical tenant via alias: a member staged under an alias gym is
            # discovered only under the canonical tenant, never under the alias.
            alias_gym = 'alias_' + uuid.uuid4().hex
            canonical = 'gym_' + uuid.uuid4().hex
            sql('insert into fixer_forward_media_tenant_alias_20261006(alias_key,tenant_id) values(%s,%s)',
                (alias_gym, canonical))
            alias_batch, alias_receipt = stage(canonical, [member(canonical, gym=alias_gym)])
            alias_id = alias_receipt['member_row_ids'][0]
            assert owner_pending([alias_gym]) == []
            assert [c['calendar_row_id'] for c in owner_pending([canonical])] == [alias_id]
            assert owner_pending([canonical])[0]['tenant_id'] == canonical

            # BINDING DRIFT after staging: any content/media change removes the
            # row from discovery and the predicate reports the exact reason.
            sql("update content_calendar set image_url='https://scratch.example/drifted' where id=%s",
                (observed_id,))
            assert owner_pending([tenant]) == []
            assert eligible(observed_id)['reason'] == 'content/media binding changed'
            original_url = sql('select source_media_url from content_calendar where id=%s',
                               (observed_id,))[0][0]
            sql('update content_calendar set image_url=%s where id=%s', (original_url, observed_id))
            assert [c['calendar_row_id'] for c in owner_pending([tenant])] == [observed_id]
            # Claim marker also excludes.
            sql('update content_calendar set publish_claim_token=%s where id=%s',
                (str(uuid.uuid4()), observed_id))
            assert owner_pending([tenant]) == []
            assert eligible(observed_id)['reason'] == 'publish claim exists'
            sql('update content_calendar set publish_claim_token=null where id=%s', (observed_id,))

            # DUPLICATE/REUSE: a staged calendar row can never join a second
            # batch, discovery lists it once, and the bound limit is honored.
            try:
                stage(tenant, [{'row': dict(member(tenant)['row'], id=observed_id), 'observation': None}])
                raise AssertionError('staged row unexpectedly moved batches')
            except psycopg.Error as exc:
                assert 'already staged' in str(exc)
            ids = [c['calendar_row_id'] for c in owner_pending([tenant, canonical])]
            assert len(ids) == len(set(ids)) == 2
            assert len(owner_pending([tenant, canonical], limit=1)) == 1
            # Bounds are enforced exactly like the active lane.
            for bad in ([], ['x'] * 33):
                try:
                    owner_pending(bad)
                    raise AssertionError('unbounded tenant scope accepted')
                except psycopg.Error as exc:
                    assert 'bounded owner tenant scope required' in str(exc)

            # TERMINAL BATCH: once finalized, members leave staged discovery and
            # the predicate reports the terminal state.
            sql("update forward_schedule_stage_batch_20261008 set state='finalized',"
                " finalize_request='{}'::jsonb, finalize_receipt='{}'::jsonb, finalized_at=now()"
                ' where batch_id=%s', (alias_batch,))
            assert owner_pending([canonical]) == []
            assert eligible(alias_id)['reason'] == 'stage batch terminal or unavailable'

            # BINDER staged discovery: service-role only; empty until the exact
            # owner authority tuple (registry + cleared history + manifest for
            # the member media binding) exists.
            assert binder_pending([tenant]) == []
            digest64 = 'sha256:' + uuid.uuid4().hex + uuid.uuid4().hex
            member_row = sql('select source_media_url,image_url,source_media_asset_id from content_calendar'
                             ' where id=%s', (observed_id,))[0]
            sql('insert into fixer_forward_media_original_registry_20261006'
                '(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref)'
                " values(%s,%s,%s,%s,10,'SYNTHETIC owner-verified original')",
                (tenant, member_row[2], member_row[0], 'md5:' + uuid.uuid4().hex))
            # The certified-positive-clearance trigger belongs to the photo
            # layer (covered by its own PG test); bypass it only to plant the
            # synthetic authority fixture for binder discovery.
            sql('alter table fixer_forward_media_history_clearance_20261006 disable trigger certified_positive_clearance;')
            sql('insert into fixer_forward_media_history_clearance_20261006'
                '(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,'
                "decision,history_evidence_ref) select tenant_id,source_asset_id,source_url,source_fingerprint,"
                "source_length,registry_evidence_ref,'cleared_unused','SYNTHETIC independent audit'"
                ' from fixer_forward_media_original_registry_20261006 where tenant_id=%s and source_asset_id=%s',
                (tenant, member_row[2]))
            sql('alter table fixer_forward_media_history_clearance_20261006 enable trigger certified_positive_clearance;')
            sql('insert into fixer_forward_media_render_manifest_20261006'
                '(manifest_digest,tenant_id,source_asset_id,image_url,image_fingerprint,image_length,'
                "operation,render_evidence_ref) values(%s,%s,%s,%s,%s,10,'same_object','SYNTHETIC render')",
                (digest64, tenant, member_row[2], member_row[1], 'md5:' + uuid.uuid4().hex))
            bound = binder_pending([tenant])
            assert [c['calendar_row_id'] for c in bound] == [observed_id], bound
            assert bound[0]['batch_id'] == batch and bound[0]['manifest_digest'] == digest64
            # Drift also removes binder candidates.
            sql("update content_calendar set image_url='https://scratch.example/drifted2' where id=%s",
                (observed_id,))
            assert binder_pending([tenant]) == []
            sql('update content_calendar set image_url=%s where id=%s', (member_row[1], observed_id))
            assert len(binder_pending([tenant])) == 1

            # PHOTO staged discovery: owner-only; empty without a complete
            # signed certificate chain, and never callable by service_role.
            assert sql('select public.fixer_forward_schedule_staged_photo_pending_20261008(%s,%s)',
                       ([tenant], 25), role='discovery_owner')[0][0] == []

            # ACLs: staged discovery is limited to its isolated role.
            for role in ('anon', 'authenticated', 'service_role'):
                denied('select public.fixer_forward_schedule_staged_owner_pending_20261008(%s,%s)',
                       ([tenant], 25), role=role)
                denied('select public.fixer_forward_schedule_staged_photo_pending_20261008(%s,%s)',
                       ([tenant], 25), role=role)
            for role in ('anon', 'authenticated', 'discovery_owner'):
                denied('select public.fixer_forward_schedule_staged_binder_pending_20261008(%s,%s)',
                       ([tenant], 25), role=role)
            denied('select public.fixer_forward_schedule_staged_authorized_20261008(%s,%s,%s)',
                   (observed_id, tenant, batch), role='service_role')
            # The active-lane discovery ACL is unchanged (owner only).
            denied('select public.fixer_forward_media_owner_pending_20261007(%s,%s)',
                   ([tenant], 25), role='service_role')

            print('PASS: PG17 staged worker discovery; staged owner discovery binds exact '
                  'observation/revision/membership/batch and canonical tenant (incl. alias); '
                  'active lane regression (active row stays active-only, staged rows excluded); '
                  'forged marker without membership refused; wrong tenant empty; binding drift '
                  'and claim exclusion with exact predicate reasons; duplicate/re-stage refusal, '
                  'dedupe and bound enforcement; finalized batch terminal; binder discovery '
                  'requires the exact owner authority tuple and drops on drift; photo discovery '
                  'owner-only and certificate-gated; role ACLs fail closed')
        finally:
            subprocess.run([_pg('pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                           capture_output=True, timeout=60)


def test_worker_discovery_pg():
    skip = _skipped()
    if skip:
        import pytest
        pytest.skip(skip)
    main()


if __name__ == '__main__':
    main()
