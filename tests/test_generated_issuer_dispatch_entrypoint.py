"""Offline composition tests: no real credentials, connections or HTTP."""
from types import SimpleNamespace

import pytest

from agent import generated_issuer_dispatch_entrypoint as entry


@pytest.fixture
def gates(monkeypatch):
    for flag in ('AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME',
                 'AGENT_GENERATED_ISSUER_DISPATCH',
                 'AGENT_GENERATED_HOSTED_BYTE_ISSUER'):
        monkeypatch.setenv(flag, '1')


def config():
    return dict(ECHO_GENERATED_ISSUER_DSN='synthetic',
                ECHO_GENERATED_ISSUER_LOGIN='synthetic_issuer',
                ECHO_GENERATED_ISSUER_TENANT='gym-a',
                ECHO_GENERATED_ISSUER_URL_PREFIX='https://cdn.example/gym-a/generated/')


def test_off_never_connects(monkeypatch):
    monkeypatch.delenv('AGENT_GENERATED_ISSUER_DISPATCH_RUNTIME', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('connected while OFF')
    assert entry.run_once(environ={}, connect=forbidden)['status'] == 'generated_issuer_dispatch_off'


@pytest.mark.parametrize('key,value', [('LOGIN', 'service_role'),
                                      ('URL_PREFIX', 'https://cdn.example/'),
                                      ('TENANT', '')])
def test_bad_provisioning(gates, key, value):
    env = config()
    env['ECHO_GENERATED_ISSUER_' + key] = value
    with pytest.raises(entry.EntrypointHold, match='configuration_invalid'):
        entry.run_once(environ=env, connect=lambda *a, **k: pytest.fail('connected'))


def test_injected_empty_queue_happy_path(gates):
    calls = []
    class Connection:
        autocommit = True
        info = SimpleNamespace(transaction_status=0)
        def execute(self, sql, args=None):
            calls.append(sql)
            if sql == entry._IDENTITY_SQL:
                return SimpleNamespace(fetchone=lambda: (
                    'synthetic_issuer', 'synthetic_issuer', True, True, True, True, True))
            assert 'generated_issuer_dispatch_pending' in sql
            assert args == (['gym-a'], 10)
            return SimpleNamespace(fetchall=lambda: [])
        def close(self):
            calls.append('close')
    def connect(dsn, *, autocommit):
        assert dsn == 'synthetic' and autocommit is True
        return Connection()
    result = entry.run_once(environ=config(), connect=connect)
    assert result['processed'] == 0
    assert result['status'] == 'generated_issuer_dispatch_ok'
    assert calls[-1] == 'close'


def test_role_mismatch_closes_and_has_safe_result(gates):
    closed = []
    conn = SimpleNamespace(
        execute=lambda sql: SimpleNamespace(fetchone=lambda: ('service_role',)),
        close=lambda: closed.append(True))
    # Job captures store holds in its safe tally; no issuance is possible.
    result = entry.run_once(environ=config(), connect=lambda *a, **k: conn)
    assert closed == [True]
    assert result['issued'] == 0


def test_no_request_configuration_arguments(capsys):
    assert entry.main(['--tenant', 'gym-a']) == 2
    assert 'arguments_rejected' in capsys.readouterr().out


@pytest.mark.parametrize('failed_column', [2, 3, 4, 5, 6])
def test_ineffective_or_broad_role_never_reaches_queue(gates, failed_column):
    calls = []
    row = ['synthetic_issuer', 'synthetic_issuer', True, True, True, True, True]
    row[failed_column] = False
    def execute(sql):
        assert sql == entry._IDENTITY_SQL
        calls.append('identity')
        return SimpleNamespace(fetchone=lambda: tuple(row))
    conn = SimpleNamespace(execute=execute, close=lambda: calls.append('close'))
    result = entry.run_once(environ=config(), connect=lambda *a, **k: conn)
    assert calls == ['identity', 'close']
    assert result['issued'] == 0
    assert result['failed'] == 1


def test_identity_sql_requires_effective_usage_and_positive_role_allowlist():
    # Guards the SQL semantics behind the injected failure-row regressions:
    # NOINHERIT / INHERIT FALSE yields USAGE false despite MEMBER true;
    # pg_read_all_data and every other unlisted role are rejected by MEMBER.
    assert "'generated_issuer_dispatch_issuer_20261009','USAGE'" in entry._IDENTITY_SQL
    assert "'generated_hosted_byte_issuer_20261009','USAGE'" in entry._IDENTITY_SQL
    assert "pg_has_role(session_user,other.oid,'MEMBER')" in entry._IDENTITY_SQL
    assert 'other.rolname not in' in entry._IDENTITY_SQL
    assert 'rolreplication' in entry._IDENTITY_SQL


def test_nonempty_dispatch_uses_idle_transactional_authority(gates, monkeypatch):
    import hashlib
    import json
    from uuid import uuid4

    data = b'offline synthetic image'
    sha = hashlib.sha256(data).hexdigest()
    version, receipt_id = str(uuid4()), str(uuid4())
    url = config()['ECHO_GENERATED_ISSUER_URL_PREFIX'] + 'image.png'
    manifest = dict(gym_id='gym-a', artifact_version_id=version,
                    hosted_url=url, delivered_sha256=sha, engine='synthetic')
    manifest_bytes = json.dumps(manifest).encode()
    receipt = dict(receipt_id=receipt_id, gym_id='gym-a',
                   artifact_version_id=version, hosted_url=url,
                   delivered_sha256=sha, render_manifest=manifest)
    events = []
    monkeypatch.setattr(entry.HostedObjectReader, 'read',
                        lambda self, tenant, exact_url: data)

    class Connection:
        def __init__(self, autocommit):
            self.autocommit = autocommit
            self.info = SimpleNamespace(transaction_status=0)
        def execute(self, sql, args=None):
            if sql == entry._IDENTITY_SQL:
                if not self.autocommit:
                    self.info.transaction_status = 2
                return SimpleNamespace(fetchone=lambda: (
                    'synthetic_issuer', 'synthetic_issuer', True, True, True, True, True))
            if 'dispatch_pending' in sql:
                assert self.autocommit is True
                return SimpleNamespace(fetchall=lambda: [
                    ('gym-a', version, url, sha, manifest_bytes)])
            if 'dispatch_claim' in sql:
                value = dict(is_new=True, binding_digest=args[1], receipt_id=None)
            elif 'dispatch_commit' in sql:
                value = True
                events.append('ledger_commit')
            elif 'byte_authorized' in sql:
                assert self.autocommit is False
                assert self.info.transaction_status == 0
                self.info.transaction_status = 2
                value = True
            elif 'byte_issue' in sql:
                assert self.autocommit is False
                value = receipt
                events.append('issue')
            else:
                raise AssertionError(sql)
            return SimpleNamespace(fetchone=lambda: (value,))
        def rollback(self):
            self.info.transaction_status = 0
            events.append('rollback')
        def commit(self):
            events.append('authority_commit')
        def close(self):
            events.append('close')

    result = entry.run_once(environ=config(), connect=lambda dsn, *, autocommit: Connection(autocommit))
    assert result['issued'] == 1
    assert result['processed'] == 1 and result['failed'] == 0
    assert events.count('issue') == 1
    assert events.count('rollback') == 3
    assert events.index('authority_commit') < events.index('ledger_commit')
