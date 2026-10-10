"""Actual event caller and frozen replay, local SQLite and fake PG only."""
from copy import deepcopy
import hashlib
import io
import json
import uuid

from PIL import Image
import pytest

from agent import db, event_calendar as ec, event_drive_use as lane
from agent import gbp_drive_use_journal as journal, gbp_planner
from agent import gym_media_index, gym_media_selector as selector
from agent import portal_calendar_store as pcs, forward_media_observation_bridge as bridge
from agent.jobs import gbp_drive_use_recovery as listener, event_drive_use_recovery as recovery
from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset, make_source
from tests.test_event_calendar_drive_use import event, row
from tests.test_gbp_staged_journal_binding import _FakeHttp, _Resp, _store, _status_payload, _terminal_receipt
from tests.test_gbp_drive_use_journal import _receipt


class EventHttp(_FakeHttp):
    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith('rpc/' + pcs._STAGE_RPC):
            response = super().post(url, json=json, headers=headers, timeout=timeout)
            request = __import__('json').loads(json['p_request'])
            response._payload['observation_row_ids'] = [m['row']['id'] for m in request['members']
                                                      if m.get('observation') is not None]
            return response
        if url.endswith('rpc/' + bridge.READY_RPC):
            return _Resp(200, True)
        return super().post(url, json=json, headers=headers, timeout=timeout)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'listener.db'))
    for flag in ('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', pcs.GBP_STAGED_JOURNAL_FLAG_ENV,
                 pcs.FORWARD_RESERVATION_FLAG_ENV, listener.FLAG, bridge.ENV,
                 'ECHO_LOGICAL_POST_ID_ENABLED'):
        monkeypatch.setenv(flag, 'true')
    monkeypatch.setattr(listener, 'EPHEMERAL_ROOTS', ())
    with db.connect():
        pass
    journal.unsettled()
    journal.pending_forward_stages()
    owner = str(uuid.uuid4())
    listener.pin_original_journal(owner)
    monkeypatch.setenv(listener.JOURNAL_ID_ENV, owner)
    blob = io.BytesIO()
    Image.new('RGB', (800, 200), 'blue').save(blob, format='JPEG')
    original = blob.getvalue()
    asset = dict(make_asset('a1', gym_id='pete', content_hash=hashlib.md5(original).hexdigest()),
                 drive_use_version=1)
    source = dict(make_source(gym_id='pete'), sync_status='ready',
                  sync_finished_at='2026-10-09T00:00:00Z', drive_use_version=2)
    media = FakeMediaStore(sources=[source], assets=[asset])
    monkeypatch.setattr(gym_media_index, 'default_store', lambda: media)
    monkeypatch.setattr(gbp_planner, '_drive_use_epoch_id',
                        lambda gym: '11111111-1111-4111-8111-111111111111')
    from agent.integrations import drive_client
    monkeypatch.setattr(drive_client, 'DriveClient', lambda: FakeDrive(blobs={'a1': original}))
    hosted = {}
    from agent import media_host, visual_writer_prepare
    def host(path, gym):
        data = __import__('pathlib').Path(path).read_bytes()
        url = 'https://cdn.test/' + gym + '/' + hashlib.sha256(data).hexdigest() + '.jpg'
        hosted[url] = data
        return url
    monkeypatch.setattr(media_host, 'host_media', host)
    monkeypatch.setattr(visual_writer_prepare, '_bytes_for_url', hosted.__getitem__)
    http = EventHttp()
    store = _store(http)
    monkeypatch.setattr(store, 'list_month', lambda *a: [])
    monkeypatch.setattr(store, '_forward_batch_observation', lambda r, o, *a: o)
    monkeypatch.setattr(store, 'check_reservation_conflicts', lambda *a, **k: dict(allowed=True))
    # Existing calendar belts are covered independently; preserve exact rows
    # here to exercise the real insert preprocessing and atomic stage caller.
    monkeypatch.setattr(pcs, '_stage_belts', lambda gym, rows: rows)
    monkeypatch.setattr(pcs, '_media_stage_belt', lambda store, gym, rows: rows)
    monkeypatch.setattr(selector, 'claim_drive_content', lambda *a: pytest.fail('prewrite claim'))
    monkeypatch.setattr(selector, 'stamp_use', lambda *a, **k: pytest.fail('producer consumed use'))
    return dict(store=store, http=http, media=media, asset=asset, original=original)


def stage(case, **over):
    return ec.stage_arc(case['store'], event(), [dict(row(caption='Bring a friend'), **over)])


def claims():
    with db.connect() as conn:
        return [tuple(r) for r in conn.execute('SELECT draft_id,account_key,status,post_id FROM socialapi_claims')]


def test_real_event_caller_has_atomic_original_derivative_and_use_before_rpc(case):
    seen = []
    def sent(args):
        entry = journal.unsettled()[0]
        bound = journal.get_forward_stage(args['p_batch_id'])
        assert entry['state'] == 'write_intent'
        assert bound['request_text'] == args['p_request']
        assert len(claims()) == 1
        request = json.loads(bound['request_text'])
        packet = json.loads(request['members'][0]['observation']['observation_json'])
        assert packet['source_sha256'] == hashlib.sha256(case['original']).hexdigest()
        assert packet['source_sha256'] != packet['delivered_sha256']
        assert entry['calendar_row']['status'] == 'pending'
        group = entry['calendar_row']['visual_group_key']
        assert group.startswith('vg_') and len(group) == 67
        seen.append(entry)
    case['http'].on_stage = sent
    result = stage(case)
    assert result['staged'] == 1 and result['held_media'] == 0
    assert len(seen) == 1
    assert journal.unsettled()[0]['state'] == 'write_intent'
    assert len(case['http'].stage_calls) == 1


def test_lost_ack_restart_replays_exact_frozen_request_without_selection(case, monkeypatch):
    case['http'].stage_exc = TimeoutError('lost ack')
    result = stage(case)
    assert result['staged'] == 0 and result['held_media'] == 1
    original = deepcopy(case['http'].stage_calls[0])
    group = json.loads(original['p_request'])['members'][0]['row']['visual_group_key']
    assert group.startswith('vg_')
    use = journal.unsettled()[0]
    assert len(claims()) == 1
    monkeypatch.setattr(lane, 'select', lambda *a, **k: pytest.fail('reselected after crash'))
    monkeypatch.setattr(lane, 'materialize', lambda *a, **k: pytest.fail('rehosted after crash'))
    case['http'].stage_exc = None
    # Recovery sends the persisted request before looking for terminal proof.
    result = recovery.run(store=case['store'], tenant_id='pete')
    assert result['replayed'] == 1
    assert case['http'].stage_calls == [original, original]
    assert json.loads(case['http'].stage_calls[-1]['p_request'])['members'][0]['row']['visual_group_key'] == group
    assert journal.unsettled()[0]['use_id'] == use['use_id']
    assert len(claims()) == 1


def test_crash_before_pick_reuses_operation_uuid_and_first_caption(case, monkeypatch):
    real_select = lane.select
    monkeypatch.setattr(lane, 'select', lambda *a, **k: dict(state='unknown'))
    assert stage(case)['held_media'] == 1
    with db.connect() as conn:
        logical = conn.execute('SELECT logical_post_id FROM event_drive_operations').fetchone()[0]
    assert claims() == [] and journal.unsettled() == []
    monkeypatch.setattr(lane, 'select', real_select)
    assert stage(case, caption='regenerated caption')['staged'] == 1
    use = journal.unsettled()[0]
    assert use['logical_post_id'] == logical
    assert use['calendar_row']['caption'] == 'Bring a friend'


def test_precommit_failure_rolls_back_claim_stage_and_use(case, monkeypatch):
    real_get = journal._get_row
    def fail(conn, uid):
        if conn.execute('SELECT count(*) FROM gbp_drive_use_journal').fetchone()[0]:
            raise RuntimeError('crash after inserts before commit')
        return real_get(conn, uid)
    monkeypatch.setattr(journal, '_get_row', fail)
    assert stage(case)['held_media'] == 1
    assert claims() == [] and journal.unsettled() == []
    assert journal.pending_forward_stages() == []
    assert case['http'].stage_calls == []
    with db.connect() as conn:
        assert conn.execute('SELECT batch_id FROM event_drive_operations').fetchone()[0] is None


def test_crash_after_freeze_before_send_keeps_replayable_claim(case, monkeypatch):
    real_call = lane._AtomicStage.__call__
    monkeypatch.setattr(lane._AtomicStage, '__call__', lambda *a: (_ for _ in ()).throw(RuntimeError('crash')))
    assert stage(case)['held_media'] == 1
    assert len(claims()) == 1 and len(journal.unsettled()) == 1
    assert case['http'].stage_calls == []
    monkeypatch.setattr(lane._AtomicStage, '__call__', real_call)
    assert recovery.run(store=case['store'], tenant_id='pete')['replayed'] == 1
    assert len(case['http'].stage_calls) == 1


@pytest.mark.parametrize('change', ['down', 'partial', 'claims', 'source'])
def test_unknown_inventory_is_not_exhaustion(case, monkeypatch, change):
    if change == 'down':
        case['media']._up = False
    elif change == 'partial':
        case['media'].sources['src1']['sync_status'] = 'running'
    elif change == 'claims':
        monkeypatch.setattr(db, 'drive_asset_claimed_ids', lambda *a: (_ for _ in ()).throw(RuntimeError()))
    else:
        case['media'].sources['src1']['gym_id'] = 'foreign'
    assert lane.select('pete', row())['state'] == 'unknown'
    assert claims() == []


def test_proven_exhaustion_and_scheduled_date_selection(case, monkeypatch):
    case['media'].assets.clear()
    dates = []
    original = selector.pickable
    def check(*a, **kw):
        dates.append(kw['post_date'])
        return original(*a, **kw)
    monkeypatch.setattr(selector, 'pickable', check)
    assert lane.select('pete', row())['state'] == 'exhausted'
    assert dates == ['2026-10-03']


def test_fresh_source_drift_and_canonical_claim_race_hold_before_rpc(case, monkeypatch):
    real_materialize = lane.materialize
    def raced(*args):
        media = real_materialize(*args)
        db.socialapi_claim(selector.drive_content_claim_id('pete', case['asset']), 'pete_gbp')
        return media
    monkeypatch.setattr(lane, 'materialize', raced)
    assert stage(case)['held_media'] == 1
    assert case['http'].stage_calls == [] and journal.unsettled() == []
    assert len(claims()) == 1  # The competing owner's claim, never released.


def test_wrong_tenant_original_never_hosts_or_claims(case, monkeypatch):
    case['media'].assets['a1']['gym_id'] = 'other'
    monkeypatch.setattr(lane, 'select', lambda *a: dict(state='eligible', asset=case['media'].assets['a1'], store=case['media']))
    from agent import media_host
    monkeypatch.setattr(media_host, 'host_media', lambda *a: pytest.fail('foreign host'))
    assert stage(case)['held_media'] == 1
    assert claims() == [] and case['http'].stage_calls == []


def test_media_held_legacy_backfill_remains_identical_and_does_not_pick(case, monkeypatch):
    old = row(id=str(uuid.uuid4()), gym_id='pete', image_url='',
              media_not_ready_reason='needs_media', logical_post_id=None)
    monkeypatch.setattr(case['store'], 'list_event_rows', lambda *a: [deepcopy(old)])
    monkeypatch.setattr(lane, 'select', lambda *a: pytest.fail('legacy pick'))
    assert ec.backfill_missing_media(case['store'], 'pete', 'evt1') == dict(backfilled=[], held=1)
    assert claims() == [] and case['http'].stage_calls == []


def test_unheld_pending_backfill_stages_replacement_and_preserves_old_identity(case, monkeypatch):
    old = row(id=str(uuid.uuid4()), gym_id='pete', image_url='', caption='Bring a friend',
              logical_post_id=None, variant_status='active', media_not_ready_reason=None)
    monkeypatch.setattr(case['store'], 'list_event_rows', lambda *a: [deepcopy(old)])
    result = ec.backfill_missing_media(case['store'], 'pete', 'evt1')
    assert result == dict(backfilled=[], held=1, staged_candidates=1)
    request = json.loads(case['http'].stage_calls[0]['p_request'])
    assert request['old_rows'] == [old]
    assert request['members'][0]['row']['id'] != old['id']
    assert old['logical_post_id'] is None and old['image_url'] == ''


def test_terminal_exact_active_listener_consumes_once_after_staging(case, monkeypatch):
    assert stage(case)['staged'] == 1
    use = journal.unsettled()[0]
    bound = journal.forward_stage_for_member(use['calendar_row']['id'])
    active = dict(use['calendar_row'], variant_status='active', media_not_ready_reason=None)
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    calls = []
    monkeypatch.setattr(selector, 'stamp_use', lambda *a, **k: calls.append(k['use_id']))
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt', lambda uid: _receipt(use))
    staged_status = _status_payload(bound, 'staged')
    staged_status['observation_row_ids'] = bound['member_row_ids']
    case['http'].status_payload = staged_status
    assert listener.run(store=case['store'], media_store=case['media'])['held'] == 1
    assert calls == []
    terminal = _status_payload(bound, 'finalized', _terminal_receipt(bound))
    terminal['observation_row_ids'] = bound['member_row_ids']
    case['http'].status_payload = terminal
    assert listener.run(store=case['store'], media_store=case['media'])['recovered'] == 1
    assert calls == [use['use_id']]
    assert journal.get(use['use_id'])['state'] == 'claim_done'
    assert listener.run(store=case['store'], media_store=case['media'])['examined'] == 0
    assert claims()[0][2:] == ('done', 'a1')


@pytest.mark.parametrize('mutate', ['date', 'tenant', 'candidate', 'caption'])
def test_terminal_receipt_with_wrong_active_member_cannot_consume(case, monkeypatch, mutate):
    assert stage(case)['staged'] == 1
    use = journal.unsettled()[0]
    bound = journal.forward_stage_for_member(use['calendar_row']['id'])
    active = dict(use['calendar_row'], variant_status='active', media_not_ready_reason=None)
    changes = dict(date=dict(post_date='2026-10-04'), tenant=dict(gym_id='foreign'),
                   candidate=dict(variant_status='candidate'), caption=dict(caption='changed'))
    active.update(changes[mutate])
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    terminal = _status_payload(bound, 'finalized', _terminal_receipt(bound))
    terminal['observation_row_ids'] = bound['member_row_ids']
    case['http'].status_payload = terminal
    assert listener.run(store=case['store'], media_store=case['media'])['held'] == 1
    assert journal.get(use['use_id'])['state'] == 'write_intent'
    assert claims()[0][2] == 'in_flight'


def test_operation_identity_does_not_duplicate_or_cross_date_generation(case):
    common = dict(gym_id='pete', event_id='evt1', beat='announce',
                  post_date='2026-10-03', account='instagram', format='feed',
                  generation=0, old_row_id=None)
    input_row = row(caption='first')
    first = journal.event_operation(common, input_row)
    retry = journal.event_operation(common, dict(input_row, caption='retry'))
    assert retry['logical_post_id'] == first['logical_post_id']
    assert retry['input_row']['caption'] == 'first'
    next_date = journal.event_operation(dict(common, post_date='2026-10-04'),
                                         dict(input_row, post_date='2026-10-04'))
    recreate = journal.event_operation(dict(common, generation=1), input_row)
    other = journal.event_operation(dict(common, gym_id='other'), input_row)
    assert len({x['logical_post_id'] for x in (first, next_date, recreate, other)}) == 4


@pytest.mark.parametrize('case_variant', ['lowercase', 'uppercase'])
def test_legacy_same_byte_alias_claim_prevents_canonical_acquisition(case, monkeypatch, case_variant):
    alias_hash = (case['asset']['content_hash'].upper() if case_variant == 'uppercase'
                  else case['asset']['content_hash'])
    alias = dict(case['asset'], id='alias', content_hash=alias_hash, review_content_hash=alias_hash)
    case['media'].assets['alias'] = alias
    # Claim arrives after pick/materialize, exercising the transaction check.
    real_materialize = lane.materialize
    def race(*args):
        result = real_materialize(*args)
        db.socialapi_claim(selector.drive_asset_claim_id('pete', 'alias'), 'pete_gbp')
        return result
    monkeypatch.setattr(lane, 'materialize', race)
    assert stage(case)['held_media'] == 1
    assert case['http'].stage_calls == []
    assert journal.pending_forward_stages() == [] and journal.unsettled() == []
    assert len(claims()) == 1 and ':alias' in claims()[0][0]


@pytest.mark.parametrize('alias_hash', [None, '', 'unreadable-md5'])
def test_unusable_alias_hash_holds_atomic_freeze_without_orphan_claim(case, monkeypatch, alias_hash):
    real_materialize = lane.materialize
    def corrupt_inventory(*args):
        result = real_materialize(*args)
        case['media'].assets['alias'] = dict(case['asset'], id='alias', content_hash=alias_hash)
        return result
    monkeypatch.setattr(lane, 'materialize', corrupt_inventory)
    assert stage(case)['held_media'] == 1
    assert claims() == [] and journal.unsettled() == []
    assert journal.pending_forward_stages() == [] and case['http'].stage_calls == []


def seed_replay_operation(gym_id, sequence):
    """Exact retained operation/claim/use/stage; no media or remote calls."""
    from agent.local_inventory_mutation import canonical_json
    asset_hash = hashlib.md5(f'{gym_id}-{sequence}'.encode()).hexdigest()
    asset = dict(make_asset(f'{gym_id}-asset-{sequence}', gym_id=gym_id, content_hash=asset_hash),
                 drive_use_version=1)
    source = dict(make_source(gym_id=gym_id), drive_use_version=2)
    identity = dict(gym_id=gym_id, event_id='evt1', beat='during',
                    post_date='2026-10-03', account='instagram', format='feed',
                    generation=sequence, old_row_id=None)
    operation = journal.event_operation(identity, row(caption='Frozen event'))
    member = dict(operation['input_row'], id=str(uuid.uuid4()), variant_status='candidate',
                  source_media_asset_id=asset['id'], source_media_url='https://cdn.test/source.jpg',
                  image_url='https://cdn.test/feed.jpg')
    request_text = canonical_json(dict(tenant_id=gym_id, members=[dict(row=member, observation={})],
                                       old_rows=[]))
    request_digest = hashlib.sha256(request_text.encode()).hexdigest()
    batch_id = pcs.forward_batch_identity(gym_id, request_digest)
    attempt = dict(batch_id=batch_id, tenant_id=gym_id, request_digest=request_digest,
                   request_text=request_text, member_row_ids=[member['id']], old_row_ids=[])
    request = dict(gym_id=gym_id, logical_post_id=operation['logical_post_id'],
                   claim_id=selector.drive_content_claim_id(gym_id, asset), post_date='2026-10-03',
                   content_hash=asset_hash, asset_id=asset['id'], source_id=source['id'],
                   calendar_row=member, payload=member, asset_before=asset, source_before=source,
                   epoch_id='11111111-1111-4111-8111-111111111111')
    entry = journal.freeze_event_stage(operation['operation_key'], attempt, request,
                                       alias_asset_ids=[asset['id']])
    return operation, entry, batch_id


def test_fair_replay_admits_33rd_after_32_persistent_holds_and_preserves_every_claim(case, monkeypatch):
    seeded = [seed_replay_operation('pete', i) for i in range(33)]
    foreign = seed_replay_operation('other', 0)
    before_claims = sorted(claims())
    before_uses = journal.unsettled()
    before_stages = journal.pending_forward_stages()
    ordered = journal.event_operations('pete', limit=32)
    first_ids = [op['batch_id'] for op in ordered]
    last_id = next(batch for _, _, batch in seeded if batch not in first_ids)
    attempts = []
    def refused(batch_id, tenant):
        attempts.append((batch_id, tenant))
        raise RuntimeError('persistent rejection')
    monkeypatch.setattr(case['store'], 'replay_frozen_event_stage', refused)
    monkeypatch.setattr(lane, 'select', lambda *a, **kw: pytest.fail('replanned frozen operation'))
    first = recovery.run(store=case['store'], tenant_id='pete', settle=False, logger=lambda m: None)
    assert first['replay_held'] == 32
    assert [batch for batch, tenant in attempts] == first_ids
    second = recovery.run(store=case['store'], tenant_id='pete', settle=False, logger=lambda m: None)
    assert second['replay_held'] == 32
    assert attempts[32] == (last_id, 'pete')
    assert foreign[2] not in [batch for batch, tenant in attempts]
    # Tenant work never advances global or another tenant's cursor.
    assert journal.event_replay_cursor() is None
    assert journal.event_replay_cursor('other') is None
    assert sorted(claims()) == before_claims
    assert journal.unsettled() == before_uses
    assert journal.pending_forward_stages() == before_stages
    assert all(entry['state'] == 'write_intent' for entry in before_uses)


def test_event_replay_cursor_requires_original_pin_and_rejects_foreign_admission(case, monkeypatch):
    seed_replay_operation('pete', 0)
    seed_replay_operation('other', 0)
    foreign = journal.event_operations('other')[0]
    with pytest.raises(journal.JournalHold, match='admission_invalid'):
        journal.advance_event_replay_cursor(foreign, 'pete')
    assert journal.event_replay_cursor('pete') is None
    monkeypatch.setenv(listener.JOURNAL_ID_ENV, str(uuid.uuid4()))
    attempts = []
    monkeypatch.setattr(case['store'], 'replay_frozen_event_stage', lambda *a: attempts.append(a))
    result = recovery.run(store=case['store'], tenant_id='pete', settle=False, logger=lambda m: None)
    assert result['ok'] is False and attempts == []
    assert len(claims()) == 2



def test_stale_replay_slice_cannot_regress_cursor_and_65th_is_reached(case, monkeypatch):
    [seed_replay_operation('pete', i) for i in range(65)]
    before_claims = sorted(claims())
    before_uses = journal.unsettled()
    before_stages = journal.pending_forward_stages()
    stale_snapshot = journal.event_replay_snapshot('pete')
    stale_slice = journal.event_operations('pete', after=stale_snapshot['after'])
    all_ids = {bound['batch_id'] for bound in before_stages}
    attempts = []
    def refused(batch_id, tenant):
        attempts.append((batch_id, tenant))
        raise RuntimeError('persistent rejection')
    monkeypatch.setattr(case['store'], 'replay_frozen_event_stage', refused)
    for _ in range(2):
        result = recovery.run(store=case['store'], tenant_id='pete', settle=False, logger=lambda m: None)
        assert result['replay_held'] == 32
    current = journal.event_replay_snapshot('pete')
    assert current['version'] == 64
    assert len({batch for batch, _ in attempts}) == 64
    last_id = (all_ids - {batch for batch, _ in attempts}).pop()
    # A stale worker resumes its tail after two other sweeps advanced. The
    # persisted version rejects it without changing the newer keyset position.
    with pytest.raises(journal.JournalHold, match='cursor_stale'):
        journal.advance_event_replay_cursor(stale_slice[-1], 'pete', expected_cursor=stale_snapshot)
    assert journal.event_replay_snapshot('pete') == current
    result = recovery.run(store=case['store'], tenant_id='pete', settle=False, logger=lambda m: None)
    assert result['replay_held'] == 32
    assert attempts[64] == (last_id, 'pete')
    # The third sweep wraps after operation 65; its monotonic version cannot
    # suffer an ABA race when the keyset returns to an older position.
    assert journal.event_replay_snapshot('pete')['version'] == 96
    assert sorted(claims()) == before_claims
    assert journal.unsettled() == before_uses
    assert journal.pending_forward_stages() == before_stages


def test_replay_admission_snapshot_is_scope_bound_even_at_equal_versions(case):
    seed_replay_operation('pete', 0)
    seed_replay_operation('other', 0)
    pete_snapshot = journal.event_replay_snapshot('pete')
    other = journal.event_operations('other')[0]
    with pytest.raises(journal.JournalHold, match='cursor_stale'):
        journal.advance_event_replay_cursor(other, 'other', expected_cursor=pete_snapshot)
    assert journal.event_replay_cursor('other') is None
    assert journal.event_replay_snapshot('other')['version'] == 0



def test_event_visual_group_is_stable_before_selection_and_accepts_only_matching_input(case, monkeypatch):
    real_select = lane.select
    groups = []
    def hold(gym_id, candidate):
        groups.append(candidate['visual_group_key'])
        return dict(state='unknown')
    monkeypatch.setattr(lane, 'select', hold)
    assert stage(case)['held_media'] == 1
    assert stage(case)['held_media'] == 1
    assert groups[0] == groups[1] and groups[0].startswith('vg_')
    assert claims() == []
    monkeypatch.setattr(lane, 'select', real_select)
    assert stage(case, visual_group_key=groups[0])['staged'] == 1
    member = json.loads(case['http'].stage_calls[0]['p_request'])['members'][0]['row']
    assert member['visual_group_key'] == groups[0]


@pytest.mark.parametrize('conflict', ['vg_foreign', '', 7])
def test_conflicting_event_visual_group_holds_before_selection_or_claim(case, monkeypatch, conflict):
    monkeypatch.setattr(lane, 'select', lambda *a: pytest.fail('conflicting group selected media'))
    assert stage(case, visual_group_key=conflict)['held_media'] == 1
    assert claims() == [] and journal.unsettled() == []
    assert case['http'].stage_calls == [] and journal.pending_forward_stages() == []


def test_event_visual_group_isolates_tenant_date_and_logical_generation(case):
    identity = dict(gym_id='pete', event_id='evt1', beat='announce',
                    post_date='2026-10-03', account='instagram', format='feed',
                    generation=0, old_row_id=None)
    first = journal.event_operation(identity, row())
    retry = journal.event_operation(identity, row())
    another_day = journal.event_operation(dict(identity, post_date='2026-10-04'),
                                           dict(row(), post_date='2026-10-04'))
    another_tenant = journal.event_operation(dict(identity, gym_id='other'), row())
    recreate = journal.event_operation(dict(identity, generation=1), row())
    groups = [lane._visual_group_for(op) for op in (first, another_day, another_tenant, recreate)]
    assert len(set(groups)) == 4
    assert lane._visual_group_for(retry) == groups[0]
