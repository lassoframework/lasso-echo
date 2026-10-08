"""Planner alias routing keeps raw calendar ownership and canonical batch identity."""
from copy import deepcopy
import hashlib
import json
import uuid

import pytest

from agent import portal_calendar_store as pcs
from agent import forward_media_observation_bridge as bridge
from test_forward_schedule_reservation import (
    _HTTP, _Resp, _group_rows, _stage_receipt, _store,
)

RAW = 'alias-gym'
CANONICAL = 'canonical-gym'


def own_row():
    return {'id': str(uuid.uuid4()), 'gym_id': RAW, 'status': 'pending',
            'logical_post_id': str(uuid.uuid4()), 'post_date': '2026-10-20',
            'account': 'instagram', 'format': 'feed', 'source_media_url': 'https://x/source',
            'image_url': 'https://x/image', 'caption': 'SYNTHETIC caption'}


def own_old():
    return dict(own_row(), variant_status='active', retained_null=None)


def http_for_alias(*, response=None, stage=_stage_receipt):
    return _HTTP(
        gets={pcs._FORWARD_TENANT_ALIAS_TABLE: response or _Resp(
            200, [{'alias_key': RAW, 'tenant_id': CANONICAL}])},
        posts={pcs._STAGE_RPC: stage})


def stage_args(http):
    return next(c[2] for c in http.calls if c[0] == 'post' and c[1].endswith(pcs._STAGE_RPC))


def producer_row(tenant=RAW):
    """Run the real still producer, including replay and hosted byte readback."""
    from io import BytesIO
    from PIL import Image
    from agent.gym_media_builder import still_materialization_observation
    from agent.forward_media_visual_index import phash_v1
    buffer = BytesIO()
    Image.new('RGB', (24, 32), (31, 84, 120)).save(buffer, format='PNG')
    source = buffer.getvalue()
    row = dict(own_row(), source_media_asset_id='alias-original')
    observation = still_materialization_observation(
        source, source, row['image_url'], tenant=tenant,
        source_asset_id=row['source_media_asset_id'], source_url=row['source_media_url'],
        bytes_fn=lambda _: source)
    row[bridge.METADATA] = [observation]
    row[pcs.RESERVATION_PROOF] = {'source_sha256': hashlib.sha256(source).hexdigest(),
        'phash_v1': phash_v1(source), 'source_media_asset_id': row['source_media_asset_id']}
    return row, observation


def rehash(observation):
    observation['observation_digest'] = hashlib.sha256(bridge._json(
        {k: v for k, v in observation.items() if k != 'observation_digest'}).encode()).hexdigest()


def test_real_producer_insert_rows_projects_only_unverified_tenant_and_self_hash(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, '1')
    monkeypatch.setenv(bridge.ENV, '1')
    row, original = producer_row()
    before = deepcopy(row)
    http = http_for_alias()
    http.posts[bridge.READY_RPC] = _Resp(200, True)
    store = _store(http)
    rows = store.insert_rows(RAW, [row], expected_old_rows=[])
    packet = json.loads(stage_args(http)['p_request'])['members'][0]['observation']
    projected = json.loads(packet['observation_json'])
    assert projected['tenant'] == CANONICAL
    assert json.loads(packet['digest_input']) == {
        k: v for k, v in projected.items() if k != 'observation_digest'}
    assert projected['observation_digest'] == hashlib.sha256(packet['digest_input'].encode()).hexdigest()
    assert projected['observation_digest'] != original['observation_digest']
    assert {k: v for k, v in projected.items() if k not in ('tenant', 'observation_digest')} == {
        k: v for k, v in original.items() if k not in ('tenant', 'observation_digest')}
    assert projected['provenance_status'] == 'unverified' and projected['hold_reasons']
    assert row == before and original['tenant'] == RAW
    assert rows[0]['gym_id'] == RAW and rows[0]['variant_status'] == 'candidate'
    assert store.last_forward_stage['observation_row_ids'] == [rows[0]['id']]
    first_args = stage_args(http)
    store.insert_rows(RAW, [row], expected_old_rows=[])
    assert [c[2] for c in http.calls if c[1].endswith(pcs._STAGE_RPC)] == [first_args, first_args]


@pytest.mark.parametrize('attack', ['foreign', 'digest', 'signed', 'edge'])
def test_real_producer_insert_rows_refuses_foreign_or_forged_packet(monkeypatch, attack):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, '1')
    monkeypatch.setenv(bridge.ENV, '1')
    row, observation = producer_row()
    if attack == 'foreign':
        observation['tenant'] = 'another-raw-account'
        rehash(observation)
    elif attack == 'digest':
        observation['source_sha256'] = 'f' * 64  # original digest stays unchanged
    elif attack == 'signed':
        observation['provenance_status'] = 'verified'
        observation['signature'] = 'SYNTHETIC signed authority must never be projected'
        rehash(observation)
    else:
        observation['source_exact_url'] = 'https://foreign/source'
        rehash(observation)
    http = http_for_alias()
    http.posts[bridge.READY_RPC] = _Resp(200, True)
    store = _store(http)
    with pytest.raises((pcs.CalendarInsertNotStartedError, pcs.ReservationArgumentError)):
        store.insert_rows(RAW, [row], expected_old_rows=[])
    assert not [c for c in http.calls if c[1].endswith(pcs._STAGE_RPC)]
    assert store.last_forward_stage_attempt is None


@pytest.mark.parametrize('tenant', [RAW, CANONICAL])
def test_stage_accepts_intact_own_raw_or_resolved_canonical_packet(tenant):
    row, observation = producer_row(tenant)
    row.pop(bridge.METADATA)
    row.pop(pcs.RESERVATION_PROOF)
    packet = bridge.prepare(dict(row, variant_status='candidate',
        media_not_ready_reason='forward_reservation_staged'), [observation])
    row['observation'] = {k: packet[k] for k in ('observation_json', 'digest_input')}
    http = http_for_alias()
    _store(http).stage_forward_schedule_batch(RAW, [row])
    projected = json.loads(json.loads(stage_args(http)['p_request'])['members'][0]
                           ['observation']['observation_json'])
    assert projected['tenant'] == CANONICAL


def test_stage_does_not_repair_forged_digest_preimage():
    row, observation = producer_row()
    row.pop(bridge.METADATA)
    row.pop(pcs.RESERVATION_PROOF)
    packet = bridge.prepare(dict(row, variant_status='candidate',
        media_not_ready_reason='forward_reservation_staged'), [observation])
    row['observation'] = {'observation_json': packet['observation_json'],
                          'digest_input': packet['digest_input'].replace(RAW, 'foreign')}
    http = http_for_alias()
    store = _store(http)
    with pytest.raises(pcs.ReservationArgumentError, match='unverified observation packet invalid'):
        store.stage_forward_schedule_batch(RAW, [row])
    assert not [c for c in http.calls if c[0] == 'post']
    assert store.last_forward_stage_attempt is None


def test_stage_verifies_exact_valid_preimage_before_canonical_projection():
    row, observation = producer_row()
    row.pop(bridge.METADATA)
    row.pop(pcs.RESERVATION_PROOF)
    preimage = json.dumps({k: v for k, v in observation.items() if k != 'observation_digest'}, indent=2)
    observation['observation_digest'] = hashlib.sha256(preimage.encode()).hexdigest()
    row['observation'] = {'observation_json': json.dumps(observation, indent=2), 'digest_input': preimage}
    http = http_for_alias()
    _store(http).stage_forward_schedule_batch(RAW, [row])
    packet = json.loads(stage_args(http)['p_request'])['members'][0]['observation']
    projected = json.loads(packet['observation_json'])
    assert projected['tenant'] == CANONICAL
    assert projected['observation_digest'] == hashlib.sha256(packet['digest_input'].encode()).hexdigest()


def test_stage_routes_canonical_batch_preserving_raw_members_and_frozen_old_rows():
    http = http_for_alias()
    store = _store(http)
    row, old = own_row(), own_old()
    before = deepcopy([row, old])
    receipt = store.stage_forward_schedule_batch(RAW, [row], [old])
    args = stage_args(http)
    request = json.loads(args['p_request'])
    assert request['tenant_id'] == CANONICAL == args['p_tenant_id'] == receipt['tenant_id']
    assert request['members'][0]['row'] == before[0]
    assert request['old_rows'] == [before[1]] and 'retained_null' in request['old_rows'][0]
    assert args['p_request_digest'] == hashlib.sha256(args['p_request'].encode()).hexdigest()
    assert args['p_batch_id'] == pcs.forward_batch_identity(CANONICAL, args['p_request_digest'])
    assert store.last_forward_stage_attempt == {
        'batch_id': args['p_batch_id'], 'tenant_id': CANONICAL,
        'request_digest': args['p_request_digest'], 'member_row_ids': [row['id']],
        'old_row_ids': [old['id']]}
    assert http.calls[0][2] == {
        'select': 'alias_key,tenant_id', 'alias_key': 'eq.' + RAW, 'limit': '2'}
    row['gym_id'] = 'changed'
    old['caption'] = 'changed'
    assert json.loads(args['p_request'])['members'][0]['row'] == before[0]
    assert json.loads(args['p_request'])['old_rows'] == [before[1]]


def test_insert_rows_entrypoint_keeps_raw_account_and_routes_resolved_tenant(monkeypatch):
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, '1')
    http = http_for_alias()
    old = own_old()
    rows = _store(http).insert_rows(RAW, _group_rows(), expected_old_rows=[old])
    assert all(r['gym_id'] == RAW and r['variant_status'] == 'candidate' for r in rows)
    request = json.loads(stage_args(http)['p_request'])
    assert request['tenant_id'] == CANONICAL
    assert all(m['row']['gym_id'] == RAW for m in request['members'])
    assert request['old_rows'] == [old]
    assert [c[0] for c in http.calls if c[0] != 'get'] == ['post']


def test_unmapped_raw_key_matches_sql_identity_fallback():
    http = http_for_alias(response=_Resp(200, []))
    result = _store(http).stage_forward_schedule_batch(RAW, [own_row()])
    assert result['tenant_id'] == RAW
    assert json.loads(stage_args(http)['p_request'])['tenant_id'] == RAW


@pytest.mark.parametrize('response', [
    _Resp(403, []), _Resp(503, []), _Resp(200, {}), _Resp(200, ValueError('invalid JSON')),
    _Resp(200, [{'alias_key': 'foreign', 'tenant_id': CANONICAL}]),
    _Resp(200, [{'alias_key': RAW, 'tenant_id': ''}]),
    _Resp(200, [{'alias_key': RAW, 'tenant_id': ' padded '}]),
    _Resp(200, [{'alias_key': RAW, 'tenant_id': None}]),
    _Resp(200, [{'alias_key': RAW, 'tenant_id': 7}]),
    _Resp(200, [None]),
    _Resp(200, [{'alias_key': RAW, 'tenant_id': CANONICAL}] * 2),
])
def test_bad_alias_read_refuses_before_any_stage_attempt(response):
    http = http_for_alias(response=response)
    store = _store(http)
    with pytest.raises(pcs.CalendarInsertNotStartedError, match='tenant unavailable'):
        store.stage_forward_schedule_batch(RAW, [own_row()])
    assert not [c for c in http.calls if c[0] == 'post']
    assert store.last_forward_stage_attempt is None and store.last_forward_stage is None


def test_alias_transport_failure_refuses_before_any_stage_attempt():
    def failed(_params): raise TimeoutError('SYNTHETIC read failure')
    http = http_for_alias()
    http.gets[pcs._FORWARD_TENANT_ALIAS_TABLE] = failed
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        _store(http).stage_forward_schedule_batch(RAW, [own_row()])
    assert not [c for c in http.calls if c[0] == 'post']


@pytest.mark.parametrize('row_key,old_key', [(CANONICAL, RAW), (RAW, CANONICAL), ('foreign', RAW)])
def test_canonical_alias_never_expands_raw_calendar_ownership(row_key, old_key):
    http = http_for_alias()
    row, old = own_row(), own_old()
    row['gym_id'], old['gym_id'] = row_key, old_key
    with pytest.raises(pcs.ReservationArgumentError):
        _store(http).stage_forward_schedule_batch(RAW, [row], [old])
    assert not http.calls


def test_canonical_account_cannot_stage_another_raw_account_rows():
    http = http_for_alias()
    with pytest.raises(pcs.ReservationArgumentError):
        _store(http).stage_forward_schedule_batch(CANONICAL, [own_row()])
    assert not http.calls


def test_canonical_tenant_is_bound_inside_request_digest_and_batch_identity():
    row = own_row()
    first_http = http_for_alias()
    second_http = http_for_alias(response=_Resp(200, [{'alias_key': RAW, 'tenant_id': 'other'}]))
    _store(first_http).stage_forward_schedule_batch(RAW, [row])
    _store(second_http).stage_forward_schedule_batch(RAW, [row])
    first, second = stage_args(first_http), stage_args(second_http)
    assert first['p_request_digest'] != second['p_request_digest']
    assert first['p_batch_id'] != second['p_batch_id']
    assert json.loads(first['p_request'])['members'] == json.loads(second['p_request'])['members']


def test_lost_response_resolves_only_exact_canonical_attempt():
    def lost(_args): raise TimeoutError('SYNTHETIC committed response lost')
    http = http_for_alias(stage=lost)
    store = _store(http)
    with pytest.raises(pcs.ReservationStoreError):
        store.stage_forward_schedule_batch(RAW, [own_row()])
    attempt = store.last_forward_stage_attempt
    status = {**attempt, 'state': 'staged', 'observation_row_ids': [], 'finalize_receipt': None}
    assert attempt['tenant_id'] == CANONICAL
    http.posts[pcs._BATCH_STATUS_RPC] = _Resp(200, status)
    assert store.resolve_forward_stage_attempt()['tenant_id'] == CANONICAL
    http.posts[pcs._BATCH_STATUS_RPC] = _Resp(200, dict(status, tenant_id=RAW))
    with pytest.raises(pcs.ReservationStoreError):
        store.resolve_forward_stage_attempt()
    assert len([c for c in http.calls if c[1].endswith(pcs._STAGE_RPC)]) == 1
