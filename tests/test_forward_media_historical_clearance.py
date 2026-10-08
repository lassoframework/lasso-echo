"""Offline historical-clearance authority tests (schema_version 1 contract).

Covers wrong tenant/version, missing media kinds, altered signature, stale
corpus, renamed/rehosted/cropped/thumbnail collision and concurrent per-audit
reservation to the extent reachable without a database. All keys are
SYNTHETIC; nothing here clears a live photo.
"""
import copy
import hashlib
import unittest
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent.forward_media_photo_certificate import (
    CORPUS_SCHEMA_VERSION, MAX_CORPUS_ROWS, PhotoCertificateHold, canonical, digest,
    reconcile_review_evidence, require_zero_exclusions, verify)
from agent.forward_media_owner_photo_prepare import run_photo_pass
from agent.forward_media_owner import ForwardMediaOwnerPersistence
from tests.test_forward_media_photo_certificate import fixtures as _raw_fixtures


def resign(packet, private):
    packet['signature_hex'] = private.sign(canonical(packet['payload']).encode()).hex()
    return packet


def zero_exclusions(snapshot):
    """New-contract exclusion evidence for an empty baseline exclusion set."""
    snapshot.update(excluded_rows_count=0, excluded_rows_digest=digest([]))
    return snapshot


def fixtures(*args, **kwargs):
    """Shared fixtures brought up to the exclusion-evidence contract."""
    packet, key, snapshot, private = _raw_fixtures(*args, **kwargs)
    return packet, key, zero_exclusions(snapshot), private


class HistoricalClearanceContractTests(unittest.TestCase):
    def test_wrong_tenant_or_version_holds(self):
        packet, key, snapshot, private = fixtures()
        expected = copy.deepcopy(packet['payload']['candidate'])
        expected['tenant_id'] = 'other-gym'
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_candidate_changed'):
            verify(packet, key, snapshot, expected)
        for version in (0, CORPUS_SCHEMA_VERSION + 1, '1'):
            packet, key, snapshot, private = fixtures()
            packet['payload']['schema_version'] = version
            resign(packet, private)
            with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_shape_invalid'):
                verify(packet, key, snapshot)

    def test_missing_media_kinds_hold_and_cannot_be_omitted(self):
        for kind in ('video', 'carousel', 'unknown_kind'):
            packet, key, snapshot, private = fixtures(candidate=None, snapshot=None)
            snapshot['rows'].append({'history_key': 'synthetic-motion', 'resolved': True,
                'media_kind': kind, 'visual_sha256': digest('synthetic motion frame'),
                'published_binding_ref': 'SYNTHETIC motion receipt'})
            packet['payload']['dispositions'].append(
                {'history_key': 'synthetic-motion', 'disposition': 'reviewed_visual_nonmatch',
                 'inspected_sha256': digest('synthetic motion frame'),
                 'published_binding_ref': 'SYNTHETIC motion receipt',
                 'review_evidence_ref': 'SYNTHETIC frame inspection'})
            packet['payload']['disposition_digest'] = digest(packet['payload']['dispositions'])
            resign(packet, private)
            with self.assertRaisesRegex(PhotoCertificateHold,
                                        'certificate_history_unresolved_or_matching'):
                verify(packet, key, snapshot)
        # Dropping the non-still row from dispositions instead of judging it
        # fails closed as an incomplete review.
        packet, key, snapshot, private = fixtures()
        snapshot['rows'].append({'history_key': 'synthetic-motion', 'resolved': True,
            'media_kind': 'video', 'visual_sha256': digest('synthetic motion frame'),
            'published_binding_ref': 'SYNTHETIC motion receipt'})
        packet['payload']['disposition_digest'] = digest(packet['payload']['dispositions'])
        resign(packet, private)
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_dispositions_incomplete'):
            verify(packet, key, snapshot)

    def test_oversized_or_stale_corpus_holds(self):
        packet, key, snapshot, private = fixtures()
        rows = []
        for i in range(MAX_CORPUS_ROWS + 1):
            rows.append({'history_key': 'k%d' % i, 'resolved': True, 'media_kind': 'still_photo',
                'visual_sha256': digest('row %d' % i), 'published_binding_ref': 'ref %d' % i})
        snapshot['rows'] = rows
        packet['payload']['dispositions'] = [
            {'history_key': r['history_key'], 'disposition': 'reviewed_visual_nonmatch',
             'inspected_sha256': r['visual_sha256'], 'published_binding_ref': r['published_binding_ref'],
             'review_evidence_ref': 'SYNTHETIC inspection'} for r in rows]
        packet['payload']['disposition_digest'] = digest(packet['payload']['dispositions'])
        resign(packet, private)
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_dispositions_incomplete'):
            verify(packet, key, snapshot)
        # Stale corpus: generation advance or baseline swap after signing.
        for mutate in (lambda s: s.update(generation=s['generation'] + 1),
                       lambda s: s.update(baseline_id=str(uuid.uuid4())),
                       lambda s: s.update(spine_digest=digest('newer corpus'))):
            packet, key, snapshot, _ = fixtures()
            mutate(snapshot)
            with self.assertRaisesRegex(PhotoCertificateHold,
                                        'certificate_corpus_stale_or_unapproved'):
                verify(packet, key, snapshot)

    def test_altered_signature_holds(self):
        packet, key, snapshot, _ = fixtures()
        packet['signature_hex'] = ('00' if packet['signature_hex'][:2] != '00' else 'ff') \
            + packet['signature_hex'][2:]
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_signature_invalid'):
            verify(packet, key, snapshot)
        # Resigning altered payload bytes with a different key also fails.
        packet, key, snapshot, _ = fixtures()
        packet['payload']['candidate']['source_receipt_ref'] = 'source-receipt:sha256:' + 'b' * 64
        other = Ed25519PrivateKey.generate()  # SYNTHETIC test key only.
        packet['signature_hex'] = other.sign(canonical(packet['payload']).encode()).hex()
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_signature_invalid'):
            verify(packet, key, snapshot)

    def test_renamed_rehosted_cropped_and_thumbnail_collision_hold(self):
        # Same bytes under a renamed Drive URL: expected binding differs.
        packet, key, snapshot, _ = fixtures()
        for field, value in (
                ('source_url', 'https://media.example.test/renamed-source.png'),
                ('image_url', 'https://cdn.example.test/rehosted-image.png')):
            expected = copy.deepcopy(packet['payload']['candidate'])
            expected[field] = value
            with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_candidate_changed'):
                verify(packet, key, snapshot, expected)
        # Cropped derivative: different delivered bytes never share clearance.
        expected = copy.deepcopy(packet['payload']['candidate'])
        expected['image_sha256'] = digest('synthetic cropped derivative')
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_candidate_changed'):
            verify(packet, key, snapshot, expected)
        # Thumbnail collision: a signed thumbnail tuple swapped for another
        # object's thumbnail invalidates the signature.
        packet, key, snapshot, private = fixtures()
        candidate = packet['payload']['candidate']
        candidate.update(thumbnail_url='https://media.example.test/thumb-a.png',
            thumbnail_sha256=digest('thumbnail a'),
            thumbnail_fingerprint='md5:' + hashlib.md5(b'thumbnail a').hexdigest(),
            thumbnail_length=len(b'thumbnail a'))
        resign(packet, private)
        verify(packet, key, snapshot, candidate)  # exact tuple verifies
        collided = copy.deepcopy(candidate)
        collided['thumbnail_sha256'] = digest('thumbnail b from another object')
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_candidate_changed'):
            verify(packet, key, snapshot, collided)
        candidate['thumbnail_url'] = 'https://media.example.test/thumb-b.png'
        with self.assertRaisesRegex(PhotoCertificateHold, 'certificate_signature_invalid'):
            verify(packet, key, snapshot)


class ExclusionEvidenceContractTests(unittest.TestCase):
    """Zero-exclusion gate: excluded rows outside snapshot.rows can never be
    accepted under schema_version 1; old-format snapshots fail closed."""

    def test_old_format_snapshot_without_exclusion_evidence_holds(self):
        packet, key, snapshot, _ = _raw_fixtures()
        # The shared fixture now models the guarded RPC. Strip its added
        # fields to exercise the frozen RPC's old snapshot shape explicitly.
        snapshot.pop('excluded_rows_count')
        snapshot.pop('excluded_rows_digest')
        self.assertNotIn('excluded_rows_count', snapshot)
        with self.assertRaisesRegex(PhotoCertificateHold,
                                    'certificate_exclusions_unaccounted'):
            verify(packet, key, snapshot)

    def test_missing_null_or_mistyped_exclusion_evidence_holds(self):
        for mutate in (lambda s: s.pop('excluded_rows_count'),
                       lambda s: s.pop('excluded_rows_digest'),
                       lambda s: s.update(excluded_rows_count=None),
                       lambda s: s.update(excluded_rows_count=False),
                       lambda s: s.update(excluded_rows_count='0'),
                       lambda s: s.update(excluded_rows_digest=None),
                       lambda s: s.update(excluded_rows_digest=digest(['nonempty']))):
            packet, key, snapshot, _ = fixtures()
            mutate(snapshot)
            with self.assertRaisesRegex(PhotoCertificateHold,
                                        'certificate_exclusions_unaccounted'):
                verify(packet, key, snapshot)

    def test_excluded_row_outside_snapshot_rows_holds_despite_valid_signature(self):
        # The frozen RPC omits baseline excluded_rows_json from rows; simulate
        # a reviewed video exclusion present in the baseline but absent from
        # the signed corpus rows and dispositions.
        packet, key, snapshot, private = fixtures()
        excluded_row = {'history_key': 'synthetic-excluded-video', 'resolved': True,
            'media_kind': 'reviewed_video_scope_exclusion',
            'visual_sha256': digest('synthetic excluded video frame'),
            'published_binding_ref': 'SYNTHETIC excluded video receipt'}
        self.assertNotIn(excluded_row['history_key'],
                         {r['history_key'] for r in snapshot['rows']})
        snapshot['excluded_rows_count'] = 1
        snapshot['excluded_rows_digest'] = digest([excluded_row])
        # Full per-row review of every visible row, validly signed, still holds.
        self.assertEqual(len(packet['payload']['dispositions']), len(snapshot['rows']))
        with self.assertRaisesRegex(PhotoCertificateHold,
                                    'certificate_exclusions_unaccounted'):
            verify(packet, key, snapshot)
        # Even claiming zero while the digest proves a non-empty exclusion set
        # (inconsistent SQL evidence) fails closed.
        packet, key, snapshot, _ = fixtures()
        snapshot['excluded_rows_digest'] = digest([excluded_row])
        with self.assertRaisesRegex(PhotoCertificateHold,
                                    'certificate_exclusions_unaccounted'):
            verify(packet, key, snapshot)

    def test_zero_exclusion_evidence_still_verifies_positive_path(self):
        packet, key, snapshot, _ = fixtures()
        verified = verify(packet, key, snapshot, packet['payload']['candidate'])
        self.assertTrue(verified.receipt_ref.startswith('photo-audit:sha256:'))


class ReviewEvidenceReconciliationTests(unittest.TestCase):
    """Pure pre-signing reconciler: exact corpus/candidate binding or hold."""

    def _evidence(self, packet, key, snapshot):
        candidate = packet['payload']['candidate']
        visuals = []
        for row in snapshot['rows']:
            visuals.append({'history_key': row['history_key'],
                'media_kind': 'still_photo', 'disposition': 'reviewed_nonmatch',
                'review_evidence_ref': 'SYNTHETIC independent inspection',
                'unresolved_reasons': None,
                'delivered': {'url': 'https://media.example.test/sent.png',
                              'sha256': row['visual_sha256'],
                              'length': 5, 'reachable': True}})
        return {'schema_version': CORPUS_SCHEMA_VERSION,
                'packet_kind': 'historical_photo_review_evidence',
                'decision': 'review_packet_only', 'clearance': False,
                'no_automatic_positive_decision': True,
                'packet_status': 'complete_for_independent_review',
                'candidate': {'calendar_row_id': candidate['calendar_row_id'],
                    'tenant_id': candidate['tenant_id'], 'group_key': candidate['group_key'],
                    'post_date': candidate['post_date'],
                    'source_asset_id': candidate['source_asset_id'],
                    'source_version_id': 'synthetic-version',
                    'source_receipt_ref': candidate['source_receipt_ref'],
                    'source': {'url': candidate['source_url'],
                               'sha256': candidate['source_sha256'],
                               'length': candidate['source_length']},
                    'delivered': {'url': candidate['image_url'],
                                  'sha256': candidate['image_sha256'],
                                  'length': candidate['image_length']},
                    'evidence_refs': ['SYNTHETIC cutover evidence'],
                    'hold_reasons': []},
                'coverage': {'total_visuals': len(visuals), 'unresolved_visuals': 0,
                             'byte_unreachable_visuals': 0, 'frames_held_visuals': 0},
                'visuals': visuals, 'corpus_digest': digest(visuals)}

    def test_exact_evidence_reconciles(self):
        packet, key, snapshot, _ = fixtures()
        result = reconcile_review_evidence(self._evidence(packet, key, snapshot),
                                           snapshot, packet['payload']['candidate'])
        self.assertEqual(result['status'], 'reconciled_for_independent_signing')
        self.assertEqual(result['visuals'], len(snapshot['rows']))

    def test_old_format_snapshot_or_positive_exclusions_hold_reconciler(self):
        packet, key, snapshot, _ = fixtures()
        evidence = self._evidence(packet, key, snapshot)
        old_format = {k: v for k, v in snapshot.items()
                      if not k.startswith('excluded_rows_')}
        with self.assertRaisesRegex(PhotoCertificateHold,
                                    'certificate_exclusions_unaccounted'):
            reconcile_review_evidence(evidence, old_format)
        excluded = dict(snapshot, excluded_rows_count=1)
        with self.assertRaisesRegex(PhotoCertificateHold,
                                    'certificate_exclusions_unaccounted'):
            reconcile_review_evidence(evidence, excluded)

    def test_unresolved_or_mismatching_visual_holds(self):
        packet, key, snapshot, _ = fixtures()
        for mutate in (
                lambda e: e['visuals'][0].update(disposition='unresolved'),
                lambda e: e['visuals'][0].update(unresolved_reasons=['object_bytes_unreachable']),
                lambda e: e['visuals'][0].update(media_kind='video'),
                lambda e: e['visuals'][0]['delivered'].update(reachable=False),
                lambda e: e['visuals'][0]['delivered'].update(sha256=digest('other bytes')),
                lambda e: e.update(packet_status='hold'),
                lambda e: e.update(clearance=True),
                lambda e: e['coverage'].update(unresolved_visuals=1),
                lambda e: e['visuals'].pop(),
                lambda e: e['visuals'][0].update(history_key='renamed-key')):
            packet, key, snapshot, _ = fixtures()
            evidence = self._evidence(packet, key, snapshot)
            mutate(evidence)
            with self.assertRaisesRegex(PhotoCertificateHold,
                                        'review_evidence_reconciliation_failed'):
                reconcile_review_evidence(evidence, snapshot)

    def test_candidate_binding_holds_on_any_field_change(self):
        packet, key, snapshot, _ = fixtures()
        candidate = packet['payload']['candidate']
        for mutate in (
                lambda e: e['candidate'].update(tenant_id='other-gym'),
                lambda e: e['candidate'].update(post_date='2026-10-11'),
                lambda e: e['candidate']['source'].update(sha256=digest('other source')),
                lambda e: e['candidate'].update(source_receipt_ref='source-receipt:sha256:' + 'b' * 64)):
            packet, key, snapshot, _ = fixtures()
            evidence = self._evidence(packet, key, snapshot)
            mutate(evidence)
            with self.assertRaisesRegex(PhotoCertificateHold,
                                        'review_evidence_reconciliation_failed'):
                reconcile_review_evidence(evidence, snapshot, candidate)


class _Info:
    def __init__(self, status):
        self.transaction_status = status


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        self.conn.queries.append(query)
        if 'current_user' in query:
            self._result = (self.conn.owner,)
        elif 'fixer_owner_photo_pending' in query:
            self._result = (list(self.conn.pending),)
        elif 'fixer_owner_photo_reserve' in query:
            self._result = (self.conn.reserve_results.pop(0),)
        elif 'fixer_forward_media_source_snapshot' in query:
            self._result = ({'calendar': {}},)  # deliberately incomplete snapshot
        else:
            self._result = (None,)

    def fetchone(self):
        return self._result


class _Conn:
    def __init__(self, owner, pending, reserve_results):
        from psycopg.pq import TransactionStatus
        self.info = _Info(TransactionStatus.IDLE)
        self.owner = owner
        self.pending = pending
        self.reserve_results = reserve_results
        self.queries = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _persistence(conn):
    persistence = object.__new__(ForwardMediaOwnerPersistence)
    persistence._conn = conn
    persistence._expected_owner = conn.owner
    return persistence


class ConcurrentReservationTests(unittest.TestCase):
    """Per-audit reserve RPC is the single-admit gate; losers never duplicate."""

    def test_lost_reservation_skips_audit_without_staging(self):
        candidate = {'audit_id': str(uuid.uuid4()), 'calendar_row_id': str(uuid.uuid4()),
                     'revision': '0' * 32, 'recipe': {}}
        conn = _Conn('synthetic-owner', [candidate], [False])
        result = run_photo_pass(persistence=_persistence(conn), reader=None,
                                drive_reader=None, tenants=('gym',), limit=1)
        self.assertEqual(result, {'status': 'complete', 'rows': []})
        self.assertFalse(any('fixer_prepare_owner_photo' in q for q in conn.queries))

    def test_won_reservation_with_unverifiable_evidence_holds_not_clears(self):
        candidate = {'audit_id': str(uuid.uuid4()), 'calendar_row_id': str(uuid.uuid4()),
                     'revision': '0' * 32, 'recipe': {}}
        conn = _Conn('synthetic-owner', [candidate], [True])
        result = run_photo_pass(persistence=_persistence(conn), reader=None,
                                drive_reader=None, tenants=('gym',), limit=1)
        self.assertEqual(result['status'], 'partial_hold')
        self.assertEqual(result['rows'][0]['status'], 'hold')
        self.assertEqual(result['rows'][0]['reason'], 'certified_owner_verification_failed')
        self.assertFalse(any('fixer_prepare_owner_photo' in q for q in conn.queries))


if __name__ == '__main__':
    unittest.main()
