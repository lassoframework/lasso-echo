import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.source_brand_collector import read_zernio_identity_readonly
from agent.source_brand_ingest import CaptureIngestError


GYM = 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'
KEY = 'swiftrivercrossfite5c9db'
PROFILE = '6a95fae1cd41729a65136d46'
ACCOUNT_ID = 'zernio-internal-account'
PLATFORM_ID = '28269859779341790'
HANDLE = 'swiftrivercrossfit'
NOW = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)

HEALTH_URL = 'https://api.zernio.com/v1/accounts/health'
ACCOUNTS_URL = 'https://api.zernio.com/v1/accounts'


def health_row(**changes):
    row = {'accountId': ACCOUNT_ID, 'profileId': PROFILE, 'platform': 'instagram',
           'username': HANDLE, 'status': 'healthy', 'tokenValid': True,
           'needsReconnect': False, 'canPost': True}
    row.update(changes)
    return row


def account(**changes):
    # Missing profileId remains supported through the health stable-ID join.
    row = {'_id': ACCOUNT_ID, 'platform': 'instagram',
           'platformUserId': PLATFORM_ID, 'username': HANDLE}
    row.update(changes)
    return row


def other_health_rows(count=47):
    rows = []
    for i in range(count):
        rows.append({'accountId': 'other-account-%d' % i,
                     'profileId': 'other-profile-%d' % i,
                     'platform': 'facebook' if i % 2 else 'instagram',
                     'username': 'otherhandle%d' % i, 'status': 'healthy',
                     'tokenValid': True, 'needsReconnect': False, 'canPost': True})
    return rows


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
    def __init__(self, accounts, health, *, accounts_raw=None, health_raw=None):
        self.accounts_raw = accounts_raw or json.dumps(
            {'accounts': accounts}, separators=(',', ':')).encode()
        self.health_raw = health_raw or json.dumps(
            {'accounts': health}, separators=(',', ':')).encode()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == HEALTH_URL:
            return SimpleNamespace(status_code=200, content=self.health_raw)
        assert url == ACCOUNTS_URL
        return SimpleNamespace(status_code=200, content=self.accounts_raw)

    def post(self, *_args, **_kwargs):
        raise AssertionError('identity lookup must not POST')


def lookup(zernio, portal=None):
    return read_zernio_identity_readonly(GYM, KEY, read_rows=portal or PortalReads(),
        environ={'ZERNIO_API_KEY': 'synthetic-secret'}, http=zernio, now=lambda: NOW)


def test_lookup_uses_health_authority_and_real_account_shape():
    health = other_health_rows() + [health_row()]
    zernio = ZernioGetOnly([account()], health)
    result = lookup(zernio)

    assert result == {'profile_id': PROFILE, 'account_id': ACCOUNT_ID,
        'platform_user_id': PLATFORM_ID, 'handle': HANDLE,
        'observed_at': NOW.isoformat(),
        'response_sha256': hashlib.sha256(
            b'echo:zernio:identity-responses:v1\0'
            + b'/v1/accounts/health\0' + len(zernio.health_raw).to_bytes(8, 'big') + zernio.health_raw
            + b'/v1/accounts\0' + len(zernio.accounts_raw).to_bytes(8, 'big') + zernio.accounts_raw
        ).hexdigest()}
    assert zernio.calls == [
        (HEALTH_URL, {'headers': {'Authorization': 'Bearer synthetic-secret'},
                      'timeout': 30, 'allow_redirects': False}),
        (ACCOUNTS_URL, {'params': {'profileId': PROFILE},
                        'headers': {'Authorization': 'Bearer synthetic-secret'},
                        'timeout': 30, 'allow_redirects': False})]


def test_readonly_complete_digest_binds_both_original_responses_stably():
    zernio = ZernioGetOnly([account()], [health_row()])
    original = lookup(zernio)
    assert original == lookup(ZernioGetOnly([account()], [health_row()]))
    assert lookup(ZernioGetOnly([account()], [health_row(providerNote='changed')]))[
        'response_sha256'] != original['response_sha256']
    assert lookup(ZernioGetOnly([account(platformUserId='99999')], [health_row()]))[
        'response_sha256'] != original['response_sha256']
    zernio.accounts_raw += b'\n'
    assert lookup(zernio)['response_sha256'] != original['response_sha256']


def test_lookup_selects_single_instagram_among_sibling_platform_rows():
    health = [health_row(accountId='facebook-account', platform='facebook',
                         username='Swift River CrossFit'),
              health_row(accountId='gbp-account', platform='googlebusiness',
                         username='Swift River CrossFit on Google'),
              health_row()]
    zernio = ZernioGetOnly([account()], health)
    result = lookup(zernio)
    assert result['account_id'] == ACCOUNT_ID
    assert result['platform_user_id'] == PLATFORM_ID
    assert result['handle'] == HANDLE


@pytest.mark.parametrize('profile_id', [PROFILE,
    {'_id': PROFILE, 'name': 'Synthetic profile display name'}])
def test_lookup_accepts_exact_scalar_or_populated_account_profile(profile_id):
    accounts = [account(profileId=profile_id),
                account(_id='facebook-account', platform='facebook', profileId=profile_id),
                account(_id='gbp-account', platform='googlebusiness', profileId=profile_id)]
    result = lookup(ZernioGetOnly(accounts, [health_row()]))
    assert result['profile_id'] == PROFILE and result['account_id'] == ACCOUNT_ID
    assert result['platform_user_id'] == PLATFORM_ID and result['handle'] == HANDLE


@pytest.mark.parametrize('profile_id', [
    {'_id': 'wrong-profile', 'name': 'Synthetic profile display name'},
    {}, {'name': PROFILE}, {'_id': None}, {'_id': 123},
    {'_id': {'_id': PROFILE}}, [], None, False,
])
def test_lookup_conflicting_or_malformed_account_profile_holds(profile_id):
    zernio = ZernioGetOnly([account(profileId=profile_id)], [health_row()])
    with pytest.raises(CaptureIngestError, match='authenticated_social_profile_mismatch'):
        lookup(zernio)


def test_lookup_duplicate_populated_profile_keys_hold():
    zernio = ZernioGetOnly([account(profileId={'_id': PROFILE})], [health_row()])
    exact = json.dumps({'_id': PROFILE}, separators=(',', ':')).encode()
    duplicate = b'{"_id":"wrong-profile","_id":"' + PROFILE.encode() + b'"}'
    zernio.accounts_raw = zernio.accounts_raw.replace(exact, duplicate)
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        lookup(zernio)


def test_owner_id_may_live_only_in_metadata():
    row = account()
    del row['platformUserId']
    row['metadata'] = {'platformUserId': PLATFORM_ID,
                       'profileData': {'username': HANDLE}}
    zernio = ZernioGetOnly([row], other_health_rows() + [health_row()])
    assert lookup(zernio)['platform_user_id'] == PLATFORM_ID


def test_tenant_or_profile_mismatch_fails_before_zernio_request():
    zernio = ZernioGetOnly([account()], [health_row()])
    with pytest.raises(CaptureIngestError, match='current_tenant_mapping_mismatch'):
        lookup(zernio, portal=PortalReads(token_rows=[]))
    assert zernio.calls == []

    portal = PortalReads(profile_rows=[{'gym_id': '00000000-0000-0000-0000-000000000001',
                                        'zernio_profile_id': PROFILE}])
    with pytest.raises(CaptureIngestError, match='exact_zernio_profile_mapping_required'):
        lookup(zernio, portal=portal)
    assert zernio.calls == []


@pytest.mark.parametrize('health,accounts,code', [
    # No candidate at all for this profile.
    (other_health_rows(), [account()], 'authenticated_social_identity_ambiguous_or_missing'),
    # Candidate present but disconnected / stale flags are a hold, not identity.
    (other_health_rows() + [health_row(status='disconnected')], [account()],
     'authenticated_social_identity_ambiguous_or_missing'),
    (other_health_rows() + [health_row(tokenValid=False)], [account()],
     'authenticated_social_identity_ambiguous_or_missing'),
    (other_health_rows() + [health_row(needsReconnect=True)], [account()],
     'authenticated_social_identity_ambiguous_or_missing'),
    (other_health_rows() + [health_row(canPost=False)], [account()],
     'authenticated_social_identity_ambiguous_or_missing'),
    # Two Instagram rows for the same profile are ambiguous, even alongside
    # legitimate sibling platform rows for that profile.
    (other_health_rows() + [health_row(), health_row(accountId='second-account')],
     [account()], 'authenticated_social_identity_ambiguous_or_missing'),
    ([health_row(accountId='facebook-account', platform='facebook'),
      health_row(), health_row(accountId='second-ig-account')],
     [account()], 'authenticated_social_identity_ambiguous_or_missing'),
    # Duplicate accountId anywhere in the health list.
    (other_health_rows() + [health_row(), health_row(accountId='other-account-0')],
     [account()], 'authenticated_social_status_incomplete'),
    # Malformed health row (missing boolean flag).
    (other_health_rows() + [health_row(canPost='yes')], [account()],
     'authenticated_social_status_incomplete'),
    (other_health_rows() + [health_row(username='Invalid IG Handle')], [account()],
     'authenticated_social_status_incomplete'),
    # Health accountId absent from the account list.
    (other_health_rows() + [health_row()], [],
     'authenticated_social_profile_mismatch'),
    # Duplicate _id in the account list.
    (other_health_rows() + [health_row()], [account(), account()],
     'authenticated_social_identity_ambiguous_or_missing'),
    # Conflicting platform between health authority and account row.
    (other_health_rows() + [health_row()], [account(platform='facebook')],
     'authenticated_social_profile_mismatch'),
    # A present-but-wrong profileId on an account row conflicts.
    (other_health_rows() + [health_row()], [account(profileId='other-profile')],
     'authenticated_social_profile_mismatch'),
    # Handle mismatch between health authority and account row.
    (other_health_rows() + [health_row()], [account(username='wrong-handle')],
     'independent_social_id_evidence_missing'),
    (other_health_rows() + [health_row()],
     [account(username='wrong-handle', metadata={'profileData': {'username': HANDLE}})],
     'independent_social_id_evidence_missing'),
    # Missing or non-numeric platform owner ID.
    (other_health_rows() + [health_row()], [account(platformUserId='not-numeric')],
     'independent_social_id_evidence_missing'),
    (other_health_rows() + [health_row()], [account(platformUserId=None)],
     'independent_social_id_evidence_missing'),
])
def test_missing_ambiguous_conflicting_or_stale_identity_fails_closed(health, accounts, code):
    zernio = ZernioGetOnly(accounts, health)
    with pytest.raises(CaptureIngestError, match=code):
        lookup(zernio)


@pytest.mark.parametrize('paged', [
    {'accounts': [health_row()], 'hasMore': True},
    {'accounts': [account()], 'nextCursor': 'abc'},
])
def test_paged_response_is_not_treated_as_complete_identity(paged):
    raw = json.dumps(paged, separators=(',', ':')).encode()
    if 'hasMore' in paged:
        zernio = ZernioGetOnly([account()], [], health_raw=raw)
    else:
        zernio = ZernioGetOnly([], [health_row()], accounts_raw=raw)
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_incomplete'):
        lookup(zernio)


@pytest.mark.parametrize('field', ['metadata', 'profileData'])
@pytest.mark.parametrize('malformed', [[], ''])
def test_present_falsy_malformed_metadata_fails_closed(field, malformed):
    row = account()
    if field == 'metadata':
        row['metadata'] = malformed
    else:
        row['metadata'] = {'profileData': malformed}
    zernio = ZernioGetOnly([row], other_health_rows() + [health_row()])
    with pytest.raises(CaptureIngestError, match='independent_social_id_evidence_missing'):
        lookup(zernio)
