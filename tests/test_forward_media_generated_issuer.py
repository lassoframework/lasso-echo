"""Offline execution/integration tests. Fixtures are not production evidence."""
import base64
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import io
import json
import uuid

import pytest
from PIL import Image
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from agent import forward_media_generated_issuer as issuer
from agent.forward_media_generated_prepare import (
    ApprovedGenerationKey, GeneratedReceiptHold, GenerationRequest,
    canonical, prepare_generated_original, sha256,
)

NOW = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)


def png(color='navy'):
    out = io.BytesIO()
    Image.new('RGB', (128, 160), color).save(out, format='PNG')
    return out.getvalue()


class Body(io.BytesIO):
    pass


class S3:
    def __init__(self):
        self.objects, self.versions, self.puts = {}, {}, []
        self.versioned, self.locked, self.mode = True, True, 'COMPLIANCE'
        self.until = NOW + timedelta(days=365)

    def get_bucket_versioning(self, **kwargs):
        return {'Status': 'Enabled' if self.versioned else 'Suspended'}

    def get_object_lock_configuration(self, **kwargs):
        return {'ObjectLockConfiguration': {'ObjectLockEnabled': 'Enabled' if self.locked else 'Disabled'}}

    def put_object(self, **kw):
        self.puts.append(kw)
        assert kw['IfNoneMatch'] == '*'
        assert kw['ObjectLockMode'] == 'COMPLIANCE'
        key = kw['Key']
        if key in self.objects:
            raise RuntimeError('precondition failed')
        self.objects[key], self.versions[key] = kw['Body'], 'v-' + str(len(self.objects))
        return {'VersionId': self.versions[key]}

    def head_object(self, **kw):
        return {'VersionId': self.versions[kw['Key']]}

    def get_object_retention(self, **kw):
        return {'Retention': {'Mode': self.mode, 'RetainUntilDate': self.until}}

    def get_object(self, **kw):
        assert kw['VersionId'] == self.versions[kw['Key']]
        return {'Body': Body(self.objects[kw['Key']])}


class Owner:
    def __init__(self, request):
        self.s = {'request': asdict(request), 'palette': {'primary': '#000080'},
            'copy': {'headline': 'Train with a coach', 'facts': ['Practice strength'], 'cta': '', 'footer': ''},
            'brand_source_verified': True, 'photo_inventory_complete': True,
            'eligible_photo_count': 0, 'photo_inventory_revision': 'photos-9',
            'history_complete': True, 'history_revision': 'history-22'}
        self.history_ok = True

    def snapshot(self, request):
        return json.loads(canonical(self.s))

    def history(self, request, data):
        return {'request': asdict(request), 'image_sha256': sha256(data),
            'history_complete': True, 'reviewed_no_match': self.history_ok,
            'history_revision': self.s['history_revision']}


class Provider:
    def __init__(self):
        self.calls, self.responses, self.retrievals = [], {}, []
        self.data, self.brand_ok = png(), True

    def create(self, payload):
        self.calls.append(payload)
        rid = 'resp_' + str(len(self.calls))
        if payload.get('tools'):
            output = [{'type': 'image_generation_call', 'status': 'completed',
                       'result': base64.b64encode(self.data).decode()}]
        else:
            review = {'scores': dict(issuer_review_weights()), 'copy_complete': True,
                'copy_accurate': True, 'placement_safe': True, 'placement_violations': [],
                'issues': [], 'palette_accurate': self.brand_ok,
                'rendered_style_safe': True, 'rendered_style_violations': []}
            output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': canonical(review)}]}]
        response = {'id': rid, 'status': 'completed', 'model': payload['model'],
            'metadata': payload['metadata'], 'created_at': int(NOW.timestamp()), 'output': output}
        self.responses[rid] = response
        return json.loads(canonical(response))

    def retrieve(self, rid):
        self.retrievals.append(rid)
        return json.loads(canonical(self.responses[rid]))


def issuer_review_weights():
    from agent.infographic_review import WEIGHTS
    return WEIGHTS


@pytest.fixture
def lane(monkeypatch):
    monkeypatch.setattr(issuer, 'utcnow', lambda: NOW)
    c = {'headline': 'Train with a coach', 'facts': ['Practice strength'], 'cta': '', 'footer': ''}
    palette = {'primary': '#000080'}
    req = GenerationRequest('gym-a', str(uuid.uuid4()), str(uuid.uuid4()), '2026-10-08',
        '2026-10-07T15:59:00Z', 'palette-4', sha256(canonical(palette).encode()),
        'copy-5', sha256(canonical(c).encode()), issuer.POLICY)
    private = Ed25519PrivateKey.generate()
    key = ApprovedGenerationKey('key-1', private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex(), True,
        'independent_generation_verifier', 'openai', 'image-model-test', 'isolated-runtime-1')
    s3, provider, owner = S3(), Provider(), Owner(req)
    storage = issuer.RetainedObjectStore(s3, 'existing', 'https://media.fixture.invalid',
        public_reader=lambda url: s3.objects[url.split('https://media.fixture.invalid/', 1)[1].split('?', 1)[0]])
    writer = issuer.GeneratedOriginalIssuer(key, private, provider, storage, owner, enabled=True)
    backend = issuer.RetainedGenerationBackend(key, provider, storage, owner)
    return writer, backend, req, s3, provider, owner


def test_fresh_issue_independent_provider_readback_and_v1_preparation(lane):
    writer, backend, req, s3, provider, _ = lane
    packet = writer.issue(req)
    prepared = prepare_generated_original(packet, req, backend, enabled=True, now=NOW)
    assert prepared.image_bytes == provider.data
    assert len(provider.calls) == 2  # generation and one independent review
    assert provider.retrievals == ['resp_1', 'resp_2']
    assert len(s3.puts) == 2  # original and execution receipt
    assert s3.puts[0]['Body'] == provider.data
    assert s3.puts[0]['Key'].startswith(issuer.PREFIX)
    assert s3.puts[1]['Key'].startswith(issuer.RECEIPTS)
    assert prepared.payload['object_version'] == 'v-0'
    review_input = provider.calls[1]['input'][0]['content'][1]['image_url']
    assert base64.b64decode(review_input.split(',', 1)[1]) == provider.data
    assert 'VERIFIED PALETTE DATA' in provider.calls[1]['input'][0]['content'][0]['text']
    assert prepared.photo_inventory_revision == 'photos-9'
    assert prepared.history_revision == 'history-22'


def test_default_off_and_no_factory_configuration_never_calls_provider(lane, monkeypatch):
    writer, _, req, _, provider, _ = lane
    writer.enabled = False
    with pytest.raises(GeneratedReceiptHold, match='generated_issuer_disabled'):
        writer.issue(req)
    monkeypatch.delenv('AGENT_GENERATED_ORIGINAL_ISSUER_ENABLED', raising=False)
    with pytest.raises(GeneratedReceiptHold, match='generated_issuer_disabled'):
        issuer.configured_lane('issuer')
    assert provider.calls == []


@pytest.mark.parametrize('field', ['versioned', 'locked'])
def test_unsupported_existing_storage_holds_before_paid_generation(lane, field):
    writer, _, req, s3, provider, _ = lane
    setattr(s3, field, False)
    with pytest.raises(GeneratedReceiptHold, match='generated_storage_'):
        writer.issue(req)
    assert provider.calls == []


@pytest.mark.parametrize('field,value', [('photo_inventory_complete', False),
    ('eligible_photo_count', 1), ('eligible_photo_count', False),
    ('history_complete', False), ('brand_source_verified', False)])
def test_incomplete_or_photo_available_snapshot_holds_before_generation(lane, field, value):
    writer, _, req, _, provider, owner = lane
    owner.s[field] = value
    with pytest.raises(GeneratedReceiptHold, match='generated_current_owner_snapshot_unverified'):
        writer.issue(req)
    assert provider.calls == []


def test_pixel_palette_failure_does_not_upload(lane):
    writer, _, req, s3, provider, _ = lane
    provider.brand_ok = False
    with pytest.raises(GeneratedReceiptHold, match='generated_independent_pixel_brand_review_failed'):
        writer.issue(req)
    assert s3.puts == []


def test_reuse_or_incomplete_history_does_not_upload(lane):
    writer, _, req, s3, _, owner = lane
    owner.history_ok = False
    with pytest.raises(GeneratedReceiptHold, match='generated_history_used_or_uncertain'):
        writer.issue(req)
    assert s3.puts == []


def test_immutable_storage_governance_is_not_compliance(lane):
    writer, _, req, s3, _, _ = lane
    s3.mode = 'GOVERNANCE'
    with pytest.raises(GeneratedReceiptHold, match='generated_storage_version_retention_unverified'):
        writer.issue(req)
    assert len(s3.puts) == 1  # orphan retained, no positive receipt


def test_existing_object_cannot_be_adopted_as_new_original(lane):
    writer, _, req, s3, provider, _ = lane
    key = issuer.object_key(req, sha256(provider.data))
    s3.objects[key] = provider.data
    s3.versions[key] = 'prior'
    with pytest.raises(GeneratedReceiptHold, match='generated_create_only_storage_write_failed'):
        writer.issue(req)
    assert len(s3.puts) == 1


def test_provider_output_replacement_or_wrong_metadata_holds(lane):
    writer, backend, req, _, provider, _ = lane
    packet = writer.issue(req)
    provider.responses['resp_1']['metadata']['job_id'] = str(uuid.uuid4())
    with pytest.raises(GeneratedReceiptHold, match='generated_provider_readback_changed'):
        backend.verified_evidence(req, canonical(packet['payload']))


@pytest.mark.parametrize('response_id', ['resp_1', 'resp_2'])
def test_live_response_identity_must_match_signed_receipt(lane, response_id):
    writer, backend, req, _, provider, _ = lane
    packet = writer.issue(req)
    # Reproduce a readback returning identical pixels/metadata but another ID.
    provider.responses[response_id]['id'] = 'resp_forged_live_identity'
    with pytest.raises(GeneratedReceiptHold, match='generated_provider_readback_changed'):
        prepare_generated_original(packet, req, backend, enabled=True, now=NOW)


def test_same_second_integer_provider_timestamp_is_valid(lane, monkeypatch):
    writer, backend, req, _, provider, owner = lane
    precise_now = NOW + timedelta(microseconds=900000)
    req = replace(req, requested_at=issuer.stamp(NOW + timedelta(microseconds=123456)))
    owner.s['request'] = asdict(req)
    monkeypatch.setattr(issuer, 'utcnow', lambda: precise_now)
    # Provider's integer second bucket overlaps the owner's precise request;
    # exact metadata and signed IDs still bind both generation and review.
    packet = writer.issue(req)
    prepared = prepare_generated_original(packet, req, backend, enabled=True, now=precise_now)
    assert prepared.image_bytes == provider.data


@pytest.mark.parametrize('timestamp', [
    int(NOW.timestamp()) - 1, NOW.timestamp() + .1,
    NOW.timestamp() - .1, float('nan'), float('inf'), True,
])
def test_provider_precision_does_not_allow_prior_second_future_or_invalid(lane, timestamp):
    _, _, req, _, _, _ = lane
    req = replace(req, requested_at=issuer.stamp(NOW))
    with pytest.raises(GeneratedReceiptHold, match='generated_provider_execution_stale_or_future'):
        issuer.check_provider_time({'created_at': timestamp}, req, NOW)


def test_provider_integer_precision_does_not_extend_age_window(lane):
    _, _, req, _, _, _ = lane
    req = replace(req, requested_at=issuer.stamp(NOW - timedelta(seconds=900)))
    with pytest.raises(GeneratedReceiptHold, match='generated_provider_execution_stale_or_future'):
        issuer.check_provider_time({'created_at': int(NOW.timestamp()) - 900}, req,
                                   NOW + timedelta(microseconds=1))


def test_fractional_provider_timestamp_retains_exact_ordering(lane):
    _, _, req, _, _, _ = lane
    req = replace(req, requested_at=issuer.stamp(NOW + timedelta(microseconds=500000)))
    now = NOW + timedelta(microseconds=900000)
    issuer.check_provider_time({'created_at': NOW.timestamp() + .7}, req, now)
    with pytest.raises(GeneratedReceiptHold, match='generated_provider_execution_stale_or_future'):
        issuer.check_provider_time({'created_at': NOW.timestamp() + .4}, req, now)


def test_same_second_integer_does_not_allow_future_owner_request(lane):
    _, _, req, _, _, _ = lane
    req = replace(req, requested_at=issuer.stamp(NOW + timedelta(microseconds=500000)))
    with pytest.raises(GeneratedReceiptHold, match='generated_provider_execution_stale_or_future'):
        issuer.check_provider_time({'created_at': int(NOW.timestamp())}, req, NOW)


@pytest.mark.parametrize('safe,violations', [
    (False, []), (None, []), ('true', []), (True, None), (True, {}),
    (True, [{'element': 'decorative label', 'rendered_text': 'Coach-led',
             'correction': 'Replace the hyphen with a space'}]),
    (True, [{'element': 'wordmark', 'rendered_text': 'Gym\u2013Name',
             'correction': 'Remove the en dash'}]),
    (True, [{'element': 'footer', 'rendered_text': 'Train\u2014Today',
             'correction': 'Remove the em dash'}]),
])
def test_rendered_style_missing_ambiguous_or_violating_evidence_blocks_upload(lane, safe, violations):
    writer, _, req, s3, provider, _ = lane
    create = provider.create
    def unsafe_review(payload):
        response = create(payload)
        if not payload.get('tools'):
            part = response['output'][0]['content'][0]
            result = json.loads(part['text'])
            result['rendered_style_safe'] = safe
            result['rendered_style_violations'] = violations
            part['text'] = canonical(result)
        return response
    provider.create = unsafe_review
    with pytest.raises(GeneratedReceiptHold, match='generated_independent_pixel_brand_review_failed'):
        writer.issue(req)
    assert s3.puts == []
    question = provider.calls[1]['input'][0]['content'][0]['text']
    for instruction in ('ALL rendered elements', 'hyphen (-)', 'en dash (\u2013)',
                        'em dash (\u2014)', 'logos', 'decorative text', 'uncertainty'):
        assert instruction in question


@pytest.mark.parametrize('field', ['rendered_style_safe', 'rendered_style_violations'])
def test_rendered_style_absent_evidence_blocks_upload(lane, field):
    writer, _, req, s3, provider, _ = lane
    create = provider.create
    def missing_review(payload):
        response = create(payload)
        if not payload.get('tools'):
            part = response['output'][0]['content'][0]
            result = json.loads(part['text'])
            del result[field]
            part['text'] = canonical(result)
        return response
    provider.create = missing_review
    with pytest.raises(GeneratedReceiptHold, match='generated_independent_pixel_brand_review_failed'):
        writer.issue(req)
    assert s3.puts == []


def test_provider_exact_original_bytes_cannot_be_substituted(lane):
    writer, backend, req, s3, provider, _ = lane
    packet = writer.issue(req)
    key = issuer.object_key(req, sha256(provider.data))
    s3.objects[key] = png('red')
    with pytest.raises(GeneratedReceiptHold, match='generated_original_bytes_changed'):
        backend.verified_evidence(req, canonical(packet['payload']))


def test_changed_current_brand_or_photo_revision_holds(lane):
    writer, backend, req, _, _, owner = lane
    packet = writer.issue(req)
    owner.s['photo_inventory_revision'] = 'photos-10'
    with pytest.raises(GeneratedReceiptHold, match='generated_current_owner_snapshot_changed'):
        backend.verified_evidence(req, canonical(packet['payload']))


def test_appended_replacement_version_holds(lane):
    writer, backend, req, s3, provider, _ = lane
    packet = writer.issue(req)
    s3.versions[issuer.object_key(req, sha256(provider.data))] = 'overwriting-version'
    with pytest.raises(GeneratedReceiptHold, match='generated_storage_original_replaced'):
        backend.verified_evidence(req, canonical(packet['payload']))


def test_tampered_execution_envelope_cannot_be_authority(lane):
    writer, backend, req, s3, _, _ = lane
    packet = writer.issue(req)
    key = issuer.receipt_key(req)
    e = json.loads(s3.objects[key])
    e['payload']['cache_hit'] = True
    s3.objects[key] = canonical(e).encode()
    with pytest.raises(GeneratedReceiptHold, match='generated_external_signature_invalid'):
        backend.verified_evidence(req, canonical(packet['payload']))


def test_url_only_generation_is_ineligible_original(lane):
    writer, _, req, s3, provider, _ = lane
    create = provider.create
    def url_output(payload):
        response = create(payload)
        if payload.get('tools'):
            response['output'][0] = {'type': 'image_generation_call', 'status': 'completed',
                                     'image_url': 'https://mutable.fixture.invalid/a.png'}
        return response
    provider.create = url_output
    with pytest.raises(GeneratedReceiptHold, match='generated_original_output_invalid'):
        writer.issue(req)
    assert s3.puts == []


def test_stale_or_non_uuid_request_never_generates(lane):
    writer, _, req, _, provider, _ = lane
    with pytest.raises(GeneratedReceiptHold, match='generated_request_invalid'):
        writer.issue(replace(req, job_id='../other-tenant'))
    assert provider.calls == []


def test_public_pinned_original_readback_is_required(lane):
    writer, _, req, s3, _, _ = lane
    writer.storage.public_reader = lambda url: b'changed public body'
    with pytest.raises(GeneratedReceiptHold, match='generated_public_original_bytes_changed'):
        writer.issue(req)
    assert len(s3.puts) == 1


def test_stale_provider_output_cannot_relabel_old_card(lane):
    writer, _, req, s3, provider, _ = lane
    create = provider.create
    def stale(payload):
        result = create(payload)
        result['created_at'] -= 3600
        return result
    provider.create = stale
    with pytest.raises(GeneratedReceiptHold, match='generated_provider_execution_stale_or_future'):
        writer.issue(req)
    assert s3.puts == []


@pytest.fixture
def registry_env(lane, monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from agent import config
    writer, _, _, s3, _, _ = lane
    root = Ed25519PrivateKey.generate()
    controls = {name: True for name in ('producer_storage_denied', 'issuer_scope_create_only',
        'verifier_scope_read_only', 'receipt_append_only', 'credential_separation_verified',
        'existing_destination_authorized', 'retention_enforced')}
    controls.update(bucket='existing', endpoint='https://existing-storage.fixture.invalid',
        public_base='https://media.fixture.invalid', audit_owner='independent-storage-owner',
        audit_receipt_sha256=sha256(b'offline controls evidence'))
    payload = {'schema_version': 1, 'valid_until': '2026-10-08T16:00:00Z',
        'key': asdict(writer.key), 'owner_evidence_url': 'https://owner.fixture.invalid',
        'storage_controls': controls}
    path = tmp_path / 'registry.json'
    def save():
        path.write_text(canonical({'payload': payload,
            'signature_hex': root.sign(canonical(payload).encode()).hex()}))
    save()
    monkeypatch.setenv('AGENT_GENERATED_ORIGINAL_ISSUER_ENABLED', 'true')
    monkeypatch.setenv('AGENT_GENERATED_REGISTRY_PATH', str(path))
    monkeypatch.setenv('AGENT_GENERATED_REGISTRY_ROOT_PUBLIC_HEX', root.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
    for role in ('ISSUER', 'VERIFIER'):
        prefix = 'AGENT_GENERATED_' + role
        monkeypatch.setenv(prefix + '_S3_ACCESS_KEY_ID', role + '-access')
        monkeypatch.setenv(prefix + '_S3_SECRET_ACCESS_KEY', role + '-secret')
        monkeypatch.setenv(prefix + '_OPENAI_API_KEY', 'offline-provider-key')
        monkeypatch.setenv(prefix + '_OWNER_READ_TOKEN', 'offline-owner-token')
    monkeypatch.setenv('AGENT_GENERATED_ISSUER_PRIVATE_KEY_HEX', writer.private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()).hex())
    monkeypatch.setattr(config, 'S3_BUCKET', controls['bucket'])
    monkeypatch.setattr(config, 'S3_ENDPOINT', controls['endpoint'])
    monkeypatch.setattr(config, 'S3_PUBLIC_BASE_URL', controls['public_base'])
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=lambda *a, **kw: s3))
    return payload, save, path


def test_production_factory_requires_independent_signed_registry_and_role_split(registry_env, monkeypatch):
    assert type(issuer.configured_lane('issuer')) is issuer.GeneratedOriginalIssuer
    with pytest.raises(GeneratedReceiptHold, match='generated_verifier_has_signing_key'):
        issuer.configured_lane('verifier')
    monkeypatch.delenv('AGENT_GENERATED_ISSUER_PRIVATE_KEY_HEX')
    assert type(issuer.configured_lane('verifier')) is issuer.RetainedGenerationBackend


@pytest.mark.parametrize('control', ['producer_storage_denied', 'issuer_scope_create_only',
    'verifier_scope_read_only', 'receipt_append_only', 'credential_separation_verified',
    'existing_destination_authorized', 'retention_enforced'])
def test_registry_cannot_omit_namespace_or_iam_approval(registry_env, control):
    payload, save, _ = registry_env
    payload['storage_controls'][control] = False
    save()
    with pytest.raises(GeneratedReceiptHold, match='generated_storage_authorization_unverified'):
        issuer.configured_lane('issuer')


def test_registry_signature_tamper_holds(registry_env):
    payload, _, path = registry_env
    envelope = json.loads(path.read_text())
    envelope['payload']['storage_controls']['bucket'] = 'other-destination'
    path.write_text(canonical(envelope))
    with pytest.raises(GeneratedReceiptHold, match='generated_external_signature_invalid'):
        issuer.configured_lane('issuer')


def test_registry_cannot_switch_existing_destination(registry_env):
    payload, save, _ = registry_env
    payload['storage_controls']['bucket'] = 'new-paid-resource'
    save()
    with pytest.raises(GeneratedReceiptHold, match='generated_existing_storage_destination_unverified'):
        issuer.configured_lane('issuer')


def test_registry_expired_holds(registry_env):
    payload, save, _ = registry_env
    payload['valid_until'] = '2026-10-06T16:00:00Z'
    save()
    with pytest.raises(GeneratedReceiptHold, match='generated_registry_expired'):
        issuer.configured_lane('issuer')


def test_mutable_hoster_credential_cannot_be_issuer(registry_env, monkeypatch):
    from agent import config
    monkeypatch.setenv(config.S3_ACCESS_KEY_ID_ENV, 'ISSUER-access')
    with pytest.raises(GeneratedReceiptHold, match='generated_storage_credentials_not_separate'):
        issuer.configured_lane('issuer')


def test_signed_runtime_input_drift_cannot_be_approved_as_current_review(lane):
    writer, backend, req, s3, _, _ = lane
    packet = writer.issue(req)
    key = issuer.receipt_key(req)
    envelope = json.loads(s3.objects[key])
    envelope['payload']['review_request']['input'][0]['content'][1]['image_url'] = 'data:image/png;base64,' + base64.b64encode(png('red')).decode()
    envelope['signature_hex'] = writer.private.sign(canonical(envelope['payload']).encode()).hex()
    s3.objects[key] = canonical(envelope).encode()
    with pytest.raises(GeneratedReceiptHold, match='generated_independent_pixel_brand_review_failed'):
        backend.verified_evidence(req, canonical(packet['payload']))
