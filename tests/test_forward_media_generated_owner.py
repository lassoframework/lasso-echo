"""Offline read API boundaries. No source completeness/provisioning evidence."""
import base64
from dataclasses import asdict
import json
import uuid
from unittest.mock import patch

import pytest

from agent import forward_media_generated_owner as owner
from agent.forward_media_generated_issuer import OwnerEvidenceClient, checked_snapshot, checked_history
from agent.forward_media_generated_prepare import GeneratedReceiptHold, GenerationRequest, PreparedGeneratedOriginal, sha256
from agent.forward_media_owner import ForwardMediaOwnerPersistence
from tests.test_forward_media_generated_issuer import png, Owner

TOKEN = 'SYNTHETIC-owner-read-token-32-characters'


def make_request():
    return GenerationRequest('gym', str(uuid.uuid4()), str(uuid.uuid4()), '2026-10-10',
        '2026-10-07T16:00:00Z', 'palette-1', sha256(b'palette'), 'copy-1',
        sha256(b'copy'), owner.POLICY)


def setup_api(enabled=True):
    req = make_request()
    binding = owner.ReadTokenBinding(TOKEN, 'verifier', frozenset({req.tenant_id}))
    store = owner.OwnerEvidenceReadStore(None, 'SYNTHETIC-reader')
    api = owner.OwnerEvidenceReadAPI(store, [binding], enabled=enabled)
    diagnostics = {'history_revision': sha256(b'history'), 'photo_inventory_revision': sha256(b'photos')}
    return req, api, diagnostics


def body(req, data=None):
    packet = {'request': asdict(req)}
    if data is not None:
        packet.update(image_sha256=sha256(data), image_base64=base64.b64encode(data).decode())
    return json.dumps(packet).encode()


@pytest.mark.parametrize('enabled', [False, None, 'true', 1])
def test_default_off_avoids_parsing_and_reads(enabled):
    _, api, _ = setup_api(enabled)
    with patch.object(owner.OwnerEvidenceReadStore, 'diagnostics', side_effect=AssertionError('read')):
        assert api.handle('POST', '/snapshot', 'Bearer '+TOKEN, b'invalid')[0] == 503


@pytest.mark.parametrize('auth', [None, '', 'Bearer bad', 'bearer '+TOKEN])
def test_auth_precedes_request_and_pixels(auth):
    _, api, _ = setup_api()
    with patch.object(owner.OwnerEvidenceReadStore, 'diagnostics', side_effect=AssertionError('read')):
        assert api.handle('POST', '/history-check', auth, b'invalid')[0] == 401


def test_duplicate_token_configuration_is_denied():
    req, api, _ = setup_api()
    api.bindings = api.bindings * 2
    assert api.handle('POST', '/snapshot', 'Bearer '+TOKEN, body(req))[0] == 401


def test_tenant_allowlist_and_no_mutation_route():
    req, api, _ = setup_api()
    packet = asdict(req); packet['tenant_id'] = 'other'
    with patch.object(owner.OwnerEvidenceReadStore, 'diagnostics', side_effect=AssertionError('read')):
        assert api.handle('POST', '/snapshot', 'Bearer '+TOKEN, json.dumps({'request': packet}).encode())[0] == 403
        assert api.handle('POST', '/reserve', 'Bearer '+TOKEN, body(req))[0] == 404
        assert api.handle('GET', '/snapshot', 'Bearer '+TOKEN, body(req))[0] == 404


@pytest.mark.parametrize('field,value', [('job_id', 'bad'), ('request_id', 'bad'),
    ('post_date', 'bad'), ('requested_at', 'bad'), ('requested_at', '2026-10-07T00:00:00+00:00'),
    ('tenant_id', ''), ('palette_digest', 'bad'), ('pixel_policy_id', 'other')])
def test_request_shape_precedes_reads(field, value):
    req, api, _ = setup_api()
    packet = asdict(req); packet[field] = value
    with patch.object(owner.OwnerEvidenceReadStore, 'diagnostics', side_effect=AssertionError('read')):
        code, result = api.handle('POST', '/snapshot', 'Bearer '+TOKEN, json.dumps({'request': packet}).encode())
        assert code == 422 and result['reason'] == 'generated_owner_request_invalid'


def test_duplicate_json_keys_and_excess_snapshot_body_hold():
    req, api, _ = setup_api()
    encoded = json.dumps(asdict(req))
    assert api.handle('POST', '/snapshot', 'Bearer '+TOKEN,
        ('{"request":'+encoded+',"request":'+encoded+'}').encode())[0] == 422
    assert api.handle('POST', '/snapshot', 'Bearer '+TOKEN, b' ' * 32769)[0] == 422


def test_read_snapshot_and_history_never_attest_missing_authority():
    req, api, diagnostics = setup_api()
    # Even forged positives from an offline store cannot reach the contract.
    diagnostics.update(history_complete=True, eligible_photo_count=0, brand_source_verified=True)
    with patch.object(owner.OwnerEvidenceReadStore, 'diagnostics', return_value=diagnostics):
        code, snapshot = api.handle('POST', '/snapshot', 'Bearer '+TOKEN, body(req))
        assert code == 200 and snapshot['request'] == asdict(req)
        assert snapshot['photo_inventory_complete'] is False
        assert snapshot['eligible_photo_count'] is None
        assert snapshot['brand_source_verified'] is False and snapshot['palette'] is None
        code, history = api.handle('POST', '/history-check', 'Bearer '+TOKEN, body(req, png()))
        assert code == 200 and history['image_sha256'] == sha256(png())
        assert history['history_complete'] is False and history['reviewed_no_match'] is False


@pytest.mark.parametrize('mutation', ['sha', 'base64', 'nonimage', 'tiny', 'extra'])
def test_pixels_invalid_or_rebound_precede_reads(mutation):
    req, api, _ = setup_api()
    packet = json.loads(body(req, png()))
    if mutation == 'sha': packet['image_sha256'] = sha256(b'other')
    if mutation == 'base64': packet['image_base64'] = 'bad!'
    if mutation == 'nonimage':
        packet.update(image_sha256=sha256(b'raw'), image_base64=base64.b64encode(b'raw').decode())
    if mutation == 'tiny':
        import io
        from PIL import Image
        out = io.BytesIO(); Image.new('RGB', (1, 1)).save(out, 'PNG')
        packet.update(image_sha256=sha256(out.getvalue()), image_base64=base64.b64encode(out.getvalue()).decode())
    if mutation == 'extra': packet['reviewed_no_match'] = True
    with patch.object(owner.OwnerEvidenceReadStore, 'diagnostics', side_effect=AssertionError('read')):
        assert api.handle('POST', '/history-check', 'Bearer '+TOKEN, json.dumps(packet).encode())[0] == 422


def test_existing_client_holds_on_adapter_before_generation_or_preparation():
    req, api, diagnostics = setup_api()
    class HTTP:
        def post(self, url, headers, json, timeout):
            code, result = api.handle('POST', '/'+url.rsplit('/', 1)[-1], headers['Authorization'],
                                      __import__('json').dumps(json).encode())
            class Response:
                status_code = code
                def json(self): return result
            return Response()
    client = OwnerEvidenceClient('https://owner.invalid', TOKEN, HTTP())
    with patch.object(owner.OwnerEvidenceReadStore, 'diagnostics', return_value=diagnostics):
        with pytest.raises(GeneratedReceiptHold, match='generated_current_owner_snapshot_unverified'):
            checked_snapshot(client, req)
        with pytest.raises(GeneratedReceiptHold, match='generated_history_used_or_uncertain'):
            checked_history(client, req, png(), Owner(req).s)


def test_final_bridge_cannot_turn_constructed_preparation_into_reservation():
    _, api, _ = setup_api()
    prepared = PreparedGeneratedOriginal('{}', 'untrusted', 'untrusted', b'bytes', 'h', 'p', 'now')
    # Constructor bypass is only an offline negative fixture. Neither object is
    # touched: no signature strings or dataclass alone can become DB authority.
    persistence = object.__new__(ForwardMediaOwnerPersistence)
    with pytest.raises(GeneratedReceiptHold, match='generated_owner_reservation_disabled'):
        owner.stage_prepared_generated_original(persistence, prepared)
    with pytest.raises(GeneratedReceiptHold, match='generated_owner_reservation_authority_unavailable'):
        owner.stage_prepared_generated_original(persistence, prepared, enabled=True)
