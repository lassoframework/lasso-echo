"""Offline tests for the prospective permanent occupancy admission helper."""
import hashlib
import os
import unittest
import uuid
from unittest.mock import patch

from agent.forward_media_owner import ForwardMediaOwnerPersistence
from agent.forward_media_photo_certificate import (
    IndependentPhotoAuditor,
    PhotoCertificateHold,
    digest,
)
from agent.forward_media_prospective_photo_prepare import (
    FLAG_ENV,
    PreparedProspectivePhoto,
    ProspectivePhotoHold,
    admission_receipt,
    admit_prepared_photo,
    admit_single_candidate,
    prepare_prospective_photo,
    reconcile_admission,
)
from agent.forward_media_attester import make_still_recipe
from agent.forward_media_source_verifier import verify_source
from tests.gym_media_fakes import bound_review_fields
from tests.test_forward_media_photo_certificate import fixtures
from tests.test_forward_media_source_verifier import Drive, Hosted, FILE, FOLDER, URL
from tests.test_forward_media_owner_two_phase_pg import png

LOGICAL_POST = str(uuid.uuid4())
REVISION = 'a' * 32
ATTESTATIONS = [str(uuid.uuid4()) for _ in range(3)]
OCCUPANCY = str(uuid.uuid4())


class Cursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, args=None):
        self._conn.queries.append(query)
        if 'fixer_forward_media_photo_content_20261007' in query:
            self._row = ('sha256:' + 'a' * 64,)
        elif 'fixer_forward_media_photo_snapshot_exclusion_20261008' in query:
            self._row = (self._conn.corpus,)
        elif 'admit_prospective_photo_occupancy_20261008' in query:
            self._conn.admit_calls.append(args)
            self._row = (self._conn.admit_result,)
        elif 'forward_prospective_photo_proof_20261008' in query:
            self._conn.proof_calls.append(args)
            self._row = (self._conn.proof_result,)
        else:
            self._row = ('offline_owner',)

    def fetchone(self):
        return self._row


class Connection:
    def __init__(self, admit_result=None, proof_result=None, fail_commit=False,
                 corpus=None):
        self.queries = []
        self.admit_calls = []
        self.proof_calls = []
        self.admit_result = admit_result
        self.proof_result = proof_result
        self.fail_commit = fail_commit
        self.corpus = corpus
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError('lost commit response')

    def rollback(self):
        self.rollbacks += 1


def proof_row(prepared, *, occupancy_id=OCCUPANCY):
    receipt = admission_receipt(prepared)
    return {'lane': 'prospective_occupancy', 'occupancy_id': occupancy_id,
            'tenant_id': receipt['tenant_id'], 'post_date': receipt['post_date'],
            'logical_post_id': LOGICAL_POST, 'source_sha256': receipt['source_sha256'],
            'audit_id': receipt['audit_id'], 'row_revision': REVISION,
            'lineage_evidence_id': str(uuid.uuid4()),
            'attestation_ids': list(ATTESTATIONS)}


class ProspectivePrepareTests(unittest.TestCase):
    def setup_candidate(self):
        data = png('blue')
        drive = Drive()
        drive.data = data
        drive.meta['size'] = str(len(data))
        drive.meta['md5Checksum'] = hashlib.md5(data).hexdigest()
        row_id = str(uuid.uuid4())
        attested = hashlib.sha256(data).hexdigest()
        snapshot = {'calendar': {'id': row_id, 'gym_id': 'gym', 'source_media_asset_id': FILE,
            'source_media_url': URL, 'image_url': URL, 'thumbnail_url': None,
            'visual_group_key': 'group', 'post_date': '2026-10-10', 'status': 'pending',
            'variant_status': 'active', 'publish_claim_token': None,
            'published_at': None, 'late_post_id': None, 'media_not_ready_reason': None},
            'asset': {'id': FILE, 'gym_id': 'gym', 'source_id': 'source',
                      'content_hash': attested, 'review_content_hash': attested},
            'source': {'id': 'source', 'gym_id': 'gym', 'kind': 'gym_drive',
                       'active': True, 'folder_id': FOLDER},
            'revision': '0' * 32, 'binding_revision': '1' * 32}
        asset = {'id': FILE, 'source_id': 'source', 'gym_id': 'gym', 'kind': 'photo',
                 'eligible': True, 'excluded_by_coach': False, 'content_hash': attested,
                 'used_count': 0, 'last_used_at': None,
                 **bound_review_fields(FILE, 'gym', content_hash=attested)}
        recipe = make_still_recipe('identity')
        source = verify_source(snapshot, drive, Hosted(data))
        candidate = {'calendar_row_id': row_id, 'tenant_id': 'gym', 'group_key': 'group',
            'post_date': '2026-10-10', 'source_asset_id': FILE, 'source_url': URL,
            'image_url': URL,
            'source_fingerprint': source.original.source_fingerprint,
            'source_sha256': source.evidence['source_sha256'],
            'source_length': len(data),
            'source_receipt_ref': source.receipt_ref,
            'image_fingerprint': source.original.source_fingerprint,
            'image_sha256': source.evidence['source_sha256'],
            'image_length': len(data), 'render_recipe_digest': digest(recipe),
            'content_digest': 'sha256:' + 'a' * 64}
        packet, key, corpus, _ = fixtures(candidate=candidate)
        auditor = IndependentPhotoAuditor(Connection(corpus=corpus), 'offline-dedicated-owner')
        def rpc(name, values):
            return {'packet': packet, 'approved_key': key} if name == 'certificate' else corpus
        return snapshot, drive, data, asset, recipe, packet, auditor, rpc

    def prepare(self):
        snapshot, drive, data, asset, recipe, packet, auditor, rpc = self.setup_candidate()
        with patch.dict(os.environ, {FLAG_ENV: 'true'}):
            with patch.object(auditor, '_rpc', side_effect=rpc):
                prepared = prepare_prospective_photo(
                    snapshot, asset=asset, drive_reader=drive,
                    hosted_reader=Hosted(data), recipe=recipe, auditor=auditor,
                    audit_id=packet['payload']['audit_id'])
        return prepared, packet

    def persistence(self, conn):
        p = ForwardMediaOwnerPersistence(conn, 'offline_owner', None)
        p._assert_owner_identity = lambda: None
        return p

    def admit(self, conn, prepared):
        return admit_single_candidate(self.persistence(conn), prepared,
                                      logical_post_id=LOGICAL_POST,
                                      expected_revision=REVISION,
                                      attestation_ids=ATTESTATIONS)

    def test_happy_path_admits_one_exact_candidate(self):
        prepared, packet = self.prepare()
        self.assertIsInstance(prepared, PreparedProspectivePhoto)
        self.assertEqual(prepared.manifest.operation, 'same_object')
        receipt = admission_receipt(prepared)
        self.assertTrue(REVISION and receipt['source_sha256'])
        conn = Connection(admit_result=OCCUPANCY)
        report = self.admit(conn, prepared)
        self.assertEqual(report['status'], 'admitted')
        self.assertEqual(report['occupancy_id'], OCCUPANCY)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(len(conn.admit_calls), 1)
        self.assertEqual(conn.admit_calls[0], (receipt['calendar_row_id'], LOGICAL_POST,
                                               REVISION, list(ATTESTATIONS),
                                               receipt['audit_id']))
        self.assertTrue(any('admit_prospective_photo_occupancy_20261008' in q
                            for q in conn.queries))

    def test_default_off_holds_before_any_read(self):
        snapshot, drive, data, asset, recipe, packet, auditor, rpc = self.setup_candidate()
        os.environ.pop(FLAG_ENV, None)
        reads_after_setup = drive.reads
        with patch.object(auditor, '_rpc', side_effect=rpc):
            with self.assertRaisesRegex(ProspectivePhotoHold, 'prospective_prepare_disabled'):
                prepare_prospective_photo(snapshot, asset=asset, drive_reader=drive,
                    hosted_reader=Hosted(data), recipe=recipe, auditor=auditor,
                    audit_id=packet['payload']['audit_id'])
        self.assertEqual(drive.reads, reads_after_setup)
        self.assertEqual(auditor.conn.queries, [])
        with patch.dict(os.environ, {FLAG_ENV: 'ambiguous'}):
            with patch.object(auditor, '_rpc', side_effect=rpc):
                with self.assertRaisesRegex(ProspectivePhotoHold, 'prospective_prepare_disabled'):
                    prepare_prospective_photo(snapshot, asset=asset, drive_reader=drive,
                        hosted_reader=Hosted(data), recipe=recipe, auditor=auditor,
                        audit_id=packet['payload']['audit_id'])
        self.assertEqual(drive.reads, reads_after_setup)

    def test_missing_approval_or_safety_evidence_holds(self):
        for damage in (
                lambda a: a.update(review_status='pending'),
                lambda a: a.update(moderation_status='flagged'),
                lambda a: a.update(review_content_hash='stale-hash'),
                lambda a: a.update(kind='video')):
            snapshot, drive, data, asset, recipe, packet, auditor, rpc = self.setup_candidate()
            damage(asset)
            reads_after_setup = drive.reads
            with patch.dict(os.environ, {FLAG_ENV: 'true'}):
                with patch.object(auditor, '_rpc', side_effect=rpc):
                    with self.assertRaisesRegex(ProspectivePhotoHold,
                                                'prospective_approval_evidence_missing'):
                        prepare_prospective_photo(snapshot, asset=asset, drive_reader=drive,
                            hosted_reader=Hosted(data), recipe=recipe, auditor=auditor,
                            audit_id=packet['payload']['audit_id'])
            self.assertEqual(drive.reads, reads_after_setup)

    def test_fresh_asset_provenance_must_match_snapshot(self):
        for field, value in (('source_id', 'different-source'),
                             ('review_content_hash', 'different-review-hash')):
            snapshot, drive, data, asset, recipe, packet, auditor, rpc = self.setup_candidate()
            asset[field] = value
            reads_after_setup = drive.reads
            with patch.dict(os.environ, {FLAG_ENV: 'true'}):
                with patch.object(auditor, '_rpc', side_effect=rpc):
                    with self.assertRaisesRegex(ProspectivePhotoHold,
                                                'prospective_approval_evidence_missing'):
                        prepare_prospective_photo(snapshot, asset=asset, drive_reader=drive,
                            hosted_reader=Hosted(data), recipe=recipe, auditor=auditor,
                            audit_id=packet['payload']['audit_id'])
            self.assertEqual(drive.reads, reads_after_setup)

    def test_attested_hash_mismatch_holds(self):
        snapshot, drive, data, asset, recipe, packet, auditor, rpc = self.setup_candidate()
        wrong = hashlib.sha256(b'other-bytes').hexdigest()
        asset.update(bound_review_fields(FILE, 'gym', content_hash=wrong))
        snapshot['asset']['content_hash'] = wrong
        snapshot['asset']['review_content_hash'] = wrong
        with patch.dict(os.environ, {FLAG_ENV: 'true'}):
            with patch.object(auditor, '_rpc', side_effect=rpc):
                with self.assertRaisesRegex(ProspectivePhotoHold,
                                            'prospective_attested_hash_mismatch'):
                    prepare_prospective_photo(snapshot, asset=asset, drive_reader=drive,
                        hosted_reader=Hosted(data), recipe=recipe, auditor=auditor,
                        audit_id=packet['payload']['audit_id'])

    def test_rendered_byte_mismatch_holds(self):
        snapshot, drive, data, asset, recipe, packet, auditor, rpc = self.setup_candidate()
        rendered_url = 'https://media.example.test/rendered.png'
        snapshot['calendar']['image_url'] = rendered_url

        class Reader(Hosted):
            def read(self, url):
                return png('red') if url == rendered_url else data

        with patch.dict(os.environ, {FLAG_ENV: 'true'}):
            with patch.object(auditor, '_rpc', side_effect=rpc):
                with self.assertRaisesRegex(ProspectivePhotoHold,
                                            'prospective_render_bytes_mismatch'):
                    prepare_prospective_photo(snapshot, asset=asset, drive_reader=drive,
                        hosted_reader=Reader(data), recipe=recipe, auditor=auditor,
                        audit_id=packet['payload']['audit_id'])

    def test_tampered_certificate_is_not_admission_evidence(self):
        snapshot, drive, data, asset, recipe, packet, auditor, rpc = self.setup_candidate()
        packet['signature_hex'] = '00' * 64
        with patch.dict(os.environ, {FLAG_ENV: 'true'}):
            with patch.object(auditor, '_rpc', side_effect=rpc):
                with self.assertRaisesRegex(PhotoCertificateHold, 'signature_invalid'):
                    prepare_prospective_photo(snapshot, asset=asset, drive_reader=drive,
                        hosted_reader=Hosted(data), recipe=recipe, auditor=auditor,
                        audit_id=packet['payload']['audit_id'])

    def test_uncertain_commit_reconciles_by_exact_receipt(self):
        prepared, _ = self.prepare()
        # Lost commit response, admission actually landed: the lane-tagged
        # proof read back by the exact receipt proves it.
        conn = Connection(admit_result=OCCUPANCY,
                          proof_result=proof_row(prepared), fail_commit=True)
        report = self.admit(conn, prepared)
        self.assertEqual(report['status'], 'reconciled_committed')
        self.assertEqual(report['occupancy']['occupancy_id'], OCCUPANCY)
        self.assertEqual(len(conn.admit_calls), 1)  # never blindly retried
        receipt = admission_receipt(prepared)
        self.assertEqual(conn.proof_calls,
                         [(receipt['calendar_row_id'], receipt['source_sha256'])])
        # Lost commit response, no stored proof: unknown, manual resolution.
        conn = Connection(admit_result=OCCUPANCY, proof_result=None, fail_commit=True)
        report = self.admit(conn, prepared)
        self.assertEqual(report['status'], 'uncertain')
        self.assertEqual(report['occupancy_id'], OCCUPANCY)
        self.assertEqual(len(conn.admit_calls), 1)
        # A proof for a different occupancy never reconciles this admission.
        conn = Connection(admit_result=OCCUPANCY,
                          proof_result=proof_row(prepared, occupancy_id=str(uuid.uuid4())),
                          fail_commit=True)
        report = self.admit(conn, prepared)
        self.assertEqual(report['status'], 'uncertain')

    def test_admission_binding_and_receipt_validation_hold(self):
        prepared, _ = self.prepare()
        conn = Connection(admit_result=OCCUPANCY)
        persistence = self.persistence(conn)
        with self.assertRaisesRegex(ProspectivePhotoHold, 'prospective_admission_binding_invalid'):
            admit_prepared_photo(persistence, prepared, logical_post_id='not-a-uuid',
                                 expected_revision=REVISION, attestation_ids=ATTESTATIONS)
        with self.assertRaisesRegex(ProspectivePhotoHold, 'prospective_admission_binding_invalid'):
            admit_prepared_photo(persistence, prepared, logical_post_id=LOGICAL_POST,
                                 expected_revision=REVISION, attestation_ids=ATTESTATIONS[:2])
        with self.assertRaisesRegex(ProspectivePhotoHold, 'dedicated_prepared'):
            admit_prepared_photo(object(), prepared, logical_post_id=LOGICAL_POST,
                                 expected_revision=REVISION, attestation_ids=ATTESTATIONS)
        with self.assertRaisesRegex(ProspectivePhotoHold, 'prospective_receipt_ref_invalid'):
            reconcile_admission(persistence, {'source_sha256': 'not-a-sha'})
        # A non-uuid admission result is not a receipt.
        bad = Connection(admit_result='not-a-uuid')
        with self.assertRaisesRegex(ProspectivePhotoHold, 'prospective_occupancy_receipt_invalid'):
            self.admit(bad, prepared)


if __name__ == '__main__':
    unittest.main()
