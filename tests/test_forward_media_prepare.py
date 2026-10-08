"""Owner-only preparation helper acceptance; no network/DB/production writes."""
import hashlib
import re

import pytest

from agent import forward_media_prepare as prep


TENANT = 'gym_alpha'
ASSET = 'original_abc123'
URL = 'https://owned.example/media/fresh.png'
BYTES = b'freshly produced original bytes'
FP = 'md5:' + hashlib.md5(BYTES).hexdigest()
DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')


def registered():
    return prep.register_original(TENANT, ASSET, URL, BYTES, 'owner-verify-receipt-1')


def test_register_original_computes_exact_fingerprint_and_length():
    original = registered()
    assert original.source_fingerprint == FP
    assert original.source_length == len(BYTES)
    assert original.row()['registry_evidence_ref'] == 'owner-verify-receipt-1'


@pytest.mark.parametrize('field,value', [
    ('tenant_id', ''), ('tenant_id', '  padded'), ('source_asset_id', ' '),
    ('source_url', 'http://insecure.example/x'), ('source_url', 'https://x.example/a b'),
    ('registry_evidence_ref', ''), ('registry_evidence_ref', '   '),
])
def test_register_original_rejects_bad_identity(field, value):
    kwargs = dict(tenant_id=TENANT, source_asset_id=ASSET, source_url=URL,
                  source_bytes=BYTES, registry_evidence_ref='ref')
    kwargs[field] = value
    with pytest.raises(prep.PreparationError):
        prep.register_original(**kwargs)


@pytest.mark.parametrize('data', [b'', '', None, bytearray()])
def test_fingerprint_requires_actual_bytes(data):
    with pytest.raises(prep.PreparationError):
        prep.fingerprint_bytes(data)


def test_fingerprint_rejects_oversize():
    with pytest.raises(prep.PreparationError):
        prep.fingerprint_bytes(b'x' * (prep.MAX_OBJECT_LENGTH + 1))


def test_cleared_unused_requires_production_and_history_evidence():
    original = registered()
    clearance = prep.clear_history(original, 'cleared_unused',
                                   'independent-fleet-audit-7',
                                   production_evidence_ref='generation-receipt-9')
    assert clearance.decision == 'cleared_unused'
    assert clearance.row()['source_fingerprint'] == FP


def test_cleared_unused_refuses_without_production_evidence():
    original = registered()
    with pytest.raises(prep.PreparationError):
        prep.clear_history(original, 'cleared_unused', 'independent-fleet-audit-7')
    with pytest.raises(prep.PreparationError):
        prep.clear_history(original, 'cleared_unused', 'independent-fleet-audit-7',
                           production_evidence_ref='  ')


@pytest.mark.parametrize('decision', ['hold_uncertain', 'hold_used'])
def test_holds_require_real_history_evidence(decision):
    original = registered()
    with pytest.raises(prep.PreparationError):
        prep.clear_history(original, decision, '')
    clearance = prep.clear_history(original, decision, 'fleet-history-evidence')
    assert clearance.decision == decision


def test_unknown_decision_rejected():
    with pytest.raises(prep.PreparationError):
        prep.clear_history(registered(), 'maybe_fresh', 'evidence')


def test_same_object_manifest_binds_original_bytes_and_url():
    original = registered()
    manifest = prep.build_render_manifest(original, URL, BYTES, 'same_object',
                                          'owner-render-evidence')
    assert DIGEST_RE.match(manifest.manifest_digest)
    assert manifest.image_fingerprint == FP
    assert manifest.image_length == len(BYTES)
    assert manifest.thumbnail_url is None
    # Deterministic digest for identical inputs.
    again = prep.build_render_manifest(original, URL, BYTES, 'same_object',
                                       'owner-render-evidence')
    assert again.manifest_digest == manifest.manifest_digest


def test_same_object_rejects_different_bytes_or_url():
    original = registered()
    with pytest.raises(prep.PreparationError):
        prep.build_render_manifest(original, URL, b'other bytes', 'same_object', 'ref')
    with pytest.raises(prep.PreparationError):
        prep.build_render_manifest(original, 'https://owned.example/other', BYTES,
                                   'same_object', 'ref')


def test_render_manifest_thumbnail_all_or_nothing():
    original = registered()
    thumb = b'thumbnail bytes'
    manifest = prep.build_render_manifest(original, 'https://owned.example/img', b'image bytes',
                                          'render', 'render-evidence',
                                          thumbnail_url='https://owned.example/thumb',
                                          thumbnail_bytes=thumb)
    assert manifest.thumbnail_fingerprint == 'md5:' + hashlib.md5(thumb).hexdigest()
    assert manifest.thumbnail_length == len(thumb)
    with pytest.raises(prep.PreparationError):
        prep.build_render_manifest(original, 'https://owned.example/img', b'image bytes',
                                   'render', 'render-evidence',
                                   thumbnail_url='https://owned.example/thumb')


@pytest.mark.parametrize('operation', ['reshoot', '', None])
def test_manifest_operation_controlled(operation):
    with pytest.raises(prep.PreparationError):
        prep.build_render_manifest(registered(), 'https://owned.example/img', b'bytes',
                                   operation, 'ref')


def test_manifest_requires_render_evidence():
    with pytest.raises(prep.PreparationError):
        prep.build_render_manifest(registered(), 'https://owned.example/img', b'bytes',
                                   'render', '  ')


def test_generated_original_requires_genuine_evidence():
    with pytest.raises(prep.PreparationError):
        prep.prepare_generated_original(TENANT, ASSET, URL, BYTES, '', 'reg-ref', 'hist-ref')
    original, clearance = prep.prepare_generated_original(
        TENANT, ASSET, URL, BYTES, 'astra-generation-receipt-42',
        'owner-verify-receipt-1', 'independent-fleet-audit-7')
    assert original.source_fingerprint == FP
    assert clearance.decision == 'cleared_unused'
    assert clearance.history_evidence_ref == 'independent-fleet-audit-7'


def test_generated_original_without_history_audit_still_refuses():
    # Generation receipt alone never self-certifies unknown history.
    with pytest.raises(prep.PreparationError):
        prep.prepare_generated_original(TENANT, ASSET, URL, BYTES,
                                        'astra-generation-receipt-42',
                                        'owner-verify-receipt-1', '')


def test_rows_match_draft_column_names():
    original = registered()
    clearance = prep.clear_history(original, 'cleared_unused', 'hist',
                                   production_evidence_ref='prod')
    manifest = prep.build_render_manifest(original, URL, BYTES, 'same_object', 'render-ref')
    assert set(original.row()) == {'tenant_id', 'source_asset_id', 'source_url',
                                   'source_fingerprint', 'source_length',
                                   'registry_evidence_ref'}
    assert set(clearance.row()) == set(original.row()) | {'decision', 'history_evidence_ref'}
    assert set(manifest.row()) == {'manifest_digest', 'tenant_id', 'source_asset_id',
                                   'image_url', 'image_fingerprint', 'image_length',
                                   'thumbnail_url', 'thumbnail_fingerprint',
                                   'thumbnail_length', 'operation', 'render_recipe',
                                   'render_evidence_ref'}
