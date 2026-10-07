"""Offline trust-contract fixtures only; no provider/storage/DB connections."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from agent.forward_media_generated_prepare import (
    ApprovedGenerationKey, GeneratedReceiptHold, GenerationRequest,
    PreparedGeneratedOriginal, VerifiedGenerationEvidence, canonical,
    prepare_generated_original, sha256,
)

NOW = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)


class FixtureBackend:
    """A deliberate trusted test seam, never a production backend."""
    def __init__(self, key, evidence):
        self.key, self.evidence, self.calls = key, evidence, []

    def approved_key(self, key_id):
        self.calls.append('key')
        return self.key

    def verified_evidence(self, request, text):
        self.calls.append('evidence')
        return self.evidence


@pytest.fixture
def package():
    data = b'offline fixture output bytes'
    request = GenerationRequest('gym-a', str(uuid.uuid4()), str(uuid.uuid4()),
        '2026-10-08', '2026-10-07T15:59:00Z', 'palette-17', sha256(b'palette'),
        'copy-23', sha256(b'approved source copy'), 'approved-pixel-policy-v4')
    p = {**request.__dict__, 'schema_version': 1, 'receipt_id': str(uuid.uuid4()),
        'key_id': 'independent-key-1', 'created_at': '2026-10-07T15:59:30Z',
        'provider': 'fixture-provider', 'model': 'fixture-astra-generator',
        'runtime_id': 'trusted-runtime-9', 'provider_output_id': 'fresh-output-11',
        'image_url': 'https://fixture.invalid/gym-a/pinned-original.png',
        'object_version': 'immutable-version-9', 'image_sha256': sha256(data),
        'image_fingerprint': 'md5:' + hashlib.md5(data).hexdigest(), 'image_length': len(data),
        'pixel_review_id': 'review-41'}
    private = Ed25519PrivateKey.generate()
    key = ApprovedGenerationKey(p['key_id'], private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex(), True,
        'independent_generation_verifier', p['provider'], p['model'], p['runtime_id'])
    evidence = VerifiedGenerationEvidence(canonical(p), '2026-10-07T15:59:45Z',
        True, True, False, True, data, True, True, True, True,
        'complete-corpus-101', True, 0, 'complete-photo-inventory-17')
    packet = {'payload': p, 'signature_hex': private.sign(canonical(p).encode()).hex()}
    return packet, request, FixtureBackend(key, evidence), private


def run(package, **kwargs):
    packet, request, backend, _ = package
    return prepare_generated_original(packet, request, backend, enabled=True, now=NOW, **kwargs)


def resign(package):
    packet, _, backend, private = package
    text = canonical(packet['payload'])
    packet['signature_hex'] = private.sign(text.encode()).hex()
    backend.evidence = replace(backend.evidence, receipt_payload_json=text)


def held(package, reason):
    with pytest.raises(GeneratedReceiptHold, match='^'+reason+'$'):
        run(package)


def test_valid_preparation_freezes_bytes_without_database_authority(package):
    prepared = run(package)
    assert type(prepared) is PreparedGeneratedOriginal
    assert prepared.image_bytes == package[2].evidence.immutable_image_bytes
    assert prepared.receipt_ref.startswith('generated-receipt:sha256:')
    assert prepared.history_revision == 'complete-corpus-101'
    assert not hasattr(prepared, 'clearance')
    assert not hasattr(prepared, 'manifest')
    # Frozen canonical payload does not retain a mutable producer dictionary.
    package[0]['payload']['tenant_id'] = 'later-mutation'
    assert prepared.payload['tenant_id'] == 'gym-a'
    prepared.payload['tenant_id'] = 'second-mutation'
    assert prepared.payload['tenant_id'] == 'gym-a'


def test_default_off_and_unconfigured_backend_hold(package):
    p, r, b, _ = package
    with pytest.raises(GeneratedReceiptHold, match='generated_preparation_disabled'):
        prepare_generated_original(p, r, b, now=NOW)
    assert b.calls == []
    with pytest.raises(GeneratedReceiptHold, match='generated_evidence_backend_unavailable'):
        prepare_generated_original(p, r, enabled=True, now=NOW)


def test_fake_receipt_never_reads_provider_evidence(package):
    package[0]['signature_hex'] = '00' * 64
    held(package, 'generated_signature_invalid')
    assert package[2].calls == ['key']


@pytest.mark.parametrize('field,value', [
    ('tenant_id', 'gym-b'), ('post_date', '2026-10-09'),
    ('job_id', str(uuid.uuid4())), ('request_id', str(uuid.uuid4())),
    ('palette_revision', 'old-palette'), ('palette_digest', sha256(b'old palette')),
    ('source_copy_revision', 'old-copy'), ('source_copy_digest', sha256(b'old copy')),
    ('pixel_policy_id', 'old-pixel-policy'),
])
def test_correctly_signed_wrong_or_stale_binding_holds(package, field, value):
    package[0]['payload'][field] = value
    resign(package)
    held(package, 'generated_request_binding_changed')
    assert package[2].calls == []


@pytest.mark.parametrize('field,value,reason', [
    ('provider_authenticated', False, 'generated_provider_or_runtime_unverified'),
    ('runtime_authenticated', False, 'generated_provider_or_runtime_unverified'),
    ('cache_hit', True, 'generated_cached_output_ineligible'),
    ('cache_hit', None, 'generated_cached_output_ineligible'),
    ('immutable_storage_verified', False, 'generated_immutable_storage_unverified'),
    ('immutable_image_bytes', b'changed bytes', 'generated_original_bytes_changed'),
    ('brand_source_verified', False, 'generated_brand_source_unverified'),
    ('pixel_review_passed', False, 'generated_pixel_review_failed'),
    ('history_complete', False, 'generated_history_used_or_uncertain'),
    ('history_reviewed_no_match', False, 'generated_history_used_or_uncertain'),
    ('history_revision', '', 'generated_history_used_or_uncertain'),
    ('photo_inventory_complete', False, 'generated_photo_first_hold'),
    ('eligible_photo_count', 1, 'generated_photo_first_hold'),
    ('eligible_photo_count', False, 'generated_photo_first_hold'),
    ('photo_inventory_revision', '', 'generated_photo_first_hold'),
])
def test_independent_evidence_must_prove_every_requirement(package, field, value, reason):
    package[2].evidence = replace(package[2].evidence, **{field: value})
    held(package, reason)


def test_unavailable_provider_proof_scrubs_error(package):
    def unavailable(*args):
        raise RuntimeError('https://private.invalid/token-secret')
    package[2].verified_evidence = unavailable
    held(package, 'generated_verification_unavailable')


def test_mutable_artifact_store_or_free_form_producer_reference_is_not_evidence(package):
    package[2].evidence = {'provider_authenticated': True, 'producer_ref': 'looks-reviewed'}
    held(package, 'generated_evidence_binding_changed')


def test_approved_signer_must_be_independent(package):
    package[2].key = replace(package[2].key, role='producer')
    held(package, 'generated_signer_unapproved')


def test_revoked_key(package):
    package[2].key = replace(package[2].key, approved=False)
    held(package, 'generated_signer_unapproved')


def test_wrong_backend_evidence_binding(package):
    package[2].evidence = replace(package[2].evidence, receipt_payload_json='{}')
    held(package, 'generated_evidence_binding_changed')


def test_created_before_job_cannot_relabel_old_artwork(package):
    package[0]['payload']['created_at'] = '2026-10-07T15:58:00Z'
    resign(package)
    held(package, 'generated_receipt_stale_or_future')


def test_stale_receipt(package):
    with pytest.raises(GeneratedReceiptHold, match='generated_receipt_stale_or_future'):
        prepare_generated_original(*package[:3], enabled=True,
            now=datetime(2026, 10, 7, 16, 30, tzinfo=timezone.utc))


@pytest.mark.parametrize('observed', ['2026-10-07T15:59:00Z', '2026-10-07T16:01:00Z'])
def test_stale_or_future_independent_evidence(package, observed):
    package[2].evidence = replace(package[2].evidence, observed_at=observed)
    held(package, 'generated_evidence_stale_or_future')


@pytest.mark.parametrize('field,value', [('schema_version', 2), ('schema_version', True)])
def test_unknown_receipt_version_holds(package, field, value):
    package[0]['payload'][field] = value
    held(package, 'generated_receipt_version_unsupported')


def test_extra_unsigned_or_unknown_fields_hold(package):
    package[0]['payload']['producer_evidence_ref'] = 'trust me'
    held(package, 'generated_receipt_shape_invalid')


def test_same_length_changed_bytes_hold(package):
    old = package[2].evidence.immutable_image_bytes
    package[2].evidence = replace(package[2].evidence, immutable_image_bytes=b'X' * len(old))
    held(package, 'generated_original_bytes_changed')


def test_malformed_signature_never_exposes_input(package):
    package[0]['signature_hex'] = 'private-input-is-not-hex'
    held(package, 'generated_verification_unavailable')


def test_boolean_length_is_not_an_integer(package):
    package[0]['payload']['image_length'] = True
    held(package, 'generated_receipt_shape_invalid')


def test_producer_claim_cannot_switch_approved_runtime(package):
    package[0]['payload']['runtime_id'] = 'unapproved-runtime'
    resign(package)
    held(package, 'generated_signer_unapproved')


def test_missing_provider_output_identity(package):
    package[0]['payload']['provider_output_id'] = ''
    held(package, 'generated_receipt_shape_invalid')
