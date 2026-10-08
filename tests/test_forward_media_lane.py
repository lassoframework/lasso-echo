"""Exact isolated env contracts and offline credential-free HTTPS reader checks."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agent import forward_media_lane as lane
from agent import forward_media_owner as owner
from agent import forward_media_guard as guard
from agent import forward_media_attester_worker as worker

OWNER = {'FORWARD_MEDIA_OWNER_DSN': 'postgresql://dedicated-owner/db',
         'FORWARD_MEDIA_OWNER_ROLE': 'owner'}
ATTESTER = {'AGENT_FORWARD_MEDIA_ATTESTER_DSN': 'postgresql://dedicated-attester/db',
            'AGENT_FORWARD_MEDIA_ATTESTER_ROLE': guard.ROLE,
            worker.TENANTS_ENV: 'pierce', 'AGENT_FORWARD_MEDIA_GUARD': 'true'}

@pytest.mark.parametrize('name', ['GOOGLE_DRIVE_SA_JSON', 'PGPASSWORD', 'FUTURE_ALIAS',
    'google_drive_sa_json', 'HTTP_PROXY', 'AWS_PROFILE', 'PGSERVICE',
    'AGENT_PUBLISH_ENABLED', 'AGENT_DB_PATH', 'AGENT_S3_ACCESS_KEY_ID'])
@pytest.mark.parametrize('value', ['', 'sentinel-do-not-log'])
def test_unknown_names_fail_both_lanes(name, value):
    with pytest.raises(owner.EnvironmentGuardError) as exc:
        owner.check_environment({**OWNER, name: value})
    assert 'sentinel-do-not-log' not in str(exc.value)
    with pytest.raises(worker.WorkerConfigurationHold) as exc:
        worker.settings_from_environment({**ATTESTER, name: value})
    assert 'sentinel-do-not-log' not in str(exc.value)


def test_opposite_lane_credentials_rejected():
    with pytest.raises(owner.EnvironmentGuardError):
        owner.check_environment({**OWNER, **ATTESTER})
    with pytest.raises(worker.WorkerConfigurationHold):
        worker.settings_from_environment({**ATTESTER, **OWNER})


def test_exact_runtime_ca_names_allowed():
    runtime = {key: '/runtime' for key in lane.RUNTIME_NAMES}
    owner.check_environment({**OWNER, **runtime})
    assert worker.settings_from_environment({**ATTESTER, **runtime}).tenants == ('pierce',)
    assert not lane.unknown_environment_names({**ATTESTER, **runtime}, 'attester')


def test_exact_owner_worker_configuration_allowed_only_for_owner():
    settings = {'AGENT_FORWARD_MEDIA_OWNER_WORKER': 'true',
                'AGENT_FORWARD_MEDIA_OWNER_TENANTS': 'pierce',
                'AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE': '25',
                'AGENT_FORWARD_MEDIA_OWNER_PHOTO_CLEARANCE': 'false'}
    owner.check_environment({**OWNER, **settings})
    assert lane.unknown_environment_names({**ATTESTER, **settings}, 'attester') == sorted(settings)


@pytest.mark.parametrize('name', ['GOOGLE_DRIVE_SA_JSON', 'PGPASSWORD', 'UNKNOWN',
                                'FORWARD_MEDIA_OWNER_DSN'])
def test_direct_attester_connection_checks_before_driver(monkeypatch, name):
    monkeypatch.setattr(guard, 'os', SimpleNamespace(environ={**ATTESTER, name: ''}))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='unrecognized'):
        guard._connect()


@pytest.mark.parametrize('url', ['http://cdn.example/media/x',
    'https://cdn.example.evil/media/x', 'https://user@cdn.example/media/x',
    'https://cdn.example/media/../x', 'https://cdn.example/media/%2e%2e/x',
    'https://cdn.example/media/a%2fb', 'https://cdn.example/media/a#fragment',
    'https://cdn.example/other/x', 'https://cdn.example/media/a%00b'])
def test_origin_and_path_fail_closed(url):
    assert not lane.approved_url(url, 'https://cdn.example/media')

@pytest.mark.parametrize('base', ['', 'https://127.0.0.1', 'https://10.0.0.1',
    'https://localhost', 'https://internal.local', 'http://cdn.example',
    'https://cdn.example?token=abc', 'https://cdn.example:444'])
def test_private_or_missing_origin_rejected(base):
    assert not lane.approved_url(base + '/x', base)


def test_query_is_preserved_and_origin_allowed():
    assert lane.approved_url('https://cdn.example/media/a?width=1', 'https://cdn.example/media')


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv('AGENT_S3_PUBLIC_BASE_URL', 'https://cdn.example/media')
    monkeypatch.setattr(lane.socket, 'getaddrinfo', lambda *a, **k: [
        (2, 1, 6, '', ('8.8.8.8', 443))])
    response = Mock(status_code=200, headers={})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.raw.read1.side_effect = [b'actual', b' bytes', b'']
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value = response
    import requests
    monkeypatch.setattr(requests, 'Session', lambda: session)
    return session, response


def test_public_get_uses_clean_session_no_redirect_and_exact_query(http):
    session, response = http
    url = 'https://cdn.example/media/a?version=123'
    assert lane.read_public_object(url) == b'actual bytes'
    assert session.trust_env is False
    args, kwargs = session.get.call_args
    assert args == (url,)
    assert kwargs == {'stream': True, 'allow_redirects': False, 'timeout': (5, 5),
                      'headers': {'Accept-Encoding': 'identity'}}
    assert response.__exit__.called and session.__exit__.called

@pytest.mark.parametrize('status', [301, 302, 401, 403, 404, 500])
def test_private_objects_and_redirects_hold(http, status):
    http[1].status_code = status
    with pytest.raises(lane.PublicObjectReadError):
        lane.read_public_object('https://cdn.example/media/a')
    http[1].raw.read1.assert_not_called()


def test_private_resolution_holds_before_http(http, monkeypatch):
    monkeypatch.setattr(lane.socket, 'getaddrinfo', lambda *a, **k: [
        (2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(lane.PublicObjectReadError, match='public media address'):
        lane.read_public_object('https://cdn.example/media/a')
    http[0].get.assert_not_called()

@pytest.mark.parametrize('headers', [{'Content-Length': '129'},
    {'Content-Length': 'invalid'}, {'Content-Encoding': 'gzip'}])
def test_unbounded_or_encoded_object_holds(http, headers):
    http[1].headers = headers
    with pytest.raises(lane.PublicObjectReadError):
        lane.read_public_object('https://cdn.example/media/a', max_bytes=128)
    http[1].raw.read1.assert_not_called()


def test_stream_byte_limit_and_empty_fail_closed(http):
    with pytest.raises(lane.PublicObjectReadError):
        lane.read_public_object('https://cdn.example/media/a', max_bytes=5)
    http[1].raw.read1.side_effect = [b'']
    with pytest.raises(lane.PublicObjectReadError):
        lane.read_public_object('https://cdn.example/media/a')


def test_total_stream_deadline(http, monkeypatch):
    times = iter([0, 1, 31])
    monkeypatch.setattr(lane.time, 'monotonic', lambda: next(times))
    with pytest.raises(lane.PublicObjectReadError, match='bounded'):
        lane.read_public_object('https://cdn.example/media/a')
    assert http[1].__exit__.called


def test_reader_errors_never_expose_url_or_driver_details(http):
    http[0].get.side_effect = RuntimeError('private-secret-sentinel')
    with pytest.raises(lane.PublicObjectReadError) as exc:
        lane.read_public_object('https://cdn.example/media/a?private-secret-sentinel')
    assert 'private-secret-sentinel' not in str(exc.value)


def test_owner_hosted_reader_uses_public_get(http):
    assert owner.HostedObjectReader().read('https://cdn.example/media/a') == b'actual bytes'


def test_owner_transaction_rechecks_environment_and_rolls_back(monkeypatch):
    conn = Mock()
    monkeypatch.setattr(owner, 'os', SimpleNamespace(environ={**OWNER, 'GOOGLE_DRIVE_SA_JSON': ''}))
    adapter = owner.ForwardMediaOwnerPersistence(conn, 'owner', owner.HostedObjectReader())
    with pytest.raises(owner.EnvironmentGuardError):
        adapter.persist(None, None, None)
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    conn.cursor.assert_not_called()

@pytest.mark.parametrize('kind,environment,module', [
    ('owner', OWNER, 'agent.forward_media_owner_packet'),
    ('attester', ATTESTER, 'agent.forward_media_attester_worker'),
])
def test_launcher_passes_only_explicit_environment(monkeypatch, kind, environment, module):
    from agent import forward_media_lane_launcher as launcher
    monkeypatch.setenv('GOOGLE_DRIVE_SA_JSON', 'ambient-sentinel')
    runner = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(launcher.subprocess, 'run', runner)
    assert launcher.launch_isolated(kind, ['--once'], environment=environment).returncode == 0
    args, kwargs = runner.call_args
    assert args[0][1:] == ['-m', module, '--once']
    assert kwargs['env'] == environment
    assert kwargs['env'] is not environment
    assert 'GOOGLE_DRIVE_SA_JSON' not in kwargs['env']

@pytest.mark.parametrize('kind,environment', [
    ('owner', {**OWNER, 'GOOGLE_DRIVE_SA_JSON': ''}),
    ('attester', {**ATTESTER, 'FORWARD_MEDIA_OWNER_DSN': ''}),
    ('owner', {'FORWARD_MEDIA_OWNER_DSN': 'dedicated'}),
    ('attester', {'AGENT_FORWARD_MEDIA_ATTESTER_ROLE': guard.ROLE}),
])
def test_launcher_refuses_unknown_or_missing_configuration(monkeypatch, kind, environment):
    from agent import forward_media_lane_launcher as launcher
    runner = Mock()
    monkeypatch.setattr(launcher.subprocess, 'run', runner)
    with pytest.raises(launcher.LaneLaunchError):
        launcher.launch_isolated(kind, [], environment=environment)
    runner.assert_not_called()


def test_owner_packet_rejects_unknown_before_packet_or_reader(monkeypatch):
    import io
    from agent import forward_media_owner_packet as packet
    monkeypatch.setattr(owner, 'os', SimpleNamespace(environ={**OWNER, 'GOOGLE_DRIVE_SA_JSON': 'sentinel'}))
    read = Mock()
    output = io.StringIO()
    code, result = packet.run(['--packet', '/missing'], reader_factory=read, out=output)
    assert code == 2 and result == {'ok': False, 'reason': 'env_guard'}
    assert 'sentinel' not in output.getvalue()
    read.assert_not_called()


def test_owner_connection_rejects_unknown_before_driver(monkeypatch):
    monkeypatch.setattr(owner, 'os', SimpleNamespace(environ={**OWNER, 'UNLISTED_ALIAS': ''}))
    with pytest.raises(owner.EnvironmentGuardError):
        owner.ForwardMediaOwnerPersistence.connect_from_environment()


def test_direct_attester_clean_connection_keeps_exact_role_check(monkeypatch):
    import sys
    env = dict(ATTESTER)
    monkeypatch.setattr(guard, 'os', SimpleNamespace(environ=env, getenv=env.get))
    conn = Mock()
    cursor = Mock()
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    cursor.fetchone.return_value = (guard.ROLE,)
    conn.cursor.return_value = cursor
    driver = Mock(connect=Mock(return_value=conn))
    monkeypatch.setitem(sys.modules, 'psycopg', driver)
    assert guard._connect() is conn
    driver.connect.assert_called_once_with(ATTESTER['AGENT_FORWARD_MEDIA_ATTESTER_DSN'],
                                           options=guard.DB_DEADLINE_OPTIONS)
    cursor.execute.assert_called_once_with('select current_user')
    cursor.fetchone.return_value = ('service_role',)
    with pytest.raises(guard.ForwardMediaVerificationHold, match='role mismatch'):
        guard._connect()
    conn.close.assert_called_once()
