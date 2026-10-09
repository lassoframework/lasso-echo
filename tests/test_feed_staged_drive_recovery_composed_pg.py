"""Offline PG17 staged PHOTO FEED -> separate real finalizer -> listener CAS recovery.

Feed twin of tests/test_gbp_staged_drive_recovery_composed_pg.py: the ordinary
photo FEED lane (account 'instagram', format 'feed') stages through the REAL
SupabaseCalendarStore.insert_rows against a disposable PostgreSQL 17 with the
frozen test-only portal 0611 fixture, a real lost stage acknowledgment is
resolved by exact retry, a same-logical Facebook mirror sibling shares the one
source use, the ACTUAL separate finalizer process activates the batch, and the
pinned original listener settles exactly one remote Drive use through the real
CAS. Nothing here mocks inserted-row readback, stamp_use, the remote receipt or
terminal payloads; the synthetic REST transport translates a small PostgREST
subset into real SQL, and Drive/hosted bytes, signing key, history review and
operator enrollment remain fixtures. Local composition evidence only. All flags
are armed only inside temporary test/subprocess environments.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import uuid
from unittest.mock import patch

import psycopg
import pytest

from agent import config, db, feed_drive_use as feed, forward_media_attester
from agent import forward_media_observation_bridge as bridge
from agent import forward_media_prepare, gbp_drive_use_journal as journal
from agent import gbp_planner, portal_calendar_store as pcs, remote_drive_use
from agent.forward_media_photo_certificate import canonical, digest
from agent.forward_media_still_certificate_v2 import IndependentStillPhotoAuditorV2
from agent.forward_media_source_history import SourceHistoryStore
from agent.forward_media_source_verifier import verify_source
from agent.forward_media_owner import ForwardMediaOwnerPersistence
from agent.jobs import gbp_drive_use_recovery as recovery
from tests.test_forward_media_owner_two_phase_pg import png
from tests.test_forward_media_prospective_still_v2_pg import ROOT, main as composed_pg
from tests.test_gbp_staged_drive_recovery_composed_pg import (
    SyntheticPgRest, install_remaining, pg_store, portal_0611_fixture)

FOLDER = 'FolderOriginal1234567'


class PgMediaStore:
    """Authoritative media snapshot reader over the real disposable PG rows."""
    def __init__(self, dsn):
        self.dsn = dsn

    def _row(self, table, key, value):
        assert table in ('media_asset', 'media_source') and key == 'id'
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                f'select to_jsonb(t) from {table} t where id=%s', (value,)).fetchone()
        return row[0] if row else None

    def get_asset(self, asset_id):
        return self._row('media_asset', 'id', asset_id)

    def get_source(self, source_id):
        return self._row('media_source', 'id', source_id)


class LostStageAckOnce(SyntheticPgRest):
    """Executes the real stage RPC once, then loses the HTTP response."""

    def __init__(self, dsn):
        super().__init__(dsn)
        self.armed = True

    def post(self, url, json=None, headers=None, timeout=None):
        response = super().post(url, json=json, headers=headers, timeout=timeout)
        if self.armed and '/rpc/' in url and url.rsplit('/', 1)[1] == pcs._STAGE_RPC:
            self.armed = False
            raise TimeoutError('SYNTHETIC lost stage acknowledgment')
        return response


def finalizer_process(dsn, batch, tenant):
    """Actual worker entrypoint, independent process and service-only login."""
    from agent import forward_schedule_batch_finalizer_worker as finalizer
    assert 'AGENT_DB_PATH' not in os.environ
    assert recovery.JOURNAL_ID_ENV not in os.environ
    store, transport = pg_store(dsn)
    with patch.object(sqlite3, 'connect',
                      side_effect=AssertionError('finalizer must not open listener SQLite')):
        report = finalizer.run_once(settings=finalizer.Settings((tenant,), (batch,)), store=store)
    assert not any(call[1] == 'content_calendar' for call in transport.calls)
    print(json.dumps({'report': report, 'calls': transport.calls}))


def run_finalizer(batch, tenant, service_dsn):
    child_env = {key: value for key, value in os.environ.items()
                 if key in ('PATH', 'PYTHONPATH', 'DYLD_LIBRARY_PATH')}
    child_env.update(AGENT_FORWARD_SCHEDULE_FINALIZER='true',
                     AGENT_FORWARD_SCHEDULE_RESERVATION='true',
                     AGENT_FORWARD_SCHEDULE_FINALIZER_TENANTS=tenant)
    child = subprocess.run([sys.executable, '-c',
        'from tests.test_feed_staged_drive_recovery_composed_pg import finalizer_process; '
        'import sys; finalizer_process(sys.argv[1],sys.argv[2],sys.argv[3])',
        service_dsn, batch, tenant], cwd=ROOT, env=child_env, capture_output=True, text=True,
        timeout=60)
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


def _observation(raw_tenant, asset_id, url, sha, data, recipe, delivered=None):
    d_url, d_sha, d_len = delivered or (url, sha, len(data))
    core = dict(schema_version=1, provenance_status='unverified', tenant=raw_tenant,
                source_asset_id=asset_id, source_exact_url=url, delivered_exact_url=d_url,
                source_sha256=sha, delivered_sha256=d_sha, source_byte_length=len(data),
                delivered_byte_length=d_len, recipe=recipe, hold_reasons=[])
    digest_input = bridge._json(core)
    return dict(core, observation_digest=hashlib.sha256(digest_input.encode()).hexdigest())


def stage_feed_batch(sql, dsn, seed, epoch, *, raw, day='2026-10-20', sibling=False,
                     lost_ack=False):
    """Real freeze_pick + stage_callback + insert_rows; no fabricated readback."""
    closure = dict(zip(seed.__code__.co_freevars, (c.cell_contents for c in seed.__closure__)))
    key, private = closure['key'], closure['private']
    data = png('blue')
    md5, sha = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest()
    asset_id, source_id = 'asset_' + uuid.uuid4().hex, 'source_' + uuid.uuid4().hex
    url = 'https://scratch.example/' + uuid.uuid4().hex + '.png'
    sql("insert into media_source(id,gym_id,kind,folder_id,active) values(%s,%s,'gym_drive',%s,true)",
        (source_id, raw, FOLDER))
    moderation = dict(verdict='clean', provider='SYNTHETIC scanner', content_hash=md5,
                      asset_id=asset_id, gym_id=raw, people_detected=False,
                      observed_at='2026-10-08T00:00:00Z', sha256=sha)
    sql('insert into media_asset(id,source_id,gym_id,content_hash,review_content_hash,moderation_json)'
        ' values(%s,%s,%s,%s,%s,%s::jsonb)',
        (asset_id, source_id, raw, md5, md5, json.dumps(moderation)))
    claim = 'gym-feed:' + uuid.uuid4().hex
    pending = feed.freeze_pick(raw, {'id': asset_id}, claim, day, PgMediaStore(dsn))
    draft = SimpleNamespace(is_story=False, _drive_use_pending=pending)
    with db.connect() as conn:
        conn.execute("insert into socialapi_claims(draft_id,account_key,status)"
                     " values(?,?,'in_flight')", (claim, raw + '_gbp'))
        conn.commit()
    logical = str(uuid.uuid4())
    recipe = forward_media_attester.make_still_recipe('identity')
    observation = _observation(raw, asset_id, url, sha, data, recipe)
    proof = dict(source_sha256=sha, phash_v1=0, source_media_asset_id=asset_id)
    row = dict(gym_id=raw, post_date=day, account='instagram', format='feed',
               status='pending', caption='SYNTHETIC gym feed caption.',
               source_media_url=url, image_url=url, source_media_asset_id=asset_id,
               visual_group_key='vg_' + uuid.uuid4().hex, logical_post_id=logical)
    row[pcs.RESERVATION_PROOF] = proof
    row[bridge.METADATA] = [observation]
    rows = [row]
    rendition = None
    if sibling:
        # Same source bytes, own signed 4x3 rendition: the legitimate
        # same-logical platform sibling shape (distinct image URL/bytes).
        s_recipe = forward_media_attester.make_still_recipe('gbp_crop_4x3')
        s_data = forward_media_attester.replay_still_recipe(data, s_recipe)['image_bytes']
        assert s_data != data
        s_md5, s_sha = hashlib.md5(s_data).hexdigest(), hashlib.sha256(s_data).hexdigest()
        s_url = 'https://scratch.example/' + uuid.uuid4().hex + '.jpg'
        rendition = dict(url=s_url, data=s_data, md5=s_md5, sha=s_sha, recipe=s_recipe)
        mirror = {k: v for k, v in row.items()
                  if k not in (bridge.METADATA, pcs.RESERVATION_PROOF)}
        mirror['account'] = 'facebook'
        mirror['image_url'] = s_url
        mirror[pcs.RESERVATION_PROOF] = dict(proof)
        mirror[bridge.METADATA] = [_observation(raw, asset_id, url, sha, data, s_recipe,
                                                delivered=(s_url, s_sha, len(s_data)))]
        rows.append(mirror)
    service_dsn = dsn.replace('user=postgres', 'user=service_role')
    if lost_ack:
        transport = LostStageAckOnce(service_dsn)
        store = pcs.SupabaseCalendarStore(url='http://synthetic.invalid',
                                          service_key='synthetic', http=transport)
        callback = feed.stage_callback(raw, [draft])
        with pytest.raises(pcs.ReservationStoreError):
            store.insert_rows(raw, deepcopy(rows), before_forward_stage=callback)
        # The server committed; only the acknowledgment was lost. The durable
        # journal holds the exact stage intent and the frozen write intent.
        assert transport.armed is False
        staged_state = sql('select state from forward_schedule_stage_batch_20261008')[0][0]
        assert staged_state == 'staged'
        entry = journal.unsettled()[0]
        assert entry['state'] == 'write_intent'
        attempt = journal.pending_forward_stages()
        assert len(attempt) == 1 and attempt[0]['state'] == 'stage_intent'
    else:
        store, transport = pg_store(service_dsn)
    staged = store.insert_rows(raw, deepcopy(rows),
                               before_forward_stage=feed.stage_callback(raw, [draft]))
    assert len(staged) == len(rows)
    assert all(r['variant_status'] == 'candidate'
               and r['media_not_ready_reason'] == 'forward_reservation_staged'
               for r in staged)
    entries = journal.unsettled()
    assert len(entries) == 1
    entry = entries[0]
    assert entry['state'] == 'write_intent' and entry['gym_id'] == raw
    bound = journal.forward_stage_for_member(staged[0]['id'])
    batch = bound['batch_id']
    assert bound['state'] == 'staged_pending'
    assert not journal.forward_remote_use_allowed(batch)
    # Staged but never activated: no remote use, no consumed counter.
    assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
    assert sql('select used_count from media_asset where id=%s', (asset_id,))[0][0] == 0
    return dict(store=store, transport=transport, staged=staged, entry=entry,
                batch=batch, bound=bound, raw=raw, day=day, logical=logical,
                asset=asset_id, source=source_id, url=url, data=data, md5=md5, sha=sha,
                recipe=recipe, rendition=rendition, key=key, private=private,
                claim=claim, service_dsn=service_dsn)


def prepare_candidate(sql, dsn, ctx, rid, group, rendition=None):
    """Real owner source verification, signed still certificate, staged manifest."""
    raw, data, md5, sha = ctx['raw'], ctx['data'], ctx['md5'], ctx['sha']
    url, asset_id, source_id = ctx['url'], ctx['asset'], ctx['source']
    key, private = ctx['key'], ctx['private']
    if rendition is None:
        i_url, i_data = url, data
        i_md5, i_sha = md5, sha
        recipe, operation = ctx['recipe'], 'same_object'
    else:
        i_url, i_data = rendition['url'], rendition['data']
        i_md5, i_sha = rendition['md5'], rendition['sha']
        recipe, operation = rendition['recipe'], 'render'

    class SyntheticDrive:
        def metadata(self, file_id):
            if file_id == FOLDER:
                return dict(id=FOLDER, mimeType='application/vnd.google-apps.folder',
                            trashed=False, version='2')
            assert file_id == asset_id
            return dict(id=asset_id, mimeType='image/png', trashed=False, version='3',
                        parents=[FOLDER], size=str(len(data)), md5Checksum=md5)

        def original_bytes(self, file_id):
            assert file_id == asset_id
            return data

    class SyntheticHosted:
        def read(self, exact_url):
            if exact_url == url:
                return data
            assert rendition is not None and exact_url == rendition['url']
            return rendition['data']

    with psycopg.connect(dsn.replace('user=postgres', 'user=photo_owner')) as conn:
        persistence = ForwardMediaOwnerPersistence(conn, 'photo_owner', SyntheticHosted())
        history = SourceHistoryStore(persistence)
        revision = sql('select md5(to_jsonb(r)::text) from content_calendar r where id=%s',
                       (rid,))[0][0]
        verified = verify_source(history.snapshot(rid, revision),
                                 SyntheticDrive(), SyntheticHosted())
        history.stage_source(verified)
    candidate = dict(calendar_row_id=rid, tenant_id=raw, group_key=group,
        post_date=ctx['day'], source_asset_id=asset_id, source_url=url, image_url=i_url,
        source_fingerprint='md5:' + md5, source_sha256='sha256:' + sha,
        source_length=len(data), image_fingerprint='md5:' + i_md5,
        image_sha256='sha256:' + i_sha, image_length=len(i_data),
        source_receipt_ref=verified.receipt_ref, render_recipe_digest=digest(recipe),
        logical_post_id=ctx['logical'],
        content_digest=sql("select 'sha256:'||encode(sha256(convert_to("
                           'fixer_forward_media_photo_content_20261007(%s)::text'
                           ",'UTF8')),'hex')", (rid,))[0][0])
    snapshot = sql('select fixer_still_photo_snapshot_v2_20261008()')[0][0]
    dispositions = [dict(history_key=h['history_key'],
        disposition='reviewed_visual_nonmatch', inspected_sha256=h['visual_sha256'],
        published_binding_ref=h['published_binding_ref'],
        review_evidence_ref='SYNTHETIC independent object review')
        for h in snapshot['rows']]
    payload = dict(schema_version=2, candidate_media_kind='still_photo',
        audit_id=str(uuid.uuid4()), auditor_id=key['auditor_id'], key_id=key['key_id'],
        policy_id=key['policy_id'], baseline_id=snapshot['baseline_id'],
        generation=snapshot['generation'], spine_digest=snapshot['spine_digest'],
        candidate=candidate, dispositions=dispositions,
        disposition_digest=digest(dispositions),
        decision='no_prior_published_still_image_or_derivative_use',
        stated_visual_uncertainty='SYNTHETIC review; video frames are outside this still-only scope.',
        scope='published_still_images_and_derivatives',
        accounted_video_digest=snapshot['excluded_rows_digest'],
        accounted_videos=[dict(history_key=v['history_key'],
            published_binding_ref=v['published_binding_ref'],
            disposition='accounted_out_of_scope_video_frames_unreviewed',
            review_evidence_ref='SYNTHETIC classified video')
            for v in snapshot['accounted_video_rows']])
    signed = dict(payload=payload, signature_hex=private.sign(canonical(payload).encode()).hex())
    with psycopg.connect(dsn) as conn:
        conn.execute('set role photo_auditor')
        cert = IndependentStillPhotoAuditorV2(conn, 'photo_auditor').submit(signed)
    original = forward_media_prepare.OriginalRegistration(
        raw, asset_id, url, 'md5:' + md5, len(data), verified.receipt_ref)
    manifest = forward_media_prepare.build_render_manifest(
        original, i_url, i_data, operation, cert.receipt_ref, render_recipe=recipe)
    sql('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',
        (payload['audit_id'], json.dumps(asdict(original)), json.dumps(asdict(manifest))),
        'photo_owner')
    sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)', (rid,),
        'service_role')
    return dict(rid=rid, tenant=raw, url=url, md5=md5, sha=sha, bytes=data,
                image_url=i_url, image_md5=i_md5, image_sha=i_sha, image_data=i_data,
                operation=operation, packet=signed, audit_id=payload['audit_id'])


def attest_row(sql, c):
    """Composed-pg attestation, generalized to a distinct delivered rendition."""
    rev = sql("select fixer_forward_media_attestation_request_20261006(%s)->>'revision'",
              (c['rid'],))[0][0]
    ev = str(uuid.uuid4())
    sql('select fixer_attest_forward_media_20261006(%s,%s,%s,%s,%s,%s,%s,null,null,%s,%s)',
        (c['rid'], rev, ev, 'md5:' + c['md5'], len(c['bytes']),
         'md5:' + c['image_md5'], len(c['image_data']), c['operation'],
         'SYNTHETIC trusted byte read'), 'fixer_forward_media_attester_20261006')
    reads = sql('select source_read_receipt,image_read_receipt'
                ' from fixer_forward_media_lineage_20261006 where evidence_id=%s',
                (ev,))[0]
    ids = []
    nrev = int(rev[:15], 16)
    for role, read, url, sha, md5h, length in (
            ('original', reads[0], c['url'], c['sha'], c['md5'], len(c['bytes'])),
            ('delivered', reads[1], c['image_url'], c['image_sha'], c['image_md5'],
             len(c['image_data'])),
            ('thumbnail', reads[1], c['image_url'], c['image_sha'], c['image_md5'],
             len(c['image_data']))):
        aid = str(uuid.uuid4())
        ids.append(aid)
        sql('insert into forward_media_visual_attestation(attestation_id,tenant_key,'
            'media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,'
            'lineage_receipt_id,object_read_receipt_id)'
            ' values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (aid, sql('select fixer_forward_schedule_canonical_tenant_20261008(%s)',
                      (c['tenant'],))[0][0], url, role, sha, md5h, length, 0, nrev, ev, read),
            'fixer_forward_media_attester_20261006')
    c.update(rev=rev, evidence=ev, ids=ids)


def listener_env(tmp_path):
    listener = tmp_path / 'listener'
    listener.mkdir()
    library = listener / 'library'
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'PYTHONPATH', 'DYLD_LIBRARY_PATH')}
    env.update({'LC_ALL': 'en_US.UTF-8',
        'AGENT_DB_PATH': str(listener / 'original.db'),
        pcs.GBP_STAGED_JOURNAL_FLAG_ENV: 'true',
        pcs.FORWARD_RESERVATION_FLAG_ENV: 'true',
        bridge.ENV: 'true',
        recovery.FLAG: 'true',
        'AGENT_REMOTE_DRIVE_USE_CAS_ENABLED': 'true',
        'AGENT_MEDIA_CROSS_DAY_GUARD': 'false'})
    return env, library


def arm_listener(sql, dsn, library, raw):
    epoch = str(uuid.uuid4())
    sql("update fixer_still_cutover_20261007 set enabled=true,epoch_id=%s,cutover_at=now(),"
        "activation_ref='SYNTHETIC original journal test'", (epoch,))
    os.environ.update(LOCAL_INVENTORY_MUTATION_EPOCH=epoch,
        LOCAL_INVENTORY_MUTATOR_DSN=dsn.replace('user=postgres', 'user=drive_writer'),
        LOCAL_INVENTORY_MUTATOR_LOGIN='drive_writer')
    sql("update fixer_remote_drive_use_control_20261008 set enabled=true,"
        "writers_verified_ref='SYNTHETIC isolated composition test'")
    sql('update fixer_calendar_admission_gate_20261009 set enabled=true')
    sql('update forward_prospective_photo_gate_20261008 set enabled=true')
    sql('update forward_media_visual_gate_20261008 set enabled=true')
    (library / raw).mkdir(parents=True)
    with db.connect():
        pass
    journal.unsettled()
    journal.pending_forward_stages()
    identity = str(uuid.uuid4())
    recovery.pin_original_journal(identity)
    os.environ[recovery.JOURNAL_ID_ENV] = identity
    return epoch


def test_feed_staged_drive_recovery_composed_pg(tmp_path):
    portal_0611_fixture()
    env, library = listener_env(tmp_path)
    with patch.dict(os.environ, env, clear=True), \
            patch.object(config, 'LIBRARY_PATH', str(library)), \
            patch.object(recovery, 'EPHEMERAL_ROOTS', ()):
        def exercise(*, sql, denied, seed, attest, dsn):
            install_remaining(sql)
            epoch = arm_listener(sql, dsn, library, 'gym')
            ctx = stage_feed_batch(sql, dsn, seed, epoch, raw='gym',
                                   sibling=True, lost_ack=True)
            raw, batch, entry = ctx['raw'], ctx['batch'], ctx['entry']
            staged, store = ctx['staged'], ctx['store']
            # The exact retry after the lost ack replayed the same batch: one
            # durable use, one batch, deterministic member identity.
            assert store.last_forward_stage_attempt['batch_id'] == batch
            assert [r['id'] for r in staged] == ctx['bound']['member_row_ids']
            # Staged batch without premature use: the listener holds.
            logs = []
            assert recovery.run(store=store, media_store=object(),
                                logger=logs.append)['held'] == 1
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            # Real preparation of both same-logical platform members: the feed
            # row ships the source object; the Facebook mirror ships its own
            # signed 4x3 rendition of the same source.
            candidates = [prepare_candidate(sql, dsn, ctx, r['id'], r['visual_group_key'],
                                            rendition=ctx['rendition'] if i else None)
                          for i, r in enumerate(staged)]
            for c in candidates:
                attest_row(sql, c)
                sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
                    (c['rid'], ctx['logical'], c['rev'], c['ids'], c['audit_id']),
                    'photo_owner')
            before_journal = Path(os.environ['AGENT_DB_PATH']).read_bytes()
            result = run_finalizer(batch, raw, ctx['service_dsn'])
            assert result['report']['status'] == 'complete', result
            assert result['report']['batches'][0]['status'] == 'finalized', result
            assert Path(os.environ['AGENT_DB_PATH']).read_bytes() == before_journal
            assert journal.get_forward_stage(batch)['state'] == 'staged_pending'
            rows = {r['id']: r for r in (
                row[0] for row in sql('select to_jsonb(c) from content_calendar c'))}
            ig = rows[staged[0]['id']]
            assert ig['variant_status'] == 'active' and ig['status'] == 'pending'
            assert ig['media_not_ready_reason'] is None
            assert ig['account'] == 'instagram' and ig['format'] == 'feed'
            assert ig['published_at'] is None and ig['late_post_id'] is None
            # The same-logical Facebook mirror activated in the same batch.
            fb = rows[staged[1]['id']]
            assert fb['variant_status'] == 'active' and fb['account'] == 'facebook'
            assert fb['logical_post_id'] == ctx['logical']
            frozen = journal.get_forward_stage(batch)
            assert gbp_planner._forward_recovery_member_matches(
                entry, ig, frozen, provisional=True)
            assert not gbp_planner._forward_recovery_member_matches(entry, ig, frozen)
            with pytest.raises(journal.JournalHold,
                               match='journal_landed_evidence_mismatch'):
                journal.confirm_landed(entry['use_id'], dict(calendar_row=ig,
                    asset_id=entry['asset_id'], content_hash=entry['content_hash']))
            # The real admission guard refuses direct mutation of every bound
            # identity field of the ACTIVE row; nothing was written.
            for field, value, fragment in (
                    ('caption', 'tampered', 'active media admission identity immutable'),
                    ('gym_id', 'foreign', 'active media admission identity immutable'),
                    ('post_date', '2026-10-21', 'active media admission identity immutable'),
                    ('account', 'facebook', 'active media admission identity immutable'),
                    ('format', 'story', 'active media admission identity immutable'),
                    ('logical_post_id', str(uuid.uuid4()),
                     'logical_post_id cannot be set or changed by UPDATE'),
                    ('image_url', 'https://scratch.example/tampered.png',
                     'active media admission identity immutable'),
                    ('source_media_url', 'https://scratch.example/tampered.png',
                     'active media admission identity immutable'),
                    ('source_media_asset_id', 'foreign_asset',
                     'active media admission identity immutable'),
                    ('render_manifest_digest', 'sha256:' + '0' * 64,
                     'active media admission identity immutable')):
                denied(f'update content_calendar set {field}=%s where id=%s',
                       (value, ig['id']), 'service_role', fragment)
            denied('delete from content_calendar where id=%s', (ig['id'],),
                   'service_role', 'active media DELETE requires retained staged replacement')
            denied("insert into content_calendar(id,gym_id,post_date,status,variant_status,"
                   "image_url) values(%s,'gym','2026-10-22','pending','active',"
                   "'https://scratch.example/foreign.png')", (str(uuid.uuid4()),),
                   'service_role', 'active media INSERT requires registered staged finalization')
            assert sql('select caption from content_calendar where id=%s',
                       (ig['id'],))[0][0] == ig['caption']
            # Forged or stale alter responses hold BEFORE landing/CAS, reading
            # real authority underneath with one corrupted field each time.
            for rpc, key, bad in (
                    (pcs._SNAPSHOT_RPC, 'render_manifest_digest', 'sha256:' + '0' * 64),
                    (pcs._SNAPSHOT_RPC, 'source_asset_id', 'foreign_asset'),
                    (pcs._PROOF_RPC, 'reservation_id', str(uuid.uuid4())),
                    (pcs._PROOF_RPC, 'row_revision', '0' * 32),
                    (pcs._PROOF_RPC, 'source_sha256', '0' * 64),
                    (pcs._PROOF_RPC, 'tenant_id', 'foreign_gym'),
                    (pcs._PROOF_RPC, 'post_date', '2026-10-21'),
                    (pcs._PROOF_RPC, 'logical_post_id', str(uuid.uuid4()))):
                tampered, _ = pg_store(ctx['service_dsn'])
                real_rpc = tampered._reservation_rpc

                def corrupt(name, arguments, *, timeout, rpc=rpc, key=key, bad=bad):
                    observed = real_rpc(name, arguments, timeout=timeout)
                    return dict(observed, **{key: bad}) if name == rpc else observed
                with patch.object(tampered, '_reservation_rpc', side_effect=corrupt):
                    refused = recovery.run(store=tampered, media_store=object(),
                                           logger=logs.append)
                assert refused['held'] == 1 and refused['recovered'] == 0, (rpc, key, refused)
                assert journal.get(entry['use_id'])['state'] == 'write_intent'
                assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            for response in ('missing', 'timeout'):
                unavailable, _ = pg_store(ctx['service_dsn'])
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
            # A direct armed settlement call against the frozen pick still holds.
            pick = dict(journal_entry=entry, asset=entry['asset_before'], base=raw,
                day_key=entry['post_date'], claim_id=entry['claim_id'],
                claim_account=raw + '_gbp', store=object())
            assert not gbp_planner._settle_armed_drive_landing(
                raw, entry['calendar_row'], pick, ig, logs.append)
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 0
            # Other-tenant progress: an unrelated tenant slice is not held by
            # this tenant's unsettled use.
            other = recovery.run(store=pg_store(ctx['service_dsn'])[0],
                                 media_store=object(), logger=logs.append,
                                 tenant_id='othergym')
            assert other['ok'] is True and other['examined'] == 0, other
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            # Restart idempotence: a fresh store has no in-memory attempt; the
            # pinned original SQLite plus real PG terminal proof recover once.
            restarted, reads = pg_store(ctx['service_dsn'])
            assert restarted.last_forward_stage_attempt is None
            summary = recovery.run(store=restarted, media_store=object(), logger=logs.append)
            assert summary['recovered'] == 1 and summary['held'] == 0, (
                json.dumps(summary) + '\n' + '\n'.join(logs))
            settled = journal.get(entry['use_id'])
            assert settled['state'] == 'claim_done'
            assert settled['landed_proof']['calendar_row'] == ig
            assert journal.get_forward_stage(batch)['state'] == 'finalized'
            assert [call[1] for call in reads.calls] == [
                'content_calendar', pcs._BATCH_STATUS_RPC,
                pcs._SNAPSHOT_RPC, pcs._PROOF_RPC, 'content_calendar']
            assert all(call[2]['image_url'] == 'eq.' + ig['image_url']
                       for call in reads.calls if call[1] == 'content_calendar')
            stored = sql('select request,receipt from fixer_remote_drive_use_20261008'
                         ' where use_id=%s', (entry['use_id'],))[0]
            assert stored[0] == journal._remote_request(settled, settled['use_id'])
            assert stored[1] == settled['receipt'] == gbp_planner._recorded_remote_receipt(
                entry['use_id'])
            assert stored[1]['asset_after']['used_count'] == 1
            replay = remote_drive_use.DriveUseAuthority.from_environment()
            try:
                assert replay.apply_use(stored[0]) == replay.use_receipt(stored[0]) == stored[1]
            finally:
                replay.close()
            assert recovery.run(store=pg_store(ctx['service_dsn'])[0],
                                media_store=object(), logger=logs.append)['examined'] == 0
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 1
            assert sql('select used_count from media_asset where id=%s',
                       (ctx['asset'],))[0][0] == 1
            with db.connect() as conn:
                assert tuple(conn.execute(
                    "select status,post_id from socialapi_claims where draft_id=?"
                    " and account_key='gym_gbp'", (entry['claim_id'],)).fetchone()
                    ) == ('done', ctx['asset'])
            # Different-day and different-client reuse of the exact consumed
            # bytes conflicts permanently at owner preparation.
            for tenant, day in (('other', ctx['day']), ('gym', '2026-10-21')):
                competitor = seed(tenant=tenant, day=day, data_bytes=ctx['data'])
                denied('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',
                       (competitor['packet']['payload']['audit_id'],
                        json.dumps(competitor['original']), json.dumps(competitor['manifest'])),
                       'photo_owner', 'already used cleared or reserved')
            derivative = ctx['rendition']['data']
            assert derivative != ctx['data']
            competitor = seed(tenant='other', day=ctx['day'], data_bytes=derivative)
            denied('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',
                   (competitor['packet']['payload']['audit_id'],
                    json.dumps(competitor['original']), json.dumps(competitor['manifest'])),
                   'photo_owner', 'already used cleared or reserved')
        composed_pg(runtime_check=exercise, genuine_sources=True)


def test_feed_alias_staged_drive_recovery_composed_pg(tmp_path):
    """Exact raw owner certificate + authorized canonical staged alias settles once."""
    portal_0611_fixture()
    env, library = listener_env(tmp_path)
    with patch.dict(os.environ, env, clear=True), \
            patch.object(config, 'LIBRARY_PATH', str(library)), \
            patch.object(recovery, 'EPHEMERAL_ROOTS', ()):
        def exercise(*, sql, denied, seed, attest, dsn):
            install_remaining(sql)
            epoch = arm_listener(sql, dsn, library, 'gymalias')
            sql("insert into fixer_forward_media_tenant_alias_20261006"
                "(alias_key,tenant_id) values('gymalias','gymcanonical')")
            ctx = stage_feed_batch(sql, dsn, seed, epoch, raw='gymalias')
            raw, batch, entry = ctx['raw'], ctx['batch'], ctx['entry']
            # Canonical batch routing with the raw owner retained on the row.
            assert ctx['bound']['tenant_id'] == 'gymcanonical'
            assert entry['gym_id'] == 'gymalias'
            assert ctx['staged'][0]['gym_id'] == 'gymalias'
            assert journal.forward_stage_tenant_matches(dict(gym_id=raw), ctx['bound'])
            logs = []
            # Other-tenant progress is unaffected by this unsettled alias use.
            assert recovery.run(store=ctx['store'], media_store=object(),
                                logger=logs.append, tenant_id='othergym')['examined'] == 0
            # Before finalization the listener holds the alias use.
            assert recovery.run(store=ctx['store'], media_store=object(),
                                logger=logs.append, tenant_id=raw)['held'] == 1
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            # Raw-owner staged preparation and canonical attestation agree.
            candidate = prepare_candidate(sql, dsn, ctx, ctx['staged'][0]['id'],
                                          ctx['staged'][0]['visual_group_key'])
            attest_row(sql, candidate)
            # Even a genuine approved signature cannot relabel the raw source
            # owner as a different alias that maps to the same canonical tenant.
            sql("insert into fixer_forward_media_tenant_alias_20261006"
                "(alias_key,tenant_id) values('foreignalias','gymcanonical')")
            forged = deepcopy(candidate['packet']['payload'])
            forged['audit_id'] = str(uuid.uuid4())
            forged['candidate']['tenant_id'] = 'foreignalias'
            current = sql('select fixer_still_photo_snapshot_v2_20261008()')[0][0]
            for field in ('baseline_id', 'generation', 'spine_digest'):
                forged[field] = current[field]
            forged['dispositions'] = [dict(history_key=h['history_key'],
                disposition='reviewed_visual_nonmatch', inspected_sha256=h['visual_sha256'],
                published_binding_ref=h['published_binding_ref'],
                review_evidence_ref='SYNTHETIC independent object review')
                for h in current['rows']]
            forged['disposition_digest'] = digest(forged['dispositions'])
            signed_text = canonical(forged)
            signature = ctx['private'].sign(signed_text.encode()).hex()
            receipt = 'photo-audit:sha256:' + hashlib.sha256(
                (signed_text + '\n' + signature).encode()).hexdigest()
            denied('select fixer_still_photo_record_v2_20261008(%s,%s,%s)',
                   (signed_text, signature, receipt), 'photo_auditor',
                   'exact durable original receipt required')
            # Cross-tenant trusted attestations cannot establish an alias proof.
            alien_ids = []
            for aid in candidate['ids']:
                alien = str(uuid.uuid4())
                alien_ids.append(alien)
                sql("insert into forward_media_visual_attestation select "
                    "(jsonb_populate_record(null::forward_media_visual_attestation, "
                    "to_jsonb(a)||jsonb_build_object('attestation_id',%s::text,"
                    "'tenant_key','foreign_gym'))).* from forward_media_visual_attestation a "
                    "where attestation_id=%s", (alien, aid),
                    'fixer_forward_media_attester_20261006')
            denied('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
                   (candidate['rid'], ctx['logical'], candidate['rev'], alien_ids,
                    candidate['audit_id']), 'photo_owner', 'attestation evidence invalid')
            sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
                (candidate['rid'], ctx['logical'], candidate['rev'], candidate['ids'],
                 candidate['audit_id']), 'photo_owner')
            before = Path(os.environ['AGENT_DB_PATH']).read_bytes()
            result = run_finalizer(batch, 'gymcanonical', ctx['service_dsn'])
            assert result['report']['status'] == 'complete', result
            assert Path(os.environ['AGENT_DB_PATH']).read_bytes() == before
            assert journal.get(entry['use_id'])['state'] == 'write_intent'
            active = sql('select to_jsonb(c) from content_calendar c where id=%s',
                         (candidate['rid'],))[0][0]
            assert active['gym_id'] == raw and active['variant_status'] == 'active'
            assert active['published_at'] is None and active['late_post_id'] is None
            restarted, _ = pg_store(ctx['service_dsn'])
            assert restarted.last_forward_stage_attempt is None
            report = recovery.run(store=restarted, media_store=object(),
                                  logger=logs.append, tenant_id=raw)
            assert report['recovered'] == 1 and report['held'] == 0, (report, logs)
            settled = journal.get(entry['use_id'])
            assert settled['state'] == 'claim_done'
            assert settled['landed_proof']['calendar_row'] == active
            assert journal.get_forward_stage(batch)['state'] == 'finalized'
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 1
            assert sql('select used_count from media_asset where id=%s',
                       (ctx['asset'],))[0][0] == 1
            assert recovery.run(store=pg_store(ctx['service_dsn'])[0], media_store=object(),
                                logger=logs.append, tenant_id=raw)['examined'] == 0
            assert sql('select count(*) from fixer_remote_drive_use_20261008')[0][0] == 1
        composed_pg(runtime_check=exercise, genuine_sources=True)
