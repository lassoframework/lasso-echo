import copy
import hashlib
import unittest

from agent.forward_media_source_verifier import verify_source, SourceVerificationHold, _BoundedBuffer
from agent.forward_media_prepare import MAX_OBJECT_LENGTH

FILE = 'FileOriginal123456789'
FOLDER = 'FolderOriginal1234567'
DATA = b'actual-original-bytes'
URL = 'https://media.example.test/original?sig=exact%2Bbytes&v=2'


def snapshot():
    return {'calendar': {'id': 'row', 'gym_id': 'gym', 'source_media_asset_id': FILE,
                         'source_media_url': URL},
            'asset': {'id': FILE, 'gym_id': 'gym', 'source_id': 'source',
                      'content_hash': 'untrusted-metadata', 'used_count': 0,
                      'rendition_url': 'https://media.example.test/rendition'},
            'source': {'id': 'source', 'gym_id': 'gym', 'kind': 'gym_drive',
                       'active': True, 'folder_id': FOLDER},
            'revision': '0'*32, 'binding_revision': '1'*32}


class Drive:
    def __init__(self):
        self.meta = {'id': FILE, 'mimeType': 'image/png', 'trashed': False, 'version': '3',
                     'parents': [FOLDER], 'size': str(len(DATA)),
                     'md5Checksum': hashlib.md5(DATA).hexdigest()}
        self.data = DATA
        self.changed = False
        self.reads = 0

    def metadata(self, file_id):
        if file_id == FOLDER:
            return {'id': FOLDER, 'mimeType': 'application/vnd.google-apps.folder',
                    'trashed': False, 'version': '2'}
        self.reads += 1
        value = copy.deepcopy(self.meta)
        if self.changed and self.reads > 1:
            value['version'] = '4'
        return value

    def original_bytes(self, file_id):
        assert file_id == FILE
        return self.data


class Hosted:
    def __init__(self, data=DATA):
        self.data, self.urls = data, []

    def read(self, url):
        self.urls.append(url)
        return self.data


class SourceTests(unittest.TestCase):
    def test_real_bytes_exact_persisted_url_and_stable_receipt(self):
        hosted = Hosted()
        result = verify_source(snapshot(), Drive(), hosted)
        self.assertEqual(hosted.urls, [URL])
        self.assertEqual(result.source_bytes, DATA)
        self.assertEqual(result.original.source_url, URL)
        self.assertEqual(result.original.registry_evidence_ref, result.receipt_ref)
        self.assertEqual(result.evidence['source_sha256'], 'sha256:'+hashlib.sha256(DATA).hexdigest())
        self.assertEqual(result.receipt_ref, verify_source(snapshot(), Drive(), Hosted()).receipt_ref)
        self.assertNotIn('decision', result.evidence)

    def test_source_tenant_inactive_and_missing_source_fail_before_reads(self):
        for key, value in [('gym_id','other'), ('active',False), ('kind','podcast_library')]:
            snap, drive = snapshot(), Drive()
            snap['source'][key] = value
            with self.assertRaisesRegex(SourceVerificationHold, 'source_binding_invalid'):
                verify_source(snap, drive, Hosted())
            self.assertEqual(drive.reads, 0)

    def test_current_metadata_is_insufficient_without_original_bytes(self):
        drive = Drive()
        drive.data = b'wrong bytes'
        with self.assertRaisesRegex(SourceVerificationHold, 'drive_original_bytes_mismatch'):
            verify_source(snapshot(), drive, Hosted())

    def test_derivative_original_url_is_held(self):
        with self.assertRaisesRegex(SourceVerificationHold, 'hosted_source_differs_from_drive_original'):
            verify_source(snapshot(), Drive(), Hosted(b'rendered derivative'))

    def test_drive_mutation_and_ambiguous_membership_hold(self):
        drive = Drive()
        drive.changed = True
        with self.assertRaisesRegex(SourceVerificationHold, 'drive_original_changed_during_read'):
            verify_source(snapshot(), drive, Hosted())
        drive = Drive()
        drive.meta['parents'] = [FOLDER, 'OtherFolder1234567890']
        with self.assertRaisesRegex(SourceVerificationHold, 'drive_folder_membership_unknown'):
            verify_source(snapshot(), drive, Hosted())

    def test_bound_stops_before_download(self):
        drive = Drive()
        drive.meta['size'] = str(MAX_OBJECT_LENGTH+1)
        with self.assertRaisesRegex(SourceVerificationHold, 'source_object_exceeds_bound'):
            verify_source(snapshot(), drive, Hosted())
        buf = _BoundedBuffer()
        buf.seek(MAX_OBJECT_LENGTH)
        with self.assertRaisesRegex(SourceVerificationHold, 'source_object_exceeds_bound'):
            buf.write(b'x')

    def test_errors_do_not_leak_remote_details(self):
        class Broken(Drive):
            def metadata(self, file_id):
                raise OSError('secret credential and signed URL')
        with self.assertRaisesRegex(SourceVerificationHold, '^source_verification_unavailable$'):
            verify_source(snapshot(), Broken(), Hosted())


if __name__ == '__main__':
    unittest.main()
