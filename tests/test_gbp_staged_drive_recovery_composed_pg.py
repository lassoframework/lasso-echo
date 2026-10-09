"""Offline PG17 staged GBP -> separate real finalizer -> listener CAS recovery.

The synthetic REST transport below translates a deliberately small PostgREST
subset into real SQL on the same disposable database; it does not supply RPC
receipts or calendar after-images. Synthetic Drive/hosted bytes, signing key,
historical review and operator enrollment are fixtures. The real finalizer runs
in a separate process with SQLite access forbidden and no listener journal pin.
This is local composition evidence, not production topology or deployment proof.
All flags are armed only inside temporary test/subprocess environments.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid
from unittest.mock import patch

import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.types.json import Jsonb

from agent import config, db, forward_media_attester, forward_media_prepare
from agent import gbp_drive_use_journal as journal, gbp_planner, remote_drive_use
from agent import portal_calendar_store as pcs
from agent.forward_media_photo_certificate import canonical, digest
from agent.forward_media_still_certificate_v2 import IndependentStillPhotoAuditorV2
from agent.jobs import gbp_drive_use_recovery as recovery
from tests.test_forward_media_owner_two_phase_pg import png
from tests.test_forward_media_prospective_still_v2_pg import ROOT, main as composed_pg


class PgRestResponse:
    def __init__(self, payload, *, count=None):
        self.status_code = 200
        self.payload = payload
        self.text = json.dumps(payload)
        self.headers = {} if count is None else {'content-range': f'0-{max(0,len(payload)-1)}/{count}'}

    def json(self):
        return deepcopy(self.payload)


class SyntheticPgRest:
    """Synthetic HTTP adapter, real login/PG queries/RPCs/counts underneath.

    Only eq predicates, listed order expressions, select and limit are supported.
    Unexpected API behavior fails rather than inventing a successful response.
    """
    def __init__(self, dsn):
        self.dsn = dsn
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit('/', 1)[1]
        assert re.fullmatch(r'[a-z0-9_]+', table)
        params = dict(params or {})
        self.calls.append(('get', table, deepcopy(params)))
        columns = params.pop('select', '*')
        order = params.pop('order', None)
        limit = int(params.pop('limit', '1000'))
        assert 1 <= limit <= 1000
        predicates, values = [], []
        for name, expression in params.items():
            assert re.fullmatch(r'[a-z0-9_]+', name) and expression.startswith('eq.')
            predicates.append(pgsql.SQL('{}=%s').format(pgsql.Identifier(name)))
            values.append(expression[3:])
        where = pgsql.SQL(' where ') + pgsql.SQL(' and ').join(predicates) if predicates else pgsql.SQL('')
        query = pgsql.SQL('select to_jsonb(t) from public.{} t').format(pgsql.Identifier(table)) + where
        if order:
            items = []
            for item in order.split(','):
                name, direction = item.split('.')
                assert re.fullmatch(r'[a-z0-9_]+', name) and direction in ('asc', 'desc')
                items.append(pgsql.SQL('{} {}').format(pgsql.Identifier(name), pgsql.SQL(direction)))
            query += pgsql.SQL(' order by ') + pgsql.SQL(',').join(items)
        query += pgsql.SQL(' limit %s')
        with psycopg.connect(self.dsn) as conn:
            assert conn.execute('select current_user').fetchone()[0] == 'service_role'
            count = conn.execute(pgsql.SQL('select count(*) from public.{}').format(pgsql.Identifier(table)) + where, values).fetchone()[0]
            rows = [r[0] for r in conn.execute(query, [*values, limit])]
        if columns != '*':
            names = columns.split(',')
            assert all(re.fullmatch(r'[a-z0-9_]+', name) for name in names)
            rows = [{name: row[name] for name in names} for row in rows]
        return PgRestResponse(rows, count=count)

    def post(self, url, json=None, headers=None, timeout=None):
        name = url.rsplit('/', 1)[1]
        assert '/rpc/' in url and re.fullmatch(r'[a-z0-9_]+', name)
        self.calls.append(('post', name, deepcopy(json)))
        args = []
        values = []
        for key, value in json.items():
            args.append(pgsql.SQL('{} => %s').format(pgsql.Identifier(key)))
            values.append(Jsonb(value) if isinstance(value, (dict, list)) else value)
        query = pgsql.SQL('select public.{}({})').format(pgsql.Identifier(name), pgsql.SQL(',').join(args))
        with psycopg.connect(self.dsn) as conn:
            assert conn.execute('select current_user').fetchone()[0] == 'service_role'
            result = conn.execute(query, values).fetchone()[0]
        return PgRestResponse(result)


def pg_store(dsn):
    transport = SyntheticPgRest(dsn)
    return pcs.SupabaseCalendarStore(url='http://synthetic.invalid', service_key='synthetic', http=transport), transport


def finalizer_process(dsn, batch):
    """Actual worker entrypoint, independent process and service-only login."""
    from agent import forward_schedule_batch_finalizer_worker as finalizer
    assert 'AGENT_DB_PATH' not in os.environ
    assert recovery.JOURNAL_ID_ENV not in os.environ
    store, transport = pg_store(dsn)
    with patch.object(sqlite3, 'connect', side_effect=AssertionError('finalizer must not open listener SQLite')):
        report = finalizer.run_once(settings=finalizer.Settings(('gym',), (batch,)), store=store)
    assert not any(call[1] == 'content_calendar' for call in transport.calls)
    print(json.dumps({'report': report, 'calls': transport.calls}))


def install_remaining(sql):
    """Actual assembled prerequisites, never stand-in authority functions."""
    # The prospective-photo fixture only declared the columns it exercised.
    # The repository baseline media_source_media_asset_20260827.sql also
    # supplies this nullable timestamptz used by the real remote-use CAS.
    sql('alter table media_asset add column last_used_at timestamptz')
    sql('create table gyms(id uuid primary key);'
        'create table app_users(id uuid primary key,clerk_user_id text unique,role text,email text);'
        'create table echo_intake_tokens(gym_id uuid primary key,echo_account_key text unique);'
        'alter role service_role login;')
    for name in ('DRAFT_fixer_generated_gap_dispatch_20261007.sql',
                 'DRAFT_generated_source_palette_authority_20261007.sql',
                 'DRAFT_generated_send_lease_20261007.sql'):
        sql((ROOT / 'migrations' / name).read_text())
    portal = ROOT.parent / 'portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.sql'
    assert portal.is_file(), 'assembled portal source-brand migration required'
    sql(portal.read_text())
    for name in ('DRAFT_fixer_generated_bundle_bridge_20261008.sql',
                 'DRAFT_fixer_generated_local_census_producer_20261008.sql',
                 'DRAFT_fixer_still_v2_owner_transport_20261008.sql',
                 'DRAFT_fixer_current_census_reservation_lookup_20261008.sql',
                 'DRAFT_fixer_inventory_mutation_protocol_20261008.sql',
                 'DRAFT_fixer_remote_drive_use_cas_20261008.sql',
                 'DRAFT_fixer_calendar_admission_guard_20261009.sql'):
        sql((ROOT / 'migrations' / name).read_text())
    sql('create role drive_writer login;grant fixer_inventory_mutator_20261008 to drive_writer;')
    assert sql('select enabled from fixer_remote_drive_use_control_20261008')[0][0] is False
    assert sql('select enabled from fixer_calendar_admission_gate_20261009')[0][0] is False


def staged_gbp(sql, dsn, seed, store, epoch):
    """Real stage caller/journal, source verifier, signed still certificate."""
    from agent.forward_media_source_history import SourceHistoryStore
    from agent.forward_media_source_verifier import verify_source
    from agent.forward_media_owner import ForwardMediaOwnerPersistence
    closure = dict(zip(seed.__code__.co_freevars, (c.cell_contents for c in seed.__closure__)))
    key, private = closure['key'], closure['private']
    data = png('blue')
    md5, sha = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()
    row_id, logical = str(uuid.uuid4()), str(uuid.uuid4())
    asset_id, source_id = 'asset_' + uuid.uuid4().hex, 'source_' + uuid.uuid4().hex
    url, folder = 'https://scratch.example/' + uuid.uuid4().hex + '.png', 'FolderOriginal1234567'
    sql("insert into media_source(id,gym_id,kind,folder_id,active) values(%s,'gym','gym_drive',%s,true)", (source_id, folder))
    moderation = dict(verdict='clean', provider='SYNTHETIC scanner', content_hash=md5,
                      asset_id=asset_id, gym_id='gym', people_detected=False,
                      observed_at='2026-10-08T00:00:00Z', sha256=sha)
    sql('insert into media_asset(id,source_id,gym_id,content_hash,review_content_hash,moderation_json) values(%s,%s,\'gym\',%s,%s,%s::jsonb)',
        (asset_id, source_id, md5, md5, json.dumps(moderation)))
    asset = sql('select to_jsonb(a) from media_asset a where id=%s', (asset_id,))[0][0]
    source = sql('select to_jsonb(s) from media_source s where id=%s', (source_id,))[0][0]
    row = dict(id=row_id, gym_id='gym', post_date='2026-11-10', account='googlebusiness',
               format='update', status='pending', logical_post_id=logical, caption='SYNTHETIC gym caption.',
               source_media_url=url, image_url=url, source_media_asset_id=asset_id,
               visual_group_key='vg_' + uuid.uuid4().hex)
    claim = 'gym-gbp:' + uuid.uuid4().hex
    entry = journal.prepare(dict(gym_id='gym', epoch_id=epoch, logical_post_id=logical,
        claim_id=claim, post_date=row['post_date'], content_hash=md5, asset_id=asset_id,
        source_id=source_id, calendar_row=row, payload=row, asset_before=asset, source_before=source))
    entry = journal.record_write_intent(entry['use_id'])
    with db.connect() as conn:
        conn.execute("insert into socialapi_claims(draft_id,account_key,status) values(?,'gym_gbp','in_flight')", (claim,))
        conn.commit()
    recipe = forward_media_attester.make_still_recipe('identity')
    observation = dict(schema_version=1, provenance_status='unverified', tenant='gym',
        source_asset_id=asset_id, source_exact_url=url, delivered_exact_url=url,
        source_sha256=sha, delivered_sha256=sha, source_byte_length=len(data),
        delivered_byte_length=len(data), recipe=recipe, hold_reasons=[])
    raw = canonical(observation)
    packet = dict(digest_input=raw, observation_json=canonical(dict(observation,
        observation_digest=hashlib.sha256(raw.encode()).hexdigest())))
    receipt = store.stage_forward_schedule_batch('gym', [dict(row, observation=packet)])
    batch = receipt['batch_id']
    frozen = journal.get_forward_stage(batch)
    assert frozen['state'] == 'staged_pending' and not journal.forward_remote_use_allowed(batch)

    class SyntheticDrive:
        def metadata(self, file_id):
            if file_id == folder:
                return dict(id=folder, mimeType='application/vnd.google-apps.folder', trashed=False, version='2')
            assert file_id == asset_id
            return dict(id=asset_id, mimeType='image/png', trashed=False, version='3',
                        parents=[folder], size=str(len(data)), md5Checksum=md5)
        def original_bytes(self, file_id):
            assert file_id == asset_id
            return data
    class SyntheticHosted:
        def read(self, exact_url):
            assert exact_url == url
            return data
    with psycopg.connect(dsn.replace('user=postgres', 'user=photo_owner')) as conn:
        persistence = ForwardMediaOwnerPersistence(conn, 'photo_owner', SyntheticHosted())
        history = SourceHistoryStore(persistence)
        revision = sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s', (row_id,))[0][0]
        verified = verify_source(history.snapshot(row_id, revision), SyntheticDrive(), SyntheticHosted())
        history.stage_source(verified)
    candidate = dict(calendar_row_id=row_id, tenant_id='gym', group_key=row['visual_group_key'],
        post_date=row['post_date'], source_asset_id=asset_id, source_url=url, image_url=url,
        source_fingerprint='md5:' + md5, source_sha256='sha256:' + sha, source_length=len(data),
        image_fingerprint='md5:' + md5, image_sha256='sha256:' + sha, image_length=len(data),
        source_receipt_ref=verified.receipt_ref, render_recipe_digest=digest(recipe), logical_post_id=logical,
        content_digest=sql("select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')", (row_id,))[0][0])
    snapshot = sql('select fixer_still_photo_snapshot_v2_20261008()')[0][0]
    dispositions = [dict(history_key=h['history_key'], disposition='reviewed_visual_nonmatch',
        inspected_sha256=h['visual_sha256'], published_binding_ref=h['published_binding_ref'],
        review_evidence_ref='SYNTHETIC independent object review') for h in snapshot['rows']]
    payload = dict(schema_version=2, candidate_media_kind='still_photo', audit_id=str(uuid.uuid4()),
        auditor_id=key['auditor_id'], key_id=key['key_id'], policy_id=key['policy_id'],
        baseline_id=snapshot['baseline_id'], generation=snapshot['generation'], spine_digest=snapshot['spine_digest'],
        candidate=candidate, dispositions=dispositions, disposition_digest=digest(dispositions),
        decision='no_prior_published_still_image_or_derivative_use',
        stated_visual_uncertainty='SYNTHETIC review; video frames are outside this still-only scope.',
        scope='published_still_images_and_derivatives', accounted_video_digest=snapshot['excluded_rows_digest'],
        accounted_videos=[dict(history_key=v['history_key'], published_binding_ref=v['published_binding_ref'],
            disposition='accounted_out_of_scope_video_frames_unreviewed',
            review_evidence_ref='SYNTHETIC classified video') for v in snapshot['accounted_video_rows']])
    signed = dict(payload=payload, signature_hex=private.sign(canonical(payload).encode()).hex())
    with psycopg.connect(dsn) as conn:
        conn.execute('set role photo_auditor')
        cert = IndependentStillPhotoAuditorV2(conn, 'photo_auditor').submit(signed)
    original = forward_media_prepare.OriginalRegistration('gym', asset_id, url, 'md5:' + md5, len(data), verified.receipt_ref)
    manifest = forward_media_prepare.build_render_manifest(original, url, data, 'same_object', cert.receipt_ref, render_recipe=recipe)
    sql('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',
        (payload['audit_id'], json.dumps(asdict(original)), json.dumps(asdict(manifest))), 'photo_owner')
    sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)', (row_id,), 'service_role')
    return dict(rid=row_id, logical=logical, batch=batch, tenant='gym', day=row['post_date'],
                asset=asset_id, url=url, sha=sha, md5=md5, bytes=data, packet=signed), entry


def test_separate_finalizer_and_original_listener_recover_staged_drive_once(tmp_path):
    # This cross-repository composition needs the actual portal 0611 SQL.
    # Ordinary Echo CI has no sibling portal checkout, so keep the test
    # explicitly unverified there rather than substituting synthetic SQL.
    portal_sql = ROOT.parent / 'portal-brand-source-bundle-20261008/supabase/migrations/0611_echo_source_brand_bundle.sql'
    if not portal_sql.is_file():
        pytest.skip('cross-repository PG composition requires portal 0611 migration checkout')
    listener = tmp_path / 'listener'; listener.mkdir()
    library = listener / 'library'; (library / 'gym').mkdir(parents=True)
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'PYTHONPATH', 'DYLD_LIBRARY_PATH')}
    env.update({'LC_ALL': 'en_US.UTF-8', 'AGENT_DB_PATH': str(listener / 'original.db'),
           pcs.GBP_STAGED_JOURNAL_FLAG_ENV: 'true', recovery.FLAG: 'true',
           'AGENT_REMOTE_DRIVE_USE_CAS_ENABLED': 'true'})
    with patch.dict(os.environ, env, clear=True), patch.object(config, 'LIBRARY_PATH', str(library)), \
            patch.object(recovery, 'EPHEMERAL_ROOTS', ()):
        def exercise(*, sql, denied, seed, attest, dsn):
            install_remaining(sql)
            epoch = str(uuid.uuid4())
            sql("update fixer_still_cutover_20261007 set enabled=true,epoch_id=%s,cutover_at=now(),activation_ref='SYNTHETIC original journal test'", (epoch,))
            os.environ.update(LOCAL_INVENTORY_MUTATION_EPOCH=epoch,
                LOCAL_INVENTORY_MUTATOR_DSN=dsn.replace('user=postgres', 'user=drive_writer'),
                LOCAL_INVENTORY_MUTATOR_LOGIN='drive_writer')
            sql("update fixer_remote_drive_use_control_20261008 set enabled=true,writers_verified_ref='SYNTHETIC isolated composition test'")
            sql('update fixer_calendar_admission_gate_20261009 set enabled=true')
            sql('update forward_prospective_photo_gate_20261008 set enabled=true')
            sql('update forward_media_visual_gate_20261008 set enabled=true')
            service_dsn = dsn.replace('user=postgres', 'user=service_role')
            store, transport = pg_store(service_dsn)
            candidate, entry = staged_gbp(sql, dsn, seed, store, epoch)
            attest(candidate)
            sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
                (candidate['rid'], candidate['logical'], candidate['rev'], candidate['ids'],
                 candidate['packet']['payload']['audit_id']), 'photo_owner')
            identity = str(uuid.uuid4())
            recovery.pin_original_journal(identity)
            os.environ[recovery.JOURNAL_ID_ENV] = identity
            logs = []
            assert recovery.run(store=store, media_store=object(), logger=logs.append)['held'] == 1
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            before_journal = Path(os.environ['AGENT_DB_PATH']).read_bytes()
            child_env = {key: value for key, value in os.environ.items() if key in ('PATH', 'PYTHONPATH', 'DYLD_LIBRARY_PATH')}
            child_env.update(AGENT_FORWARD_SCHEDULE_FINALIZER='true',
                AGENT_FORWARD_SCHEDULE_RESERVATION='true',
                AGENT_FORWARD_SCHEDULE_FINALIZER_TENANTS='gym')
            child = subprocess.run([sys.executable, '-c',
                'from tests.test_gbp_staged_drive_recovery_composed_pg import finalizer_process; '
                'import sys; finalizer_process(sys.argv[1],sys.argv[2])', service_dsn, candidate['batch']],
                cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=60)
            assert child.returncode == 0, child.stderr
            result = json.loads(child.stdout)
            assert result['report']['status'] == 'complete', result
            assert result['report']['batches'][0]['status'] == 'finalized', result
            assert Path(os.environ['AGENT_DB_PATH']).read_bytes() == before_journal
            assert journal.get_forward_stage(candidate['batch'])['state'] == 'staged_pending'
            active = sql('select to_jsonb(c) from content_calendar c where id=%s', (candidate['rid'],))[0][0]
            assert active['variant_status'] == 'active' and active['status'] == 'pending'
            assert active['media_not_ready_reason'] is None
            assert active['account'] == 'googlebusiness' and active['format'] == 'update'
            assert active['published_at'] is None and active['late_post_id'] is None
            frozen = journal.get_forward_stage(candidate['batch'])
            extras = set(active) - set(entry['calendar_row'])
            unknown = extras - ({'id', 'created_at', 'updated_at'} | set(journal._LANDED_OMITTED_DEFAULTS))
            assert gbp_planner._forward_recovery_member_matches(entry, active, frozen, provisional=True), (
                'actual finalized PG row violates frozen listener member contract; '
                'unrecognized server columns=' + ','.join(sorted(unknown)))
            # Discovery cannot serve as consumption authority. Null pins are
            # accepted at their exact default; generated or unknown extras hold.
            assert not gbp_planner._forward_recovery_member_matches(entry, active, frozen)
            for changed in (dict(active, generated_authority_pins={}),
                            dict(active, unexpected_server_column=None),
                            dict(active, render_manifest_digest='sha256:untrusted')):
                assert not gbp_planner._forward_recovery_member_matches(
                    entry, changed, frozen, provisional=True)
            with pytest.raises(journal.JournalHold, match='journal_landed_evidence_mismatch'):
                journal.confirm_landed(entry['use_id'], dict(calendar_row=active,
                    asset_id=entry['asset_id'], content_hash=entry['content_hash']))
            # Alter responses read from real authority. Every forged or stale
            # binding must hold BEFORE landing/CAS; no synthetic success object.
            for rpc, key, bad in (
                    (pcs._SNAPSHOT_RPC, 'render_manifest_digest', 'sha256:' + '0' * 64),
                    (pcs._SNAPSHOT_RPC, 'source_asset_id', 'foreign_asset'),
                    (pcs._PROOF_RPC, 'reservation_id', str(uuid.uuid4())),
                    (pcs._PROOF_RPC, 'row_revision', '0' * 32),
                    (pcs._PROOF_RPC, 'source_sha256', '0' * 64),
                    (pcs._PROOF_RPC, 'tenant_id', 'foreign_gym'),
                    (pcs._PROOF_RPC, 'post_date', '2026-11-11'),
                    (pcs._PROOF_RPC, 'logical_post_id', str(uuid.uuid4()))):
                tampered, _ = pg_store(service_dsn)
                real_rpc = tampered._reservation_rpc
                def corrupt(name, arguments, *, timeout, rpc=rpc, key=key, bad=bad):
                    observed = real_rpc(name, arguments, timeout=timeout)
                    return dict(observed, **{key: bad}) if name == rpc else observed
                with patch.object(tampered, '_reservation_rpc', side_effect=corrupt):
                    refused = recovery.run(store=tampered, media_store=object(), logger=logs.append)
                assert refused['held'] == 1 and refused['recovered'] == 0, (rpc, key, refused)
                assert journal.get(entry['use_id'])['state'] == 'write_intent'
                assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            for response in ('missing', 'timeout'):
                unavailable, _ = pg_store(service_dsn)
                real_rpc = unavailable._reservation_rpc
                def missing(name, arguments, *, timeout, response=response):
                    if name == pcs._PROOF_RPC:
                        if response == 'timeout':
                            raise TimeoutError('SYNTHETIC proof read timeout')
                        return {}
                    return real_rpc(name, arguments, timeout=timeout)
                with patch.object(unavailable, '_reservation_rpc', side_effect=missing):
                    assert recovery.run(store=unavailable, media_store=object(),
                        logger=logs.append)['held'] == 1
                assert journal.get(entry['use_id'])['state'] == 'write_intent'
                assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            pick = dict(journal_entry=entry, asset=entry['asset_before'], base='gym',
                day_key=entry['post_date'], claim_id=entry['claim_id'], claim_account='gym_gbp', store=object())
            assert not gbp_planner._settle_armed_drive_landing(
                'gym', entry['calendar_row'], pick, active, logs.append)
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            # New listener store has no previous in-memory stage attempt. Only
            # pinned original SQLite plus real PG terminal/ACTIVE proof survive.
            restarted, reads = pg_store(service_dsn)
            assert restarted.last_forward_stage_attempt is None
            summary = recovery.run(store=restarted, media_store=object(), logger=logs.append)
            assert summary['recovered'] == 1 and summary['held'] == 0, json.dumps(summary) + '\n' + '\n'.join(logs)
            settled = journal.get(entry['use_id'])
            assert settled['state'] == 'claim_done' and settled['landed_proof']['calendar_row'] == active
            assert journal.get_forward_stage(candidate['batch'])['state'] == 'finalized'
            assert [call[1] for call in reads.calls] == ['content_calendar', pcs._BATCH_STATUS_RPC,
                pcs._SNAPSHOT_RPC, pcs._PROOF_RPC, 'content_calendar']
            assert all(call[2]['image_url'] == 'eq.' + active['image_url'] for call in reads.calls if call[1] == 'content_calendar')
            stored = sql('select request,receipt from fixer_remote_drive_use_20261008 where use_id=%s', (entry['use_id'],))[0]
            assert stored[0] == journal._remote_request(settled, settled['use_id'])
            assert stored[1] == settled['receipt'] == gbp_planner._recorded_remote_receipt(entry['use_id'])
            assert stored[1]['asset_after']['used_count'] == 1
            replay = remote_drive_use.DriveUseAuthority.from_environment()
            try:
                assert replay.apply_use(stored[0]) == replay.use_receipt(stored[0]) == stored[1]
            finally:
                replay.close()
            assert recovery.run(store=pg_store(service_dsn)[0], media_store=object(), logger=logs.append)['examined'] == 0
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 1
            assert sql('select used_count from media_asset where id=%s', (candidate['asset'],))[0][0] == 1
            with db.connect() as conn:
                assert tuple(conn.execute('select status,post_id from socialapi_claims where draft_id=? and account_key=\'gym_gbp\'', (entry['claim_id'],)).fetchone()) == ('done', candidate['asset'])
        composed_pg(runtime_check=exercise, genuine_sources=True)
