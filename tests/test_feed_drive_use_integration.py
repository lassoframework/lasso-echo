from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from agent import db, feed_drive_use as feed, gbp_drive_use_journal as journal
from agent import gbp_planner, gym_media_selector, portal_calendar_store as pcs
from agent.jobs import gbp_drive_use_recovery as recovery
from tests.test_gbp_drive_use_journal import make_request, _receipt
from tests.test_gbp_staged_journal_binding import _FakeHttp, _store, _status_payload, _terminal_receipt


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'listener.db'))
    monkeypatch.setenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', 'true')
    monkeypatch.setenv(pcs.GBP_STAGED_JOURNAL_FLAG_ENV, 'true')
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, 'true')
    monkeypatch.setenv(recovery.FLAG, 'true')
    monkeypatch.setattr(recovery, 'EPHEMERAL_ROOTS', ())
    with db.connect():
        pass
    journal.unsettled()
    journal.pending_forward_stages()
    identity = str(uuid.uuid4())
    recovery.pin_original_journal(identity)
    monkeypatch.setenv(recovery.JOURNAL_ID_ENV, identity)
    req = make_request(gym='gym1', logical=str(uuid.uuid4()))
    row = dict(req['calendar_row'], id=str(uuid.uuid4()), account='instagram',
               format='image', status='pending')
    pending = {k: req[k] for k in ('gym_id', 'asset_before', 'source_before',
                                  'claim_id', 'epoch_id', 'post_date')}
    draft = SimpleNamespace(is_story=False, _drive_use_pending=pending)
    with db.connect() as conn:
        conn.execute("INSERT INTO socialapi_claims(draft_id,account_key,status) VALUES(?,?,'in_flight')",
                     (req['claim_id'], 'gym1_gbp'))
    http = _FakeHttp()
    return dict(row=row, draft=draft, http=http, store=_store(http))


def test_exact_stage_then_separate_finalizer_then_original_listener(lane, monkeypatch):
    row, store, http = lane['row'], lane['store'], lane['http']
    callback = feed.stage_callback('gym1', [lane['draft']])
    def on_stage(args):
        entries = journal.unsettled()
        assert len(entries) == 1 and entries[0]['state'] == 'write_intent'
        assert entries[0]['calendar_row'] == row
        assert journal.get_forward_stage(args['p_batch_id'])['state'] == 'stage_intent'
    http.on_stage = on_stage
    receipt = store.stage_forward_schedule_batch('gym1', [row], before_forward_stage=callback)
    entry = journal.unsettled()[0]
    bound = journal.get_forward_stage(receipt['batch_id'])
    active = dict(row, variant_status='active', media_not_ready_reason=None)
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    calls = []
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: calls.append(k['use_id']))
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt', lambda uid: _receipt(entry))
    # A staged row remains unlanded and must make no remote use call.
    http.status_payload = _status_payload(bound, 'staged')
    assert recovery.run(store=store, media_store=object())['held'] == 1
    assert calls == []
    # A separate finalizer supplies the exact PG terminal receipt. Listener
    # rereads it and settles the original durable use UUID, then finishes claim.
    http.status_payload = _status_payload(bound, 'finalized', _terminal_receipt(bound))
    assert recovery.run(store=store, media_store=object())['recovered'] == 1
    assert calls == [entry['use_id']]
    assert journal.get(entry['use_id'])['state'] == 'claim_done'
    assert recovery.run(store=store, media_store=object())['examined'] == 0


def test_callback_failure_never_sends_stage_rpc(lane):
    def refuse(request):
        assert journal.pending_forward_stages()
        raise ValueError('journal hold')
    with pytest.raises(pcs.CalendarInsertNotStartedError):
        lane['store'].stage_forward_schedule_batch('gym1', [lane['row']], before_forward_stage=refuse)
    assert lane['http'].stage_calls == []


def test_callback_cannot_mutate_frozen_wire_payload(lane):
    def change(request):
        request['members'][0]['row']['caption'] = 'changed'
    lane['store'].stage_forward_schedule_batch('gym1', [lane['row']], before_forward_stage=change)
    import json
    sent = json.loads(lane['http'].stage_calls[0]['p_request'])
    assert sent['members'][0]['row']['caption'] == lane['row']['caption']


@pytest.mark.parametrize('field,value', [('account', 'facebook'), ('gym_id', 'other')])
def test_wrong_primary_member_holds_before_remote_stage(lane, field, value):
    callback = feed.stage_callback('gym1', [lane['draft']])
    row = dict(lane['row'], **{field: value})
    with pytest.raises((pcs.CalendarInsertNotStartedError, pcs.ReservationArgumentError)):
        lane['store'].stage_forward_schedule_batch('gym1', [row], before_forward_stage=callback)
    assert lane['http'].stage_calls == []


def test_pending_draft_rollback_never_resets_counters_or_releases_claim(lane, monkeypatch):
    from agent import client_month_run as cmr
    monkeypatch.setattr(gym_media_selector, 'rollback_use', lambda *a, **k: pytest.fail('counter reset'))
    monkeypatch.setattr(db, 'socialapi_claim_release', lambda *a, **k: pytest.fail('claim released'))
    cmr._rollback_drive_asset(lane['draft'], '2026-10-10', lambda m: None)


def test_recovery_runs_before_month_flags_or_media_early_exit(monkeypatch):
    from agent import client_month_run as cmr
    calls = []
    monkeypatch.setattr(feed, 'recover', lambda **k: calls.append('recover') or dict(ok=False, reason='held'))
    monkeypatch.setattr(cmr.config, 'client_month_enabled', lambda: pytest.fail('early flag before recovery'))
    result = cmr.build_client_month(None, 'gym1', '2026-10-10', voice=None, store=None)
    assert calls == ['recover'] and result['ok'] is False


def test_insert_rows_callback_observes_final_ids_and_stripped_proof(monkeypatch, tmp_path):
    from tests.test_forward_schedule_reservation import _armed_http, _group_rows, _store as atomic_store
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'listener.db'))
    monkeypatch.setenv(pcs.GBP_STAGED_JOURNAL_FLAG_ENV, 'true')
    monkeypatch.setenv(pcs.FORWARD_RESERVATION_FLAG_ENV, 'true')
    rows = _group_rows()
    http, _ = _armed_http(rows)
    observed = []
    def callback(request):
        assert journal.pending_forward_stages()
        for member in request['members']:
            row = member['row']
            assert str(uuid.UUID(row['id'])) == row['id']
            assert pcs.RESERVATION_PROOF not in row
        observed.append(deepcopy(request))
    result = atomic_store(http).insert_rows('lasso', rows, before_forward_stage=callback)
    assert len(observed) == 1
    assert [m['row']['id'] for m in observed[0]['members']] == [r['id'] for r in result]


def test_unbound_daily_writer_holds_before_inventory_or_claim(monkeypatch):
    from agent import gym_media_builder as builder
    monkeypatch.setenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', 'true')
    monkeypatch.setattr(gym_media_selector, 'claim_drive_content', lambda *a: pytest.fail('unbound claim'))
    assert builder.build_gym_media_draft(None, '2026-10-10', 'faces', None, None) is None


def test_bound_month_builder_defers_stamp_and_claim_completion(monkeypatch, tmp_path):
    from tests.test_gym_media_builder import _wire, _Acct
    from tests.gym_media_fakes import FakeMediaStore, FakeDrive, make_asset
    from agent import gym_media_builder as builder
    _wire(monkeypatch)
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', 'true')
    marker = dict(frozen=True)
    calls = []
    monkeypatch.setattr(feed, 'freeze_pick', lambda *a: calls.append(a) or marker)
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: pytest.fail('pre-stage stamp'))
    monkeypatch.setattr(db, 'socialapi_claim_done', lambda *a: pytest.fail('pre-stage claim done'))
    store = FakeMediaStore(assets=[make_asset('p1', gym_id='pierce', kind='photo')])
    draft = builder.build_gym_media_draft(_Acct(), '2026-08-27', 'faces',
        voice=object(), source=object(), store=store, drive=FakeDrive(blobs={'p1': b'jpgbytes'}),
        library_dir=str(tmp_path), remote_writer_bound=True)
    assert draft is not None and draft._drive_use_pending == marker
    assert len(calls) == 1
    assert store.assets['p1']['used_count'] == 0


def _real_feed_stage(lane, canonical_tenant=None):
    from tests.test_forward_schedule_reservation import _armed_http, _group_rows, _store as atomic_store
    row = dict(lane['row'])
    row.pop('id')
    row[pcs.RESERVATION_PROOF] = _group_rows()[0][pcs.RESERVATION_PROOF]
    row.update(creative_origin=None, generated_artifact_version_id=None,
               generated_artifact_sha256=None)
    http, _ = _armed_http([row])
    if canonical_tenant is not None:
        from tests.test_forward_schedule_reservation import _Resp
        http.gets[pcs._FORWARD_TENANT_ALIAS_TABLE] = _Resp(200, [
            dict(alias_key='gym1', tenant_id=canonical_tenant)])
    store = atomic_store(http)
    staged = store.insert_rows('gym1', [row], before_forward_stage=feed.stage_callback('gym1', [lane['draft']]))
    entry = journal.unsettled()[0]
    bound = journal.forward_stage_for_member(staged[0]['id'])
    active = dict(staged[0], variant_status='active', media_not_ready_reason=None)
    # Use a restarted transport adapter: exact PG terminal receipt is supplied
    # after real insert_rows transformed and journaled the candidate batch.
    read_http = lane['http']
    read_http.status_payload = _status_payload(bound, 'finalized', _terminal_receipt(bound))
    return entry, active, lane['store'], read_http


def test_real_insert_rows_candidate_finalizes_and_settles_original_use(lane, monkeypatch):
    entry, active, store, http = _real_feed_stage(lane)
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    calls = []
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: calls.append(k['use_id']))
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt', lambda uid: _receipt(entry))
    result = recovery.run(store=store, media_store=object())
    assert result['recovered'] == 1 and result['held'] == 0
    assert calls == [entry['use_id']]
    assert journal.get(entry['use_id'])['state'] == 'claim_done'


def test_unrelated_tenant_pending_batch_does_not_hold_month(lane):
    _real_feed_stage(lane)
    result = feed.recover(store=lane['store'], logger=lambda _: None, tenant_id='gym2')
    assert result['ok'] is True and result['examined'] == 0
    assert journal.unsettled()[0]['state'] == 'write_intent'


def test_current_tenant_unresolved_batch_still_holds_month(lane):
    _real_feed_stage(lane)
    result = feed.recover(store=lane['store'], logger=lambda _: None, tenant_id='gym1')
    assert result['ok'] is False


def test_global_journal_pin_error_holds_even_unrelated_tenant(lane, monkeypatch):
    monkeypatch.setenv(recovery.JOURNAL_ID_ENV, str(uuid.uuid4()))
    result = feed.recover(store=lane['store'], logger=lambda _: None, tenant_id='gym2')
    assert result['ok'] is False


@pytest.mark.parametrize('field,value', [
    ('caption', 'changed'), ('source_media_asset_id', 'other-asset'),
    ('image_url', 'https://img/changed'), ('render_manifest_digest', 'sha256:' + 'a' * 64),
    ('gym_id', 'other'), ('creative_origin', 'generated'),
    ('generated_artifact_version_id', '33333333-3333-4333-8333-333333333333'),
    ('generated_artifact_sha256', 'a' * 64),
])
def test_real_finalized_feed_changed_bound_fields_never_consume(lane, monkeypatch, field, value):
    entry, active, store, _ = _real_feed_stage(lane)
    active[field] = value
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: pytest.fail('changed row consumed'))
    result = recovery.run(store=store, media_store=object())
    assert result['held'] == 1 and result['recovered'] == 0
    assert journal.get(entry['use_id'])['state'] == 'write_intent'


def test_real_candidate_cannot_land_from_active_row_without_terminal_proof(lane):
    entry, active, _, _ = _real_feed_stage(lane)
    bound = journal.forward_stage_for_member(active['id'])
    assert bound['state'] == 'staged_pending'
    assert journal.forward_landed_entry_matches(entry, active, bound) is False
    with pytest.raises(journal.JournalHold):
        journal.confirm_landed(entry['use_id'], dict(calendar_row=active,
            asset_id=entry['asset_id'], content_hash=entry['content_hash']))
    assert journal.get(entry['use_id'])['state'] == 'write_intent'


def test_real_candidate_lost_stage_ack_recovers_same_uuid_after_finalization(lane, monkeypatch):
    from tests.test_forward_schedule_reservation import _armed_http, _group_rows, _store as atomic_store
    row = dict(lane['row'])
    row.pop('id')
    row[pcs.RESERVATION_PROOF] = _group_rows()[0][pcs.RESERVATION_PROOF]
    def lost_ack(args):
        raise RuntimeError('lost stage acknowledgment')
    http, _ = _armed_http([row], stage_resp=lost_ack)
    with pytest.raises(Exception):
        atomic_store(http).insert_rows('gym1', [row],
            before_forward_stage=feed.stage_callback('gym1', [lane['draft']]))
    entry = journal.unsettled()[0]
    assert entry['calendar_row']['variant_status'] == 'candidate'
    bound = journal.forward_stage_for_member(entry['calendar_row']['id'])
    assert bound['state'] == 'stage_intent'
    active = dict(entry['calendar_row'], variant_status='active', media_not_ready_reason=None)
    lane['http'].status_payload = _status_payload(bound, 'finalized', _terminal_receipt(bound))
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    calls = []
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: calls.append(k['use_id']))
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt', lambda uid: _receipt(entry))
    assert recovery.run(store=lane['store'], media_store=object())['recovered'] == 1
    assert calls == [entry['use_id']]


def test_actual_month_early_exit_is_independent_of_other_tenant_stage(lane, monkeypatch):
    from agent import client_month_run as cmr
    _real_feed_stage(lane)
    monkeypatch.setattr(cmr.config, 'client_month_enabled', lambda: False)
    other = cmr.build_client_month(None, 'gym2', '2026-10-10', voice=None,
                                   store=lane['store'], logger=lambda _: None)
    assert other['reason'] == 'AGENT_CLIENT_MONTH off'
    own = cmr.build_client_month(None, 'gym1', '2026-10-10', voice=None,
                                 store=lane['store'], logger=lambda _: None)
    assert own['ok'] is False and own['reason'] != 'AGENT_CLIENT_MONTH off'


def test_verified_alias_real_insert_and_original_uuid_recovery(lane, monkeypatch):
    entry, active, store, http = _real_feed_stage(lane, canonical_tenant='canonical-gym1')
    assert entry['gym_id'] == active['gym_id'] == 'gym1'
    bound = journal.forward_stage_for_member(active['id'])
    assert bound['tenant_id'] == 'canonical-gym1'
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    calls = []
    def stamp(*a, **k):
        assert a[1] == 'gym1'  # CAS owns the original raw asset/source tenant.
        calls.append(k['use_id'])
    monkeypatch.setattr(gym_media_selector, 'stamp_use', stamp)
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt', lambda uid: _receipt(entry))
    assert recovery.run(store=store, media_store=object(), tenant_id='gym1')['recovered'] == 1
    assert calls == [entry['use_id']]
    assert http.status_calls[0]['p_batch_id'] == bound['batch_id']
    assert journal.get_forward_stage(bound['batch_id'])['tenant_id'] == 'canonical-gym1'
    assert journal.get(entry['use_id'])['state'] == 'claim_done'


@pytest.mark.parametrize('damage', ['missing', 'raw', 'canonical', 'digest'])
def test_alias_binding_missing_or_conflicting_never_consumes(lane, monkeypatch, damage):
    entry, active, store, _ = _real_feed_stage(lane, canonical_tenant='canonical-gym1')
    import sqlite3
    with sqlite3.connect(str(db.db_path())) as conn:
        if damage == 'missing':
            conn.execute('DELETE FROM gbp_forward_stage_tenant_binding')
        else:
            column = {'raw': 'raw_tenant_id', 'canonical': 'tenant_id', 'digest': 'request_digest'}[damage]
            conn.execute(f'UPDATE gbp_forward_stage_tenant_binding SET {column}=?', ('wrong',))
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: pytest.fail('alias mismatch consumed'))
    result = recovery.run(store=store, media_store=object(), tenant_id='gym1')
    assert result['held'] == 1 and result['recovered'] == 0
    assert journal.get(entry['use_id'])['state'] == 'write_intent'


def test_alias_terminal_raw_tenant_response_cannot_replace_canonical_receipt(lane, monkeypatch):
    entry, active, store, http = _real_feed_stage(lane, canonical_tenant='canonical-gym1')
    http.status_payload['tenant_id'] = 'gym1'
    http.status_payload['finalize_receipt']['tenant_id'] = 'gym1'
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: pytest.fail('raw routing receipt consumed'))
    assert recovery.run(store=store, media_store=object())['held'] == 1
    assert journal.get(entry['use_id'])['state'] == 'write_intent'


def test_alias_cannot_change_raw_calendar_owner_to_canonical(lane, monkeypatch):
    entry, active, store, _ = _real_feed_stage(lane, canonical_tenant='canonical-gym1')
    active['gym_id'] = 'canonical-gym1'
    monkeypatch.setattr(gbp_planner, '_readback_inserted_rows', lambda *a, **k: [deepcopy(active)])
    monkeypatch.setattr(gym_media_selector, 'stamp_use', lambda *a, **k: pytest.fail('cross-tenant row consumed'))
    assert recovery.run(store=store, media_store=object())['held'] == 1
    assert journal.get(entry['use_id'])['state'] == 'write_intent'


def test_alias_manifest_proof_keeps_raw_owner_and_canonical_authority(lane, monkeypatch):
    entry, active, store, _ = _real_feed_stage(lane, canonical_tenant='canonical-gym1')
    bound = journal.forward_stage_for_member(active['id'])
    store.bind_forward_finalization(bound['batch_id'])
    bound = journal.forward_stage_for_member(active['id'])
    # Isolate authority tenant validation; source extraction has independent
    # packet/alias tests. No remote manifest or source proof is fabricated.
    sha = 'b' * 64
    monkeypatch.setattr(journal, 'forward_stage_source_sha256', lambda b, rid: sha)
    active['render_manifest_digest'] = 'sha256:' + 'c' * 64
    fields = {'calendar_row_id': 'id', 'gym_id': 'gym_id', 'account': 'account',
              'format': 'format', 'gbp_location_id': 'gbp_location_id',
              'post_date': 'post_date', 'group_key': 'visual_group_key',
              'source_asset_id': 'source_media_asset_id', 'source_url': 'source_media_url',
              'image_url': 'image_url', 'thumbnail_url': 'thumbnail_url',
              'render_manifest_digest': 'render_manifest_digest'}
    snapshot = {k: active.get(v) for k, v in fields.items()}
    snapshot.update(tenant_id='canonical-gym1', revision='d' * 32)
    proof = dict(tenant_id='canonical-gym1', row_revision='d' * 32,
        post_date=entry['post_date'], logical_post_id=entry['logical_post_id'],
        source_sha256=sha, lineage_evidence_id=str(uuid.uuid4()),
        attestation_ids=[str(uuid.uuid4()) for _ in range(3)],
        reservation_id=bound['finalize_receipt']['reservation_ids'][0])
    evidence = dict(snapshot=snapshot, reservation_proof=proof)
    assert journal.forward_manifest_evidence_matches(entry, active, bound, evidence)
    for container, field, value in [('snapshot', 'tenant_id', 'gym1'),
            ('reservation_proof', 'tenant_id', 'gym1'),
            ('snapshot', 'gym_id', 'canonical-gym1'),
            ('reservation_proof', 'source_sha256', 'e' * 64)]:
        changed = deepcopy(evidence)
        changed[container][field] = value
        assert not journal.forward_manifest_evidence_matches(entry, active, bound, changed)
