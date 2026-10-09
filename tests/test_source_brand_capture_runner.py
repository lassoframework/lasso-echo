"""Integrated offline transport→exact storage→durable origin authentication."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import copy
import base64
import hashlib
import json
import os
import sqlite3
import uuid

import pytest

from agent.apify_run_capture import SQLiteStartJournal
from agent.source_brand_capture_runner import (CaptureReceiptJournal,
    SourceBrandCaptureRunner, build_capture_runner)
from agent.source_brand_collector import (AuthenticatedZernioIdentityReader,
    CollectorPortalReader, PortalMappingResolver, ServerMapping, TrustedSourceCollector)
from agent.source_brand_ingest import CaptureIngestError

GYM = 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'
KEY = 'swiftrivercrossfite5c9db'
URL = 'https://swiftrivercrossfit.com/'
HANDLE = 'swiftrivercrossfit'
OWNER = '17841400000000001'
PROFILE = 'profile123'
ACTOR = 'actor123'


def environment():
    return {'ECHO_SOURCE_CAPTURE_RUNNER_ENABLED': 'true',
        'ECHO_SOURCE_COLLECTOR_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true',
        'ECHO_SOURCE_SOCIAL_RUN_CAPTURE_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_SUPABASE_URL': 'https://synthetic.supabase.co',
        'ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY': 'synthetic-service',
        'ZERNIO_API_KEY': 'zernio_fixture_secret',
        'ECHO_SOURCE_APIFY_ACTOR_ID': ACTOR, 'ECHO_SOURCE_APIFY_MAX_CHARGE_USD': '1'}


def account(**changes):
    return {'_id': 'internalAccount123', 'profileId': PROFILE, 'platform': 'instagram',
        'platformUserId': OWNER, 'metadata': {'profileData': {'username': HANDLE}}, **changes}


class Storage:
    def __init__(self, connected=True):
        self.rows = {}
        self.posts = []
        self.attestations = []
        self.cache = ([{'gym_id': GYM, 'platform': 'instagram', 'state': 'connected',
            'handle': HANDLE, 'last_verified_at': datetime.now(timezone.utc).isoformat()}]
            if connected else [{'gym_id': GYM, 'platform': 'instagram',
                                'state': 'not_connected', 'handle': None, 'last_verified_at': None}])
        self.corrupt_attestation = False
        self.profile_rows = None
        self.profile_read_error = False
        self.corrupt_capture = False
        self.timeout_after_write = False

    def get(self, url, **kw):
        assert kw['allow_redirects'] is False
        assert kw['headers']['Authorization'] == 'Bearer synthetic-service'
        if url.endswith('echo_intake_tokens'):
            data = [{'gym_id': GYM, 'echo_account_key': KEY}]
        elif url.endswith('echo_social_connections'):
            data = self.cache
        elif url.endswith('echo_gym_settings'):
            if self.profile_read_error:
                raise RuntimeError('synthetic portal lookup failure')
            data = self.profile_rows if self.profile_rows is not None else [{'gym_id': GYM, 'zernio_profile_id': PROFILE}]
        else:
            data = [self.rows[kw['params']['id'][3:]]] if kw['params']['id'][3:] in self.rows else []
            if self.corrupt_capture and data:
                data = [dict(data[0], bytes_sha256='0' * 64)]
        return SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(data))

    def post(self, url, **kw):
        self.posts.append((url, copy.deepcopy(kw)))
        if url.endswith('echo_source_brand_attest_provider'):
            payload = kw['json']
            row = {'id': len(self.attestations) + 1, 'gym_id': payload['p_gym'],
                'echo_account_key': payload['p_key'], 'attestation': payload['p_status'],
                'request_id': payload['p_request'], 'created_at': datetime.now(timezone.utc).isoformat()}
            self.attestations.append(row)
            if self.corrupt_attestation:
                row = dict(row, echo_account_key='other')
            return SimpleNamespace(status_code=200, json=lambda: copy.deepcopy(row))
        row = copy.deepcopy(kw['json'])
        raw = bytes.fromhex(row['raw_bytes'][2:])
        row.update(bytes_sha256=hashlib.sha256(raw).hexdigest(),
                   captured_at=datetime.now(timezone.utc).isoformat())
        self.rows.setdefault(row['id'], row)
        if self.timeout_after_write:
            raise RuntimeError('synthetic transport timeout')
        return SimpleNamespace(status_code=201)


class Zernio:
    def __init__(self, accounts):
        self.accounts = accounts
        self.calls = []
        self.extra = {}
        self.health_rows = None

    @staticmethod
    def _health_row(a):
        # Complete /v1/accounts/health envelope: one healthy row per connected
        # account, always owned by the exact stored profile.
        return {'accountId': a['_id'], 'profileId': PROFILE, 'platform': a['platform'],
                'username': a['metadata']['profileData']['username'], 'status': 'healthy',
                'tokenValid': True, 'needsReconnect': False, 'canPost': True}

    def get(self, url, **kw):
        self.calls.append((url, kw))
        assert kw['headers']['Authorization'] == 'Bearer zernio_fixture_secret'
        assert kw['allow_redirects'] is False
        if url == 'https://api.zernio.com/v1/accounts/health':
            rows = (self.health_rows if self.health_rows is not None
                    else [self._health_row(a) for a in self.accounts])
            raw = json.dumps({'accounts': rows}, indent=2).encode()
            return SimpleNamespace(status_code=200, content=raw)
        assert url == 'https://api.zernio.com/v1/accounts'
        assert kw['params'] == {'profileId': PROFILE}
        raw = json.dumps({'accounts': self.accounts, **self.extra}, indent=2).encode()
        return SimpleNamespace(status_code=200, content=raw)


class Apify:
    def __init__(self, owner=OWNER):
        self.calls = []
        self.owner = owner

    def token(self):
        return 'apify_fixture_secret'

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        if '/items?' in url:
            return json.dumps([{'ownerUsername': HANDLE, 'ownerId': self.owner,
                               'caption': 'Real caption from synthetic source'}], indent=2).encode()
        return json.dumps({'data': {'id': 'run123', 'actId': ACTOR,
            'defaultDatasetId': 'dataset123', 'status': 'SUCCEEDED'}}).encode()


def setup(tmp_path, connected=True, urls=(URL,)):
    os.chmod(tmp_path, 0o700)
    env, storage = environment(), Storage(connected)
    zernio = Zernio([account()] if connected else [])
    reader = CollectorPortalReader(environ=env, http=storage)
    identity = AuthenticatedZernioIdentityReader(read_rows=reader, environ=env, http=zernio)
    entry = ServerMapping(GYM, KEY, urls, ('synthetic-domain-proof',),
        'synthetic-approval', (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        HANDLE if connected else None)
    resolver = PortalMappingResolver(read_rows=reader, approved_mappings=[entry], social_identity_reader=identity)
    journal = CaptureReceiptJournal(tmp_path / 'origin.sqlite3')
    apify = Apify()
    calls = []
    def website(domains, **kwargs):
        from agent.website_source_capture import WebsiteSourceCapture, RawResponse
        def transport(url, pinned_ip, headers, timeout):
            calls.append(url)
            assert pinned_ip == '93.184.216.34'
            raw = b'<html>#123456 #ffffff Real gym evidence</html>'
            return RawResponse(200, {}, [raw[:12], raw[12:]])
        return WebsiteSourceCapture(domains, transport=transport,
            resolver=lambda host: ['93.184.216.34'],
            robots_fetch=lambda url: (200, b'User-agent: *\nAllow: /\n'),
            rate_gate=kwargs['rate_gate'], min_host_delay=0)
    collector = TrustedSourceCollector(resolver=resolver, environ=env, ingest_http=storage,
        website_factory=website, min_host_delay=0, receipt_journal=journal, apify_client=apify,
        apify_journal=SQLiteStartJournal(tmp_path / 'starts.sqlite3'))
    return SourceBrandCaptureRunner(collector=collector, environ=env), storage, zernio, apify, calls


def test_real_apify_ingest_restart_replay_and_observation_hook(tmp_path):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    result = runner.capture(GYM, 'stable-request')
    assert result['source_policy'] == 'website_and_instagram'
    assert len(result['captures']) == len(storage.rows) == 2
    assert [m for m, _ in apify.calls].count('POST') == 1
    assert websites == [URL]
    social = next(r for r in storage.rows.values() if r['source_kind'] == 'social')
    assert social['provider_account_id'] == OWNER
    assert social['provider_response_id'] == 'apify:run:run123:dataset:dataset123'
    assert social['source_url'] == 'https://api.apify.com/v2/datasets/dataset123/items'
    assert storage.attestations and all(r['attestation']['lookup_status'] == 'complete' for r in storage.attestations)
    report = storage.attestations[0]['attestation']
    assert report['instagram']['account_id'] != report['instagram']['platform_user_id']
    uuid.UUID(storage.attestations[0]['request_id'])
    # Fresh collector object and journal instance retain receipt authority.
    runner.collector._journal = CaptureReceiptJournal(tmp_path / 'origin.sqlite3')
    mapping = runner.collector._resolve(GYM)
    for row in storage.rows.values():
        raw = bytes.fromhex(row['raw_bytes'][2:])
        assert runner.collector.authenticate_capture(row, raw, mapping)
        assert not runner.collector.authenticate_capture(dict(row, id=str(uuid.uuid4())), raw, mapping)
        assert not runner.collector.authenticate_capture(row, raw + b'x', mapping)
        assert not runner.collector.authenticate_capture(dict(row, source_revision='forged'), raw, mapping)
    before = len(zernio.calls)
    again = runner.capture(GYM, 'stable-request')
    assert again == result
    assert len(zernio.calls) > before  # replay still requires a fresh authenticated status
    assert [m for m, _ in apify.calls].count('POST') == 1
    assert websites == [URL]


@pytest.mark.parametrize('changes', [None, {'status': 'disconnected'},
    {'tokenValid': False}, {'needsReconnect': True}, {'canPost': False}])
def test_missing_or_unhealthy_health_never_permits_website_only_capture(tmp_path, changes):
    runner, storage, zernio, apify, websites = setup(tmp_path, connected=False)
    zernio.accounts = [account()]
    zernio.health_rows = ([] if changes is None
                          else [dict(zernio._health_row(account()), **changes)])
    with pytest.raises(CaptureIngestError,
                       match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'website-only-held')
    assert not websites and not apify.calls and not storage.rows
    assert zernio.calls[0][0].endswith('/v1/accounts/health')
    assert not any(url == 'https://api.zernio.com/v1/accounts'
                   for url, _ in zernio.calls)
    # Empty health may probe the exact stored profile for an absence proof;
    # this fixture cannot confirm it, so capture must still hold.
    latest = storage.attestations[-1]['attestation']
    assert latest['lookup_status'] == 'partial' and latest['instagram'] is None


def test_populated_profile_object_supports_capture_and_complete_receipt(tmp_path):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    zernio.accounts = [account(profileId={'_id': PROFILE,
                                        'name': 'Synthetic profile display name'})]
    result = runner.capture(GYM, 'populated-profile')
    assert result['source_policy'] == 'website_and_instagram'
    assert len(result['captures']) == len(storage.rows) == 2
    assert websites == [URL] and [m for m, _ in apify.calls].count('POST') == 1
    latest = storage.attestations[-1]['attestation']
    assert latest['lookup_status'] == 'complete'
    assert latest['instagram']['platform_user_id'] == OWNER


@pytest.mark.parametrize('changes', [
    {'platformUserId': None}, {'platformUserId': 'internalAccount123'},
    {'platformUserId': True}, {'profileId': 'other'},
    {'profileId': {'_id': 'other', 'name': 'Synthetic profile display name'}}, {'_id': None},
    {'metadata': {'profileData': {'username': 'other'}}, 'username': HANDLE},
])
def test_authenticated_identity_never_uses_scraped_or_ambiguous_ids(tmp_path, changes):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    zernio.accounts = [account(**changes)]
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'bad')
    assert not apify.calls and not websites and not storage.rows
    assert storage.attestations[-1]['attestation']['lookup_status'] != 'complete'
    assert storage.attestations[-1]['attestation']['instagram'] is None


def test_partial_provider_list_is_hold_never_negative(tmp_path):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    zernio.extra = {'pagination': {'hasMore': True}}
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'partial')
    assert not websites and not apify.calls
    assert storage.attestations[-1]['attestation']['lookup_status'] == 'partial'


def test_attestation_scope_readback_mismatch_holds_before_capture(tmp_path):
    runner, storage, _, apify, websites = setup(tmp_path)
    storage.corrupt_attestation = True
    with pytest.raises(CaptureIngestError, match='provider_attestation_readback_mismatch'):
        runner.capture(GYM, 'bad-receipt')
    assert not websites and not apify.calls


def test_wrong_apify_owner_holds_with_no_social_storage_or_receipt(tmp_path):
    runner, storage, _, apify, _ = setup(tmp_path)
    apify.owner = '999'
    with pytest.raises(CaptureIngestError, match='social_run_capture_held'):
        runner.capture(GYM, 'wrong-owner')
    assert all(r['source_kind'] == 'website' for r in storage.rows.values())


def test_storage_readback_mismatch_never_authenticates_prepared_receipt(tmp_path):
    runner, storage, _, _, _ = setup(tmp_path)
    storage.corrupt_capture = True
    with pytest.raises(CaptureIngestError, match='capture_readback_digest_mismatch'):
        runner.capture(GYM, 'bad-storage')
    mapping = runner.collector._resolve(GYM)
    row = next(iter(storage.rows.values()))
    assert not runner.collector.authenticate_capture(row, bytes.fromhex(row['raw_bytes'][2:]), mapping)
    storage.corrupt_capture = False
    runner.capture(GYM, 'bad-storage')
    assert runner.collector.authenticate_capture(row, bytes.fromhex(row['raw_bytes'][2:]), mapping)


def test_uncertain_insert_reconciles_and_repeat_request_cannot_change_mapping(tmp_path):
    runner, storage, _, _, websites = setup(tmp_path)
    storage.timeout_after_write = True
    runner.capture(GYM, 'uncertain-insert')
    assert len(storage.rows) == 2
    entry = runner.collector._resolve._approved[GYM]
    runner.collector._resolve._approved[GYM] = replace(entry, approval_receipt='new-approval')
    with pytest.raises(CaptureIngestError, match='capture_request_binding_changed'):
        runner.capture(GYM, 'uncertain-insert')
    assert websites == [URL]


def test_default_off_before_journal_or_network_and_private_path_required(tmp_path):
    with pytest.raises(CaptureIngestError, match='source_capture_runner_disabled'):
        build_capture_runner(environ={})
    os.chmod(tmp_path, 0o755)
    with pytest.raises(CaptureIngestError, match='private_capture_journal_required'):
        CaptureReceiptJournal(tmp_path / 'unsafe.sqlite3')
    assert not (tmp_path / 'unsafe.sqlite3').exists()


def test_collection_receipts_drive_real_observation_producer(tmp_path):
    from agent.source_brand_observation import TrustedSourceObservationProducer, ObservationHold
    runner, storage, _, _, _ = setup(tmp_path)
    runner.capture(GYM, 'observe-collected')
    rows = list(storage.rows.values())
    website = next(c for c in rows if c['source_kind'] == 'website')
    raw = bytes.fromhex(website['raw_bytes'][2:])
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    snap = {'schema_version': 1, 'gym_id': GYM, 'echo_account_key': KEY,
        'fact_policy': 'delegated_supported_facts',
        'captures': [{**{k: v for k, v in c.items() if k not in ('raw_bytes', 'captured_at')},
                      'bytes_base64': base64.b64encode(bytes.fromhex(c['raw_bytes'][2:])).decode()}
                     for c in rows],
        'selected_facts': [{'key': 'gym', 'capture_id': website['id'],
            'bytes_sha256': website['bytes_sha256'], 'source_locator': URL,
            'byte_offset': raw.index(b'Real gym evidence'), 'byte_length': 17,
            'text': 'Real gym evidence'}],
        'palette': {'capture_id': website['id'], 'bytes_sha256': website['bytes_sha256'],
            'primary': '#123456', 'secondary': '#ffffff',
            'primary_byte_offset': raw.index(b'#123456'),
            'secondary_byte_offset': raw.index(b'#ffffff')}}
    frozen = canonical(snap)
    now = datetime.now(timezone.utc)
    bundle = {'id': str(uuid.uuid4()), 'gym_id': GYM, 'echo_account_key': KEY,
        'version': 1, 'capture_ids': [c['id'] for c in rows], 'snapshot_bytes': frozen,
        'content_sha256': hashlib.sha256(frozen.encode()).hexdigest(),
        'created_at': (now - timedelta(days=2)).isoformat()}
    approval = {'id': 1, 'gym_id': GYM, 'bundle_id': bundle['id'], 'bundle_version': 1,
        'content_sha256': bundle['content_sha256'], 'request_id': str(uuid.uuid4()),
        'purpose': 'echo_source_brand_configuration', 'action': 'approve',
        'actor_authority': 'blake', 'actor_clerk_user_id': 'synthetic-server-user',
        'created_at': (now - timedelta(days=1)).isoformat()}
    config = {'bundle': bundle, 'approval_receipt': approval}
    class Store:
        observation = None
        def latest(self, gym, prior):
            assert gym == GYM
            return copy.deepcopy(next(c for c in rows if c['id'] == prior['id']))
        def rpc(self, name, params):
            assert params['p_gym'] == GYM
            if name == 'echo_source_brand_configuration':
                return copy.deepcopy(config)
            if name == 'echo_source_brand_revalidate':
                self.observation = {'id': 1, 'gym_id': GYM, 'bundle_id': bundle['id'],
                    'configuration_sha256': bundle['content_sha256'], 'snapshot_bytes': frozen,
                    'content_sha256': bundle['content_sha256'],
                    'validator_revision': params['p_validator_revision'],
                    'validation_report': params['p_validation_report']}
                return copy.deepcopy(self.observation)
            assert name == 'echo_source_brand_active'
            return {**copy.deepcopy(config), 'observation': copy.deepcopy(self.observation),
                    'fact_validation': 'supported_uncontradicted'}
    class Assessor:
        def assess(self, evidence):
            return {'evidence_sha256': hashlib.sha256(canonical(evidence).encode()).hexdigest(),
                    'selected_facts_status': 'supported_uncontradicted'}
    producer = TrustedSourceObservationProducer(resolve_mapping=runner.collector._resolve,
        authenticate_capture=runner.collector.authenticate_capture,
        store=Store(), assessor=Assessor(), environ={'ECHO_SOURCE_OBSERVATION_ENABLED': 'true'})
    assert producer.observe(GYM)['state'] == 'verified'
    # A lookalike DB row without a collector confirmation is held by the actual
    # producer, even when byte digests and all provenance labels match.
    with sqlite3.connect(tmp_path / 'origin.sqlite3') as journal:
        journal.execute('UPDATE receipts SET stored=NULL')
    with pytest.raises(ObservationHold, match='capture_transport_authentication_required'):
        producer.observe(GYM)


def test_maximum_request_length_and_changed_entire_source_set_are_fenced(tmp_path):
    runner, storage, _, _, _ = setup(tmp_path)
    request = 'x' * 256
    runner.capture(GYM, request)
    entry = runner.collector._resolve._approved[GYM]
    runner.collector._resolve._approved[GYM] = replace(entry, website_urls=('https://other.example/',))
    with pytest.raises(CaptureIngestError, match='capture_request_binding_changed'):
        runner.capture(GYM, request)
    assert len(storage.rows) == 2


def test_missing_health_holds_and_provider_positive_needs_approval(tmp_path):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    zernio.accounts = []
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'cache-disagreement')
    assert not websites and not apify.calls
    runner, storage, zernio, apify, websites = setup(tmp_path, connected=False)
    zernio.accounts = [account()]
    with pytest.raises(CaptureIngestError, match='current_social_mapping_mismatch'):
        runner.capture(GYM, 'new-connection')
    assert not websites and not apify.calls


@pytest.mark.parametrize('rows,error', [([], False),
    ([{'gym_id': GYM, 'zernio_profile_id': None}], False),
    ([{'gym_id': GYM, 'zernio_profile_id': PROFILE}] * 2, False),
    (None, True)])
def test_missing_or_failed_profile_invalidates_previous_fresh_identity(tmp_path, rows, error):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    runner.capture(GYM, 'previous-identity')
    assert storage.attestations[-1]['attestation']['lookup_status'] == 'complete'
    before = len(zernio.calls), len(websites), len(apify.calls)
    storage.profile_rows, storage.profile_read_error = rows, error
    with pytest.raises(CaptureIngestError, match='exact_zernio_profile_mapping_required'):
        runner.capture(GYM, 'failed-profile')
    latest = storage.attestations[-1]['attestation']
    assert latest['lookup_status'] == 'unavailable' and latest['profile_id'] is None
    assert latest['instagram'] is None and latest['authenticated'] is False
    assert (len(zernio.calls), len(websites), len(apify.calls)) == before


def test_missing_credentials_invalidate_previous_fresh_identity(tmp_path):
    runner, storage, zernio, _, websites = setup(tmp_path)
    runner.capture(GYM, 'first')
    runner.collector._env['ZERNIO_API_KEY'] = ''
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'missing-credential')
    latest = storage.attestations[-1]['attestation']
    assert latest['lookup_status'] == 'unavailable' and latest['profile_id'] == PROFILE
    assert latest['instagram'] is None
    assert len(websites) == 1


def test_reviewed_composition_root_uses_same_trusted_execution_path(tmp_path):
    original, storage, zernio, apify, _ = setup(tmp_path)
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path))
    entries = list(original.collector._resolve._approved.values())
    runner = build_capture_runner(approved_mappings=entries, environ=env,
        http=storage, identity_http=zernio, apify_client=apify)
    runner.collector._website_factory = original.collector._website_factory
    runner.collector._min_host_delay = 0
    result = runner.capture(GYM, 'composition-root')
    assert len(result['captures']) == 2
    assert all(runner.collector.authenticate_capture(row,
        bytes.fromhex(row['raw_bytes'][2:]), runner.collector._resolve(GYM))
        for row in storage.rows.values())


def test_duplicate_connected_or_noncanonical_platform_metadata_holds(tmp_path):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    zernio.accounts = [account(), account(_id='secondAccount')]
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'duplicate-account')
    zernio.accounts = [account(platform='Instagram')]
    with pytest.raises(CaptureIngestError, match='authenticated_social_status_unavailable_or_incomplete'):
        runner.capture(GYM, 'bad-platform')
    assert not websites and not apify.calls and not storage.rows


def test_attestation_write_failure_holds_without_capture(tmp_path):
    runner, storage, zernio, apify, websites = setup(tmp_path)
    zernio.accounts = []
    storage.post = lambda *a, **k: SimpleNamespace(status_code=503)
    with pytest.raises(CaptureIngestError, match='provider_attestation_unconfirmed'):
        runner.capture(GYM, 'unconfirmed-status')
    assert not websites and not apify.calls and not storage.rows


CSS_URL = 'https://swiftrivercrossfit.com/styles/site.css'


def test_approved_exact_css_url_classified_as_website_asset(tmp_path):
    runner, storage, zernio, apify, websites = setup(tmp_path, urls=(URL, CSS_URL))
    result = runner.capture(GYM, 'css-request')
    assert websites == [URL, CSS_URL]  # approved URLs only; no discovery, no new URL
    by_url = {r['source_url']: r for r in storage.rows.values()}
    assert by_url[URL]['source_kind'] == 'website'
    assert by_url[CSS_URL]['source_kind'] == 'website_asset'
    assert len(result['captures']) == 3  # page + css asset + social
    # Replay of the same request reuses the same classification.
    assert runner.capture(GYM, 'css-request') == result


def test_non_css_approved_urls_stay_website_kind(tmp_path):
    runner, storage, zernio, apify, websites = setup(
        tmp_path, urls=(URL, 'https://swiftrivercrossfit.com/programs'))
    runner.capture(GYM, 'kinds-request')
    assert {r['source_kind'] for r in storage.rows.values()
            if r['source_kind'] != 'social'} == {'website'}
