"""Offline exact URL bytes and trusted retention tests; no live R2 claim."""
import base64
import io
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent.r2_immutable_media import (
    ImmutableMediaError, SignedLockRuleSource, verify_immutable_media,
)

NOW = 2000000000
BASE = 'https://media.example.test'
KEY = 'echo-generated-originals/gym/asset.png'
URL = BASE + '/' + KEY


def document():
    return {'schema': 'echo-r2-lock-attestation-v1', 'account_id': 'account',
            'bucket': 'bucket', 'public_base_url': BASE,
            'public_serving': 'controlled-direct-byte-serving',
            'observed_at': NOW, 'expires_at': NOW + 300,
            'rules': [{'id': 'lock', 'enabled': True,
                       'prefix': 'echo-generated-originals/',
                       'protection': 'overwrite-and-delete',
                       'retention_until': NOW + 86400}]}


def signed(doc):
    key = Ed25519PrivateKey.generate()
    payload = json.dumps(doc).encode()
    envelope = json.dumps({'payload': base64.b64encode(payload).decode(),
                           'signature': base64.b64encode(key.sign(payload)).decode()}).encode()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return SignedLockRuleSource(lambda: envelope, pub)


class S3:
    meta = SimpleNamespace(endpoint_url='https://account.r2.cloudflarestorage.com')

    def __init__(self, data=b'image', length=None):
        self.data = data
        self.length = len(data) if length is None else length
        self.body = None

    def get_object(self, **kwargs):
        assert kwargs == {'Bucket': 'bucket', 'Key': KEY}
        self.body = io.BytesIO(self.data)
        return {'Body': self.body, 'ContentLength': self.length}


class Response:
    status_code = 200
    history = []

    def __init__(self, data=b'image', url=URL, headers=None):
        self.url = url
        self.headers = headers or {}
        self.raw = io.BytesIO(data)
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def verify(doc=None, origin=None, response=None, **kwargs):
    return verify_immutable_media(
        kwargs.pop('url', URL), key=kwargs.pop('key', KEY), bucket='bucket',
        account_id='account', public_base_url=BASE, s3=origin or S3(),
        lock_source=kwargs.pop('lock_source', signed(doc or document())),
        required_retention_until=NOW + 600, now=NOW,
        session=Session(response or Response()), **kwargs)


def test_valid_signed_lock_and_exact_bytes():
    origin, response = S3(), Response()
    proof = verify(origin=origin, response=response)
    assert proof.key == KEY and proof.size_bytes == 5
    assert len(proof.sha256) == 64 and proof.lock_rule_id == 'lock'
    assert origin.body.closed and response.closed


@pytest.mark.parametrize('field,value', [('bucket', 'wrong'), ('account_id', 'wrong'),
    ('public_base_url', 'https://wrong.example'), ('public_serving', 'transformed'),
    ('observed_at', NOW - 301), ('observed_at', NOW + 1), ('expires_at', NOW),
    ('expires_at', NOW + 301), ('schema', 'unsigned-v0')])
def test_identity_freshness_fail_closed(field, value):
    doc = document()
    doc[field] = value
    with pytest.raises(ImmutableMediaError):
        verify(doc)


@pytest.mark.parametrize('rules', [None, [], [{'enabled': True}],
    [{'id': 'x', 'enabled': True, 'prefix': '', 'protection': 'overwrite-and-delete',
      'retention_until': NOW + 1000}]])
def test_missing_lock(rules):
    doc = document()
    doc['rules'] = rules
    with pytest.raises(ImmutableMediaError):
        verify(doc)


@pytest.mark.parametrize('field,value', [('enabled', False), ('enabled', 'true'),
    ('prefix', 'echo-mutable/'), ('prefix', 'echo-generated-original'),
    ('protection', 'delete-only'), ('retention_until', NOW + 599),
    ('retention_until', 'indefinite')])
def test_mutable_or_expiring_prefix(field, value):
    doc = document()
    doc['rules'][0][field] = value
    with pytest.raises(ImmutableMediaError):
        verify(doc)


@pytest.mark.parametrize('url', [URL + '?v=1', URL + '#x',
    URL.replace('asset', '%61sset'), URL.replace('https:', 'http:'),
    URL.replace('media.example.test', 'media.example.test.evil'),
    URL.replace('/gym/', '/gym/../gym/'), URL.replace('/gym/', '%2Fgym%2F')])
def test_noncanonical_url(url):
    with pytest.raises(ImmutableMediaError):
        verify(url=url)


@pytest.mark.parametrize('mode', ['redirect', 'url', 'encoding', 'history'])
def test_redirects_and_transform_encodings(mode):
    response = Response()
    if mode == 'redirect':
        response.status_code = 302
    elif mode == 'url':
        response.url = 'https://other.example/asset.png'
    elif mode == 'encoding':
        response.headers = {'Content-Encoding': 'gzip'}
    else:
        response.history = [object()]
    with pytest.raises(ImmutableMediaError):
        verify(response=response)
    assert response.closed


def test_exact_outgoing_bytes_must_match():
    with pytest.raises(ImmutableMediaError, match='bytes differ'):
        verify(response=Response(b'other'))


@pytest.mark.parametrize('origin,response', [(S3(b'123456'), Response()),
    (S3(), Response(b'123456')), (S3(b'123456', length=5), Response()),
    (S3(), Response(headers={'Content-Length': '6'}))])
def test_cap_is_applied_before_or_during_stream(origin, response):
    with pytest.raises(ImmutableMediaError):
        verify(origin=origin, response=response, max_bytes=5)


def test_signed_source_is_mandatory():
    with pytest.raises(ImmutableMediaError, match='trusted lock source required'):
        verify(lock_source=lambda: document())


def test_forged_signature_is_rejected():
    public = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    raw = json.dumps({'payload': base64.b64encode(json.dumps(document()).encode()).decode(),
                      'signature': base64.b64encode(b'x' * 64).decode()}).encode()
    with pytest.raises(ImmutableMediaError, match='attestation unavailable'):
        verify(lock_source=SignedLockRuleSource(lambda: raw, public))


def test_unbounded_attestation_is_rejected():
    public = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with pytest.raises(ImmutableMediaError):
        verify(lock_source=SignedLockRuleSource(lambda: b'x' * 65537, public))


def test_wrong_origin_endpoint_is_rejected():
    origin = S3()
    origin.meta = SimpleNamespace(endpoint_url='https://evil.example')
    with pytest.raises(ImmutableMediaError, match='endpoint mismatch'):
        verify(origin=origin)


def test_provider_error_is_sanitized():
    class Broken(S3):
        def get_object(self, **kwargs):
            raise RuntimeError('secret-token')
    with pytest.raises(ImmutableMediaError) as caught:
        verify(origin=Broken())
    assert 'secret-token' not in str(caught.value)
    assert caught.value.__suppress_context__


def test_request_is_streamed_and_redirects_disabled():
    session = Session(Response())
    verify_immutable_media(URL, key=KEY, bucket='bucket', account_id='account',
        public_base_url=BASE, s3=S3(), lock_source=signed(document()),
        required_retention_until=NOW + 600, now=NOW, session=session)
    _, options = session.calls[0]
    assert options['stream'] is True and options['allow_redirects'] is False
    assert options['headers']['Accept-Encoding'] == 'identity'


def test_url_space_encoding_is_canonical(monkeypatch):
    from agent import r2_immutable_media as module
    monkeypatch.setattr(module, '_origin_bytes', lambda *args: b'image')
    monkeypatch.setattr(module, '_public_bytes', lambda *args: b'image')
    proof = verify(key=KEY.replace('asset', 'my asset'),
                   url=URL.replace('asset', 'my%20asset'))
    assert proof.key.endswith('my asset.png')


@pytest.mark.parametrize('mode', ['pass', 'mismatch', 'sink-failure', 'verification-failure'])
def test_adapter_binds_guard_bytes_and_records_typed_deadline(monkeypatch, mode):
    import hashlib
    from agent import r2_immutable_media as module
    monkeypatch.setattr(module.time, 'time', lambda: NOW)
    proof = module.ImmutableMediaProof(URL, 'account', 'bucket', KEY,
        hashlib.sha256(b'image').hexdigest(), 5, NOW, NOW + 1000, 'lock', 'hash')
    def fake_verify(*args, **kwargs):
        if mode == 'verification-failure':
            raise ImmutableMediaError('not locked')
        assert kwargs['key'] == KEY
        return proof
    monkeypatch.setattr(module, 'verify_immutable_media', fake_verify)
    recorded = []
    def sink(value):
        if mode == 'sink-failure':
            raise RuntimeError('audit unavailable')
        recorded.append(value)
    callback = module.trusted_bool_adapter(bucket='bucket', account_id='account',
        public_base_url=BASE, s3=S3(), lock_source=signed(document()),
        retention_seconds=600, proof_sink=sink)
    data = b'other' if mode == 'mismatch' else b'image'
    result = callback(URL, data)
    if mode == 'pass':
        assert isinstance(result, module.ImmutableMediaEvidence)
        assert result.proof is proof
        assert result.proof.retention_until == NOW + 1000
        assert result.provider_fetch_horizon_seconds == 600
    else:
        assert result is False
    assert len(recorded) == (1 if mode == 'pass' else 0)


def test_adapter_holds_if_durable_sink_uses_fetch_horizon_margin(monkeypatch):
    import hashlib
    from agent import r2_immutable_media as module
    ticks = {'now': NOW}
    monkeypatch.setattr(module.time, 'time', lambda: ticks['now'])
    proof = module.ImmutableMediaProof(URL, 'account', 'bucket', KEY,
        hashlib.sha256(b'image').hexdigest(), 5, NOW, NOW + 1000, 'lock', 'a' * 64)
    monkeypatch.setattr(module, 'verify_immutable_media', lambda *args, **kwargs: proof)
    recorded = []
    def slow_sink(value):
        recorded.append(value)
        ticks['now'] += 401
    callback = module.trusted_bool_adapter(bucket='bucket', account_id='account',
        public_base_url=BASE, s3=S3(), lock_source=signed(document()),
        retention_seconds=600, proof_sink=slow_sink)
    assert callback(URL, b'image') is False
    assert recorded == [proof]


def test_signed_adapter_propagates_actual_lock_into_send_deadline(monkeypatch):
    from agent import delivered_byte_send_guard as guard
    from agent import r2_immutable_media as module
    ticks = {'now': NOW, 'mono': 100}
    monkeypatch.setattr(module.time, 'time', lambda: ticks['now'])
    monkeypatch.setattr(module.time, 'monotonic', lambda: ticks['mono'])
    monkeypatch.setattr(module, '_public_bytes', lambda *args: b'image')
    recorded = []
    callback = module.trusted_bool_adapter(bucket='bucket', account_id='account',
        public_base_url=BASE, s3=S3(), lock_source=signed(document()),
        retention_seconds=600, proof_sink=recorded.append)
    result = callback(URL, b'image')
    assert isinstance(result, module.ImmutableMediaEvidence)
    retention = guard._verified_retention(result, URL, b'image')
    assert retention.retention_until == NOW + 86400
    assert retention.provider_fetch_horizon_seconds == 600
    assert recorded == [result.proof]
    ticks['now'] += 86400 - 599
    ticks['mono'] += 86400 - 599
    with pytest.raises(guard.ExactByteSendHold, match='retention'):
        guard._require_retention(retention)


def test_signature_covers_exact_payload_bytes():
    private = Ed25519PrivateKey.generate()
    payload = json.dumps(document()).encode()
    changed = payload.replace(b'"enabled": true', b'"enabled": false')
    raw = json.dumps({'payload': base64.b64encode(changed).decode(),
        'signature': base64.b64encode(private.sign(payload)).decode()}).encode()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    with pytest.raises(ImmutableMediaError, match='attestation unavailable'):
        verify(lock_source=SignedLockRuleSource(lambda: raw, public))


@pytest.mark.parametrize('key', ['../asset.png', 'echo//asset.png',
    '/echo/asset.png', 'echo/./asset.png', 'echo/asset\\name.png'])
def test_ambiguous_keys_rejected(key):
    with pytest.raises(ImmutableMediaError):
        verify(key=key, url=BASE + '/' + key)


def test_expiry_during_download_is_rejected(monkeypatch):
    from agent import r2_immutable_media as module
    ticks = iter([NOW, NOW + 300])
    monkeypatch.setattr(module.time, 'time', lambda: next(ticks))
    with pytest.raises(ImmutableMediaError, match='expired during read'):
        verify_immutable_media(URL, key=KEY, bucket='bucket', account_id='account',
            public_base_url=BASE, s3=S3(), lock_source=signed(document()),
            required_retention_until=NOW + 600, session=Session(Response()))
