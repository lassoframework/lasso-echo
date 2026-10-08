import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.source_brand_collector import read_zernio_identity_readonly
from agent.source_brand_ingest import CaptureIngestError


GYM = 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'
KEY = 'swiftrivercrossfite5c9db'
PROFILE = 'stored-profile-123'
PLATFORM_ID = '17841400000000001'
HANDLE = 'swiftrivercrossfit'
NOW = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)


def account(**changes):
    row = {'_id': 'zernio-internal-account', 'profileId': PROFILE,
        'platform': 'instagram', 'platformUserId': PLATFORM_ID,
        'username': HANDLE, 'status': 'connected'}
    row.update(changes)
    return row


class PortalReads:
    def __init__(self, *, token_rows=None, profile_rows=None):
        self.calls = []
        self.token_rows = token_rows if token_rows is not None else [
            {'gym_id': GYM, 'echo_account_key': KEY}]
        self.profile_rows = profile_rows if profile_rows is not None else [
            {'gym_id': GYM, 'zernio_profile_id': PROFILE}]

    def __call__(self, table, params):
        self.calls.append((table, params))
        if table == 'echo_intake_tokens':
            return self.token_rows
        if table == 'echo_gym_settings':
            return self.profile_rows
        raise AssertionError('unexpected portal table')


class ZernioGetOnly:
    def __init__(self, accounts):
        self.raw = json.dumps({'accounts': accounts}, separators=(',', ':')).encode()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, content=self.raw)

    def post(self, *_args, **_kwargs):
        raise AssertionError('identity lookup must not POST')


def test_lookup_returns_minimal_fresh_identity_and_hash_without_writes():
    portal = PortalReads()
    zernio = ZernioGetOnly([account()])
    result = read_zernio_identity_readonly(GYM, KEY, read_rows=portal,
        environ={'ZERNIO_API_KEY': 'synthetic-secret'}, http=zernio,
        now=lambda: NOW)

    assert result == {'profile_id': PROFILE, 'platform_user_id': PLATFORM_ID,
        'handle': HANDLE, 'observed_at': NOW.isoformat(),
        'response_sha256': hashlib.sha256(zernio.raw).hexdigest()}
    assert set(result) == {'profile_id', 'platform_user_id', 'handle',
        'observed_at', 'response_sha256'}
    assert [table for table, _ in portal.calls] == [
        'echo_intake_tokens', 'echo_gym_settings']
    assert all(params['gym_id'] == 'eq.' + GYM for _, params in portal.calls)
    assert zernio.calls == [('https://api.zernio.com/v1/accounts', {
        'params': {'profileId': PROFILE},
        'headers': {'Authorization': 'Bearer synthetic-secret'},
        'timeout': 30, 'allow_redirects': False})]


def test_tenant_or_profile_mismatch_fails_before_zernio_request():
    zernio = ZernioGetOnly([account()])
    with pytest.raises(CaptureIngestError, match='current_tenant_mapping_mismatch'):
        read_zernio_identity_readonly(GYM, KEY, read_rows=PortalReads(token_rows=[]),
            environ={'ZERNIO_API_KEY': 'synthetic-secret'}, http=zernio, now=lambda: NOW)
    assert zernio.calls == []

    portal = PortalReads(profile_rows=[{'gym_id': '00000000-0000-0000-0000-000000000001',
                                        'zernio_profile_id': PROFILE}])
    with pytest.raises(CaptureIngestError, match='exact_zernio_profile_mapping_required'):
        read_zernio_identity_readonly(GYM, KEY, read_rows=portal,
            environ={'ZERNIO_API_KEY': 'synthetic-secret'}, http=zernio, now=lambda: NOW)
    assert zernio.calls == []


@pytest.mark.parametrize('rows', [
    [], [account(), account(_id='second-account')],
    [account(platformUserId='not-numeric')],
    [account(profileId='other-profile')],
    [account(username='wrong-handle', metadata={'profileData': {'username': HANDLE}})],
])
def test_incomplete_or_ambiguous_identity_fails_closed(rows):
    zernio = ZernioGetOnly(rows)
    with pytest.raises(CaptureIngestError):
        read_zernio_identity_readonly(GYM, KEY, read_rows=PortalReads(),
            environ={'ZERNIO_API_KEY': 'synthetic-secret'}, http=zernio, now=lambda: NOW)
    assert len(zernio.calls) == 1


def test_paged_response_is_not_treated_as_complete_identity():
    zernio = ZernioGetOnly([account()])
    zernio.raw = json.dumps({'accounts': [account()], 'hasMore': True}).encode()
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_incomplete'):
        read_zernio_identity_readonly(GYM, KEY, read_rows=PortalReads(),
            environ={'ZERNIO_API_KEY': 'synthetic-secret'}, http=zernio, now=lambda: NOW)


@pytest.mark.parametrize('malformed', [[], ''])
@pytest.mark.parametrize('field', ['metadata', 'profileData'])
def test_present_falsy_malformed_metadata_fails_closed(field, malformed):
    row = account()
    if field == 'metadata':
        row['metadata'] = malformed
    else:
        row['metadata'] = {'profileData': malformed}
    zernio = ZernioGetOnly([row])

    with pytest.raises(CaptureIngestError, match='independent_social_id_evidence_missing'):
        read_zernio_identity_readonly(GYM, KEY, read_rows=PortalReads(),
            environ={'ZERNIO_API_KEY': 'synthetic-secret'}, http=zernio, now=lambda: NOW)
