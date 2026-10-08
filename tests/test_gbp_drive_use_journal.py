"""Synthetic local-only checks for the GBP drive use journal. No provider calls."""
from copy import deepcopy
import uuid

import pytest

from agent import gbp_drive_use_journal as journal
from agent.gbp_drive_use_journal import JournalHold


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / 'journal.db')


def make_request(logical='lp-1', gym='gym', asset_id='asset-1', source_id='src-1',
                 content_hash='hash-1', caption='hello'):
    row = dict(logical_post_id=logical, gym_id=gym, post_date='2026-10-10',
               format='update', image_url='https://img/1', account='acct',
               caption=caption, source_media_asset_id=asset_id)
    return dict(
        gym_id=gym, logical_post_id=logical, claim_id='claim-1',
        post_date='2026-10-10', content_hash=content_hash,
        epoch_id='11111111-1111-4111-8111-111111111111',
        asset_id=asset_id, source_id=source_id,
        calendar_row=row, payload=deepcopy(row),
        asset_before=dict(id=asset_id, gym_id=gym, source_id=source_id,
                          content_hash=content_hash, used_count=0,
                          drive_use_version=1, eligible=True, excluded_by_coach=False,
                          last_used_at=None),
        source_before=dict(id=source_id, gym_id=gym, kind='gym_drive',
                           active=True, drive_use_version=2))


def test_same_identity_across_restart_reuses_uuid(db):
    request = make_request()
    first = journal.prepare(request, path=db)
    # Simulate a process restart: no shared state beyond the DB file.
    second = journal.prepare(deepcopy(request), path=db)
    assert second['use_id'] == first['use_id']
    assert second['state'] == 'prepared'
    assert second['asset_before'] == first['asset_before']
    assert second['source_before'] == first['source_before']
    assert second['request_digest'] == first['request_digest']
    by_logical = journal.get_by_logical_post('gym', 'lp-1', path=db)
    assert by_logical['use_id'] == first['use_id']


def test_payload_conflict_under_same_logical_id_holds(db):
    journal.prepare(make_request(), path=db)
    changed = make_request(caption='different caption')
    with pytest.raises(JournalHold, match='journal_identity_conflict'):
        journal.prepare(changed, path=db)
    # And no second UUID was minted.
    assert len(journal.unsettled(path=db)) == 1


def test_tenant_asset_source_mismatch_holds(db):
    journal.prepare(make_request(), path=db)
    for mutate in (
            lambda r: r.update(gym_id='othergym'),
            lambda r: r.update(asset_id='asset-2'),
            lambda r: r.update(source_id='src-2'),
            lambda r: r.update(content_hash='hash-2')):
        changed = make_request()
        mutate(changed)
        with pytest.raises(JournalHold, match='journal_identity_conflict|journal_request_invalid'):
            journal.prepare(changed, path=db)
    assert len(journal.unsettled(path=db)) == 1


def test_prewrite_cancel_marks_abandoned_without_use(db):
    entry = journal.prepare(make_request(), path=db)
    cancelled = journal.abandon(entry['use_id'], path=db)
    assert cancelled['state'] == 'abandoned'
    assert cancelled['landed_proof'] is None and cancelled['receipt'] is None
    assert journal.unsettled(path=db) == []
    # Cannot send intent after abandonment.
    with pytest.raises(JournalHold):
        journal.record_write_intent(entry['use_id'], path=db)
    # A genuinely new placement may reuse the logical id with a fresh UUID.
    fresh = journal.prepare(make_request(), path=db)
    assert fresh['use_id'] != entry['use_id']
    # Abandon after write intent is refused.
    journal.record_write_intent(fresh['use_id'], path=db)
    with pytest.raises(JournalHold):
        journal.abandon(fresh['use_id'], path=db)


def test_write_intent_required_before_send_outcomes(db):
    entry = journal.prepare(make_request(), path=db)
    with pytest.raises(JournalHold, match='journal_unknown_requires_intent'):
        journal.mark_unknown(entry['use_id'], path=db)
    with pytest.raises(JournalHold, match='journal_landed_requires_intent_or_unknown'):
        journal.confirm_landed(entry['use_id'], _evidence(entry), path=db)
    intent = journal.record_write_intent(entry['use_id'], path=db)
    assert intent['state'] == 'write_intent'
    # One-shot: intent cannot be re-recorded (no duplicate send arming).
    with pytest.raises(JournalHold):
        journal.record_write_intent(entry['use_id'], path=db)


def _evidence(entry):
    return dict(calendar_row=deepcopy(entry['calendar_row']),
                asset_id=entry['asset_id'], content_hash=entry['content_hash'])


def test_unknown_timeout_and_zero_readback_stay_unknown(db):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    unknown = journal.mark_unknown(entry['use_id'], path=db)
    assert unknown['state'] == 'unknown_result'
    after = journal.record_zero_readback(entry['use_id'], path=db)
    assert after['state'] == 'unknown_result'
    assert after['zero_readbacks'] == 1
    again = journal.record_zero_readback(entry['use_id'], path=db)
    assert again['state'] == 'unknown_result'
    assert again['zero_readbacks'] == 2
    # Still unsettled and recoverable after 'restart'.
    pending = journal.unsettled(path=db)
    assert [e['use_id'] for e in pending] == [entry['use_id']]


def test_exact_landed_proof_and_mismatch_hold(db):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    journal.mark_unknown(entry['use_id'], path=db)
    wrong = _evidence(entry)
    wrong['calendar_row'] = dict(wrong['calendar_row'], caption='tampered')
    with pytest.raises(JournalHold, match='journal_landed_evidence_mismatch'):
        journal.confirm_landed(entry['use_id'], wrong, path=db)
    assert journal.get(entry['use_id'], path=db)['state'] == 'unknown_result'
    landed = journal.confirm_landed(entry['use_id'], _evidence(entry), path=db)
    assert landed['state'] == 'confirmed_landed'
    assert landed['landed_proof']['calendar_row'] == entry['calendar_row']
    # Proof is durable across reopen and consumption cannot start twice-early.
    reopened = journal.get(entry['use_id'], path=db)
    assert reopened['landed_proof']['use_id'] == entry['use_id']


def test_partial_batch_settles_only_exact_rows(db):
    one = journal.prepare(make_request(logical='lp-a'), path=db)
    two = journal.prepare(make_request(logical='lp-b', asset_id='asset-2',
                                       content_hash='hash-2'), path=db)
    for entry in (one, two):
        journal.record_write_intent(entry['use_id'], path=db)
        journal.mark_unknown(entry['use_id'], path=db)
    # Only the row with exact readback evidence lands.
    landed = journal.confirm_landed(one['use_id'], _evidence(one), path=db)
    assert landed['state'] == 'confirmed_landed'
    assert journal.get(two['use_id'], path=db)['state'] == 'unknown_result'
    # The landed row is still unsettled (consumption outstanding) and durable;
    # the row without exact evidence remains unknown.
    pending = {e['use_id']: e['state'] for e in journal.unsettled(path=db)}
    assert pending[two['use_id']] == 'unknown_result'
    assert pending[one['use_id']] == 'confirmed_landed'


def test_receipt_pending_and_same_uuid_recovery(db):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    journal.confirm_landed(entry['use_id'], _evidence(entry), path=db)
    # Landed proof exists before consumption begins; receipt alone is refused.
    with pytest.raises(JournalHold, match='journal_receipt_requires_pending'):
        journal.confirm_receipt(entry['use_id'], {'ok': True}, path=db)
    pending = journal.begin_consumption(entry['use_id'], path=db)
    assert pending['state'] == 'consumption_pending'
    # Crash before receipt: recovery resumes the SAME UUID, no new mint.
    recovered = journal.prepare(make_request(), path=db)
    assert recovered['use_id'] == entry['use_id']
    assert recovered['state'] == 'consumption_pending'
    settled = journal.confirm_receipt(entry['use_id'],
                                      _receipt(entry), path=db)
    assert settled['state'] == 'receipt_confirmed'
    assert settled['receipt']['request']['use_id'] == entry['use_id']
    assert journal.unsettled(path=db) == []


def test_local_write_failure_holds_prior_state(db, monkeypatch):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    # Simulate a local ack failure: the guarded update silently matches nothing.
    real = journal.sqlite3.connect

    class FlakyCursor:
        rowcount = 0
    class FlakyConn:
        def __init__(self, inner):
            self._inner = inner
        def execute(self, sql, params=()):
            cur = self._inner.execute(sql, params)
            if sql.startswith('UPDATE'):
                return FlakyCursor()
            return cur
        def executescript(self, s):
            return self._inner.executescript(s)
        def close(self):
            return self._inner.close()

    monkeypatch.setattr(journal.sqlite3, 'connect',
                        lambda *a, **k: FlakyConn(real(*a, **k)))
    with pytest.raises(JournalHold, match='journal_unknown_requires_intent|journal_local_write_failed'):
        journal.mark_unknown(entry['use_id'], path=db)
    monkeypatch.setattr(journal.sqlite3, 'connect', real)
    assert journal.get(entry['use_id'], path=db)['state'] == 'write_intent'


def test_identity_conflict_blocks_retry_after_partial_progress(db):
    # A changed payload may not ride the existing logical id even mid-flight.
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    with pytest.raises(JournalHold, match='journal_identity_conflict'):
        journal.prepare(make_request(caption='edited'), path=db)
    assert journal.get(entry['use_id'], path=db)['state'] == 'write_intent'


def _receipt(entry):
    request = journal._remote_request(entry, entry['use_id'])
    after = deepcopy(entry['asset_before'])
    after.update(used_count=1, last_used_at='2026-10-10T12:00:00Z',
                 drive_use_version=after['drive_use_version'] + 1)
    return dict(state='applied', request=request, asset_after=after,
                source_after=deepcopy(entry['source_before']))


def _pending(db):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    journal.confirm_landed(entry['use_id'], _evidence(entry), path=db)
    journal.begin_consumption(entry['use_id'], path=db)
    return entry


def test_logical_identity_is_tenant_scoped(db):
    one = journal.prepare(make_request(gym='gym'), path=db)
    assert journal.get_by_logical_post('othergym', 'lp-1', path=db) is None
    two = journal.prepare(make_request(gym='othergym'), path=db)
    assert one['use_id'] != two['use_id']
    for gym, entry in [('gym', one), ('othergym', two)]:
        assert journal.get_by_logical_post(gym, 'lp-1', path=db)['use_id'] == entry['use_id']


@pytest.mark.parametrize('field,value', [
    ('gym_id', 'othergym'), ('id', 'wrong'), ('source_id', 'wrong'),
    ('content_hash', 'wrong'), ('used_count', 1), ('drive_use_version', 0),
    ('eligible', False), ('excluded_by_coach', True)])
def test_inconsistent_asset_snapshot_holds(db, field, value):
    request = make_request()
    request['asset_before'][field] = value
    with pytest.raises(JournalHold, match='journal_request_invalid'):
        journal.prepare(request, path=db)


@pytest.mark.parametrize('part,field,value', [
    ('source_before', 'gym_id', 'othergym'), ('source_before', 'id', 'wrong'),
    ('source_before', 'kind', 'other'), ('source_before', 'active', False),
    ('calendar_row', 'gym_id', 'othergym'),
    ('calendar_row', 'post_date', '2026-10-11'),
    ('calendar_row', 'source_media_asset_id', 'wrong'),
    ('payload', 'caption', 'contradictory')])
def test_inconsistent_request_shapes_hold(db, part, field, value):
    request = make_request()
    request[part][field] = value
    with pytest.raises(JournalHold, match='journal_request_invalid'):
        journal.prepare(request, path=db)


def test_epoch_is_required_and_frozen(db):
    request = make_request()
    missing = deepcopy(request)
    missing.pop('epoch_id')
    with pytest.raises(JournalHold, match='journal_request_invalid'):
        journal.prepare(missing, path=db)
    journal.prepare(request, path=db)
    request['epoch_id'] = str(uuid.uuid4())
    with pytest.raises(JournalHold, match='journal_identity_conflict'):
        journal.prepare(request, path=db)


@pytest.mark.parametrize('mutate', [
    lambda r: r.clear(),
    lambda r: r['request'].update(use_id=str(uuid.uuid4())),
    lambda r: r['request'].update(gym_id='othergym'),
    lambda r: r['request'].update(asset_id='wrong'),
    lambda r: r['request'].update(source_id='wrong'),
    lambda r: r['request'].update(content_hash='wrong'),
    lambda r: r['request'].update(epoch_id=str(uuid.uuid4())),
    lambda r: r['request'].update(post_date='2026-10-11'),
    lambda r: r['asset_after'].update(used_count=0),
    lambda r: r['asset_after'].update(drive_use_version=1),
    lambda r: r['asset_after'].update(last_used_at=None),
    lambda r: r['asset_after'].update(gym_id='othergym'),
    lambda r: r['source_after'].update(active=False),
    lambda r: r.update(unverified=True),
])
def test_invalid_remote_receipts_never_settle(db, mutate):
    entry = _pending(db)
    receipt = _receipt(entry)
    mutate(receipt)
    with pytest.raises(JournalHold, match='journal_receipt_invalid'):
        journal.confirm_receipt(entry['use_id'], receipt, path=db)
    persisted = journal.get(entry['use_id'], path=db)
    assert persisted['state'] == 'consumption_pending'
    assert persisted['receipt'] is None


@pytest.mark.parametrize('field,value', [
    ('gym_id', 'othergym'), ('source_id', 'wrong'),
    ('logical_post_id', 'wrong'), ('epoch_id', str(uuid.uuid4())),
    ('payload', {'caption': 'contradictory'}),
    ('asset_before', {}), ('source_before', {})])
def test_contradictory_landing_envelope_holds(db, field, value):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    evidence = _evidence(entry)
    evidence[field] = value
    with pytest.raises(JournalHold, match='journal_landed_evidence_mismatch'):
        journal.confirm_landed(entry['use_id'], evidence, path=db)
    assert journal.get(entry['use_id'], path=db)['state'] == 'write_intent'


def test_server_added_landing_fields_are_explicit(db):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    evidence = _evidence(entry)
    evidence['calendar_row'].update(id='server-row', created_at='2026-10-08T12:00:00Z',
                                  updated_at='2026-10-08T12:00:00Z',
                                  published_at=None, late_post_id=None)
    for additions in ({'unknown_server_field': None}, {'published_at': 'now'},
                      {'source_media_asset_id': 'wrong'}, {'caption': 'different'}):
        bad = deepcopy(evidence)
        bad['calendar_row'].update(additions)
        with pytest.raises(JournalHold, match='journal_landed_evidence_mismatch'):
            journal.confirm_landed(entry['use_id'], bad, path=db)
    assert journal.confirm_landed(entry['use_id'], evidence, path=db)['state'] == 'confirmed_landed'


def test_connect_failure_is_typed(db, monkeypatch):
    def fail(*args, **kwargs):
        raise journal.sqlite3.OperationalError('private database path')
    monkeypatch.setattr(journal.sqlite3, 'connect', fail)
    for operation in (lambda: journal.prepare(make_request(), path=db),
                      lambda: journal.get('uuid', path=db),
                      lambda: journal.get_by_logical_post('gym', 'lp-1', path=db),
                      lambda: journal.unsettled(path=db),
                      lambda: journal.mark_unknown('uuid', path=db)):
        with pytest.raises(JournalHold, match='^journal_local_db_unavailable$'):
            operation()


@pytest.mark.parametrize('committed', [True, False])
@pytest.mark.parametrize('operation', ['prepare', 'transition'])
def test_lost_commit_ack_requires_exact_durable_readback(db, monkeypatch, committed, operation):
    entry = journal.prepare(make_request(), path=db) if operation == 'transition' else None
    real = journal.sqlite3.connect
    connections = []

    class LostAck:
        def __init__(self, inner):
            self.inner = inner
        def execute(self, sql, params=()):
            if sql == 'COMMIT':
                if committed:
                    self.inner.execute(sql, params)
                raise journal.sqlite3.OperationalError('lost commit acknowledgment')
            return self.inner.execute(sql, params)
        def close(self):
            self.inner.close()

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        connections.append(conn)
        return LostAck(conn) if len(connections) == 1 else conn

    monkeypatch.setattr(journal.sqlite3, 'connect', connect)
    action = (lambda: journal.prepare(make_request(), path=db)) if operation == 'prepare' else (
        lambda: journal.record_write_intent(entry['use_id'], path=db))
    if committed:
        result = action()
        assert result == journal.get(result['use_id'], path=db)
        assert result['state'] == ('prepared' if operation == 'prepare' else 'write_intent')
    else:
        with pytest.raises(JournalHold, match='journal_commit_outcome_unknown'):
            action()
        if entry:
            assert journal.get(entry['use_id'], path=db)['state'] == 'prepared'
        else:
            assert journal.get_by_logical_post('gym', 'lp-1', path=db) is None


def test_lost_ack_with_unavailable_readback_holds_without_claiming_prior_state(db, monkeypatch):
    entry = journal.prepare(make_request(), path=db)
    real = journal.sqlite3.connect
    calls = 0

    class LostAck:
        def __init__(self, inner):
            self.inner = inner
        def execute(self, sql, params=()):
            result = self.inner.execute(sql, params)
            if sql == 'COMMIT':
                raise journal.sqlite3.OperationalError('lost ack')
            return result
        def close(self):
            self.inner.close()

    def connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise journal.sqlite3.OperationalError('readback unavailable')
        return LostAck(real(*args, **kwargs))

    monkeypatch.setattr(journal.sqlite3, 'connect', connect)
    with pytest.raises(JournalHold, match='journal_commit_outcome_unknown'):
        journal.record_write_intent(entry['use_id'], path=db)
    monkeypatch.setattr(journal.sqlite3, 'connect', real)
    # The intended state actually committed; the hold did NOT assert rollback.
    assert journal.get(entry['use_id'], path=db)['state'] == 'write_intent'


def test_lost_ack_with_different_durable_after_image_holds(db, monkeypatch):
    entry = journal.prepare(make_request(), path=db)
    real = journal.sqlite3.connect
    calls = 0

    class DifferentAfterImage:
        def __init__(self, inner):
            self.inner = inner
        def execute(self, sql, params=()):
            result = self.inner.execute(sql, params)
            if sql == 'COMMIT':
                self.inner.execute('UPDATE gbp_drive_use_journal SET zero_readbacks=7 WHERE use_id=?',
                                   (entry['use_id'],))
                raise journal.sqlite3.OperationalError('lost ack')
            return result
        def close(self):
            self.inner.close()

    def connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        inner = real(*args, **kwargs)
        return DifferentAfterImage(inner) if calls == 1 else inner

    monkeypatch.setattr(journal.sqlite3, 'connect', connect)
    with pytest.raises(JournalHold, match='journal_commit_outcome_unknown'):
        journal.record_write_intent(entry['use_id'], path=db)
    assert journal.get(entry['use_id'], path=db)['zero_readbacks'] == 7


def test_duplicate_landing_use_id_cannot_contradict_frozen_identity(db):
    entry = journal.prepare(make_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    evidence = _evidence(entry)
    evidence['use_id'] = str(uuid.uuid4())
    with pytest.raises(JournalHold, match='journal_landed_evidence_mismatch'):
        journal.confirm_landed(entry['use_id'], evidence, path=db)


def test_receipt_stable_after_image_requires_exact_json_types(db):
    entry = _pending(db)
    receipt = _receipt(entry)
    receipt['asset_after']['eligible'] = 1
    with pytest.raises(JournalHold, match='journal_receipt_invalid'):
        journal.confirm_receipt(entry['use_id'], receipt, path=db)


def _real_gbp_request(logical=None):
    request = make_request(logical=logical or str(uuid.uuid4()))
    row = request['calendar_row']
    row.update(account='googlebusiness', status='pending', pillar='photo',
               gbp_topic_type='STANDARD', gbp_cta_type='LEARN_MORE',
               gbp_cta_url='https://gym.example/book',
               source_media_url='https://img/1')
    request['payload'] = deepcopy(row)
    return request


def _full_known_readback(entry):
    # Independently named fields from the cited repository schema contracts;
    # deliberately not derived from the implementation's default mapping.
    result = dict.fromkeys((
        'thumbnail_url', 'source_media_url', 'time_slot', 'slot_index',
        'media_not_ready_reason', 'scheduled_at', 'published_at', 'late_post_id',
        'publish_claim_token', 'publish_reservation_day', 'variant_of',
        'hook_family', 'ask_type', 'caption_len_band', 'has_member_face',
        'experiment_label', 'event_id', 'approval_kind', 'approved_by',
        'approved_at', 'approval_digest', 'gbp_topic_type', 'gbp_cta_type',
        'gbp_cta_url', 'gbp_event', 'gbp_offer', 'gbp_location_id', 'reject_reason',
    ), None)
    result.update(variant_status='active', id=str(uuid.uuid4()),
                  created_at='2026-10-08T12:00:00Z', updated_at='2026-10-08T12:00:00Z')
    result.update(deepcopy(entry['calendar_row']))
    return result


def test_uuid_gbp_full_known_nullable_readback_lands(db):
    entry = journal.prepare(_real_gbp_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    evidence = _evidence(entry)
    evidence['calendar_row'] = _full_known_readback(entry)
    assert evidence['calendar_row']['publish_claim_token'] is None
    landed = journal.confirm_landed(entry['use_id'], evidence, path=db)
    assert landed['state'] == 'confirmed_landed'
    assert landed['landed_proof']['calendar_row'] == evidence['calendar_row']


@pytest.mark.parametrize('field,value', [
    ('publish_claim_token', str(uuid.uuid4())),
    ('publish_reservation_day', '2026-10-10'), ('published_at', '2026-10-10T12:00:00Z'),
    ('late_post_id', 'provider-id'), ('variant_status', 'candidate'),
    ('variant_status', 'archived'), ('media_not_ready_reason', 'media_hidden'),
    ('approved_at', '2026-10-08T12:00:00Z'), ('approved_by', 'foreign-owner'),
    ('gbp_offer', {'couponCode': 'surprise'}), ('unknown_column', None),
    ('caption', 'changed'), ('gym_id', 'foreign'), ('source_media_asset_id', 'foreign'),
])
def test_full_readback_conflicting_state_and_unknown_fields_hold(db, field, value):
    entry = journal.prepare(_real_gbp_request(), path=db)
    journal.record_write_intent(entry['use_id'], path=db)
    evidence = _evidence(entry)
    evidence['calendar_row'] = _full_known_readback(entry)
    evidence['calendar_row'][field] = value
    with pytest.raises(JournalHold, match='journal_landed_evidence_mismatch'):
        journal.confirm_landed(entry['use_id'], evidence, path=db)
    assert journal.get(entry['use_id'], path=db)['state'] == 'write_intent'


def test_heterogeneous_batch_key_union_keeps_exact_member_projection(db):
    one = _real_gbp_request()
    two = _real_gbp_request()
    two['calendar_row']['format'] = 'offer'
    two['calendar_row']['gbp_offer'] = {'couponCode': 'BOOK', 'redeemOnlineUrl': 'https://gym.example'}
    two['calendar_row']['gbp_event'] = {'title': 'Open house'}
    two['calendar_row'].pop('gbp_cta_type')
    two['calendar_row'].pop('gbp_cta_url')
    two['payload'] = deepcopy(two['calendar_row'])
    entries = [journal.prepare(request, path=db) for request in (one, two)]
    # portal_calendar_store.insert_rows: normalize UNION of keys, missing=None.
    all_keys = set().union(*(request['calendar_row'].keys() for request in (one, two)))
    normalized = [{key: request['calendar_row'].get(key) for key in all_keys}
                  for request in (one, two)]
    for entry, row in zip(entries, normalized):
        journal.record_write_intent(entry['use_id'], path=db)
        evidence = _evidence(entry)
        evidence['calendar_row'] = dict(_full_known_readback(entry), **row)
        assert journal.confirm_landed(entry['use_id'], evidence, path=db)['state'] == 'confirmed_landed'
    # Non-null fields from another member cannot bleed into this member.
    assert normalized[0]['gbp_offer'] is None
    assert normalized[1]['gbp_cta_type'] is None
