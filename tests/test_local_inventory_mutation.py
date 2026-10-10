"""Offline contract and real filesystem/SQLite checks for the DRAFT local fence."""
import fcntl
import json
from pathlib import Path
import sqlite3
import uuid
import threading
from types import SimpleNamespace

import pytest

from agent import dam, db, local_inventory_mutation as mutation
from agent import upload_media_approvals as approvals
from agent.accounts import Account, Platform


class Authority:
    def __init__(self, config, events=None, fail=None):
        self.config = config
        self.events = events if events is not None else []
        self.fail = fail
        self.pending = None

    def _assert_unlocked(self):
        with open(self.config.lock_directory / (self.config.gym_id + '.lock'), 'a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def begin(self, request):
        self.events.append('begin')
        self._assert_unlocked()
        self.pending = dict(request, state='pending', generation=3,
                            result_digest=None, begun_at='2026-10-08T00:00:00+00:00', completed_at=None)
        if self.fail == 'begin':
            raise mutation.MutationHold('ack_lost')
        if self.fail == 'wrong_identity':
            return dict(self.pending, gym_id='neighbor')
        return dict(self.pending)

    def complete(self, request, result_digest):
        self.events.append('complete')
        self._assert_unlocked()
        with sqlite3.connect(self.config.sqlite_path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] >= 0
        if self.fail == 'complete':
            raise mutation.MutationHold('ack_lost')
        self.pending.update(state='complete', result_digest=result_digest,
                            completed_at='2026-10-08T00:00:01+00:00')
        if self.fail == 'wrong_result':
            return dict(self.pending, result_digest=mutation.digest('wrong'))
        if self.fail == 'wrong_generation':
            return dict(self.pending, generation=99)
        return dict(self.pending)

    def close(self):
        pass


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(dam.config, 'LIBRARY_PATH', str(tmp_path))
    library = tmp_path / 'gymx'
    library.mkdir()
    database = tmp_path / 'echo.db'
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE evidence (value TEXT)')
    return mutation.MutationConfig('gymx', str(uuid.uuid4()), library, database,
                                   tmp_path / 'journals', tmp_path / 'locks')


def _journal(cfg):
    return json.loads(next(cfg.journal_directory.glob('*.json')).read_text())


def test_pg_begin_before_files_sqlite_commit_before_complete_and_no_pg_under_flock(cfg):
    events = []
    auth = Authority(cfg, events)
    target = cfg.library_path / 'photo.json'
    def apply(conn):
        assert auth.pending['state'] == 'pending'
        events.append('apply')
        mutation.atomic_write_json(target, {'approved': True})
        conn.execute('INSERT INTO evidence VALUES (?)', ('approved',))
        return {'file_digest': mutation.digest({'approved': True})}
    result = mutation.run(cfg, auth, 'test_write', {'asset': 'photo'}, apply)
    assert events == ['begin', 'apply', 'complete']
    assert auth.pending['result_digest'] == mutation.digest(result)
    assert _journal(cfg)['local_state'] == 'complete'
    with sqlite3.connect(cfg.sqlite_path) as conn:
        assert conn.execute('SELECT value FROM evidence').fetchone()[0] == 'approved'
    mutation.assert_settled(cfg)


@pytest.mark.parametrize('failure', ['begin', 'wrong_identity'])
def test_uncertain_or_wrong_begin_never_changes_local_supply(cfg, failure):
    auth = Authority(cfg, fail=failure)
    calls = []
    with pytest.raises(mutation.MutationHold):
        mutation.run(cfg, auth, 'test_write', {}, lambda conn: calls.append('unsafe'))
    assert calls == []
    assert _journal(cfg)['local_state'] == 'prepared'
    assert _journal(cfg)['mutation_id'] == auth.pending['mutation_id']
    with pytest.raises(mutation.MutationHold, match='reconciliation_required'):
        mutation.run(cfg, auth, 'test_write', {}, lambda conn: {})


@pytest.mark.parametrize('failure', ['complete', 'wrong_result', 'wrong_generation'])
def test_complete_uncertainty_retains_pending_exact_identity_after_local_commit(cfg, failure):
    auth = Authority(cfg, fail=failure)
    target = cfg.library_path / 'photo.json'
    def apply(conn):
        mutation.atomic_write_json(target, {'approved': True})
        conn.execute('INSERT INTO evidence VALUES (?)', ('done',))
        return {'approved': True}
    with pytest.raises(mutation.MutationHold, match='pending_reconciliation'):
        mutation.run(cfg, auth, 'test_write', {}, apply)
    assert json.loads(target.read_text()) == {'approved': True}
    record = _journal(cfg)
    assert record['local_state'] == 'local_committed'
    assert record['result_digest'] == mutation.digest({'approved': True})
    assert record['mutation_id'] == auth.pending['mutation_id']
    with pytest.raises(mutation.MutationHold, match='reconciliation_required'):
        mutation.assert_settled(cfg)


def test_crash_after_atomic_file_does_not_complete_or_expire_pending(cfg):
    auth = Authority(cfg)
    target = cfg.library_path / 'photo.json'
    def apply(conn):
        mutation.atomic_write_json(target, {'partial': True})
        conn.execute('INSERT INTO evidence VALUES (?)', ('rolled back',))
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        mutation.run(cfg, auth, 'test_write', {}, apply)
    assert json.loads(target.read_text()) == {'partial': True}
    assert _journal(cfg)['local_state'] == 'pending'
    assert auth.pending['state'] == 'pending'
    with sqlite3.connect(cfg.sqlite_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 0
    with pytest.raises(mutation.MutationHold):
        mutation.assert_settled(cfg)


def test_invalid_asset_alias_and_missing_history_fail_before_begin(cfg):
    alias = cfg.library_path / 'alias.json'
    outside = cfg.library_path.parent / 'outside.json'
    outside.write_text('{}')
    alias.symlink_to(outside)
    with pytest.raises(mutation.MutationHold, match='asset_binding_invalid'):
        cfg.asset_path(alias)
    invalid = mutation.MutationConfig('GymX', cfg.epoch_id, cfg.library_path,
                                      cfg.sqlite_path, cfg.journal_directory, cfg.lock_directory)
    with pytest.raises(mutation.MutationHold, match='binding_invalid'):
        mutation.run(invalid, Authority(cfg), 'test_write', {}, lambda conn: {})
    cfg.sqlite_path.unlink()
    with pytest.raises(mutation.MutationHold, match='durable_paths_unavailable'):
        mutation.run(cfg, Authority(cfg), 'test_write', {}, lambda conn: {})


def test_no_service_credential_fallback(monkeypatch):
    monkeypatch.delenv('LOCAL_INVENTORY_MUTATOR_DSN', raising=False)
    monkeypatch.delenv('LOCAL_INVENTORY_MUTATOR_LOGIN', raising=False)
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'forbidden-fallback')
    with pytest.raises(mutation.MutationHold, match='dedicated_authority_unavailable'):
        mutation.MutationAuthority.from_environment()


class PGConnection:
    autocommit = False
    def __init__(self, *, login='isolated_mutator', member=True, forbidden=False,
                 superuser=False, commit_failure=False, transaction_status=0):
        self.info = SimpleNamespace(transaction_status=transaction_status)
        self.login, self.member, self.forbidden = login, member, forbidden
        self.superuser, self.commit_failure = superuser, commit_failure
        self.events = []
        self.value = {'receipt': 'only returned after commit'}

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        self.events.append(sql)
        if sql.startswith('select current_user'):
            self.row = (self.login, self.member, self.superuser, False)
        elif sql.startswith('select pg_has_role'):
            self.row = (self.forbidden,)
        else:
            self.row = (self.value,)

    def fetchone(self):
        return self.row

    def commit(self):
        self.events.append('commit')
        if self.commit_failure:
            raise OSError('lost COMMIT acknowledgement')

    def rollback(self):
        self.events.append('rollback')


def test_authority_commits_before_exposing_begin_ack():
    conn = PGConnection()
    auth = mutation.MutationAuthority(conn, 'isolated_mutator')
    request = dict(mutation_id=str(uuid.uuid4()), gym_id='gymx', epoch_id=str(uuid.uuid4()),
                   kind='test_write', request_digest=mutation.digest({}))
    assert auth.begin(request) == conn.value
    assert conn.events[-1] == 'commit'
    assert any(mutation.BEGIN_RPC in sql for sql in conn.events)


@pytest.mark.parametrize('kwargs', [
    {'login': 'service_role'}, {'member': False}, {'forbidden': True},
    {'superuser': True}, {'transaction_status': 2},
])
def test_authority_refuses_wrong_privileged_mixed_role_or_active_pg_transaction(kwargs):
    conn = PGConnection(**kwargs)
    auth = mutation.MutationAuthority(conn, 'isolated_mutator')
    with pytest.raises(mutation.MutationHold):
        auth._rpc(mutation.BEGIN_RPC, ())
    assert not any('select public.' in sql for sql in conn.events)
    assert conn.events[-1] == 'rollback'


def test_authority_commit_failure_is_uncertain_not_an_ack():
    conn = PGConnection(commit_failure=True)
    auth = mutation.MutationAuthority(conn, 'isolated_mutator')
    with pytest.raises(mutation.MutationHold, match='authority_uncertain'):
        auth._rpc(mutation.BEGIN_RPC, ())
    assert conn.events[-2:] == ['commit', 'rollback']


def _arm(monkeypatch, cfg, auth):
    monkeypatch.setenv('AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED', 'true')
    monkeypatch.setenv('AGENT_DB_PATH', str(cfg.sqlite_path))
    monkeypatch.setenv('LOCAL_INVENTORY_MUTATION_EPOCH', cfg.epoch_id)
    monkeypatch.setattr(mutation, 'configured', lambda gym, library: cfg)
    monkeypatch.setattr(mutation.MutationAuthority, 'from_environment', lambda: auth)
    monkeypatch.setattr(dam.config, 'LIBRARY_PATH', str(cfg.library_path.parent))


def test_dam_checked_atomic_merge_and_corrupt_sidecar_hold(monkeypatch, cfg):
    auth = Authority(cfg)
    _arm(monkeypatch, cfg, auth)
    photo = cfg.library_path / 'photo.jpg'
    photo.write_bytes(b'photo')
    side = photo.with_suffix('.json')
    side.write_text('{"note":"preserve"}')
    assert dam.write_sidecar(str(photo), {'review': True}) == {'note': 'preserve', 'review': True}
    assert _journal(cfg)['kind'] == 'dam_sidecar'
    side.write_text('broken')
    with pytest.raises(mutation.MutationHold):
        dam.write_sidecar(str(photo), {'approved': True})
    assert side.read_text() == 'broken'
    assert auth.pending['state'] == 'pending'


def _approval_setup(monkeypatch, cfg, auth):
    from PIL import Image
    _arm(monkeypatch, cfg, auth)
    monkeypatch.setenv('AGENT_PORTAL_APPROVALS', 'true')
    account = Account(key='gymx_ig', display_name='Gym X', platform=Platform.INSTAGRAM,
                      token_env='X', target_id_env='Y', library_prefix='', approvers=['U_REVIEWER'])
    monkeypatch.setattr(approvals, 'get_account', lambda key: account if key == 'gymx_ig' else None)
    with db.connect() as conn:
        conn.commit()
    photo = cfg.library_path / 'photo.jpg'
    Image.new('RGB', (12, 12), color=(30, 90, 130)).save(photo, format='JPEG')
    photo.with_suffix('.json').write_text('{"note":"preserve"}')
    calls = []
    monkeypatch.setattr(approvals, '_rearm_after_approval', lambda *args: calls.append(args))
    return photo, calls


def test_approval_fences_sidecar_and_audit_then_holds_unfenced_bridge(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    status, result = approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')
    assert status == 503 and result['approval_recorded'] is True
    assert result['inventory_mutation_complete'] is True
    side = json.loads(photo.with_suffix('.json').read_text())
    assert side['approved'] is True and side['note'] == 'preserve'
    assert calls == []
    assert len(db.audit_rows(account_key='gymx')) == 1
    assert _journal(cfg)['local_state'] == 'complete'


def test_fresh_approval_holds_bridge_when_other_pending_mutation_appears_during_complete(monkeypatch, cfg):
    from agent import media_bridge
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    episode = media_bridge.episode('gymx')
    def unsafe_rearm(base, account_key, path, asset_name):
        calls.append(base)
        return media_bridge.rearm_for_new_upload(base, asset_name, usable=True)
    monkeypatch.setattr(approvals, '_rearm_after_approval', unsafe_rearm)
    real_complete = auth.complete
    concurrent = Authority(cfg)
    second_request = dict(mutation_id=str(uuid.uuid4()), gym_id='gymx',
                          epoch_id=cfg.epoch_id, kind='concurrent_write',
                          request_digest=mutation.digest({'concurrent': True}))
    def complete(request, result_digest):
        acknowledged = real_complete(request, result_digest)
        pending_receipt = concurrent.begin(second_request)
        mutation.atomic_write_json(
            cfg.journal_directory / (second_request['mutation_id'] + '.json'),
            dict(second_request, local_state='pending', begin_receipt=pending_receipt))
        return acknowledged
    monkeypatch.setattr(auth, 'complete', complete)
    status, body = approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')
    assert status == 503 and body['approval_recorded'] is True
    assert body['inventory_mutation_complete'] is True
    assert calls == []
    assert dam.read_sidecar(str(photo))['approved'] is True
    assert len(db.audit_rows(account_key='gymx')) == 1
    assert auth.pending['state'] == 'complete' and concurrent.pending['state'] == 'pending'
    assert media_bridge.episode('gymx', create=False)['id'] == episode['id']
    with pytest.raises(mutation.MutationHold, match='reconciliation_required'):
        mutation.assert_settled(cfg)


def test_approval_lost_complete_ack_cannot_rearm_or_succeed_on_retry(monkeypatch, cfg):
    auth = Authority(cfg, fail='complete')
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    assert approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')[0] == 503
    assert dam.read_sidecar(str(photo))['approved'] is True
    assert calls == []
    assert approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')[0] == 503
    assert calls == []
    assert len(db.audit_rows(account_key='gymx')) == 1


def test_approval_audit_failure_preserves_sidecar_and_pg_pending(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    monkeypatch.setattr(approvals, '_audit_approval', lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    assert approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')[0] == 503
    assert 'approved' not in dam.read_sidecar(str(photo))
    assert auth.pending['state'] == 'pending' and calls == []


def test_already_approved_retry_holds_when_another_mutation_begins_after_settled_check(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    photo.with_suffix('.json').write_text(
        '{"approved":true,"review":false,"moderation":"clean"}')
    settled_checked = threading.Event()
    pending = threading.Event()
    release = threading.Event()
    errors = []
    concurrent = Authority(cfg)
    real_settled = mutation.assert_settled
    def checked(config):
        real_settled(config)
        if threading.current_thread() is threading.main_thread():
            settled_checked.set()
            assert pending.wait(5)
    monkeypatch.setattr(mutation, 'assert_settled', checked)
    def apply(conn):
        pending.set()
        assert release.wait(5)
        return {'concurrent': True}
    def writer():
        try:
            assert settled_checked.wait(5)
            mutation.run(cfg, concurrent, 'concurrent_write', {}, apply)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        status, body = approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')
        assert status == 503 and body['ok'] is False
        assert calls == []
        assert concurrent.pending['state'] == 'pending'
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and errors == []
    assert len(db.audit_rows(account_key='gymx')) == 0


def test_already_approved_retry_holds_without_mutating_bridge_even_if_settled(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    photo.with_suffix('.json').write_text(
        '{"approved":true,"review":false,"moderation":"clean"}')
    assert approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')[0] == 503
    assert calls == [] and auth.pending is None


def test_cross_tenant_config_cannot_bind_neighbor_to_gym_library(cfg):
    forged = mutation.MutationConfig('neighbor', cfg.epoch_id, cfg.library_path,
                                     cfg.sqlite_path, cfg.journal_directory, cfg.lock_directory)
    photo = cfg.library_path / 'photo.jpg'
    photo.write_bytes(b'photo')
    auth = Authority(cfg)
    with pytest.raises(mutation.MutationHold, match='library_binding_invalid'):
        forged.asset_path(photo)
    with pytest.raises(mutation.MutationHold, match='library_binding_invalid'):
        mutation.run(forged, auth, 'test_write', {}, lambda conn: {})
    assert auth.pending is None and not cfg.journal_directory.exists()


def test_enabled_approval_refuses_persisted_account_library_of_other_gym(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    # Restore the real runtime config constructor; the persisted account points
    # neighbor's library_prefix at gymx's existing photo.
    monkeypatch.undo()
    monkeypatch.setattr(dam.config, 'LIBRARY_PATH', str(cfg.library_path.parent))
    monkeypatch.setenv('AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED', 'true')
    monkeypatch.setenv('AGENT_DB_PATH', str(cfg.sqlite_path))
    monkeypatch.setenv('LOCAL_INVENTORY_MUTATION_EPOCH', cfg.epoch_id)
    monkeypatch.setenv('AGENT_PORTAL_APPROVALS', 'true')
    neighbor = Account(key='neighbor_ig', display_name='Neighbor', platform=Platform.INSTAGRAM,
                       token_env='X', target_id_env='Y', library_prefix=str(cfg.library_path),
                       approvers=['U_REVIEWER'])
    monkeypatch.setattr(approvals, 'get_account', lambda key: neighbor if key == 'neighbor_ig' else None)
    monkeypatch.setattr(approvals, '_rearm_after_approval', lambda *args: calls.append(args))
    monkeypatch.setattr(mutation.MutationAuthority, 'from_environment', lambda: auth)
    status, body = approvals.approve('neighbor', photo.name, 'U_REVIEWER', moderation='clean')
    assert status == 503 and body['ok'] is False
    assert auth.pending is None and calls == []
    assert 'approved' not in dam.read_sidecar(str(photo))
    assert len(db.audit_rows(account_key='neighbor')) == 0


def test_enabled_approval_refuses_gym_account_pointing_to_neighbor_library(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    # Exercise the production constructor, not the test's bound config seam.
    monkeypatch.undo()
    monkeypatch.setattr(dam.config, 'LIBRARY_PATH', str(cfg.library_path.parent))
    monkeypatch.setenv('AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED', 'true')
    monkeypatch.setenv('AGENT_DB_PATH', str(cfg.sqlite_path))
    monkeypatch.setenv('LOCAL_INVENTORY_MUTATION_EPOCH', cfg.epoch_id)
    monkeypatch.setenv('AGENT_PORTAL_APPROVALS', 'true')
    neighbor = cfg.library_path.parent / 'neighbor'
    neighbor.mkdir()
    neighbor_photo = neighbor / photo.name
    neighbor_photo.write_bytes(photo.read_bytes())
    neighbor_photo.with_suffix('.json').write_text('{"note":"neighbor"}')
    account = Account(key='gymx_ig', display_name='Gym X', platform=Platform.INSTAGRAM,
                      token_env='X', target_id_env='Y', library_prefix=str(neighbor),
                      approvers=['U_REVIEWER'])
    monkeypatch.setattr(approvals, 'get_account', lambda key: account if key == 'gymx_ig' else None)
    monkeypatch.setattr(approvals, '_rearm_after_approval', lambda *args: calls.append(args))
    monkeypatch.setattr(mutation.MutationAuthority, 'from_environment', lambda: auth)
    assert approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')[0] == 503
    assert auth.pending is None and calls == []
    assert dam.read_sidecar(str(neighbor_photo)) == {'note': 'neighbor'}
    assert len(db.audit_rows(account_key='gymx')) == 0


def test_approval_replacement_after_initial_validation_fails_exact_byte_recheck(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    real_fenced = approvals._fenced_approval
    def replace_after_validated(*args):
        photo.write_bytes(b'not a JPEG')
        return real_fenced(*args)
    monkeypatch.setattr(approvals, '_fenced_approval', replace_after_validated)
    assert approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')[0] == 503
    assert 'approved' not in dam.read_sidecar(str(photo))
    assert auth.pending['state'] == 'pending' and calls == []
    assert len(db.audit_rows(account_key='gymx')) == 0


def test_approval_replacement_after_pg_begin_fails_validity_inside_gym_flock(monkeypatch, cfg):
    auth = Authority(cfg)
    photo, calls = _approval_setup(monkeypatch, cfg, auth)
    original_begin = auth.begin
    def begin(request):
        result = original_begin(request)
        photo.write_bytes(b'not a JPEG')
        return result
    monkeypatch.setattr(auth, 'begin', begin)
    assert approvals.approve('gymx', photo.name, 'U_REVIEWER', moderation='clean')[0] == 503
    assert 'approved' not in dam.read_sidecar(str(photo))
    assert auth.pending['state'] == 'pending' and calls == []
    assert len(db.audit_rows(account_key='gymx')) == 0


def test_fence_is_off_by_default(monkeypatch, cfg):
    monkeypatch.delenv('AGENT_LOCAL_INVENTORY_MUTATIONS_ENABLED', raising=False)
    monkeypatch.setattr(mutation, 'write_sidecar', lambda *args: (_ for _ in ()).throw(AssertionError()))
    photo = cfg.library_path / 'photo.jpg'
    assert dam.write_sidecar(str(photo), {'review': True}) == {'review': True}
