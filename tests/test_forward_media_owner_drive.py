"""Real owner factory + Google credentials; offline Drive HTTP responses only."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from agent import forward_media_owner as owner, forward_media_owner_worker as worker
from agent import forward_media_owner_drive as drive
from agent import forward_media_owner_transport as transport_module
from agent.forward_media_source_verifier import OriginalDriveReader, verify_source
from tests.test_forward_media_source_verifier import DATA, FILE, FOLDER, URL, snapshot


@pytest.fixture
def owner_environment(monkeypatch):
    for key in owner.forbidden_credential_names(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_DSN', 'postgres://isolated-owner@example.invalid/db')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_ROLE', 'isolated_owner')
    monkeypatch.setenv(worker.WORKER_ENV, 'true')
    monkeypatch.setenv(worker.TENANTS_ENV, 'gym')
    class ProcessEnvironment:
        @property
        def environ(self):
            return {k:v for k,v in os.environ.items() if k != 'PYTEST_CURRENT_TEST'}
        getenv = staticmethod(os.getenv)
    monkeypatch.setattr(owner, 'os', ProcessEnvironment())


@pytest.fixture
def service_account_json():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return json.dumps({'type':'service_account', 'project_id':'offline-test',
        'private_key_id':'synthetic', 'private_key':key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode(),
        'client_email':'owner-reader@offline-test.iam.gserviceaccount.com',
        'token_uri':drive.TOKEN_URI})


def test_real_worker_factory_reads_original_with_dedicated_readonly_credentials(
        owner_environment, service_account_json, monkeypatch):
    import googleapiclient.discovery
    monkeypatch.setenv(drive.DRIVE_CREDENTIAL_ENV, service_account_json)
    events = []
    class Request:
        def __init__(self, value): self.value = value
        def execute(self, *, num_retries):
            assert num_retries == 0
            return self.value
    class DownloadHttp:
        def request(self, uri, method, **kwargs):
            assert uri == 'https://www.googleapis.com/drive/v3/files/offline?alt=media'
            assert method == 'GET'
            events.append('download')
            class Response(dict):
                status = 200
            return Response({'content-length':str(len(DATA))}), DATA
    class Files:
        def get(self, *, fileId, fields, supportsAllDrives):
            events.append('metadata')
            if fileId == FOLDER:
                return Request({'id':FOLDER,'mimeType':'application/vnd.google-apps.folder',
                    'trashed':False,'version':'1','parents':[]})
            assert fileId == FILE
            return Request({'id':FILE,'mimeType':'image/png','trashed':False,'version':'3',
                'parents':[FOLDER],'size':str(len(DATA)),'md5Checksum':hashlib.md5(DATA).hexdigest()})
        def get_media(self, *, fileId, supportsAllDrives):
            assert fileId == FILE and supportsAllDrives
            return SimpleNamespace(http=DownloadHttp(),
                uri='https://www.googleapis.com/drive/v3/files/offline?alt=media', headers={})
    def build(api, version, *, http, cache_discovery):
        assert (api,version,cache_discovery) == ('drive','v3',False)
        # Credentials and AuthorizedHttp are REAL; only Drive HTTP is offline.
        assert http.credentials.scopes == [drive.DRIVE_READ_SCOPE]
        assert http.credentials.service_account_email == 'owner-reader@offline-test.iam.gserviceaccount.com'
        assert http.credentials._subject is None
        assert http.http.timeout == 30
        assert http._max_refresh_attempts == 0
        return SimpleNamespace(_http=http, files=lambda:Files())
    monkeypatch.setattr(googleapiclient.discovery, 'build', build)
    connection = SimpleNamespace(closed=False)
    connection.close = lambda:setattr(connection,'closed',True)
    persistence = SimpleNamespace(_conn=connection)
    class Hosted:
        def read(self, url):
            assert url == URL
            return DATA
    monkeypatch.setattr(owner, 'HostedObjectReader', Hosted)
    monkeypatch.setattr(owner.ForwardMediaOwnerPersistence, 'connect_from_environment',
        lambda **kwargs:persistence)
    monkeypatch.setattr(transport_module, 'DedicatedOwnerTransport', lambda value:object())
    def adapter(*, drive_reader, reader, **kwargs):
        assert type(drive_reader) is OriginalDriveReader
        verified = verify_source(snapshot(), drive_reader, reader)
        assert verified.source_bytes == DATA
        return {'status':'complete','rows':[]}
    monkeypatch.setattr(worker, 'run_adapter', adapter)
    assert worker.run_once() == {'status':'complete','rows':[]}
    assert connection.closed and events.count('download') == 1
    assert events.count('metadata') == 4


@pytest.mark.parametrize('credential', ['', '/tmp/not-a-credential', '{invalid',
    json.dumps({'type':'authorized_user','token_uri':drive.TOKEN_URI}),
    json.dumps({'type':'service_account','token_uri':'https://attacker.invalid/token'}),
    json.dumps({'type':'service_account','token_uri':drive.TOKEN_URI,'subject':'delegated'}),
    json.dumps({'type':'service_account','token_uri':drive.TOKEN_URI,'universe_domain':'other'}),
    'x'*65537])
def test_missing_or_invalid_dedicated_credential_is_static_hold(owner_environment, monkeypatch, credential):
    monkeypatch.setenv(drive.DRIVE_CREDENTIAL_ENV, credential)
    with pytest.raises(drive.OwnerDriveHold, match='^owner_drive_read_transport_unavailable$'):
        drive.DedicatedOwnerDriveTransport()._service()


@pytest.mark.parametrize('name', ['GOOGLE_DRIVE_SA_JSON','AGENT_GDRIVE_SA_JSON',
    'GOOGLE_APPLICATION_CREDENTIALS','ZERNIO_API_KEY','SUPABASE_SERVICE_ROLE_KEY'])
def test_shared_credentials_rejected_before_real_worker_factories(owner_environment, monkeypatch, name):
    monkeypatch.setenv(name, '')
    assert worker.run_once() == {'status':'hold','reason':'owner_environment_invalid','rows':[]}


def test_dedicated_credential_rejected_by_attester(owner_environment):
    from agent.forward_media_lane import unknown_environment_names
    assert unknown_environment_names({drive.DRIVE_CREDENTIAL_ENV:'synthetic'},'attester') == [drive.DRIVE_CREDENTIAL_ENV]


def test_cached_transport_still_rejects_new_shared_credentials(owner_environment, monkeypatch):
    transport = drive.DedicatedOwnerDriveTransport()
    transport._svc = object()
    monkeypatch.setenv('AGENT_GDRIVE_SA_JSON', '')
    with pytest.raises(owner.EnvironmentGuardError):
        transport._service()
