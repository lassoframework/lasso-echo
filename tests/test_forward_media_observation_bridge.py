"""Offline exact-row writer boundary tests. No owner or production credentials."""
import copy
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from agent import forward_media_observation_bridge as bridge
from agent import portal_calendar_store as pcs
from agent.gym_media_index import materialization_observation
from agent.real_calendar_mirror import _real_row


def observation(**changes):
    item = materialization_observation(b'synthetic', b'synthetic',
        'https://media.example.test/source.jpg', tenant='gym', source_asset_id='asset',
        source_url='https://media.example.test/source.jpg',
        recipe={'image': {'name': 'identity'}, 'runtime_verified': False},
        bytes_fn=lambda _: b'synthetic')
    item.update(changes)
    item['observation_digest'] = hashlib.sha256(json.dumps(
        {k: v for k, v in item.items() if k != 'observation_digest'},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return item


def row(**changes):
    item = {'gym_id': 'gym', 'account': 'instagram', 'format': 'photo',
            'post_date': '2026-10-10', 'status': 'pending', 'caption': 'A real caption.',
            'source_media_asset_id': 'asset',
            'source_media_url': 'https://media.example.test/source.jpg',
            'image_url': 'https://media.example.test/source.jpg'}
    item.update(changes)
    return item


class Response:
    text = 'redacted'

    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class HTTP:
    def __init__(self, *, ready=True, mutate=None, refused=False):
        self.calls = []
        self.ready, self.mutate, self.refused = ready, mutate, refused
        self.inserted = []

    def post(self, url, *, headers, json, timeout):
        name = url.rsplit('/', 1)[-1]
        self.calls.append((name, copy.deepcopy(json)))
        if name == bridge.READY_RPC:
            return Response(self.ready)
        if name == 'content_calendar':
            self.inserted = [dict(r, id=r.get('id') or str(uuid.uuid4()),
                                 variant_status='active') for r in json]
            result = copy.deepcopy(self.inserted[::-1])
            if self.mutate:
                self.mutate(result)
            return Response(result)
        if name == bridge.RPC:
            if self.refused:
                return Response({}, 409)
            return Response({'calendar_row_id': json['p_calendar_row_id'],
                             'revision': 'a' * 32,
                             'observation_digest': __import__('json').loads(
                                 json['p_observation_json'])['observation_digest'],
                             'provenance_status': 'unverified'})
        raise AssertionError(name)


@pytest.fixture
def writer(monkeypatch):
    from agent import plan_horizon
    monkeypatch.setattr(plan_horizon, 'belt_filter', lambda key, rows: (rows, []))
    monkeypatch.setattr(pcs, '_stage_belts', lambda key, rows: rows)
    monkeypatch.setattr(pcs, '_media_stage_belt', lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, '_retry_story_hold_provenance', lambda *args: None)
    monkeypatch.setattr(pcs, '_reconcile_story_media_holds', lambda store, key, rows: (rows, []))
    monkeypatch.setattr(pcs, '_preserve_held_slots', lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, '_dedupe_slots', lambda store, key, rows: rows)
    monkeypatch.setattr(pcs, '_record_confirmed_story_holds', lambda *args: None)
    monkeypatch.setenv(bridge.ENV, 'true')
    return lambda http: pcs.SupabaseCalendarStore(
        url='https://synthetic.test', service_key='synthetic-test-only', http=http)


def test_default_off_metadata_stripped_without_rpc(writer, monkeypatch):
    monkeypatch.delenv(bridge.ENV)
    http = HTTP()
    result = writer(http).insert_rows('gym', [row(**{bridge.METADATA: [observation()]})])
    assert len(result) == 1
    assert [c[0] for c in http.calls] == ['content_calendar']
    assert bridge.METADATA not in http.calls[0][1][0]
    assert 'id' not in http.calls[0][1][0]


def test_mirror_preserves_private_observation_only_when_enabled(monkeypatch):
    draft = SimpleNamespace(media_materialization_observations=[observation()],
                            status='pending', day_key='2026-10-10', platform='instagram')
    assert bridge.METADATA not in _real_row('gym', draft)
    monkeypatch.setenv(bridge.ENV, 'true')
    mirrored = _real_row('gym', draft)
    assert mirrored[bridge.METADATA] == draft.media_materialization_observations
    mirrored[bridge.METADATA][0]['tenant'] = 'other'
    assert draft.media_materialization_observations[0]['tenant'] == 'gym'


def test_identical_media_two_dates_pair_by_explicit_uuid_and_reverse_response(writer):
    http = HTTP()
    proposals = [row(post_date=day, **{bridge.METADATA: [observation()]})
                 for day in ('2026-10-10', '2026-10-11')]
    result = writer(http).insert_rows('gym', proposals)
    assert len(result) == 2
    sent = http.calls[1][1]
    assert len({r['id'] for r in sent}) == 2
    calls = [c[1] for c in http.calls if c[0] == bridge.RPC]
    for request in calls:
        original = next(r for r in sent if r['id'] == request['p_calendar_row_id'])
        assert request['p_expected_row']['post_date'] == original['post_date']
        assert bridge.METADATA not in original
        assert json.loads(request['p_observation_json'])['provenance_status'] == 'unverified'


def test_mixed_observed_unobserved_batch_has_no_null_ids(writer):
    http = HTTP()
    writer(http).insert_rows('gym', [row(**{bridge.METADATA: [observation()]}),
                                   row(post_date='2026-10-11')])
    payload = next(c[1] for c in http.calls if c[0] == 'content_calendar')
    assert all(uuid.UUID(r['id']) for r in payload)
    assert len([c for c in http.calls if c[0] == bridge.RPC]) == 1


@pytest.mark.parametrize('field,value', [('id', str(uuid.uuid4())), ('gym_id', 'other'),
    ('source_media_asset_id', 'other'), ('source_media_url', 'https://other.test/a'),
    ('image_url', 'https://other.test/b'), ('post_date', '2026-10-12'),
    ('status', 'approved')])
def test_returned_mismatch_never_attaches_observation(writer, field, value):
    http = HTTP(mutate=lambda rows: rows[0].update({field: value}))
    with pytest.raises(bridge.ObservationBridgeHold, match='pairing_unverified'):
        writer(http).insert_rows('gym', [row(**{bridge.METADATA: [observation()]})])
    assert http.inserted
    assert not any(c[0] == bridge.RPC for c in http.calls)


def test_missing_schema_fails_before_calendar_insert(writer):
    http = HTTP(ready=False)
    with pytest.raises(bridge.ObservationBridgeHold, match='schema_unavailable'):
        writer(http).insert_rows('gym', [row(**{bridge.METADATA: [observation()]})])
    assert not http.inserted


def test_persistence_failure_returns_hold_after_unverified_insert(writer):
    http = HTTP(refused=True)
    with pytest.raises(bridge.ObservationBridgeHold, match='bridge_refused'):
        writer(http).insert_rows('gym', [row(**{bridge.METADATA: [observation()]})])
    assert len(http.inserted) == 1
    assert http.inserted[0].get('render_manifest_digest') is None


@pytest.mark.parametrize('status', ['approved', 'publishing', 'published', 'denied'])
def test_protected_status_refuses_before_insert(writer, status):
    http = HTTP()
    with pytest.raises(bridge.ObservationBridgeHold, match='not_unverified_candidate'):
        writer(http).insert_rows('gym', [row(status=status,
            **{bridge.METADATA: [observation()]})])
    assert not http.calls


def test_digest_or_ambiguous_edge_refuses():
    proposal = row(id=str(uuid.uuid4()))
    bad = observation()
    bad['observation_digest'] = 'f' * 64
    with pytest.raises(bridge.ObservationBridgeHold, match='digest_invalid'):
        bridge.prepare(proposal, [bad])
    with pytest.raises(bridge.ObservationBridgeHold, match='ambiguous'):
        bridge.prepare(proposal, [observation(), observation(hold_reasons=['changed'])])
    assert bridge.prepare(proposal, [observation(), observation()])


def test_transport_exception_scrubs_secret(writer):
    class BrokenHTTP(HTTP):
        def post(self, *args, **kwargs):
            raise RuntimeError('credential=DO_NOT_LOG')
    with pytest.raises(bridge.ObservationBridgeHold) as error:
        writer(BrokenHTTP()).insert_rows('gym', [row(**{bridge.METADATA: [observation()]})])
    assert 'DO_NOT_LOG' not in str(error.value)


def test_real_pending_store_roundtrip_keeps_untrusted_explicit_pair(tmp_path, monkeypatch):
    from agent.drafter import Draft
    from agent.store import PendingStore, _to_dict
    monkeypatch.setenv(bridge.ENV, 'true')
    draft = Draft(draft_id='synthetic_draft', account_key='gym', platform='instagram',
                  caption='Real caption.', hashtags=[], creative_path='',
                  creative_public_url='https://media.example.test/source.jpg',
                  scheduled_for='', day_key='2026-10-10', source_media_asset_id='asset',
                  source_media_url='https://media.example.test/source.jpg')
    legacy = _to_dict(draft)
    assert 'media_materialization_observations' not in legacy
    assert 'source_media_asset_id' not in legacy
    draft.media_materialization_observations = [observation()]
    pending = PendingStore(path=str(tmp_path / 'pending.sqlite'))
    pending.put(draft)
    rehydrated = pending.get(draft.draft_id)
    assert rehydrated.media_materialization_observations == [observation()]
    assert rehydrated.source_media_asset_id == 'asset'
    mirrored = _real_row('gym', rehydrated)
    assert mirrored['source_media_asset_id'] == 'asset'
    assert bridge.prepare(dict(mirrored, id=str(uuid.uuid4())),
                          mirrored[bridge.METADATA])
    # A metadata packet's claimed asset is never inferred into a missing explicit ID.
    draft.source_media_asset_id = ''
    pending.put(draft)
    rehydrated = pending.get(draft.draft_id)
    assert rehydrated.source_media_asset_id == ''
    with pytest.raises(bridge.ObservationBridgeHold, match='not_unverified_candidate'):
        bridge.prepare(dict(_real_row('gym', rehydrated), id=str(uuid.uuid4())),
                       rehydrated.media_materialization_observations)
