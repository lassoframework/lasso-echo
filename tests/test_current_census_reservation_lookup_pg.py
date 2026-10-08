"""Disposable composed PG17 provenance/attestation/claim regression.

Retained legacy original authority and census fixtures are explicitly synthetic
trusted migration-owner inserts. Actual ordered DRAFT functions, alias stage
membership, isolated attestation and final claims run without provider/network
operations, signed-source forgery, table-guard bypass or production activation.
"""
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import uuid

import pytest

from agent import forward_media_prepare as prepare
from agent.forward_media_photo_certificate import canonical
from agent.visual_scene import scene_fingerprint
from tests.test_forward_media_still_v2_owner_transport_pg import pg17_bin

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / 'migrations/DRAFT_fixer_current_census_reservation_lookup_20261008.sql'
PORTAL = ROOT.parent / 'portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.sql'


def test_current_census_reservation_lookup_pg():
    psycopg = pytest.importorskip('psycopg')
    if not PORTAL.is_file():
        pytest.skip('assembled sibling portal source-brand draft unavailable')
    try:
        pg = pg17_bin()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    assert shutil.disk_usage('/tmp').free > 5 * 1024**3
    with tempfile.TemporaryDirectory(prefix='current_census_lookup_pg_', dir='/tmp') as temp:
        root = Path(temp)
        sock = root / 'sock'
        sock.mkdir()
        port = random.randint(41000, 59000)
        subprocess.run([str(pg/'initdb'), '-D', str(root/'data'), '-U', 'postgres', '--no-sync'],
                       check=True, capture_output=True, timeout=60)
        subprocess.run([str(pg/'pg_ctl'), '-D', str(root/'data'), '-l', str(root/'pg.log'),
                        '-o', f"-k {sock} -p {port} -c listen_addresses=''", '-w', 'start'],
                       check=True, capture_output=True, timeout=60)
        connections = []
        try:
            dsn = f'host={sock} port={port} dbname=postgres user=postgres'
            admin = psycopg.connect(dsn, autocommit=True)
            connections.append(admin)
            def sql(query, args=None, role=None):
                conn = admin if role is None else psycopg.connect(dsn)
                try:
                    if role:
                        conn.execute('set role ' + role)
                    if query != 'rollback':
                        conn.execute("set statement_timeout='5s'")
                    cur = conn.execute(query, args)
                    out = cur.fetchall() if cur.description else None
                    if role:
                        conn.commit()
                    return out
                finally:
                    if role:
                        conn.close()
            def rpc(name, *args, role=None):
                return sql('select public.' + name + '(' + ','.join(['%s'] * len(args)) + ')', args, role)[0][0]
            def denied(name, *args, role='fixer_forward_media_attester_20261006', phrase=None):
                with pytest.raises(psycopg.Error, match=phrase):
                    rpc(name, *args, role=role)
            sql('''create role anon;create role authenticated;create role service_role;
                create table content_calendar(id uuid primary key,gym_id text,post_date date,account text,format text,gbp_location_id text,status text,variant_status text,published_at timestamptz,publish_claim_token uuid,publish_reservation_day date,late_post_id text,image_url text,thumbnail_url text,media_not_ready_reason text,caption text);
                create table media_source(id text primary key,gym_id text,kind text,folder_id text,active boolean,sync_status text,sync_finished_at timestamptz);
                create table media_asset(id text primary key,source_id text,gym_id text,content_hash text,rendition_url text,kind text,eligible boolean,excluded_by_coach boolean,review_status text,moderation_status text,review_content_hash text,reviewed_by text,reviewed_at timestamptz,moderation_json jsonb,people_detected boolean,used_count integer);
                create table gyms(id uuid primary key);
                create table app_users(id uuid primary key,clerk_user_id text unique,role text,email text);
                create table echo_intake_tokens(gym_id uuid primary key,echo_account_key text unique);''')
            def apply(name):
                sql((ROOT/'migrations'/name).read_text())
            for name in ('logical_post_id_20261004.sql', 'DRAFT_fixer_forward_media_claim_20261006.sql',
                         'DRAFT_fixer_forward_visual_index_20261008.sql',
                         'DRAFT_fixer_forward_media_observation_bridge_20261007.sql',
                         'DRAFT_fixer_forward_media_source_history_20261007.sql',
                         'DRAFT_fixer_forward_media_owner_transport_20261007.sql',
                         'DRAFT_fixer_forward_media_photo_certificate_20261007.sql'):
                apply(name)
            cases = []
            # Retained synthetic legacy authority is seeded BEFORE prospective
            # positive-clearance guards. Do not disable or bypass those guards.
            for case, raw, tenant in (('ordinary', 'current-gym', 'current-gym'),
                                      ('alias', 'current-alias', 'canonical-alias'),
                                      ('canonical', 'canonical-original-alias', 'canonical-original-tenant')):
                from PIL import Image
                # Actual canonical fixture pHash is 36 bits from both prior
                # fixtures, outside the existing 7-30 similarity review fence.
                image_seed = 'canonical-6' if case == 'canonical' else case
                image = Image.frombytes('RGB', (128, 128), random.Random(image_seed).randbytes(128*128*3))
                stream = io.BytesIO()
                image.save(stream, format='PNG')
                pixels = stream.getvalue()
                asset, url = 'legacy-' + case, 'https://owned.example/' + case + '.png'
                authority_tenant = tenant if case == 'canonical' else raw
                registration = prepare.OriginalRegistration(authority_tenant, asset, url, 'md5:' + hashlib.md5(pixels).hexdigest(),
                                                             len(pixels), 'SYNTHETIC retained exact original ' + case)
                original = asdict(registration)
                manifest = asdict(prepare.build_render_manifest(registration, url, pixels, 'same_object',
                                                                'SYNTHETIC retained exact same object'))
                sql('insert into fixer_forward_media_original_registry_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref) values(%s,%s,%s,%s,%s,%s)',
                    tuple(original.values()))
                sql("insert into fixer_forward_media_history_clearance_20261006(tenant_id,source_asset_id,source_url,source_fingerprint,source_length,registry_evidence_ref,decision,history_evidence_ref) "
                    "values(%s,%s,%s,%s,%s,%s,'cleared_unused','SYNTHETIC retained legacy clearance')", tuple(original.values()))
                columns = list(manifest)
                sql('insert into fixer_forward_media_render_manifest_20261006(' + ','.join(columns) + ') values(' + ','.join(['%s']*len(columns)) + ')', tuple(manifest.values()))
                cases.append(dict(case=case, raw=raw, tenant=tenant, bytes=pixels, original=original, manifest=manifest))
            for name in ('DRAFT_fixer_owner_photo_clearance_20261007.sql', 'DRAFT_fixer_generated_owner_20261007.sql',
                         'DRAFT_fixer_forward_schedule_reservation_20261008.sql',
                         'DRAFT_fixer_forward_schedule_stage_20261008.sql',
                         'DRAFT_fixer_forward_schedule_worker_discovery_20261008.sql',
                         'DRAFT_fixer_forward_schedule_staged_preparation_20261008.sql',
                         'DRAFT_fixer_generated_gap_dispatch_20261007.sql',
                         'DRAFT_generated_source_palette_authority_20261007.sql',
                         'DRAFT_generated_send_lease_20261007.sql'):
                apply(name)
            sql(PORTAL.read_text())
            apply('DRAFT_fixer_generated_bundle_bridge_20261008.sql')
            apply('DRAFT_fixer_generated_local_census_producer_20261008.sql')
            for name in ('DRAFT_fixer_photo_historical_clearance_20261008.sql',
                         'DRAFT_fixer_prospective_photo_authority_20261008.sql',
                         'DRAFT_fixer_photo_historical_clearance_20261008.sql',
                         'DRAFT_fixer_prospective_still_v2_20261008.sql',
                         'DRAFT_fixer_still_v2_owner_transport_20261008.sql'):
                apply(name)
            catalog_query = "select oid,proname,proowner,proacl::text,proconfig,prosrc from pg_proc where (proname like '%provenance%' or proname='fixer_still_occupancy_check_20261007') and pronamespace='public'::regnamespace order by oid"
            before = sql(catalog_query)
            selectors = [row for row in before if 'order by reserved_at limit 1' in row[-1]
                         and 'fixer_still_reservation_20261007' in row[-1]]
            assert {row[1] for row in selectors} == {'fixer_pre_staged_provenance_20261008', 'fixer_forward_media_provenance_lookup_20261006'}
            print('BEFORE selector OIDs:', [(row[0], row[1]) for row in selectors])
            sql(OVERLAY.read_text())
            after = sql(catalog_query)
            assert [row[:-1] for row in after] == [row[:-1] for row in before]
            for old, new in zip(before, after):
                if old in selectors:
                    assert 'fixer_current_census_reservation_private_20261008' in new[-1]
                    assert 'order by reserved_at limit 1' not in new[-1]
                    for guard in ('fixer_prospective_conflict_fence_20261008', 'fixer_assert_still_row_v2_or_v1_20261008',
                                  'fixer_still_negative_check_20261007', 'fixer_still_final_check_20261007'):
                        assert old[-1].count(guard) == new[-1].count(guard) > 0
                elif old[1] == 'fixer_still_occupancy_check_20261007':
                    assert 'u.tenant_id=r.gym_id' in old[-1]
                    assert 'u.tenant_id=r.gym_id' not in new[-1]
                    assert 'fixer_forward_media_tenant_alias_20261006' in new[-1]
                    assert old[-1].count('fixer_still_negative_check_20261007') == new[-1].count('fixer_still_negative_check_20261007') == 1
                else:
                    assert old == new
            for role in ('anon', 'authenticated', 'service_role', 'fixer_forward_media_owner_20261006',
                         'fixer_forward_media_attester_20261006', 'fixer_forward_media_photo_auditor_20261007'):
                assert not sql("select has_function_privilege(%s,'public.fixer_current_census_reservation_private_20261008(uuid,jsonb)','EXECUTE')", (role,))[0][0]
            print('AFTER OIDs/options/ACLs preserved, ordinary and alias selectors replaced, all composed guards retained')
            sql('create role current_owner;grant fixer_forward_media_owner_20261006 to current_owner;'
                'grant select,insert,update,delete on content_calendar to service_role;')
            baseline = uuid.uuid4()
            sql("insert into fixer_forward_media_photo_policy_20261007 values('SYNTHETIC current lookup',true,'complete_fleet_still_photo_history',null,'SYNTHETIC routes','SYNTHETIC reviewer')")
            sql("insert into fixer_forward_media_photo_baseline_20261007(baseline_id,policy_id,scope_complete,rows_json,historical_manifest_ref,declared_full_fleet_row_count) "
                "values(%s,'SYNTHETIC current lookup',true,'[]','SYNTHETIC empty corpus',0)", (baseline,))
            sql("update fixer_forward_media_photo_state_20261007 set baseline_id=%s,generation=1,enabled=true,routes_reconciled_ref='SYNTHETIC routes'", (baseline,))
            rpc('fixer_still_cutover_control_20261007', True, 'SYNTHETIC disposable cutover', role='current_owner')
            epoch = sql('select epoch_id from fixer_still_cutover_20261007')[0][0]
            sql('update forward_schedule_reservation_gate_20261008 set enabled=true')
            sql('update forward_media_visual_gate_20261008 set enabled=true')
            for item in cases:
                _exercise_case(item, sql, rpc, denied, epoch)
        finally:
            for conn in connections:
                conn.close()
            subprocess.run([str(pg/'pg_ctl'), '-D', str(root/'data'), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, timeout=60)


def _exercise_case(item, sql, rpc, denied, epoch):
    raw, tenant, original, manifest = (item[k] for k in ('raw', 'tenant', 'original', 'manifest'))
    canonical_legacy = item['case'] == 'canonical'
    rid, logical, batch = (uuid.uuid4() for _ in range(3))
    group = 'vg_' + logical.hex
    if raw != tenant:
        sql('insert into fixer_forward_media_tenant_alias_20261006 values(%s,%s)', (raw, tenant))
    row = dict(id=str(rid), gym_id=raw, logical_post_id=str(logical), post_date='2026-10-10',
               account='instagram', format='feed', status='pending', source_media_asset_id=original['source_asset_id'],
               source_media_url=original['source_url'], image_url=original['source_url'], visual_group_key=group,
               caption='SYNTHETIC current reservation')
    def insert_active(candidate):
        columns = list(candidate) + ['variant_status']
        sql('insert into content_calendar(' + ','.join(columns) + ') values(' + ','.join(['%s']*len(columns)) + ')',
            tuple(candidate.values()) + ('active',))
        rpc('fixer_bind_forward_media_manifest_20261006', candidate['id'], role='service_role')
    if canonical_legacy:
        # Independent reproducer /tmp/echo-current-census-independent-diag.py:
        # active RAW alias row, canonical registry/manifest, exact normal binder,
        # and no retained reservation. This previously failed in the helper.
        insert_active(row)
    else:
        request = canonical(dict(members=[dict(row=row, observation=None)], old_rows=[]))
        rpc('stage_forward_schedule_batch_20261008', tenant, batch, request, hashlib.sha256(request.encode()).hexdigest(), role='service_role')
        sql('update content_calendar set render_manifest_digest=%s where id=%s',
            (manifest['manifest_digest'], rid))
        rpc('fixer_bind_forward_schedule_staged_manifest_20261008', rid, role='service_role')
    provenance = lambda: rpc('fixer_forward_media_provenance_lookup_20261006', rid, role='fixer_forward_media_attester_20261006')
    if canonical_legacy:
        assert sql("select count(*) from fixer_still_reservation_20261007 where original->>'source_asset_id'=%s",
                   (original['source_asset_id'],))[0][0] == 0
        assert provenance()['original']['tenant_id'] == tenant
        assert 'staged_alias_binding' not in provenance()
        # Private helper validation precedes legacy fallback even without any
        # reservation. Missing/unrelated tenants or an unverified alias HOLD.
        import psycopg
        p = provenance()
        for bad in (None, '', 'unrelated-tenant'):
            with pytest.raises(psycopg.Error, match='exact current source tenant'):
                rpc('fixer_current_census_reservation_private_20261008', rid,
                    json.dumps({**p, 'original': {**p['original'], 'tenant_id': bad}}))
        wrong_alias = 'canonical-wrong-map'
        sql('insert into fixer_forward_media_tenant_alias_20261006 values(%s,%s)', (wrong_alias, 'unrelated-tenant'))
        for bad_raw in ('canonical-missing-map', wrong_alias):
            sql('begin')
            try:
                sql('update content_calendar set gym_id=%s where id=%s', (bad_raw, rid))
                with pytest.raises(psycopg.Error, match='exact current source tenant'):
                    rpc('fixer_current_census_reservation_private_20261008', rid, json.dumps(p))
            finally:
                sql('rollback')
    snapshot = rpc('fixer_generated_snapshot_20261007', rid)
    def inventory(complete=True, revision=None, age=0, target_epoch=epoch, target_gym=raw):
        ident = uuid.uuid4()
        sql("insert into fixer_still_inventory_20261007 values(%s,%s,%s,%s,%s,0,'SYNTHETIC retained census',clock_timestamp()-(%s * interval '1 second'))",
            (ident, target_epoch, target_gym, revision or snapshot['inventory_revision'], complete, age))
        return ident
    o = dict(gym_id=raw, source_asset_id=original['source_asset_id'], source_url=original['source_url'],
             sha256='sha256:' + hashlib.sha256(item['bytes']).hexdigest(), md5=original['source_fingerprint'],
             phash=scene_fingerprint(item['bytes']), length=original['source_length'])
    clearance = uuid.uuid4()
    rpc('fixer_still_known_record_20261007', clearance, 'cleared_fresh', json.dumps(o),
        'authenticated-post-epoch:SYNTHETIC retained original', role='current_owner')
    old_census = inventory(age=30)
    def bound_reservation(census_id, **overrides):
        ident = uuid.uuid4()
        values = dict(epoch=epoch, gym=raw, date='2026-10-10', logical=logical, group=group, original=o)
        values.update(overrides)
        sql("insert into fixer_still_reservation_20261007(receipt_id,epoch_id,calendar_row_id,gym_id,local_date,logical_post_id,group_key,media_kind,original,inventory_receipt,clearance_receipt) "
            "values(%s,%s,%s,%s,%s,%s,%s,'graphic',%s::jsonb,%s,%s)",
            (ident, values['epoch'], rid, values['gym'], values['date'], values['logical'], values['group'],
             json.dumps(values['original']), census_id, clearance))
        return ident
    if raw != tenant:
        # Any retained reservation under RAW or verified canonical authority
        # enrolls the source across ALL epochs/dates/census IDs. None may vanish
        # into legacy fallback. Rollback preserves immutable fixtures and avoids
        # fabricating a later exception to the existing visual occupancy guard.
        # Exercise both normal canonical authority and staged RAW fallback.
        for retained_gym in (raw, tenant):
            sql('begin')
            try:
                retained_epoch = uuid.uuid4()
                retained_inventory = inventory(target_epoch=retained_epoch, age=1200, target_gym=retained_gym)
                bound_reservation(retained_inventory, epoch=retained_epoch, gym=retained_gym,
                                  date='2026-10-09', original={**o, 'gym_id': retained_gym})
                sql('set local role fixer_forward_media_attester_20261006')
                import psycopg
                with pytest.raises(psycopg.Error, match='exact current census binding'):
                    rpc('fixer_forward_media_provenance_lookup_20261006', rid)
            finally:
                sql('rollback')
        assert provenance()['original']['tenant_id'] == original['tenant_id']
        # A reservation for the same asset key under an unrelated tenant is
        # outside this row's authority and cannot enroll this legacy source.
        sql('begin')
        try:
            bound_reservation(old_census, gym='unrelated-tenant',
                              original={**o, 'gym_id': 'unrelated-tenant'})
            sql('set local role fixer_forward_media_attester_20261006')
            assert rpc('fixer_forward_media_provenance_lookup_20261006', rid)['original']['tenant_id'] == original['tenant_id']
        finally:
            sql('rollback')
    old_bound = bound_reservation(old_census)
    assert provenance()['original']['tenant_id'] == original['tenant_id']
    if raw != tenant and not canonical_legacy:
        assert provenance()['staged_alias_binding']['snapshot']['tenant_id'] == tenant
    # Pre-fix ordering failure: newest valid census, still only old reservation.
    new_census = inventory()
    denied('fixer_forward_media_provenance_lookup_20261006', rid, phrase='exact current census binding')
    # Newer reservations with only one wrong identity must never shadow HOLD.
    for field, wrong in (('epoch', uuid.uuid4()), ('gym', 'another-gym'), ('date', '2026-10-11'),
                         ('logical', uuid.uuid4()), ('group', 'vg_wrong'),
                         ('original', {**o, 'md5': 'md5:' + '0'*32}),
                         ('original', {**o, 'source_url': 'https://owned.example/wrong.png'}),
                         ('original', {**o, 'length': o['length'] + 1}),
                         ('original', {**o, 'gym_id': 'wrong-original-tenant'})):
        # Identity-negative fixtures are rolled back together with their read;
        # permanently committing wrong-date same bytes would correctly reserve
        # global occupancy and prevent the later positive fixture.
        sql('begin')
        try:
            bound_reservation(new_census, **{field: wrong})
            sql('set local role fixer_forward_media_attester_20261006')
            import psycopg
            with pytest.raises(psycopg.Error, match='exact current census binding'):
                rpc('fixer_forward_media_provenance_lookup_20261006', rid)
        finally:
            sql('rollback')
    current_bound = bound_reservation(new_census)
    assert provenance()['original']['source_fingerprint'] == original['source_fingerprint']
    # A later invalid row is ignored after exact bindings are applied.
    bound_reservation(new_census, group='vg_wrong_later', original={**o,
        'source_url': 'https://owned.example/nonmatching.png',
        'sha256': 'sha256:' + hashlib.sha256((raw + 'unrelated').encode()).hexdigest(),
        'md5': 'md5:' + hashlib.md5((raw + 'unrelated').encode()).hexdigest(),
        'phash': 'scene:phash64:' + hashlib.sha256((raw + 'unrelated').encode()).hexdigest()[:16]})
    assert provenance()['original']['tenant_id'] == original['tenant_id']
    assert sql('select inventory_receipt from fixer_still_reservation_20261007 where receipt_id=%s', (old_bound,))[0][0] == old_census
    revision = rpc('fixer_forward_media_attestation_request_20261006', rid)['revision']
    evidence = uuid.uuid4()
    assert rpc('fixer_attest_forward_media_20261006', rid, revision, evidence, original['source_fingerprint'],
               original['source_length'], original['source_fingerprint'], original['source_length'], None, None,
               'same_object', 'SYNTHETIC isolated exact byte observation', role='fixer_forward_media_attester_20261006') == evidence
    read_ids = sql('select source_read_receipt,image_read_receipt from fixer_forward_media_lineage_20261006 where evidence_id=%s', (evidence,))[0]
    attestation_ids = []
    phash = int(o['phash'].split(':')[-1], 16)
    phash = phash - 2**64 if phash >= 2**63 else phash
    for role, read_id in (('original', read_ids[0]), ('delivered', read_ids[1]), ('thumbnail', read_ids[1])):
        ident = uuid.uuid4()
        attestation_ids.append(str(ident))
        sql('insert into forward_media_visual_attestation(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id) '
            'values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (ident, tenant, original['source_url'], role, o['sha256'][7:], o['md5'][4:], o['length'], phash,
             int(revision[:15], 16), evidence, read_id), role='fixer_forward_media_attester_20261006')
    candidates = [dict(calendar_row_id=str(rid), logical_post_id=str(logical), expected_revision=revision,
                       attestation_ids=attestation_ids, expected_reservation_id=None)]
    if not canonical_legacy:
        finalized = rpc('finalize_forward_schedule_staged_batch_20261008', tenant, batch, json.dumps(candidates), '[]', role='service_role')
        assert finalized['state'] == 'finalized'
    else:
        slot = rpc('reserve_forward_slot_20261008', rid, logical, revision, attestation_ids, None, role='service_role')
        assert sql('select calendar_row_id from forward_schedule_reservation where reservation_id=%s', (slot,))[0][0] == rid
    assert provenance()['original']['tenant_id'] == original['tenant_id']
    # Once activated, the real bounded owner RPC can explicitly restage the
    # same original under the current census; no immutable row is rewritten.
    restaged = uuid.uuid4()
    assert rpc('fixer_still_reserve_20261007', restaged, rid, json.dumps(o), 'graphic',
               new_census, clearance, role='current_owner') == restaged
    assert provenance()['original']['tenant_id'] == original['tenant_id']
    sql('insert into fixer_forward_media_claim_gate_20261006 values(%s,true)', (tenant,))
    token = uuid.uuid4()
    sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s", (token, rid))
    assert rpc('fixer_forward_visual_index_claim_20261008', rid, token, evidence, revision, attestation_ids, role='service_role') is True
    assert sql('select calendar_row_id from fixer_forward_media_claim_receipt_20261006 where claim_token=%s', (token,))[0][0] == rid
    assert rpc('fixer_forward_visual_index_claim_20261008', rid, token, evidence, revision, attestation_ids, role='service_role') is True
    # Same tenant/date/logical/group platform sibling may consume the same
    # explicit current original reservation, while membership stays genuine.
    sibling, sibling_batch = uuid.uuid4(), uuid.uuid4()
    sibling_row = {**row, 'id': str(sibling), 'account': 'facebook'}
    if canonical_legacy:
        insert_active(sibling_row)
    else:
        sibling_request = canonical(dict(members=[dict(row=sibling_row, observation=None)], old_rows=[]))
        rpc('stage_forward_schedule_batch_20261008', tenant, sibling_batch, sibling_request,
            hashlib.sha256(sibling_request.encode()).hexdigest(), role='service_role')
        rpc('fixer_bind_forward_schedule_staged_manifest_20261008', sibling, role='service_role')
    assert rpc('fixer_forward_media_provenance_lookup_20261006', sibling,
               role='fixer_forward_media_attester_20261006')['original']['tenant_id'] == original['tenant_id']
    # Newest receipt must remain fresh/current even when explicitly restaged.
    for kind in ('stale', 'revision', 'epoch'):
        sql('begin')
        try:
            target_epoch = uuid.uuid4() if kind in ('stale', 'epoch') else epoch
            if kind in ('stale', 'epoch'):
                sql('update fixer_still_cutover_20261007 set epoch_id=%s', (target_epoch,))
            invalid = inventory(age=1200 if kind == 'stale' else 0,
                                revision='SYNTHETIC drift' if kind == 'revision' else None,
                                target_epoch=target_epoch)
            bound_reservation(invalid, epoch=epoch if kind == 'epoch' else target_epoch)
            sql('set local role fixer_forward_media_attester_20261006')
            import psycopg
            with pytest.raises(psycopg.Error, match='fresh inventory authority|exact current census binding'):
                rpc('fixer_forward_media_provenance_lookup_20261006', rid)
        finally:
            sql('rollback')
    # Calendar binding drift, missing/wrong aliases and changed source never
    # inherit an exception for the canonical committed use. Restore only the
    # disposable row transaction; immutable mappings/receipts are never edited.
    wrong_alias = 'wrong-alias-' + item['case']
    sql('insert into fixer_forward_media_tenant_alias_20261006 values(%s,%s)', (wrong_alias, 'wrong-canonical'))
    for column, value in (('gym_id', 'missing-alias-' + item['case']), ('gym_id', wrong_alias),
                          ('post_date', '2026-10-11'), ('logical_post_id', uuid.uuid4()),
                          ('visual_group_key', 'vg_wrong_live'),
                          ('source_media_url', 'https://owned.example/changed-source.png')):
        sql('begin')
        try:
            import psycopg
            with pytest.raises(psycopg.Error):
                sql('update content_calendar set ' + column + '=%s where id=%s', (value, rid))
                sql('set local role fixer_forward_media_attester_20261006')
                rpc('fixer_forward_media_provenance_lookup_20261006', rid)
        finally:
            sql('rollback')
    assert sql('select count(*) from fixer_forward_media_claim_receipt_20261006 where claim_token=%s', (token,))[0][0] == 1
    assert sql('select count(*) from fixer_forward_media_use_20261006 where first_claim_token=%s', (token,))[0][0] == 1
    # Latest incomplete observation invalidates provenance and claim replay.
    newest = inventory(complete=False)
    denied('fixer_forward_media_provenance_lookup_20261006', rid, phrase='exact current census binding')
    denied('fixer_forward_visual_index_claim_20261008', rid, token, evidence, revision, attestation_ids,
           role='service_role', phrase='exact current census binding')
    bound_reservation(newest)
    denied('fixer_forward_media_provenance_lookup_20261006', rid, phrase='fresh inventory authority')
    rpc('fixer_still_known_record_20261007', uuid.uuid4(), 'deny', json.dumps(o),
        'SYNTHETIC independently established negative history', role='current_owner')
    denied('fixer_forward_media_provenance_lookup_20261006', rid, phrase='known still history denied or quarantined')
    print('PASS', item['case'], 'canonical no-reservation legacy provenance and stale raw/canonical enrollment HOLD;' if canonical_legacy else '',
          'old receipt HOLD, exact new reservation, attestation, real schedule reservation, real owner restage, final claim/replay, current sibling, identity/alias/source/freshness negatives, newer incomplete and negative history HOLD; frozen old reservation', old_bound, 'current', current_bound)
