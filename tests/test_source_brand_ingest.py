"""Synthetic service adapter tests. No provider, DB, or live HTTP calls."""
import copy
import hashlib
from datetime import datetime, timezone

import pytest

from agent.source_brand_ingest import (CaptureIngestError, TrustedCaptureIngest,
                                       VerifiedMapping)

GYM = '4893c289-eaa5-416f-b2e2-bcdd83c1cba1'
RAW = b'[{"caption":"Train here", "ownerId":"123"}]\n\xff'
ENV = {'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true',
       'ECHO_SOURCE_CAPTURE_SUPABASE_URL': 'https://database.example',
       'ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY': 'synthetic-service-key'}


def capture():
    return dict(gym_id=GYM, echo_account_key='gym_exact', source_kind='social',
                source_url='https://api.apify.com/v2/datasets/response/items',
                source_locator='https://www.instagram.com/mapped/',
                capture_provider='apify', provider_response_id='dataset:response',
                provider_account_id='123', source_revision='collector-v1:response',
                mapping_revision='portal:rev1', mapping_evidence={'receipt': 'mapping-1'},
                fetched_at='2026-01-01T00:00:00Z')


def mapping():
    return VerifiedMapping(GYM, 'gym_exact', 'portal:rev1', {'receipt': 'mapping-1'},
                           ('https://gym.example/',), ('https://www.instagram.com/mapped/',), '123')


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        return copy.deepcopy(self.body)


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.row = None
        self.post_status = 201
        self.mapping_rows = [{'gym_id': GYM, 'echo_account_key': 'gym_exact'}]
        self.read_transform = lambda row: row
        self.persist = True
        self.timeout = False

    def get(self, url, **kwargs):
        self.calls.append(('get', url, kwargs))
        assert kwargs['allow_redirects'] is False
        if url.endswith('echo_intake_tokens'):
            return Response(200, self.mapping_rows)
        return Response(200, self.read_transform([self.row]) if self.row else [])

    def post(self, url, **kwargs):
        self.calls.append(('post', url, kwargs))
        if self.persist and self.row is None:
            self.row = copy.deepcopy(kwargs['json'])
            self.row.update(bytes_sha256=hashlib.sha256(RAW).hexdigest(),
                            captured_at=datetime.now(timezone.utc).isoformat())
        if self.timeout:
            raise RuntimeError('secret in exception')
        return Response(self.post_status)


def adapter(http, **kwargs):
    return TrustedCaptureIngest(resolve_mapping=kwargs.pop('resolve_mapping', lambda gym: mapping()),
                                authenticate_response=kwargs.pop('authenticate_response', lambda *args: True),
                                http=http, environ=kwargs.pop('environ', ENV),
                                sleep=kwargs.pop('sleep', lambda seconds: None), **kwargs)


def ingest(client, metadata=None, **kwargs):
    return client.ingest(metadata or capture(), RAW, transport_receipt=object(),
                         expected_length=kwargs.get('length', len(RAW)),
                         expected_sha256=kwargs.get('digest', hashlib.sha256(RAW).hexdigest()))


def test_exact_bytea_digest_and_deterministic_duplicate_readback():
    http = FakeHTTP()
    result = ingest(adapter(http))
    assert http.row['raw_bytes'] == '\\x' + RAW.hex()
    assert ingest(adapter(http)) == result
    posts = [call for call in http.calls if call[0] == 'post']
    assert posts[0][2]['json']['id'] == posts[1][2]['json']['id']
    assert 'ignore-duplicates' in posts[0][2]['headers']['Prefer']
    assert posts[0][2]['params'] == {'on_conflict': 'id'}
    assert 'actor' not in http.row


def test_timeout_after_commit_reconciles_without_duplicate_write():
    http = FakeHTTP()
    http.timeout = True
    assert ingest(adapter(http))['bytes_sha256'] == hashlib.sha256(RAW).hexdigest()
    assert len([c for c in http.calls if c[0] == 'post']) == 1


def test_bounded_retry_uses_identical_payload():
    http = FakeHTTP()
    http.persist = False
    http.post_status = 503
    sleeps = []
    with pytest.raises(CaptureIngestError, match='storage_unconfirmed'):
        ingest(adapter(http, sleep=sleeps.append))
    posts = [c[2]['json'] for c in http.calls if c[0] == 'post']
    assert len(posts) == 3 and posts[0] == posts[1] == posts[2]
    assert sleeps == [0.25, 0.5]


@pytest.mark.parametrize('env', [{}, dict(ENV, ECHO_SOURCE_CAPTURE_INGEST_ENABLED='false'),
                               dict(ENV, ECHO_SOURCE_CAPTURE_SERVICE_ROLE_KEY='')])
def test_missing_or_disabled_config_never_calls_http(env):
    http = FakeHTTP()
    with pytest.raises(CaptureIngestError):
        ingest(adapter(http, environ=env))
    assert http.calls == []


@pytest.mark.parametrize('field,value', [
    ('gym_id', 'not-uuid'), ('echo_account_key', 'other_gym'),
    ('mapping_revision', 'unverified'), ('mapping_evidence', {}),
    ('provider_account_id', 'wrong'), ('capture_provider', 'direct'),
    ('source_locator', 'https://www.instagram.com/guess/'),
    ('source_url', 'https://www.instagram.com/mapped/'),
    ('source_url', 'https://api.apify.com/v2/items?token=secret'),
    ('provider_response_id', ''), ('fetched_at', '2999-01-01T00:00:00Z'),
])
def test_invalid_provenance_has_no_write(field, value):
    http = FakeHTTP()
    metadata = capture()
    metadata[field] = value
    with pytest.raises(CaptureIngestError):
        ingest(adapter(http), metadata)
    assert http.calls == []


@pytest.mark.parametrize('kwargs', [{'length': len(RAW)-1}, {'digest': '0'*64}])
def test_partial_or_wrong_digest_no_calls(kwargs):
    http = FakeHTTP()
    with pytest.raises(CaptureIngestError):
        ingest(adapter(http), **kwargs)
    assert not http.calls


@pytest.mark.parametrize('raw', [b'', b'x' * 2_000_001, bytearray(b'abc')])
def test_empty_oversize_mutable_response_no_calls(raw):
    http = FakeHTTP()
    with pytest.raises(CaptureIngestError):
        adapter(http).ingest(capture(), raw, transport_receipt=None,
                             expected_length=len(raw), expected_sha256=hashlib.sha256(raw).hexdigest())
    assert not http.calls


def test_requires_trusted_mapping_and_authenticator():
    http = FakeHTTP()
    with pytest.raises(CaptureIngestError, match='trusted_mapping_required'):
        ingest(adapter(http, resolve_mapping=lambda gym: {'verified': True}))
    with pytest.raises(CaptureIngestError, match='authentication_failed'):
        ingest(adapter(http, authenticate_response=lambda *args: {'verified': True}))
    assert not http.calls


@pytest.mark.parametrize('rows', [[], [{'gym_id': GYM, 'echo_account_key': 'other'}],
                                  [{'gym_id': GYM, 'echo_account_key': 'gym_exact'}]*2])
def test_current_mapping_missing_changed_ambiguous_prevents_write(rows):
    http = FakeHTTP()
    http.mapping_rows = rows
    with pytest.raises(CaptureIngestError, match='current_mapping_mismatch'):
        ingest(adapter(http))
    assert all(c[0] != 'post' for c in http.calls)


@pytest.mark.parametrize('field,value', [('gym_id', 'another'), ('echo_account_key', 'other'),
                                        ('raw_bytes', '\\x00'), ('bytes_sha256', '0'*64),
                                        ('provider_account_id', 'different')])
def test_readback_identity_or_byte_mismatch_fails(field, value):
    http = FakeHTTP()
    def change(rows):
        rows[0][field] = value
        return rows
    http.read_transform = change
    with pytest.raises(CaptureIngestError, match='readback'):
        ingest(adapter(http))
    assert len([c for c in http.calls if c[0] == 'post']) == 1


@pytest.mark.parametrize('transform', [lambda rows: rows * 2, lambda rows: {'row': rows}])
def test_ambiguous_readback_fails(transform):
    http = FakeHTTP()
    http.read_transform = transform
    with pytest.raises(CaptureIngestError, match='ambiguous_readback'):
        ingest(adapter(http))


def test_rejected_insert_never_reports_success():
    http = FakeHTTP()
    http.post_status = 403
    with pytest.raises(CaptureIngestError, match='insert_rejected'):
        ingest(adapter(http))


def test_website_exact_mapping_and_no_social_identity():
    http = FakeHTTP()
    metadata = capture()
    metadata.update(source_kind='website', source_url='https://gym.example/',
                    source_locator=None, provider_account_id=None,
                    provider_response_id=None, capture_provider='direct')
    assert ingest(adapter(http), metadata)['gym_id'] == GYM


def test_browser_actor_field_rejected():
    http = FakeHTTP()
    metadata = capture()
    metadata['actor'] = 'owner'
    with pytest.raises(CaptureIngestError, match='invalid_capture_fields'):
        ingest(adapter(http), metadata)
    assert not http.calls


def test_mapping_change_before_write_fails():
    http = FakeHTTP()
    calls = []
    def resolver(gym):
        calls.append(gym)
        if len(calls) == 1:
            return mapping()
        return VerifiedMapping(GYM, 'changed', 'portal:rev2', {'receipt': 'changed'}, (), (), '123')
    with pytest.raises(CaptureIngestError, match='mapping_changed_during_ingest'):
        ingest(adapter(http, resolve_mapping=resolver))
    assert not http.calls


def test_authenticator_cannot_change_already_checked_metadata():
    http = FakeHTTP()
    def auth(metadata, *args):
        metadata['echo_account_key'] = 'other_gym'
        return True
    with pytest.raises(CaptureIngestError, match='mutated_metadata'):
        ingest(adapter(http, authenticate_response=auth))
    assert not http.calls


def test_no_ambient_publisher_credentials_are_used():
    http = FakeHTTP()
    env = {'ECHO_SOURCE_CAPTURE_INGEST_ENABLED': 'true',
           'SUPABASE_URL': 'https://database.example',
           'SUPABASE_SERVICE_ROLE_KEY': 'publisher-key'}
    with pytest.raises(CaptureIngestError, match='collector_service_config_missing'):
        ingest(adapter(http, environ=env))
    assert not http.calls
