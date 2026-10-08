"""Offline adapter contract tests; no credentials, Postgres or feature flags."""
import unittest
from types import SimpleNamespace

from agent.generated_authority import (
    AuthorityHold, GeneratedAuthority, source_pending, source_approval,
    palette_verified, revoke_source, revoke_palette, sha256,
)


class Connection:
    def __init__(self):
        self.info = SimpleNamespace(transaction_status=0)
        self.autocommit = False
        self.calls = []
        self.result = 1
        self.fail_execute = self.fail_commit = False
        self.commits = self.rollbacks = 0

    def execute(self, sql, args):
        self.calls.append((sql, args))
        if self.fail_execute:
            raise OSError('private transport diagnostic')
        return SimpleNamespace(fetchone=lambda: (self.result,))

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise OSError('unknown delivery')

    def rollback(self):
        self.rollbacks += 1


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = Connection()
        self.authority = GeneratedAuthority(self.conn, tenant_id='gym-a',
            tenant_directory={'gym-a': 'gym-a', 'gym-a_ig': 'gym-a', 'gym-b': 'gym-b'},
            authorized_tenants={'gym-a'})
        self.source = source_pending('intake:r2:source-1', category='educational',
            exact_text='Exact fact', citation='Original form', origin_ref='r2:intake/gym-a/form',
            intake_revision='object-version-1', original_intake=b'{ "facts": "Exact fact" }')

    def test_original_bytes_and_pending_explicit_approval(self):
        self.assertNotIn('status', self.source)
        self.assertEqual(self.source['content_sha256'], sha256(b'Exact fact'))
        self.assertNotEqual(sha256(b'{ "colors": [] }'), sha256(b'{"colors":[]}'))
        approval = source_approval(self.source['source_id'], revision=1,
            content_sha256=self.source['content_sha256'], intake_sha256=self.source['intake_sha256'],
            actor='human:coach', receipt='approval:event-1')
        self.assertEqual(approval['approval_mode'], 'explicit')
        palette = palette_verified('brand', colors=['#123456'], origin_ref='r2:palette',
            intake_revision='intake-1', original_intake=b'original intake', file_revision='file-1',
            original_file=b'{ "colors": ["#123456"] }', actor='human:coach', receipt='verify:event-1')
        self.assertEqual(palette['file_sha256'], sha256(b'{ "colors": ["#123456"] }'))
        self.assertEqual(palette['approval_mode'], 'explicit')
        self.assertEqual(revoke_source('src')['op'], 'source_tombstone')
        self.assertEqual(revoke_palette('brand')['op'], 'palette_revoke')
        for bad in (True, 0, None, '1'):
            with self.assertRaises(AuthorityHold):
                source_approval('src', revision=bad, content_sha256='x', intake_sha256='x', actor='a', receipt='r')
        for bad in (b'', None, 'not bytes'):
            with self.assertRaises(AuthorityHold):
                sha256(bad)

    def test_exact_alias_principal_and_idle_connection(self):
        for alias in ('gym-b', 'gym_a', 'gym-a_fb', ''):
            with self.assertRaisesRegex(AuthorityHold, 'alias_unverified'):
                self.authority.snapshot(alias)
        self.assertFalse(self.conn.calls)
        with self.assertRaisesRegex(AuthorityHold, 'not_authorized'):
            GeneratedAuthority(self.conn, tenant_id='gym-a', tenant_directory={'gym-a': 'gym-a'}, authorized_tenants={'gym-b'})
        with self.assertRaisesRegex(AuthorityHold, 'not_authorized'):
            GeneratedAuthority(self.conn, tenant_id='gym-a', tenant_directory={'gym-a_ig': 'gym-a'}, authorized_tenants={'gym-a'})
        self.conn.info.transaction_status = 2
        with self.assertRaisesRegex(AuthorityHold, 'connection_busy'):
            self.authority.write('gym-a', expected_epoch=0, operations=[self.source])
        self.assertEqual(self.conn.commits, 0)
        self.conn.info.transaction_status = 0
        self.conn.autocommit = True
        with self.assertRaisesRegex(AuthorityHold, 'connection_busy'):
            self.authority.write('gym-a', expected_epoch=0, operations=[self.source])

    def test_success_is_only_after_commit(self):
        self.assertEqual(self.authority.write('gym-a_ig', expected_epoch=0, operations=[self.source]), 1)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.calls[0][1][:2], ('gym-a', 0))

    def test_failed_transport_no_retry_or_authority(self):
        self.conn.fail_execute = True
        with self.assertRaisesRegex(AuthorityHold, '^generated_authority_write_rejected$'):
            self.authority.write('gym-a', expected_epoch=0, operations=[self.source])
        self.assertEqual(len(self.conn.calls), 1)
        self.assertEqual(self.conn.commits, 0)
        self.assertEqual(self.conn.rollbacks, 1)

    def test_unknown_commit_requires_reconciliation_no_retry(self):
        self.conn.fail_commit = True
        with self.assertRaisesRegex(AuthorityHold, '^generated_authority_write_uncertain$'):
            self.authority.write('gym-a', expected_epoch=0, operations=[self.source])
        self.assertEqual(len(self.conn.calls), 1)
        self.assertEqual(self.conn.commits, 1)

    def test_bad_epoch_readback_rolls_back(self):
        for result in (True, None, 2):
            self.conn.result = result
            with self.assertRaises(AuthorityHold):
                self.authority.write('gym-a', expected_epoch=0, operations=[self.source])
        self.assertEqual(self.conn.commits, 0)

    def test_empty_canonical_snapshot_no_local_fallback(self):
        self.conn.result = dict(tenant_id='gym-a', epoch=None, sources=[], palettes=[])
        self.assertEqual(self.authority.snapshot('gym-a')['sources'], [])
        self.assertEqual(self.conn.rollbacks, 1)
        for result in (None, {}, dict(tenant_id='gym-b', epoch=1, sources=[], palettes=[]),
                       dict(tenant_id='gym-a', epoch=1, sources=[{'tenant_id': 'gym-b'}], palettes=[]),
                       dict(tenant_id='gym-a', epoch=None, sources=[{'tenant_id': 'gym-a'}], palettes=[])):
            self.conn.result = result
            with self.assertRaises(AuthorityHold):
                self.authority.snapshot('gym-a')

    def test_current_reader_requires_canonical_approval_and_revocation(self):
        source = {**self.source, 'tenant_id': 'gym-a', 'status': 'approved', 'revision': 1,
                  'approval_revision': 1, 'approval_mode': 'explicit', 'approval_actor': 'human:coach',
                  'approval_evidence': 'approval:event-1', 'approved_by': 'service_role'}
        palette = palette_verified('brand', colors=['#123456'], origin_ref='r2:palette',
            intake_revision='intake-1', original_intake=b'original intake', file_revision='file-1',
            original_file=b'original palette', actor='human:coach', receipt='verify:event-1')
        palette.update(tenant_id='gym-a', status='active', revision=1)
        self.conn.result = dict(tenant_id='gym-a', epoch=1, sources=[source], palettes=[palette])
        def current():
            return self.authority.current('gym-a', expected_epoch=1, source_id=source['source_id'],
                                          source_revision=1, palette_key='brand', palette_revision=1)
        self.assertEqual(current()['source']['exact_text'], 'Exact fact')
        for field, bad in [('status', 'pending'), ('status', 'revoked'), ('approval_revision', 2),
                           ('approval_mode', 'auto'), ('content_sha256', sha256(b'other')),
                           ('approval_evidence', '')]:
            original = source[field]
            source[field] = bad
            with self.assertRaises(AuthorityHold):
                current()
            source[field] = original
        palette['status'] = 'revoked'
        with self.assertRaises(AuthorityHold):
            current()
        palette['status'] = 'active'
        self.conn.result['epoch'] = 2
        with self.assertRaisesRegex(AuthorityHold, 'epoch_changed'):
            current()


if __name__ == '__main__':
    unittest.main()
