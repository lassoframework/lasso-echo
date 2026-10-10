"""Real owner entrypoint over composed disposable PG17 and genuine signatures.

Remote factories provide synthetic bytes/Drive metadata only. Owner DB factory,
transport RPCs, helper, signatures, quarantine and occupancy are unmocked.
Service binder and trusted attester have separate connections between passes.
"""
import json
import os
import uuid
from unittest.mock import patch

from agent import forward_media_owner as owner, forward_media_owner_worker as worker
from agent import forward_media_source_verifier as source
from agent.forward_media_prospective_photo_prepare import FLAG_ENV
from tests.test_forward_media_prospective_still_v2_pg import main as composed
from tests.test_forward_media_owner_two_phase_pg import png


def exercise(*, sql, denied, seed, attest, dsn):
    env = {
        'FORWARD_MEDIA_OWNER_DSN': dsn.replace('user=postgres', 'user=photo_owner'),
        'FORWARD_MEDIA_OWNER_ROLE': 'photo_owner',
        'AGENT_S3_PUBLIC_BASE_URL': 'https://scratch.example',
        worker.WORKER_ENV: 'true', worker.STAGED_ENV: 'true',
        worker.TENANTS_ENV: 'gym,other-gym', worker.PROSPECTIVE_V2_ENV: 'true',
        FLAG_ENV: 'true', 'AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE': '2',
    }
    files, urls, reads, connections = {}, {}, [], []
    connect_owner = owner.ForwardMediaOwnerPersistence.connect_from_environment

    def tracked_owner(*, reader):
        persistence = connect_owner(reader=reader)
        connections.append(persistence._conn)
        return persistence

    def idle_remote_read():
        from psycopg.pq import TransactionStatus
        assert connections and connections[-1].info.transaction_status == TransactionStatus.IDLE

    def add(**kwargs):
        candidate = seed(**kwargs)
        files[candidate['asset']] = candidate
        urls[candidate['url']] = candidate
        return candidate

    class Drive:
        def metadata(self, file_id):
            if file_id == 'FolderOriginal1234567':
                return {'id': file_id, 'mimeType': 'application/vnd.google-apps.folder',
                        'trashed': False, 'version': '2'}
            c = files[file_id]
            return {'id': file_id, 'mimeType': 'image/png', 'trashed': False,
                    'version': '3', 'parents': ['FolderOriginal1234567'],
                    'size': str(len(c['bytes'])), 'md5Checksum': c['md5']}

        def original_bytes(self, file_id):
            idle_remote_read()
            reads.append(('drive', file_id))
            return files[file_id].get('drive_bytes', files[file_id]['bytes'])

    class Hosted(owner.ObjectReader):
        def read(self, exact_url):
            idle_remote_read()
            reads.append(('host', exact_url))
            return urls[exact_url].get('hosted_bytes', urls[exact_url]['bytes'])

    def pending():
        return sql('select fixer_still_v2_owner_pending_20261008(%s,100)',
                   (['gym', 'other-gym'],), 'photo_owner')[0][0]

    def outcome(c, phase):
        return sql('select state,outcome from fixer_still_v2_owner_progress_20261008 '
                   'where audit_id=%s and phase=%s', (c['packet']['payload']['audit_id'], phase))

    def occupancy_count():
        return sql('select count(*) from forward_prospective_photo_occupancy_20261008')[0][0]

    def lost_commit(*, commit_number, landed):
        import psycopg
        connect = psycopg.connect
        class Connection:
            def __init__(self, real):
                self.real, self.commits = real, 0
            def __getattr__(self, name):
                return getattr(self.real, name)
            def commit(self):
                self.commits += 1
                if self.commits == commit_number:
                    if landed:
                        self.real.commit()
                    raise ConnectionError('SYNTHETIC lost COMMIT acknowledgment')
                self.real.commit()
        def wrapped(*args, **kwargs):
            return Connection(connect(*args, **kwargs))
        with patch.object(psycopg, 'connect', wrapped):
            return worker.run_once()

    with patch.dict(os.environ, env, clear=True), \
            patch.object(owner, 'HostedObjectReader', Hosted), \
            patch.object(owner.ForwardMediaOwnerPersistence, 'connect_from_environment', tracked_owner), \
            patch.object(source, 'OriginalDriveReader', Drive):
        sql('update forward_prospective_photo_gate_20261008 set enabled=true')
        c = add(data_bytes=png('purple'))
        assert [p['phase'] for p in pending()] == ['prepare']
        report = worker.run_once()
        assert report['status'] == 'complete', report
        assert report['rows'][0]['phase'] == 'prepare'
        assert report['rows'][0]['status'] == 'persisted'
        assert reads and occupancy_count() == 0
        assert outcome(c, 'prepare')[0][0] == 'final'
        assert sql('select variant_status from content_calendar where id=%s', (c['rid'],))[0][0] == 'candidate'
        assert worker.run_once()['rows'] == []  # No unverified attester fallback.

        sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)', (c['rid'],), 'service_role')
        attest(c)
        assert [p['phase'] for p in pending()] == ['admit']
        reads.clear()
        admitted = worker.run_once()
        assert admitted['status'] == 'complete', admitted
        assert admitted['rows'][0]['phase'] == 'admit'
        assert admitted['rows'][0]['occupancy_id']
        assert occupancy_count() == 1 and reads
        assert outcome(c, 'admit')[0][0] == 'final'
        assert sql('select variant_status from content_calendar where id=%s', (c['rid'],))[0][0] == 'candidate'
        assert worker.run_once()['rows'] == []  # No replay of an authority write.

        sql('update forward_media_visual_gate_20261008 set enabled=true')
        candidates = [{'calendar_row_id': c['rid'], 'logical_post_id': c['logical'],
                       'expected_revision': c['rev'], 'attestation_ids': c['ids'],
                       'expected_reservation_id': None}]
        finalized = sql('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',
                        (c['tenant'], c['batch'], json.dumps(candidates), '[]'), 'service_role')[0][0]
        assert finalized['reservation_ids']
        sql('insert into fixer_forward_media_claim_gate_20261006 values(%s,true)', (c['tenant'],))
        token = str(uuid.uuid4())
        sql("update content_calendar set status='publishing',publish_claim_token=%s,publish_reservation_day=post_date where id=%s",
            (token, c['rid']))
        claim = (c['rid'], token, c['evidence'], c['rev'], c['ids'])
        assert sql('select fixer_forward_visual_index_claim_20261008(%s,%s,%s,%s,%s::uuid[])', claim, 'service_role')[0][0]

        ambiguous = add(data_bytes=png('navy'))
        reads.clear()
        assert lost_commit(commit_number=1, landed=True) == {
            'status': 'hold', 'reason': 'uncertain_authority_commit', 'rows': []}
        assert reads == [] and outcome(ambiguous, 'prepare')[0][0] == 'quarantine'
        assert worker.run_once()['rows'] == []

        prepare_unknown = add(data_bytes=png('olive'))
        assert lost_commit(commit_number=2, landed=True) == {
            'status': 'hold', 'reason': 'uncertain_authority_commit', 'rows': []}
        assert outcome(prepare_unknown, 'prepare')[0][0] == 'final'
        assert worker.run_once()['rows'] == []
        sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)',
            (prepare_unknown['rid'],), 'service_role')
        attest(prepare_unknown, phash=-1)
        reconciled = lost_commit(commit_number=2, landed=True)
        assert reconciled['status'] == 'complete', reconciled
        assert reconciled['rows'][0]['reconciled_committed'] is True
        assert outcome(prepare_unknown, 'admit')[0][0] == 'final'
        assert worker.run_once()['rows'] == []
        permanent_count = occupancy_count()
        assert permanent_count == 2

        admission_unknown = add(data_bytes=png('pink'))
        assert worker.run_once()['status'] == 'complete'
        sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)',
            (admission_unknown['rid'],), 'service_role')
        attest(admission_unknown, phash=(1 << 32) - 1)
        failed = lost_commit(commit_number=2, landed=False)
        assert failed == {'status': 'hold', 'reason': 'uncertain_authority_commit', 'rows': []}
        assert outcome(admission_unknown, 'admit')[0][0] == 'quarantine'
        assert occupancy_count() == permanent_count
        assert worker.run_once()['rows'] == []

        # Same bytes remain permanently occupied across tenants, dates and new
        # logical posts; source used_count=0 cannot establish fresh inventory.
        for tenant, day in [('other-gym', '2026-10-10'), ('gym', '2026-10-11'), ('gym', '2026-10-10')]:
            competitor = add(tenant=tenant, day=day, data_bytes=c['bytes'])
            report = worker.run_once()
            assert report['status'] == 'hold', report
            assert occupancy_count() == permanent_count
            assert outcome(competitor, 'prepare')[0][0] == 'quarantine'
            assert worker.run_once()['rows'] == []

        # Swift's pending/flagged inventory is HOLD, regardless of the legacy
        # photo release field. No occupancy/depletion stamp is created.
        blocked = add(data_bytes=png('orange'))
        sql("update media_asset set review_status='pending',moderation_status='flagged' where id=%s", (blocked['asset'],))
        before_reads = len(reads)
        assert worker.run_once()['rows'] == []
        assert len(reads) == before_reads and occupancy_count() == permanent_count
        assert outcome(blocked, 'prepare') == []
        assert sql('select used_count from media_asset where id=%s', (blocked['asset'],))[0][0] == 0

        drift = add(data_bytes=png('cyan'))
        drift['hosted_bytes'] = png('magenta')
        held = worker.run_once()
        assert held['status'] == 'partial_hold', held
        assert held['rows'][0]['reason'] == 'prospective_v2_verification_failed'
        assert outcome(drift, 'prepare')[0][0] == 'final'
        assert occupancy_count() == permanent_count and worker.run_once()['rows'] == []

        original_drift = add(data_bytes=png('teal'))
        original_drift['drive_bytes'] = png('gold')
        held = worker.run_once()
        assert held['status'] == 'partial_hold', held
        assert held['rows'][0]['reason'] == 'prospective_v2_verification_failed'
        assert outcome(original_drift, 'prepare')[0][0] == 'final'
        assert occupancy_count() == permanent_count and worker.run_once()['rows'] == []

        unknown = add(data_bytes=png('yellow'))
        unknown_rid = str(uuid.uuid4())
        sql("insert into content_calendar(id,gym_id,post_date,status,variant_status,image_url) "
            "values(%s,'unknown-gym','2026-10-09','published','active','https://scratch.example/unknown')", (unknown_rid,))
        before_reads = len(reads)
        assert worker.run_once()['rows'] == []
        assert len(reads) == before_reads and outcome(unknown, 'prepare') == []
        assert occupancy_count() == permanent_count
        print('PASS: actual owner run_once prepare/admit, real Ed25519/Drive-source receipt, '
              'bound service attester handoff, permanent admission before finalization/claim; '
              'repeat/tenant/date/quarantine, pending flagged Swift zero-use inventory, '
              'hosted drift and unknown-history HOLD; lost initial/final COMMIT '
              'never retries and admission reconciles exact proof+outcome only. '
              'Synthetic local evidence only.')


def test_prospective_v2_owner_runtime_pg():
    composed(runtime_check=exercise,
             extra_migrations=('DRAFT_fixer_still_v2_owner_transport_20261008.sql',),
             genuine_sources=True)


if __name__ == '__main__':
    test_prospective_v2_owner_runtime_pg()
