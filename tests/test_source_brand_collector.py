from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import hashlib
import pytest
from agent.source_brand_collector import (PortalMappingResolver, ServerMapping,
    TrustedSourceCollector, CollectorPortalReader, build_collector)
from agent.source_brand_ingest import CaptureIngestError

GYM = 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'
KEY = 'swiftrivercrossfite5c9db'
URL = 'https://swiftrivercrossfit.com/'
NOW = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)

def authority(**kwargs):
    return replace(ServerMapping(GYM, KEY, (URL,), ('synthetic-domain-proof',),
        'synthetic-operator-approval', '2026-10-09T00:00:00Z', 'swiftrivercrossfit'), **kwargs)

def resolver(entry=None, rows=None):
    rows = rows if rows is not None else {
        'echo_intake_tokens': [{'gym_id': GYM, 'echo_account_key': KEY}],
        'echo_social_connections': [{'gym_id': GYM, 'platform': 'instagram',
            'state': 'connected', 'handle': 'swiftrivercrossfit',
            'last_verified_at': '2026-10-08T00:00:00Z'}]}
    def read(table, params):
        assert params['gym_id'] == 'eq.' + GYM
        return rows[table]
    return PortalMappingResolver(read_rows=read, approved_mappings=[entry or authority()],
        now=lambda: NOW), rows

@pytest.mark.parametrize('entry,code', [
    (authority(approval_receipt=''), 'mapping_approval_missing'),
    (authority(domain_evidence=()), 'mapping_approval_missing'),
    (authority(valid_until='2026-10-07T00:00:00Z'), 'mapping_approval_expired'),
    (authority(website_urls=('http://wrong.example/',)), 'mapping_domain_invalid'),
    (authority(instagram_owner_id='123', owner_id_evidence='response',
        owner_id_evidence_source='apify'), 'independent_social_id_evidence_missing'),
])
def test_missing_authority_holds(entry, code):
    r, _ = resolver(entry)
    with pytest.raises(CaptureIngestError, match=code): r(GYM)

def test_unknown_tenant_never_name_guesses():
    r, _ = resolver()
    with pytest.raises(CaptureIngestError, match='exact_tenant_domain_mapping_missing'):
        r('11111111-1111-1111-1111-111111111111')
    with pytest.raises(CaptureIngestError, match='mapping_gym_invalid'): r('swift river')

@pytest.mark.parametrize('table,field,value,code', [
    ('echo_intake_tokens', 'echo_account_key', 'other', 'current_tenant_mapping_mismatch'),
    ('echo_intake_tokens', 'gym_id', 'other', 'current_tenant_mapping_mismatch'),
    ('echo_social_connections', 'handle', 'other', 'current_social_mapping_mismatch'),
    ('echo_social_connections', 'gym_id', 'other', 'current_social_mapping_mismatch'),
    ('echo_social_connections', 'state', 'not_connected', 'current_social_mapping_mismatch'),
])
def test_live_changed_mapping_holds(table, field, value, code):
    r, rows = resolver()
    rows[table][0][field] = value
    with pytest.raises(CaptureIngestError, match=code): r(GYM)

def test_duplicate_mapping_holds():
    r, rows = resolver()
    rows['echo_social_connections'] *= 2
    with pytest.raises(CaptureIngestError, match='social_mapping_ambiguous'): r(GYM)

def test_revision_binds_domain_social_owner_and_live_verification():
    r, rows = resolver()
    before = r(GYM)
    rows['echo_social_connections'][0]['last_verified_at'] = '2026-10-08T00:01:00Z'
    assert r(GYM).mapping_revision != before.mapping_revision
    r2, _ = resolver(authority(website_urls=('https://different.example/',)))
    assert r2(GYM).mapping_revision != before.mapping_revision
    r3, _ = resolver(authority(instagram_owner_id='123', owner_id_evidence='synthetic-proof',
        owner_id_evidence_source='meta_authenticated_account'))
    assert r3(GYM).provider_account_id == '123'
    assert r3(GYM).mapping_revision != before.mapping_revision

def test_disabled_before_any_calls_and_empty_default_authority():
    def never(*a, **k): raise AssertionError('unexpected I/O')
    c = TrustedSourceCollector(resolver=never, environ={}, website_factory=never)
    with pytest.raises(CaptureIngestError, match='source_collector_disabled'):
        c.collect_website(GYM, URL)
    c = build_collector(environ={'ECHO_SOURCE_COLLECTOR_ENABLED': 'true'})
    with pytest.raises(CaptureIngestError, match='exact_tenant_domain_mapping_missing'):
        c._resolve(GYM)

@pytest.mark.parametrize('owner', [False, True])
def test_social_hold_never_calls_provider(owner):
    entry = authority(instagram_owner_id='123', owner_id_evidence='synthetic-proof',
        owner_id_evidence_source='meta_authenticated_account') if owner else authority()
    r, _ = resolver(entry)
    c = TrustedSourceCollector(resolver=r, environ={'ECHO_SOURCE_COLLECTOR_ENABLED': 'true'})
    code = 'authenticated_provider_response_identity_missing' if owner else 'independent_social_id_evidence_missing'
    with pytest.raises(CaptureIngestError, match=code): c.collect_social(GYM)

def test_response_receipt_is_bound_and_removed_after_ingest():
    r, rows = resolver()
    raw = b'\xff original bytes'
    digest = hashlib.sha256(raw).hexdigest()
    def factory(mapping, **_kw):
        entry = mapping.entry_for(URL)
        result = SimpleNamespace(gym_id=GYM, echo_account_key=KEY, source_url=URL,
            source_kind='website', status=200, raw_bytes=raw, bytes_sha256=digest,
            mapping_revision=entry.mapping_revision, mapping_evidence=entry.mapping_evidence,
            fetched_at='2026-10-07T00:00:00Z')
        return SimpleNamespace(capture=lambda url: result)
    c = TrustedSourceCollector(resolver=r, environ={'ECHO_SOURCE_COLLECTOR_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_SUPABASE_URL': 'https://synthetic.supabase.co',
        'ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY': 'synthetic-service'}, website_factory=factory)
    def ingest(metadata, actual, **kwargs):
        receipt = kwargs['transport_receipt']
        mapping = r(GYM)
        assert c.authenticate_response(metadata, actual, receipt, mapping)
        assert not c.authenticate_response(metadata, actual, object(), mapping)
        assert not c.authenticate_response(metadata, actual + b'x', receipt, mapping)
        assert not c.authenticate_response(dict(metadata, source_revision='forged'), actual, receipt, mapping)
        rows['echo_social_connections'][0]['last_verified_at'] = '2026-10-08T00:01:00Z'
        assert not c.authenticate_response(metadata, actual, receipt, mapping)
        return {'synthetic': True}
    c._ingest = SimpleNamespace(ingest=ingest, _config=lambda: None)
    assert c.collect_website(GYM, URL) == {'synthetic': True}
    assert c._receipts == {}

def test_unapproved_exact_url_holds_before_fetch():
    r, _ = resolver()
    c = TrustedSourceCollector(resolver=r, environ={'ECHO_SOURCE_COLLECTOR_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_SUPABASE_URL': 'https://synthetic.supabase.co',
        'ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY': 'synthetic-service'},
        website_factory=lambda *a: pytest.fail('unexpected fetch'))
    with pytest.raises(CaptureIngestError, match='website_mapping_mismatch'):
        c.collect_website(GYM, URL + 'unapproved.css')

def test_portal_reader_authenticates_and_disables_redirects():
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: [])
    reader = CollectorPortalReader(environ={'ECHO_SOURCE_COLLECTOR_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_SUPABASE_URL': 'https://synthetic.supabase.co',
        'ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY': 'synthetic-service'},
        http=SimpleNamespace(get=get))
    assert reader('echo_intake_tokens', {'gym_id': 'eq.' + GYM}) == []
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['headers']['Authorization'] == 'Bearer synthetic-service'
    with pytest.raises(CaptureIngestError, match='mapping_table_not_allowed'):
        reader('other', {})
    for unscoped in ({}, {'gym_id': 'neq.' + GYM},
                     {'gym_id': 'eq.' + GYM, 'or': '(gym_id.neq.' + GYM + ')'},
                     {'gym_id': 'eq.not-a-uuid'}):
        with pytest.raises(CaptureIngestError, match='mapping_scope_invalid|mapping_gym_invalid'):
            reader('echo_intake_tokens', unscoped)
    assert len(calls) == 1


def test_stale_live_connection_hold():
    r, rows = resolver()
    rows['echo_social_connections'][0]['last_verified_at'] = '2026-09-01T00:00:00Z'
    with pytest.raises(CaptureIngestError, match='social_mapping_verification_expired'): r(GYM)

def test_storage_config_preflight_before_source_network():
    c = TrustedSourceCollector(resolver=lambda *a: pytest.fail('unexpected lookup'),
        environ={'ECHO_SOURCE_COLLECTOR_ENABLED': 'true',
                 'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true'},
        website_factory=lambda *a: pytest.fail('unexpected source fetch'))
    with pytest.raises(CaptureIngestError, match='collector_service_config_missing'):
        c.collect_website(GYM, URL)


def test_exact_bytes_flow_through_real_ingest_and_readback():
    r, _ = resolver()
    raw = b'\x00\xff exact retained entity bytes'
    digest = hashlib.sha256(raw).hexdigest()
    def factory(domains, **_kw):
        entry = domains.entry_for(URL)
        return SimpleNamespace(capture=lambda url: SimpleNamespace(
            gym_id=GYM, echo_account_key=KEY, source_kind='website',
            source_url=URL, status=200, raw_bytes=raw, bytes_sha256=digest,
            mapping_revision=entry.mapping_revision,
            mapping_evidence=entry.mapping_evidence, fetched_at='2020-01-01T00:00:00Z'))
    class Http:
        stored = None
        writes = 0
        def get(self, url, **kwargs):
            if url.endswith('echo_intake_tokens'):
                rows = [{'gym_id': GYM, 'echo_account_key': KEY}]
            else:
                rows = [self.stored] if self.stored else []
            return SimpleNamespace(status_code=200, json=lambda: rows)
        def post(self, url, **kwargs):
            self.writes += 1
            assert url.endswith('echo_source_captures')
            self.stored = dict(kwargs['json'], bytes_sha256=digest,
                               captured_at='2020-01-01T00:00:01Z')
            return SimpleNamespace(status_code=201)
    http = Http()
    collector = TrustedSourceCollector(resolver=r, environ={
        'ECHO_SOURCE_COLLECTOR_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_SUPABASE_URL': 'https://synthetic.supabase.co',
        'ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY': 'synthetic-service'},
        ingest_http=http, website_factory=factory)
    receipt = collector.collect_website(GYM, URL)
    assert receipt['bytes_sha256'] == digest
    assert http.stored['raw_bytes'] == '\\x' + raw.hex()
    assert http.writes == 1
    assert collector._receipts == {}


def test_cross_call_rate_limit_shared_across_fresh_capture_instances():
    """collect_website builds a fresh capture instance per call, so instance
    level state would bypass the per-host minimum delay. The collector's
    shared gate must hold across calls (defect repro)."""
    r, _ = resolver()
    raw = b'x'
    digest = hashlib.sha256(raw).hexdigest()
    gates = []

    def factory(domains, rate_gate=None, **_kw):
        e = domains.entry_for(URL)
        assert callable(rate_gate)
        gates.append(rate_gate)

        def capture(url):
            # what the real capture loop does per fetch
            rate_gate(url.split('//', 1)[1].split('/')[0])
            return SimpleNamespace(gym_id=GYM, echo_account_key=KEY,
                source_kind='website', source_url=URL, status=200, raw_bytes=raw,
                bytes_sha256=digest, mapping_revision=e.mapping_revision,
                mapping_evidence=e.mapping_evidence,
                fetched_at='2026-10-08T00:00:00Z')
        return SimpleNamespace(capture=capture)

    t = [0.0]
    slept = []
    c = TrustedSourceCollector(resolver=r, environ={
        'ECHO_SOURCE_COLLECTOR_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_SUPABASE_URL': 'https://synthetic.supabase.co',
        'ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY': 'synthetic-service'},
        website_factory=factory, clock=lambda: t[0],
        sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    c._ingest = SimpleNamespace(ingest=lambda *a, **k: {'ok': True},
                                _config=lambda: None)
    c.collect_website(GYM, URL)
    c.collect_website(GYM, URL)   # second call, brand-new capture instance
    assert len(gates) == 2
    assert all(g.__self__ is c for g in gates)  # same collector-owned gate
    assert slept == [pytest.approx(1.0)]


def test_rate_gate_is_keyed_by_host_not_tenant():
    t = [0.0]
    slept = []
    c = TrustedSourceCollector(resolver=lambda *a: None, environ={},
        website_factory=lambda *a, **k: None, clock=lambda: t[0],
        sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    c._rate_gate('gym-one.example')
    c._rate_gate('gym-two.example')   # a different tenant's host: no delay owed
    c._rate_gate('gym-one.example')   # same host again: delay owed
    assert slept == [pytest.approx(1.0)]
    assert list(c._rate_last) == ['gym-one.example', 'gym-two.example']


def test_disconnected_portal_row_without_approved_handle_is_website_mapping():
    r, rows = resolver(authority(instagram_handle=None))
    rows['echo_social_connections'][0].update(state='not_connected', handle=None,
                                              last_verified_at=None)
    mapping = r(GYM)
    assert mapping.website_response_urls == (URL,)
    assert mapping.social_locators == () and mapping.provider_account_id is None


# --- AuthenticatedZernioIdentityReader: health endpoint is the profile authority.

from agent.source_brand_collector import AuthenticatedZernioIdentityReader

ZPROFILE = '6a95fae1cd41729a65136d46'
ZACCOUNT = 'zernio-internal-account'
ZOWNER = '28269859779341790'
ZHANDLE = 'swiftrivercrossfit'


def zhealth(**changes):
    row = {'accountId': ZACCOUNT, 'profileId': ZPROFILE, 'platform': 'instagram',
           'username': ZHANDLE, 'status': 'healthy', 'tokenValid': True,
           'needsReconnect': False, 'canPost': True}
    row.update(changes)
    return row


def zaccount(**changes):
    # Missing profileId remains supported through the health stable-ID join.
    row = {'_id': ZACCOUNT, 'platform': 'instagram',
           'platformUserId': ZOWNER, 'username': ZHANDLE}
    row.update(changes)
    return row


class IdentityReadRows:
    def __init__(self, health):
        self.health = health
        self.attestations = []

    def __call__(self, table, params):
        assert table == 'echo_gym_settings'
        assert params == {'gym_id': 'eq.' + GYM, 'select': 'gym_id,zernio_profile_id'}
        return [{'gym_id': GYM, 'zernio_profile_id': ZPROFILE}]

    def attest_provider(self, status, request_id):
        self.attestations.append((status, request_id))


class IdentityHttp:
    def __init__(self, health, accounts):
        import json as _json
        self.health_raw = _json.dumps({'accounts': health}, separators=(',', ':')).encode()
        self.accounts_raw = _json.dumps({'accounts': accounts}, separators=(',', ':')).encode()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url == 'https://api.zernio.com/v1/accounts/health':
            return SimpleNamespace(status_code=200, content=self.health_raw)
        return SimpleNamespace(status_code=200, content=self.accounts_raw)


def zreader(http, rows=None):
    return AuthenticatedZernioIdentityReader(read_rows=rows or IdentityReadRows(None),
        environ={'ECHO_SOURCE_COLLECTOR_ENABLED': 'true', 'ZERNIO_API_KEY': 'synthetic-secret'},
        http=http, now=lambda: NOW)


def test_identity_reader_health_authority_pairs_account_list():
    rows = IdentityReadRows(None)
    http = IdentityHttp([zhealth()], [zaccount()])
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is True and result['account_id'] == ZACCOUNT
    assert result['platform_user_id'] == ZOWNER and result['handle'] == ZHANDLE
    assert http.calls == ['https://api.zernio.com/v1/accounts/health',
                          'https://api.zernio.com/v1/accounts']
    assert len(rows.attestations) == 1


def test_identity_reader_selects_single_instagram_among_sibling_platform_rows():
    # Live Swift Zernio profile shape: one Facebook, one Google Business and
    # one Instagram row for the exact same profile must not be ambiguous.
    rows = IdentityReadRows(None)
    health = [zhealth(accountId='facebook-account', platform='facebook'),
              zhealth(accountId='gbp-account', platform='google'),
              zhealth()]
    http = IdentityHttp(health, [zaccount()])
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is True and result['account_id'] == ZACCOUNT
    assert result['platform_user_id'] == ZOWNER and result['handle'] == ZHANDLE
    assert http.calls == ['https://api.zernio.com/v1/accounts/health',
                          'https://api.zernio.com/v1/accounts']


@pytest.mark.parametrize('profile_id', [ZPROFILE,
    {'_id': ZPROFILE, 'name': 'Synthetic profile display name'}])
def test_identity_reader_accepts_exact_scalar_or_populated_account_profile(profile_id):
    rows = IdentityReadRows(None)
    accounts = [zaccount(profileId=profile_id),
                zaccount(_id='facebook-account', platform='facebook', profileId=profile_id),
                zaccount(_id='gbp-account', platform='googlebusiness', profileId=profile_id)]
    result = zreader(IdentityHttp([zhealth()], accounts), rows)(GYM, KEY)
    assert result['connected'] is True and result['account_id'] == ZACCOUNT
    assert result['profile_id'] == ZPROFILE and result['platform_user_id'] == ZOWNER
    assert rows.attestations[0][0]['lookup_status'] == 'complete'


@pytest.mark.parametrize('profile_id', [
    {'_id': 'wrong-profile', 'name': 'Synthetic profile display name'},
    {}, {'name': ZPROFILE}, {'_id': None}, {'_id': 123},
    {'_id': {'_id': ZPROFILE}}, [], None, False,
])
def test_identity_reader_conflicting_or_malformed_account_profile_holds(profile_id):
    rows = IdentityReadRows(None)
    http = IdentityHttp([zhealth()], [zaccount(profileId=profile_id)])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    status = rows.attestations[0][0]
    assert status['lookup_status'] == 'partial' and status['instagram'] is None
    assert status['response_sha256'] == hashlib.sha256(http.health_raw).hexdigest()


def test_identity_reader_duplicate_populated_profile_keys_hold():
    import json
    rows = IdentityReadRows(None)
    http = IdentityHttp([zhealth()], [zaccount(profileId={'_id': ZPROFILE})])
    exact = json.dumps({'_id': ZPROFILE}, separators=(',', ':')).encode()
    duplicate = b'{"_id":"wrong-profile","_id":"' + ZPROFILE.encode() + b'"}'
    http.accounts_raw = http.accounts_raw.replace(exact, duplicate)
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'partial'
    assert rows.attestations[0][0]['instagram'] is None


def test_identity_reader_unrelated_malformed_rows_do_not_hold_requested_profile():
    # A row for another exact profile may carry malformed non-identity fields;
    # it cannot hold this gym's ownership decision.
    rows = IdentityReadRows(None)
    unrelated = {'accountId': '', 'profileId': 'other-exact-profile',
                 'platform': '!!!not-a-platform', 'username': 123, 'status': None,
                 'tokenValid': 'yes', 'needsReconnect': 1, 'canPost': {}}
    http = IdentityHttp([unrelated, zhealth()], [zaccount()])
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is True and result['account_id'] == ZACCOUNT
    assert rows.attestations[0][0]['lookup_status'] == 'complete'


@pytest.mark.parametrize('unrelated_account_id', [[], {}])
def test_identity_reader_unrelated_unhashable_account_id_is_ignored(unrelated_account_id):
    rows = IdentityReadRows(None)
    unrelated = zhealth(profileId='other-exact-profile', accountId=unrelated_account_id)
    result = zreader(IdentityHttp([unrelated, zhealth()], [zaccount()]), rows)(GYM, KEY)
    assert result['connected'] is True and result['account_id'] == ZACCOUNT
    assert rows.attestations[0][0]['lookup_status'] == 'complete'


def test_identity_reader_matched_malformed_row_holds():
    rows = IdentityReadRows(None)
    http = IdentityHttp([zhealth(tokenValid='yes')], [zaccount()])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert len(rows.attestations) == 1


@pytest.mark.parametrize('profile_id', [None, 123, ''])
def test_identity_reader_unreadable_profile_identity_holds(profile_id):
    # Absent or non-string profileId could refer to the requested profile, so
    # it fails closed at the profile-identity check even when every other
    # field looks well-formed (including the account ID).
    rows = IdentityReadRows(None)
    http = IdentityHttp([zhealth(profileId=profile_id)], [zaccount()])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert len(rows.attestations) == 1


def test_identity_reader_cross_profile_reuse_of_target_account_holds():
    # The target account ID appearing on another profile's row is conflicting
    # ownership evidence and fails closed, even when that row is otherwise
    # well-formed.
    rows = IdentityReadRows(None)
    http = IdentityHttp(
        [zhealth(), zhealth(profileId='other-exact-profile')], [zaccount()])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert len(rows.attestations) == 1


def test_identity_reader_duplicate_matched_instagram_rows_hold():
    rows = IdentityReadRows(None)
    health = [zhealth(), zhealth(accountId='second-ig-account')]
    http = IdentityHttp(health, [zaccount()])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert len(rows.attestations) == 1


def test_identity_reader_duplicate_instagram_rows_still_hold():
    rows = IdentityReadRows(None)
    health = [zhealth(accountId='facebook-account', platform='facebook'),
              zhealth(), zhealth(accountId='second-ig-account')]
    http = IdentityHttp(health, [zaccount()])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert len(rows.attestations) == 1


@pytest.mark.parametrize('health', [
    [zhealth(status='disconnected')],
    [zhealth(tokenValid=False)], [zhealth(needsReconnect=True)],
    [zhealth(canPost=False)],
])
def test_identity_reader_unhealthy_profile_instagram_is_partial_hold(health):
    rows = IdentityReadRows(None)
    http = IdentityHttp(health, [zaccount()])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert http.calls == ['https://api.zernio.com/v1/accounts/health']
    assert len(rows.attestations) == 1
    status = rows.attestations[0][0]
    assert status['lookup_status'] == 'partial' and status['instagram'] is None
    assert status['authenticated'] is True
    assert status['response_sha256'] == hashlib.sha256(http.health_raw).hexdigest()


def test_identity_reader_complete_digest_binds_both_original_responses():
    def receipt(http):
        rows = IdentityReadRows(None)
        zreader(http, rows)(GYM, KEY)
        return rows.attestations[0][0]

    http = IdentityHttp([zhealth()], [zaccount()])
    original = receipt(http)
    framed = (b'echo:zernio:identity-responses:v1\0'
              + b'/v1/accounts/health\0' + len(http.health_raw).to_bytes(8, 'big') + http.health_raw
              + b'/v1/accounts\0' + len(http.accounts_raw).to_bytes(8, 'big') + http.accounts_raw)
    assert original['response_sha256'] == hashlib.sha256(framed).hexdigest()
    assert original == receipt(IdentityHttp([zhealth()], [zaccount()]))
    # A different healthy authority response and a different numeric owner ID
    # each change the complete receipt while preserving valid identity shape.
    assert receipt(IdentityHttp([zhealth(providerNote='changed')], [zaccount()]))[
        'response_sha256'] != original['response_sha256']
    assert receipt(IdentityHttp([zhealth()], [zaccount(platformUserId='99999')]))[
        'response_sha256'] != original['response_sha256']
    # Exact bytes, including JSON whitespace, are evidence.
    http.health_raw += b'\n'
    assert receipt(http)['response_sha256'] != original['response_sha256']


def test_identity_reader_account_failure_preserves_partial_health_digest():
    rows = IdentityReadRows(None)
    http = IdentityHttp([zhealth()], [zaccount(platformUserId='not-numeric')])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    status = rows.attestations[0][0]
    assert status['lookup_status'] == 'partial' and status['instagram'] is None
    assert status['response_sha256'] == hashlib.sha256(http.health_raw).hexdigest()


def test_identity_response_digest_frames_boundaries_and_response_roles():
    from agent.source_brand_collector import _zernio_identity_response_sha256
    # The concatenated bytes are identical; endpoint-specific length framing
    # must still bind which exact bytes came from which response.
    assert _zernio_identity_response_sha256(b'a', b'bc') != (
        _zernio_identity_response_sha256(b'ab', b'c'))
    assert _zernio_identity_response_sha256(b'a', b'b') != (
        _zernio_identity_response_sha256(b'b', b'a'))


@pytest.mark.parametrize('health,accounts', [
    ([zhealth(), zhealth(accountId='second-account')], [zaccount()]),
    ([zhealth()], [zaccount(), zaccount()]),
    ([zhealth()], [zaccount(username='someone-else')]),
    ([zhealth()], []),
    ([{'accountId': ZACCOUNT}], [zaccount()]),
])
def test_identity_reader_ambiguous_or_mismatched_holds(health, accounts):
    rows = IdentityReadRows(None)
    http = IdentityHttp(health, accounts)
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert len(rows.attestations) == 1


# --- Conservative authenticated absence proof for website-only capture.

class AbsenceHttp(IdentityHttp):
    def __init__(self, health, accounts, profile='default', status=200):
        super().__init__(health, accounts)
        import json as _json
        if profile == 'default':
            profile = {'_id': ZPROFILE, 'name': 'Synthetic profile display name'}
        self.profile_raw = (profile if type(profile) is bytes else
            _json.dumps(profile, separators=(',', ':')).encode())
        self.profile_status = status
        self.params = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        self.params.append(kwargs.get('params'))
        if url == 'https://api.zernio.com/v1/accounts/health':
            return SimpleNamespace(status_code=200, content=self.health_raw)
        if url == 'https://api.zernio.com/v1/profiles/' + ZPROFILE:
            return SimpleNamespace(status_code=self.profile_status, content=self.profile_raw)
        return SimpleNamespace(status_code=200, content=self.accounts_raw)


SIBLINGS = [zhealth(accountId='facebook-account', platform='facebook'),
            zhealth(accountId='gbp-account', platform='google')]


def test_identity_reader_proven_absence_writes_complete_negative():
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [zaccount(_id='facebook-account', platform='facebook'),
                                  zaccount(_id='gbp-account', platform='googlebusiness')])
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is False
    assert result['account_id'] is None and result['handle'] is None
    assert result['platform_user_id'] is None and result['profile_id'] == ZPROFILE
    assert http.calls == ['https://api.zernio.com/v1/accounts/health',
                          'https://api.zernio.com/v1/profiles/' + ZPROFILE,
                          'https://api.zernio.com/v1/accounts']
    # The negative listing is requested complete: over-limit and hidden rows
    # included, and no status filter is ever sent.
    assert http.params[2] == {'profileId': ZPROFILE, 'includeOverLimit': 'true',
                              'excludeHidden': 'false'}
    status = rows.attestations[0][0]
    assert status['lookup_status'] == 'complete' and status['authenticated'] is True
    assert status['instagram'] == {'connected': False, 'account_id': None,
                                   'platform_user_id': None, 'handle': None}
    framed = (b'echo:zernio:absence-responses:v1\0'
              + b'/v1/profiles/{profileId}\0' + len(http.profile_raw).to_bytes(8, 'big') + http.profile_raw
              + b'/v1/accounts/health\0' + len(http.health_raw).to_bytes(8, 'big') + http.health_raw
              + b'/v1/accounts\0' + len(http.accounts_raw).to_bytes(8, 'big') + http.accounts_raw)
    assert status['response_sha256'] == hashlib.sha256(framed).hexdigest()


def test_identity_reader_proven_absence_accepts_wrapped_profile_and_empty_health():
    rows = IdentityReadRows(None)
    http = AbsenceHttp([], [], profile={'profile': {'_id': ZPROFILE}})
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is False
    assert rows.attestations[0][0]['lookup_status'] == 'complete'


@pytest.mark.parametrize('account', [
    zaccount(status='overLimit'),
    zaccount(_id='hidden-ig', hidden=True),
    zaccount(_id='unhealthy-ig', status='expired'),
])
def test_identity_reader_overlimit_hidden_or_unhealthy_instagram_row_holds(account):
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [zaccount(_id='facebook-account', platform='facebook'),
                                  account])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    status = rows.attestations[0][0]
    assert status['lookup_status'] == 'partial' and status['instagram'] is None
    assert status['response_sha256'] == hashlib.sha256(http.health_raw).hexdigest()


def test_identity_reader_health_instagram_row_contradicts_absent_listing():
    # An unhealthy Instagram row in the health authority is present evidence:
    # hold even when the account listing shows no Instagram row.
    rows = IdentityReadRows(None)
    http = AbsenceHttp([zhealth(status='disconnected')], [])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert http.calls == ['https://api.zernio.com/v1/accounts/health']
    assert rows.attestations[0][0]['lookup_status'] == 'partial'


@pytest.mark.parametrize('profile', [
    {'_id': 'wrong-profile'}, {'name': 'no identity'}, {'_id': None}, [],
    {'profile': {'_id': 'wrong-profile'}}, {'profile': {'_id': ZPROFILE}, 'data': {'_id': ZPROFILE}},
    b'{"_id":"' + ZPROFILE.encode() + b'","_id":"other"}',
    'status-not-200',
])
def test_identity_reader_stale_partial_or_ambiguous_profile_holds(profile):
    rows = IdentityReadRows(None)
    status = 404 if profile == 'status-not-200' else 200
    if status != 200:
        profile = 'default'
    http = AbsenceHttp(SIBLINGS, [], profile=profile, status=status)
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'partial'


def test_identity_reader_paginated_absence_listing_holds():
    import json
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [])
    http.accounts_raw = json.dumps({'accounts': [], 'hasMore': True}).encode()
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'partial'


def test_identity_reader_health_summary_total_contradicting_empty_list_holds():
    # Reproduction: health summary.total=1 with accounts=[] must not yield a
    # connected=false complete negative; the count metadata contradicts the
    # delivered rows, so the lookup holds.
    import json
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [])
    http.health_raw = json.dumps(
        {'summary': {'total': 1}, 'accounts': []}, separators=(',', ':')).encode()
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert http.calls == ['https://api.zernio.com/v1/accounts/health']
    # Health-authority contradictions hold before any partial receipt exists.
    assert rows.attestations[0][0]['lookup_status'] == 'unavailable'


@pytest.mark.parametrize('health_body', [
    {'total': 1, 'accounts': []},
    {'total': 0, 'accounts': [zhealth()]},
    {'total': '1', 'accounts': []},
    {'count': -1, 'accounts': []},
    {'summary': 'not-a-dict', 'accounts': []},
])
def test_identity_reader_incoherent_health_count_metadata_holds(health_body):
    import json
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [])
    http.health_raw = json.dumps(health_body, separators=(',', ':')).encode()
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'unavailable'


def test_identity_reader_absence_listing_total_contradiction_holds():
    import json
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [zaccount(_id='facebook-account', platform='facebook')])
    http.accounts_raw = json.dumps(
        {'total': 2, 'accounts': [zaccount(_id='facebook-account', platform='facebook')]},
        separators=(',', ':')).encode()
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'partial'


def test_identity_reader_coherent_count_metadata_preserves_negative_and_positive():
    # Coherent metadata is accepted: an exact summary.total of zero preserves
    # the proven absence, and a matching positive total preserves identity.
    import json
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [zaccount(_id='facebook-account', platform='facebook'),
                                  zaccount(_id='gbp-account', platform='googlebusiness')])
    http.health_raw = json.dumps(
        {'summary': {'total': 2}, 'accounts': SIBLINGS}, separators=(',', ':')).encode()
    http.accounts_raw = json.dumps(
        {'total': 2, 'accounts': [zaccount(_id='facebook-account', platform='facebook'),
                                  zaccount(_id='gbp-account', platform='googlebusiness')]},
        separators=(',', ':')).encode()
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is False and result['profile_id'] == ZPROFILE
    rows = IdentityReadRows(None)
    http = IdentityHttp([zhealth()], [zaccount()])
    http.health_raw = json.dumps(
        {'total': 1, 'accounts': [zhealth()]}, separators=(',', ':')).encode()
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is True and result['handle'] == ZHANDLE


@pytest.mark.parametrize('health_body', [
    {'summary': {'total': None}, 'accounts': []},
    {'total': None, 'accounts': []},
    {'count': None, 'accounts': []},
])
def test_identity_reader_present_null_count_metadata_holds(health_body):
    # Reproduction: {"summary":{"total":null},"accounts":[]} is a present
    # malformed count claim, not absent metadata. It must not prove a
    # connected=false complete negative; the lookup holds.
    import json
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [])
    http.health_raw = json.dumps(health_body, separators=(',', ':')).encode()
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'unavailable'


def test_identity_reader_absence_listing_present_null_summary_holds():
    # The same present-null count claim in the absence listing holds instead
    # of proving the complete zero-Instagram listing required for a negative.
    import json
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [])
    http.accounts_raw = json.dumps(
        {'summary': {'total': None}, 'accounts': []}, separators=(',', ':')).encode()
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'partial'


@pytest.mark.parametrize('profile', [
    {'profile': {'_id': ZPROFILE}, 'data': {'_id': 123}},
    {'profile': {'_id': ZPROFILE}, 'data': {'_id': None}},
    {'profile': {'_id': ZPROFILE}, 'data': {'_id': []}},
    {'profile': {'_id': ZPROFILE}, 'data': {}},
    {'profile': {'_id': ZPROFILE}, 'data': None},
    {'profile': {'_id': ZPROFILE}, 'data': 'not-a-wrapper'},
    {'_id': ZPROFILE, 'profile': {'_id': 123}},
])
def test_identity_reader_malformed_secondary_envelope_identity_holds(profile):
    # Reproduction: a valid profile wrapper plus a malformed secondary
    # data._id (numeric/null/list/absent/null wrapper) is a present malformed
    # identity claim. It must hold, never silently complete a connected=false
    # proof from the single readable wrapper identity.
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [], profile=profile)
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'partial'


@pytest.mark.parametrize('profile', [
    {'_id': ZPROFILE},
    {'profile': {'_id': ZPROFILE}},
    {'data': {'_id': ZPROFILE}},
])
def test_identity_reader_valid_single_identity_and_count_complete(profile):
    # Valid single identity claims with coherent count metadata still
    # complete the conservative negative; absent optional metadata (no
    # summary wrapper) remains accepted.
    import json
    rows = IdentityReadRows(None)
    listing = [zaccount(_id='facebook-account', platform='facebook'),
               zaccount(_id='gbp-account', platform='googlebusiness')]
    http = AbsenceHttp(SIBLINGS, listing, profile=profile)
    http.accounts_raw = json.dumps(
        {'summary': {'total': 2}, 'accounts': listing}, separators=(',', ':')).encode()
    result = zreader(http, rows)(GYM, KEY)
    assert result['connected'] is False and result['profile_id'] == ZPROFILE
    assert rows.attestations[0][0]['lookup_status'] == 'complete'


@pytest.mark.parametrize('profile', [
    {'_id': ZPROFILE, 'profile': {'_id': 'other-exact-profile'}},
    {'_id': 'other-exact-profile', 'profile': {'_id': ZPROFILE}},
    {'_id': ZPROFILE, 'data': {'_id': 'other-exact-profile'}},
    {'_id': ZPROFILE, 'profile': {'_id': ZPROFILE}},
])
def test_identity_reader_conflicting_profile_envelope_identity_holds(profile):
    # Reproduction: a matching top-level profile _id must not complete when a
    # nested profile/data envelope carries a second (conflicting or merely
    # duplicated) readable identity.
    rows = IdentityReadRows(None)
    http = AbsenceHttp(SIBLINGS, [], profile=profile)
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        zreader(http, rows)(GYM, KEY)
    assert rows.attestations[0][0]['lookup_status'] == 'partial'


def test_identity_reader_absence_negative_contradicts_connected_portal_row():
    # A proven-absence identity never waives a live connected portal row:
    # the tenant contradiction holds at the resolver.
    evidence = {'gym_id': GYM, 'echo_account_key': KEY,
                'source': 'zernio_authenticated_accounts', 'connected': False,
                'account_id': None, 'platform_user_id': None, 'handle': None}
    r, rows = resolver()
    read = lambda table, params: rows[table]
    r = PortalMappingResolver(read_rows=read, approved_mappings=[authority()],
        now=lambda: NOW, social_identity_reader=lambda gym_id, key: evidence)
    with pytest.raises(CaptureIngestError, match='current_social_mapping_mismatch'):
        r(GYM)
