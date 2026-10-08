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
