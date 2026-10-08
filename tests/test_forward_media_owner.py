"""Fake-DB tests for agent/forward_media_owner.py (owner-only persistence adapter)."""
import json
import pytest

from agent.forward_media_prepare import (
    PreparationError,
    build_render_manifest,
    prepare_generated_original,
)
from agent.forward_media_owner import (
    CURRENT_USER,
    EnvironmentGuardError,
    ForwardMediaOwnerPersistence,
    ObjectReader,
    OwnerPersistenceError,
    UncertainCommitError,
    check_environment,
)

OWNER = 'forward_media_owner_20261006'
TABLE_SUFFIX = {'registry': 'original_registry', 'clearance': 'history_clearance',
                'manifest': 'render_manifest'}
COLUMNS = {
    'registry': ['tenant_id', 'source_asset_id', 'source_url', 'source_fingerprint',
                 'source_length', 'registry_evidence_ref'],
    'clearance': ['tenant_id', 'source_asset_id', 'source_url', 'source_fingerprint',
                  'source_length', 'registry_evidence_ref', 'decision',
                  'history_evidence_ref'],
    'manifest': ['manifest_digest', 'tenant_id', 'source_asset_id', 'image_url',
                 'image_fingerprint', 'image_length', 'thumbnail_url',
                 'thumbnail_fingerprint', 'thumbnail_length', 'operation',
                 'render_recipe', 'render_evidence_ref'],
}
SRC_BYTES = b'owner-verified original bytes'
IMG_BYTES = b'rendered image bytes v1'
THUMB_BYTES = b'thumbnail bytes v1'
STORE = {'https://cdn.example.com/original.jpg': SRC_BYTES,
         'https://cdn.example.com/render.jpg': IMG_BYTES,
         'https://cdn.example.com/thumb.jpg': THUMB_BYTES}


class FakeReader(ObjectReader):
    """Trusted configured reader over an in-memory object store."""

    def __init__(self, store):
        self.store = store

    def read(self, url):
        if url not in self.store:
            raise KeyError(f'trusted reader has no bytes for {url}')
        return self.store[url]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None

    def execute(self, query, params=None):
        self.conn.executed.append((query, params))
        self.conn.last_query = query
        self.conn.last_result = self.conn._run(query, params or {})
        if query.strip().lower() == CURRENT_USER:
            self.description = [('current_user',)]
        else:
            table = self.conn._table(query)
            self.description = [(c,) for c in COLUMNS[table]] if table else None

    def fetchone(self):
        if not self.conn.last_result:
            return None
        if self.conn.last_query.strip().lower() == CURRENT_USER:
            return (self.conn.last_result[0]['current_user'],)
        return tuple(self.conn.last_result[0][c] for c in COLUMNS[self.conn._table(self.conn.last_query)])

    def fetchall(self):
        cols = COLUMNS[self.conn._table(self.conn.last_query)]
        return [tuple(row[c] for c in cols) for row in self.conn.last_result]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """In-memory stand-in matching the DB-API surface the adapter uses."""

    def __init__(self, current_user=OWNER, fail_commit=False):
        self.autocommit = False
        self.tables = {t: {} for t in TABLE_SUFFIX}
        self.current_user = current_user
        self.fail_commit = fail_commit
        self.executed = []
        self.last_query = ''
        self.last_result = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.fail_commit:
            raise RuntimeError('connection lost during COMMIT')
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def _table(self, query):
        q = query.lower()
        return next((t for t, sfx in TABLE_SUFFIX.items()
                     if f'fixer_forward_media_{sfx}_20261006' in q), None)

    def _run(self, query, params):
        q = ' '.join(query.lower().split())
        if q.startswith('set local '):
            return []
        if q.startswith('select current_user'):
            return [{'current_user': self.current_user}]
        table = self._table(q)
        assert table, q
        if q.startswith('select'):
            rows = list(self.tables[table].values())
            if table == 'manifest':
                rows = [r for r in rows if r['manifest_digest'] == params['manifest_digest']]
            else:
                rows = [r for r in rows if r['tenant_id'] == params['tenant_id']
                        and r['source_asset_id'] == params['source_asset_id']]
            return rows
        if q.startswith('insert'):
            key = params['manifest_digest'] if table == 'manifest' \
                else (params['tenant_id'], params['source_asset_id'])
            assert key not in self.tables[table], 'duplicate insert'
            record = dict(params)
            if table == 'manifest':
                record['render_recipe'] = json.loads(record['render_recipe'])
            self.tables[table][key] = record
            return []
        raise AssertionError(f'unexpected query {q}')

    def corrupt(self, table, field, value):
        for row in self.tables[table].values():
            row[field] = value


@pytest.fixture
def prepared():
    original, clearance = prepare_generated_original(
        tenant_id='gym-1', source_asset_id='asset-1',
        source_url='https://cdn.example.com/original.jpg', source_bytes=SRC_BYTES,
        generation_evidence_ref='gen-receipt:owner-produced-now',
        registry_evidence_ref='registry-evidence:owner-verified',
        history_evidence_ref='history-audit:independent-fleet-wide')
    manifest = build_render_manifest(
        original, image_url='https://cdn.example.com/render.jpg',
        image_bytes=IMG_BYTES, operation='render',
        thumbnail_url='https://cdn.example.com/thumb.jpg',
        thumbnail_bytes=THUMB_BYTES,
        render_recipe={'op': 'overlay'}, render_evidence_ref='render-evidence:owner')
    return original, clearance, manifest, FakeReader(STORE)


@pytest.fixture(autouse=True)
def owner_environment(monkeypatch):
    import os
    from agent.forward_media_owner import forbidden_credential_names
    for name in forbidden_credential_names(os.environ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_DSN', 'postgres://owner@localhost/fake')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_ROLE', OWNER)
    for name in ('SUPABASE_SERVICE_ROLE_KEY', 'META_PUBLISH_TOKEN'):
        monkeypatch.delenv(name, raising=False)


def adapter(conn=None, reader=None):
    conn = conn or FakeConnection()
    reader = reader or FakeReader(STORE)
    return ForwardMediaOwnerPersistence(conn, OWNER, reader), conn


# --- happy path --------------------------------------------------------------
def test_persists_all_three_rows_in_one_transaction(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    result = svc.persist(original, clearance, manifest)
    assert [q for q, _ in conn.executed[:3]] == [
        "set local lock_timeout = '5s'", "set local statement_timeout = '20s'", CURRENT_USER]
    assert conn.commits == 1 and conn.rollbacks == 0
    assert len(conn.tables['registry']) == len(conn.tables['clearance']) == 1
    assert len(conn.tables['manifest']) == 1
    assert not result['replayed']
    order = [q.split()[2].removeprefix('public.') for q, _ in conn.executed if q.startswith('insert')]
    assert order == ['fixer_forward_media_original_registry_20261006',
                     'fixer_forward_media_history_clearance_20261006',
                     'fixer_forward_media_render_manifest_20261006']


def test_owner_startup_deadlines_precede_all_commands(monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []
    conn = FakeConnection()
    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return conn
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(connect=connect))
    ForwardMediaOwnerPersistence.connect_from_environment(reader=FakeReader(STORE))
    assert calls[0][1] == {'autocommit': False,
                           'options': '-c lock_timeout=5000 -c statement_timeout=20000'}
    assert conn.executed == []


def test_idempotent_exact_replay(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    result = svc.persist(original, clearance, manifest)
    assert result['replayed'] is True
    assert len(conn.tables['registry']) == 1  # no duplicate rows
    assert conn.commits == 2


def test_idempotent_replay_fails_closed_on_persisted_diff(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    conn.corrupt('clearance', 'decision', 'hold_used')
    with pytest.raises(OwnerPersistenceError):
        svc.persist(original, clearance, manifest)
    assert conn.rollbacks == 1
    persisted = conn.tables['clearance'][(original.tenant_id, original.source_asset_id)]
    assert persisted['decision'] == 'hold_used'  # fake DB not mutated by adapter


def test_partial_existing_rows_fail_closed(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    del conn.tables['clearance'][(original.tenant_id, original.source_asset_id)]  # torn authority
    with pytest.raises(OwnerPersistenceError, match='single exact row'):
        svc.persist(original, clearance, manifest)
    assert conn.rollbacks == 1


# --- rollback / uncertain commit ---------------------------------------------
def test_rollback_on_insert_failure(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)

    def boom(query, params):
        if query.startswith('insert'):
            raise RuntimeError('constraint violation')
        return FakeConnection._run(conn, query, params)

    conn._run = boom
    with pytest.raises(RuntimeError):
        svc.persist(original, clearance, manifest)
    assert conn.rollbacks == 1 and conn.commits == 0


def test_uncertain_commit_fails_closed(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    conn.fail_commit = True
    with pytest.raises(UncertainCommitError):
        svc.persist(original, clearance, manifest)
    assert conn.rollbacks == 1


# --- environment / identity guards -------------------------------------------
def test_missing_owner_dsn_fails_without_fallback():
    with pytest.raises(EnvironmentGuardError, match='no generic'):
        check_environment({})


def test_service_role_credential_in_environment_fails():
    env = {'FORWARD_MEDIA_OWNER_DSN': 'postgres://owner@x/db',
           'SUPABASE_SERVICE_ROLE_KEY': 'secret'}
    with pytest.raises(EnvironmentGuardError, match='SERVICE_ROLE'):
        check_environment(env)


def test_publisher_token_in_environment_fails():
    env = {'FORWARD_MEDIA_OWNER_DSN': 'postgres://owner@x/db',
           'META_PUBLISH_TOKEN': 'secret'}
    with pytest.raises(EnvironmentGuardError, match='PUBLISH'):
        check_environment(env)


@pytest.mark.parametrize('credential', [
    'AGENT_SOCIALAPI_KEY', 'AGENT_SOCIALAPI_ENC_KEY',
    'AGENT_GBP_ACCESS_TOKEN', 'ZERNIO_API_KEY',
    'AGENT_WHATSAPP_TOKEN', 'AGENT_WHATSAPP_APP_SECRET',
    'AGENT_META_APP_SECRET', 'META_APP_SECRET', 'AGENT_SLACK_APP_TOKEN',
    'AGENT_SUPPORT_SLACK_BOT_TOKEN', 'AGENT_LASSO_IG_TOKEN',
    'AGENT_FUTURE_GYM_FB_TOKEN', 'UNLISTED_PROVIDER_API_KEY',
    'AWS_SECRET_ACCESS_KEY', 'AWS_ACCESS_KEY_ID', 'NEW_PROVIDER_PASSWORD',
    'AGENT_INTAKE_TOKEN_12', 'AGENT_INTAKE_TOKEN_PIERCE',
    'AGENT_INTAKE_TOKEN_FUTURE_GYM', 'agent_intake_token_mixed-tenant',
    'PGPASSWORD', 'MYSQLPASSWORD',
])
def test_provider_credential_in_owner_environment_fails(credential):
    env = {'FORWARD_MEDIA_OWNER_DSN': 'postgres://owner@x/db', credential: 'secret'}
    with pytest.raises(EnvironmentGuardError, match=credential) as exc:
        check_environment(env)
    assert 'secret' not in str(exc.value)


def test_clean_environment_passes():
    env = {'FORWARD_MEDIA_OWNER_DSN': 'postgres://owner@x/db', 'PATH': '/usr/bin',
           'FORWARD_MEDIA_OWNER_ROLE': OWNER, 'PGSSLMODE': 'verify-full',
           'SSL_CERT_FILE': '/etc/ssl/cert.pem'}
    check_environment(env)


def test_autocommit_connection_cannot_write(prepared):
    original, clearance, manifest, reader = prepared
    conn = FakeConnection()
    conn.autocommit = True
    svc, _ = adapter(conn=conn, reader=reader)
    with pytest.raises(OwnerPersistenceError, match='one transaction'):
        svc.persist(original, clearance, manifest)
    assert not conn.executed


def test_production_factory_rejects_missing_owner_role(monkeypatch):
    monkeypatch.delenv('FORWARD_MEDIA_OWNER_ROLE', raising=False)
    with pytest.raises(EnvironmentGuardError, match='dedicated owner role'):
        ForwardMediaOwnerPersistence.connect_from_environment(reader=FakeReader(STORE))


def test_wrong_current_user_fails_before_any_write(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(conn=FakeConnection(current_user='service_role'), reader=reader)
    with pytest.raises(OwnerPersistenceError, match='current_user'):
        svc.persist(original, clearance, manifest)
    assert not any(conn.tables.values())
    assert conn.commits == 0
    assert not any(q.startswith('insert') for q, _ in conn.executed)


def test_attester_role_also_fails(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(
        conn=FakeConnection(current_user='fixer_forward_media_attester_20261006'),
        reader=reader)
    with pytest.raises(OwnerPersistenceError):
        svc.persist(original, clearance, manifest)
    assert not any(conn.tables.values())


# --- trusted reader byte verification -----------------------------------------
def test_reader_byte_mismatch_fails_closed(prepared):
    original, clearance, manifest, _ = prepared
    tampered = FakeReader({**STORE, 'https://cdn.example.com/original.jpg': b'different'})
    svc, conn = adapter(reader=tampered)
    with pytest.raises(OwnerPersistenceError, match='immutable object bytes'):
        svc.persist(original, clearance, manifest)
    assert not any(conn.tables.values())


def test_reader_missing_object_fails_closed(prepared):
    original, clearance, manifest, _ = prepared
    svc, conn = adapter(reader=FakeReader({}))
    with pytest.raises(KeyError):
        svc.persist(original, clearance, manifest)
    assert not any(conn.tables.values())


def test_untrusted_reader_rejected(prepared):
    original, clearance, manifest, _ = prepared

    class NotTrusted:
        def read(self, url):
            return SRC_BYTES

    svc, conn = adapter(reader=NotTrusted())
    with pytest.raises(OwnerPersistenceError, match='ObjectReader'):
        svc.persist(original, clearance, manifest)


# --- tuple validation ---------------------------------------------------------
def test_mismatched_clearance_tuple_fails(prepared):
    original, clearance, manifest, reader = prepared
    forged = type(clearance)(**{**clearance.row(), 'source_fingerprint': 'md5:' + '0' * 32})
    svc, conn = adapter(reader=reader)
    with pytest.raises(OwnerPersistenceError, match='clearance source_fingerprint'):
        svc.persist(original, forged, manifest)
    assert not any(conn.tables.values())


def test_manifest_bound_to_different_original_fails(prepared):
    original, clearance, manifest, reader = prepared
    forged = type(manifest)(**{**manifest.row(), 'source_asset_id': 'asset-other'})
    svc, conn = adapter(reader=reader)
    with pytest.raises(OwnerPersistenceError, match='bound to the original'):
        svc.persist(original, clearance, forged)


def test_tampered_manifest_digest_fails(prepared):
    original, clearance, manifest, reader = prepared
    forged = type(manifest)(**{**manifest.row(),
                               'manifest_digest': 'sha256:' + 'f' * 64})
    svc, conn = adapter(reader=reader)
    with pytest.raises(OwnerPersistenceError, match='refusing mutable manifest'):
        svc.persist(original, clearance, forged)


def test_malformed_refs_fail(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    bad_url = type(original)(**{**original.row(), 'source_url': 'http://insecure/x.jpg'})
    with pytest.raises(OwnerPersistenceError, match='https'):
        svc.persist(bad_url, clearance, manifest)
    bad_fp = type(original)(**{**original.row(), 'source_fingerprint': 'md5:ZZZ'})
    with pytest.raises(OwnerPersistenceError, match='md5'):
        svc.persist(bad_fp, clearance, manifest)
    assert not any(conn.tables.values())


def test_invalid_operation_and_decision_fail(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    with pytest.raises(OwnerPersistenceError, match='controlled operation'):
        svc.persist(original, clearance,
                    type(manifest)(**{**manifest.row(), 'operation': 'generate'}))
    with pytest.raises(OwnerPersistenceError, match='controlled decision'):
        svc.persist(original,
                    type(clearance)(**{**clearance.row(), 'decision': 'auto_cleared'}),
                    manifest)


def test_transaction_staging_never_commits_or_rolls_back(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    assert svc.persist_in_transaction(original, clearance, manifest)['replayed'] is False
    assert conn.commits == conn.rollbacks == 0
    assert svc.persist_in_transaction(original, clearance, manifest)['replayed'] is True
    assert conn.commits == conn.rollbacks == 0


def test_transaction_staging_error_leaves_rollback_to_owner(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    def failed_insert(*args):
        raise RuntimeError('synthetic SQL failure')
    svc._insert = failed_insert
    with pytest.raises(RuntimeError):
        svc.persist_in_transaction(original, clearance, manifest)
    assert conn.commits == conn.rollbacks == 0


def second_manifest(original, reader):
    url = 'https://cdn.example.com/story.jpg'
    reader.store = {**reader.store, url: b'story crop bytes'}
    return build_render_manifest(
        original, image_url=url, image_bytes=reader.store[url], operation='render',
        render_recipe={'op': 'story_crop'}, render_evidence_ref='render:story')


def test_new_manifest_for_existing_exact_authority_and_replay(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    new = second_manifest(original, reader)
    before = len(conn.executed)
    assert svc.persist_in_transaction(original, clearance, new)['replayed'] is False
    inserts = [q for q, _ in conn.executed[before:] if q.startswith('insert')]
    assert len(inserts) == 1 and 'render_manifest' in inserts[0]
    assert len(conn.tables['registry']) == len(conn.tables['clearance']) == 1
    assert len(conn.tables['manifest']) == 2
    assert svc.persist_in_transaction(original, clearance, new)['replayed'] is True
    assert conn.commits == 1 and conn.rollbacks == 0


@pytest.mark.parametrize('table,field,value', [
    ('registry', 'source_url', 'https://cdn.example.com/other.jpg'),
    ('clearance', 'history_evidence_ref', 'history:other'),
    ('manifest', 'tenant_id', 'another-gym'),
])
def test_new_manifest_rejects_immutable_authority_or_digest_conflict(
        prepared, table, field, value):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    new = second_manifest(original, reader)
    if table == 'manifest':
        svc.persist(original, clearance, new)
    conn.corrupt(table, field, value)
    with pytest.raises(OwnerPersistenceError, match='immutable authority'):
        svc.persist(original, clearance, new)
    assert conn.rollbacks == 1


def test_new_manifest_concurrent_insert_conflict_rolls_back(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    new = second_manifest(original, reader)
    insert = svc._insert
    def competing_insert(query, params):
        # Simulate another transaction winning the unique digest after our read.
        insert(query, params)
        raise RuntimeError('unique manifest digest conflict')
    svc._insert = competing_insert
    with pytest.raises(RuntimeError, match='unique manifest digest conflict'):
        svc.persist(original, clearance, new)
    assert conn.rollbacks == 1 and conn.commits == 1


def test_new_manifest_requires_owner_identity(prepared):
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    conn.current_user = 'service_role'
    with pytest.raises(OwnerPersistenceError, match='dedicated owner'):
        svc.persist(original, clearance, second_manifest(original, reader))
    assert len(conn.tables['manifest']) == 1


def test_new_manifest_failed_insert_preserves_committed_authority(prepared):
    import copy
    original, clearance, manifest, reader = prepared
    svc, conn = adapter(reader=reader)
    svc.persist(original, clearance, manifest)
    frozen = copy.deepcopy(conn.tables)
    new = second_manifest(original, reader)
    def failed_insert(query, params):
        assert 'render_manifest' in query
        raise RuntimeError('manifest constraint failed')
    svc._insert = failed_insert
    with pytest.raises(RuntimeError, match='manifest constraint failed'):
        svc.persist(original, clearance, new)
    assert conn.tables == frozen
    assert conn.rollbacks == 1 and conn.commits == 1


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
