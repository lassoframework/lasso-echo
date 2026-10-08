"""Offline tests for agent/forward_media_owner_packet.py (one-packet CLI).

Uses an injectable reader and persistence factory; no database, no network,
no production access.
"""
import io
import json

import pytest

from agent import forward_media_owner_packet as cli
from agent.forward_media_prepare import fingerprint_bytes

SRC_URL = 'https://cdn.example.com/original.jpg'
IMG_URL = 'https://cdn.example.com/rehost/original.jpg'
THUMB_URL = 'https://cdn.example.com/thumb.jpg'
SRC_BYTES = b'owner packet original bytes'
THUMB_BYTES = b'owner packet thumbnail bytes'


class FakeReader:
    def __init__(self, store):
        self.store = store

    def read(self, url):
        if url not in self.store:
            raise KeyError('no bytes')
        return self.store[url]


class FakePersistence:
    def __init__(self):
        self.persisted = None
        self.closed = False

    def persist(self, original, clearance, manifest):
        self.persisted = (original, clearance, manifest)
        return {'registry': original.row(), 'clearance': clearance.row(),
                'manifest': manifest.row(), 'replayed': False}

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def owner_env(monkeypatch):
    import os
    from agent.forward_media_owner import forbidden_credential_names
    for name in forbidden_credential_names(os.environ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_DSN', 'postgresql://owner@localhost/db')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_ROLE', 'forward_media_owner_20261006')


def packet(**overrides):
    base = {
        'schema_version': 1,
        'tenant_id': 'tenant-a',
        'source_asset_id': 'asset-1',
        'source_url': SRC_URL,
        'registry_evidence_ref': 'registry-evidence-1',
        'render_evidence_ref': 'render-evidence-1',
        'decision': 'hold_uncertain',
        'history_evidence_ref': 'fleet-history-audit-1',
        'operation': 'same_object',
    }
    base.update(overrides)
    return base


def write_packet(tmp_path, data):
    path = tmp_path / 'packet.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return str(path)


def store():
    return {SRC_URL: SRC_BYTES, THUMB_URL: THUMB_BYTES}


def run_cli(tmp_path, data, apply=False, reader=None, persistence=None):
    path = write_packet(tmp_path, data)
    out = io.StringIO()
    argv = ['--packet', path] + (['--apply'] if apply else [])
    kwargs = {'reader_factory': lambda: reader or FakeReader(store()),
              'out': out}
    if persistence is not None:
        kwargs['persistence_factory'] = lambda: persistence
    code, result = cli.run(argv, **kwargs)
    return code, result, out.getvalue().strip()


def test_dry_run_validates_without_db_write(tmp_path):
    persistence = FakePersistence()
    code, result, line = run_cli(tmp_path, packet(), apply=False,
                                 persistence=persistence)
    assert code == 0
    assert result == {'ok': True, 'applied': False, 'decision': 'hold_uncertain',
                      'operation': 'same_object'}
    assert json.loads(line) == result
    assert persistence.persisted is None  # dry-run: no DB write
    assert not persistence.closed  # factory not even invoked without --apply


def test_apply_persists_exact_tuple_with_no_caller_hashes(tmp_path):
    persistence = FakePersistence()
    data = packet(decision='cleared_unused',
                  production_evidence_ref='fresh-production-receipt-1')
    code, result, _ = run_cli(tmp_path, data, apply=True, persistence=persistence)
    assert code == 0 and result['applied'] is True
    assert persistence.closed  # connection closed on success path
    original, clearance, manifest = persistence.persisted
    src_fp, src_len = fingerprint_bytes(SRC_BYTES)
    assert (original.tenant_id, original.source_asset_id, original.source_url) == \
        ('tenant-a', 'asset-1', SRC_URL)
    assert (original.source_fingerprint, original.source_length) == (src_fp, src_len)
    assert original.registry_evidence_ref == 'registry-evidence-1'
    assert clearance.decision == 'cleared_unused'
    assert clearance.history_evidence_ref == 'fleet-history-audit-1'
    assert clearance.source_fingerprint == src_fp
    assert manifest.operation == 'same_object'
    assert manifest.image_url == SRC_URL
    assert manifest.image_fingerprint == src_fp and manifest.image_length == src_len
    # same_object carries no thumbnail at all.
    assert manifest.thumbnail_url is None
    assert manifest.thumbnail_fingerprint is None
    assert manifest.thumbnail_length is None
    assert manifest.manifest_digest.startswith('sha256:')
    # The packet carried no hash/length fields at all; everything above was
    # computed from the hosted bytes.
    assert not any(k in data for k in cli._FORBIDDEN_PACKET_FIELDS)


def test_rehost_thumbnail_must_be_exact_source_bytes(tmp_path):
    # A rehost thumbnail with the exact source bytes is accepted and lands in
    # the manifest with the source fingerprint.
    persistence = FakePersistence()
    reader = FakeReader({SRC_URL: SRC_BYTES, IMG_URL: SRC_BYTES,
                         THUMB_URL: SRC_BYTES})
    data = packet(operation='rehost', image_url=IMG_URL, thumbnail_url=THUMB_URL)
    code, result, _ = run_cli(tmp_path, data, apply=True, reader=reader,
                              persistence=persistence)
    assert code == 0 and result['applied'] is True
    _, _, manifest = persistence.persisted
    src_fp, src_len = fingerprint_bytes(SRC_BYTES)
    assert manifest.thumbnail_url == THUMB_URL
    assert manifest.thumbnail_fingerprint == src_fp
    assert manifest.thumbnail_length == src_len


def test_same_object_thumbnail_refused(tmp_path):
    # same_object carries no thumbnail: any thumbnail would be classified as
    # render/rehost by the attester and mismatch the manifest operation.
    for thumb_bytes in (SRC_BYTES, THUMB_BYTES):
        reader = FakeReader({SRC_URL: SRC_BYTES, THUMB_URL: thumb_bytes})
        data = packet(thumbnail_url=THUMB_URL)
        code, result, _ = run_cli(tmp_path, data, reader=reader)
        assert code == 2 and result['reason'] == 'preparation_invalid'


def test_rehost_transformed_thumbnail_refused(tmp_path):
    # Rehost thumbnail must be exact source bytes; a transformed thumbnail
    # would be classified as a render by the attester.
    reader = FakeReader({SRC_URL: SRC_BYTES, IMG_URL: SRC_BYTES,
                         THUMB_URL: THUMB_BYTES})
    data = packet(operation='rehost', image_url=IMG_URL, thumbnail_url=THUMB_URL)
    code, result, _ = run_cli(tmp_path, data, reader=reader)
    assert code == 2 and result['reason'] == 'preparation_invalid'


def test_reader_factory_error_maps_to_static_reason(tmp_path):
    def bad_factory():
        raise RuntimeError('connect failed postgresql://owner@localhost/db')

    path = write_packet(tmp_path, packet())
    out = io.StringIO()
    code, result = cli.run(['--packet', path],
                           reader_factory=bad_factory, out=out)
    assert code == 2 and result['reason'] == 'source_read_failed'
    assert 'postgresql' not in out.getvalue()


def test_persistence_factory_error_maps_to_static_reason(tmp_path):
    def bad_factory():
        raise RuntimeError('connect failed postgresql://owner@localhost/db')

    path = write_packet(tmp_path, packet())
    out = io.StringIO()
    code, result = cli.run(['--packet', path, '--apply'],
                           reader_factory=lambda: FakeReader(store()),
                           persistence_factory=bad_factory, out=out)
    assert code == 2 and result['reason'] == 'persistence_failed'
    assert 'postgresql' not in out.getvalue()


def test_factory_uncertain_commit_semantics_preserved(tmp_path):
    from agent.forward_media_owner import UncertainCommitError

    def uncertain_factory():
        raise UncertainCommitError('commit outcome unknown')

    path = write_packet(tmp_path, packet())
    out = io.StringIO()
    code, result = cli.run(['--packet', path, '--apply'],
                           reader_factory=lambda: FakeReader(store()),
                           persistence_factory=uncertain_factory, out=out)
    assert code == 2 and result['reason'] == 'uncertain_commit'


def test_close_failure_is_suppressed(tmp_path):
    class BadClosePersistence(FakePersistence):
        def close(self):
            raise RuntimeError('close exploded postgresql://owner@localhost/db')

    persistence = BadClosePersistence()
    code, result, line = run_cli(tmp_path, packet(), apply=True,
                                 persistence=persistence)
    assert code == 0 and result['applied'] is True
    assert 'postgresql' not in line


def test_apply_exact_byte_rehost(tmp_path):
    persistence = FakePersistence()
    reader = FakeReader({SRC_URL: SRC_BYTES, IMG_URL: SRC_BYTES})
    data = packet(operation='rehost', image_url=IMG_URL)
    code, result, _ = run_cli(tmp_path, data, apply=True, reader=reader,
                              persistence=persistence)
    assert code == 0 and result['applied'] is True
    original, _, manifest = persistence.persisted
    assert manifest.operation == 'rehost'
    assert manifest.image_url == IMG_URL
    assert manifest.image_fingerprint == original.source_fingerprint
    assert manifest.image_length == original.source_length


def test_rehost_requires_a_distinct_hosted_url(tmp_path):
    code, result, _ = run_cli(tmp_path, packet(operation='rehost', image_url=SRC_URL))
    assert code == 2 and result['reason'] == 'preparation_invalid'


def test_caller_supplied_hashes_rejected(tmp_path):
    for field in ('source_fingerprint', 'source_length', 'image_fingerprint',
                  'manifest_digest', 'used_count'):
        code, result, _ = run_cli(tmp_path, packet(**{field: 'md5:' + '0' * 32}))
        assert code == 2 and result['reason'] == 'field_forbidden'


def test_fresh_receipt_required_for_cleared_unused(tmp_path):
    for missing in ({}, {'production_evidence_ref': 'receipt-1',
                         'history_evidence_ref': ''},
                    {'history_evidence_ref': 'audit-1'}):
        data = packet(decision='cleared_unused', **missing)
        code, result, _ = run_cli(tmp_path, data)
        assert code == 2 and result['reason'] == 'fresh_receipts_required'


def test_old_or_unknown_history_cannot_auto_clear(tmp_path):
    # No used_count/URL inference path exists: hold decisions are fine, and
    # cleared_unused without BOTH explicit refs fails closed.
    for decision in ('hold_uncertain', 'hold_used'):
        code, result, _ = run_cli(tmp_path, packet(decision=decision))
        assert code == 0 and result['decision'] == decision
    # used_count alone can never upgrade to cleared (field is rejected outright).
    code, result, _ = run_cli(tmp_path, packet(decision='cleared_unused',
                                               used_count=0,
                                               production_evidence_ref='r',
                                               history_evidence_ref='h'))
    assert code == 2 and result['reason'] == 'field_forbidden'


def test_transformed_renders_held(tmp_path):
    for op in ('render', 'reburn'):
        code, result, _ = run_cli(tmp_path, packet(operation=op))
        assert code == 2 and result['reason'] == 'operation_held'


def test_bad_source_url_bytes(tmp_path):
    reader = FakeReader({})  # hosted object missing
    code, result, _ = run_cli(tmp_path, packet(), reader=reader)
    assert code == 2 and result['reason'] == 'source_read_failed'
    reader = FakeReader({'not-a-url': SRC_BYTES})  # bytes exist but URL invalid
    code, result, _ = run_cli(tmp_path, packet(source_url='not-a-url'), reader=reader)
    assert code == 2 and result['reason'] == 'preparation_invalid'


def test_rehost_bytes_mismatch_fails_closed(tmp_path):
    reader = FakeReader({SRC_URL: SRC_BYTES, IMG_URL: b'different bytes'})
    data = packet(operation='rehost', image_url=IMG_URL)
    code, result, _ = run_cli(tmp_path, data, reader=reader)
    assert code == 2 and result['reason'] == 'rehost_bytes_mismatch'


def test_rehost_missing_image_bytes(tmp_path):
    reader = FakeReader({SRC_URL: SRC_BYTES})
    data = packet(operation='rehost', image_url=IMG_URL)
    code, result, _ = run_cli(tmp_path, data, reader=reader)
    assert code == 2 and result['reason'] == 'image_read_failed'


def test_forbidden_publisher_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'redacted')
    persistence = FakePersistence()
    code, result, _ = run_cli(tmp_path, packet(), apply=True,
                              persistence=persistence)
    assert code == 2 and result['reason'] == 'env_guard'
    assert persistence.persisted is None
    assert not persistence.closed


def test_missing_owner_dsn_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv('FORWARD_MEDIA_OWNER_DSN')
    code, result, _ = run_cli(tmp_path, packet())
    assert code == 2 and result['reason'] == 'env_guard'


def test_connection_closed_on_persist_failure(tmp_path):
    class FailingPersistence(FakePersistence):
        def persist(self, *args):
            raise cli_owner_error()

    from agent.forward_media_owner import OwnerPersistenceError

    def cli_owner_error():
        return OwnerPersistenceError('generic failure')

    persistence = FailingPersistence()
    code, result, _ = run_cli(tmp_path, packet(), apply=True,
                              persistence=persistence)
    assert code == 2 and result['reason'] == 'persistence_failed'
    assert persistence.closed  # closed on failure path too


def test_generic_persist_error_and_malformed_result_are_static_holds(tmp_path):
    class BrokenPersistence(FakePersistence):
        def persist(self, *args):
            raise RuntimeError('postgresql://secret@db/internal')

    broken = BrokenPersistence()
    code, result, line = run_cli(tmp_path, packet(), apply=True, persistence=broken)
    assert code == 2 and result['reason'] == 'persistence_failed'
    assert 'secret' not in line and broken.closed

    class MalformedPersistence(FakePersistence):
        def persist(self, *args):
            return None

    malformed = MalformedPersistence()
    code, result, _ = run_cli(tmp_path, packet(), apply=True, persistence=malformed)
    assert code == 2 and result['reason'] == 'persistence_failed'
    assert malformed.closed


def test_output_never_contains_urls_dsn_or_packet_data(tmp_path):
    persistence = FakePersistence()
    reader = FakeReader({})  # forces source_read_failed after full packet parse
    code, result, line = run_cli(tmp_path, packet(), apply=True, reader=reader,
                                 persistence=persistence)
    assert code == 2
    for secret in (SRC_URL, IMG_URL, THUMB_URL, 'postgresql', 'tenant-a',
                   'asset-1', 'receipt', 'audit'):
        assert secret not in line


@pytest.fixture(autouse=True)
def isolated_process_environment(monkeypatch):
    # Pytest injects PYTEST_CURRENT_TEST after fixture setup. This test-only
    # process view excludes that harness marker; production accepts no such name.
    import os
    class ProcessEnvironment:
        @property
        def environ(self):
            return {k: v for k, v in os.environ.items() if k != 'PYTEST_CURRENT_TEST'}
        getenv = staticmethod(os.getenv)
    from agent import forward_media_owner
    monkeypatch.setattr(forward_media_owner, 'os', ProcessEnvironment())
