"""Persisted shared Story hold provenance and cross-service recovery; all offline."""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent import fixer_business_seed as seeds, fixer_business_evidence as proof
from agent import fixer_ops, ops_alerts, stories, portal_calendar_store as calendar
from agent.drafter import Draft, DraftStatus, _make_id
from agent.slack_convo.bus import Bus, BusError

CLIENT = '22222222-2222-4222-8222-222222222222'
ROW_ID = '33333333-3333-4333-8333-333333333333'
DAY = '2026-10-02'
PARAMS = {'row_id': ROW_ID, 'calendar_gym_key': 'chateau123',
          'account': 'instagram', 'post_date': DAY, 'created_at': DAY + 'T12:00:00+00:00'}


def held_row(**patch):
    return {'id': ROW_ID, 'gym_id': 'chateau123', 'account': 'instagram',
            'post_date': DAY, 'format': 'story', 'status': 'pending', 'image_url': '',
            'media_not_ready_reason': 'Story media not ready: render failed',
            'created_at': DAY + 'T12:00:00+00:00', 'variant_status': 'active', **patch}


class MemoryBus(Bus):
    def __init__(self):
        self.rows = {}
        self.calendar = [held_row()]
        self.messages = []
        self.tokens = [{'gym_id': CLIENT, 'echo_account_key': 'chateau123'}]

    def _get(self, table, params):
        data = {'echo_intake_tokens': self.tokens, 'support_tickets': list(self.rows.values()),
                'content_calendar': self.calendar, 'support_messages': self.messages}[table]
        return [deepcopy(row) for row in data
                if all(row.get(k) == v[3:] for k, v in params.items()
                       if k not in {'select', 'limit', 'order'} and v.startswith('eq.'))]

    def _insert(self, table, row):
        assert table == 'support_tickets'
        if row['id'] in self.rows:
            return None, True
        self.rows[row['id']] = deepcopy(row)
        return deepcopy(row), False


def test_shared_story_source_has_exact_row_tenant_submission_and_scout_request_key():
    bus = MemoryBus()
    seed = seeds.prepare_story_hold_seed(held_row(), bus=bus)
    text = seeds.story_hold_message(held_row())
    a = seeds.persist(seed, text, bus=bus)
    b = seeds.persist(seed, text, bus=bus)
    row = a['ticket']
    assert a['duplicate'] is False and b['duplicate'] is True and len(bus.rows) == 1
    assert row['client_id'] == CLIENT and row['submission_key'] == row['id']
    assert row['reporter'] == 'echo_story_hold'
    plan = row['verification_before']['fixer']['business_check']
    assert plan['params'] == PARAMS and plan['check_id'] == 'story_calendar_media_ready'
    identity = [row['id'], row['created_at'], row['raw_text']]
    assert plan['request_key'] == hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    # A retry's changed failure prose does not select a target or mint another request.
    assert seed == seeds.prepare_story_hold_seed(held_row(media_not_ready_reason='Story media not ready: hosting failed'), bus=bus)


@pytest.mark.parametrize('field,value', [('row_id', 'bad'), ('client_id', 'bad'),
                                       ('source_event_id', 'a' * 64), ('params', {'post_date': DAY})])
def test_forged_or_incomplete_story_seed_cannot_persist(field, value):
    bus = MemoryBus()
    seed = seeds.prepare_story_hold_seed(held_row(), bus=bus)
    seed[field] = value
    with pytest.raises(seeds.SeedError):
        seeds.persist(seed, 'Story held', bus=bus)
    assert not bus.rows


def test_remapped_or_ambiguous_tenant_refuses_story_ticket():
    bus = MemoryBus()
    seed = seeds.prepare_story_hold_seed(held_row(), bus=bus)
    bus.tokens.append({'gym_id': CLIENT, 'echo_account_key': 'othergym'})
    with pytest.raises(BusError):
        seeds.persist(seed, 'Story held', bus=bus)
    assert not bus.rows


def test_composer_hold_cannot_emit_before_shared_persistence(monkeypatch):
    monkeypatch.setattr(ops_alerts, 'record_story_hold', lambda *a, **kw: pytest.fail('premature seed'))
    monkeypatch.setattr(ops_alerts, 'alert', lambda *a, **kw: None)
    result = stories._needs_media_hold(SimpleNamespace(key='chateau123_ig', platform='instagram'),
                                      DAY, _make_id('chateau123_ig', 'story', DAY), None, [], 'quality hold')
    assert result.needs_media is True
    with pytest.raises(seeds.SeedError):
        seeds.prepare_story_hold_seed(result, bus=MemoryBus())


def enable(monkeypatch):
    monkeypatch.setattr(ops_alerts.config, 'ops_fix_triage_enabled', lambda: True)
    monkeypatch.setattr(ops_alerts.config, 'ops_alerts_enabled', lambda: True)


def test_confirmed_insert_emits_after_db_and_failed_insert_emits_nothing(monkeypatch):
    enable(monkeypatch)
    calls = []
    monkeypatch.setattr(calendar, '_stage_belts', lambda key, rows: rows)
    monkeypatch.setattr(calendar, '_media_stage_belt', lambda store, key, rows: rows)
    monkeypatch.setattr(calendar, '_dedupe_slots', lambda store, key, rows: rows)
    from agent import plan_horizon
    monkeypatch.setattr(plan_horizon, 'belt_filter', lambda key, rows: (rows, 0))
    monkeypatch.setattr(calendar.config, 'caption_cooldown_enabled', lambda: False)
    monkeypatch.setattr(ops_alerts, 'record_story_hold', lambda row: calls.append(('seed', row['id'])))
    def post(*a, **kw):
        calls.append(('insert', None))
        return SimpleNamespace(status_code=201, json=lambda: [held_row()])
    http = SimpleNamespace(post=post, get=lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: []))
    store = calendar.SupabaseCalendarStore(url='https://db.test', service_key='test-key', http=http)
    assert store.insert_rows('chateau123', [held_row()]) == [held_row()]
    assert calls == [('insert', None), ('seed', ROW_ID)]
    calls.clear()
    http.post = lambda *a, **kw: SimpleNamespace(status_code=500, text='unavailable')
    with pytest.raises(calendar.PortalStoreError):
        store.insert_rows('chateau123', [held_row()])
    assert not calls


def test_durable_shared_hold_retries_after_seed_failure_but_ready_rows_do_not_emit(monkeypatch):
    enable(monkeypatch)
    bus = MemoryBus()
    bus.tokens = []
    # This is the production seam, with only its service-auth bus replaced.
    record = ops_alerts.record_story_hold
    monkeypatch.setattr(ops_alerts, 'record_story_hold', lambda row: record(row, bus=bus))
    actual = held_row()
    calendar._record_confirmed_story_holds('chateau123', [actual])
    assert not bus.rows and actual['media_not_ready_reason']
    bus.tokens = [{'gym_id': CLIENT, 'echo_account_key': 'chateau123'}]
    store = SimpleNamespace(rows_in_range=lambda *a: [actual])
    calendar._retry_story_hold_provenance(store, 'chateau123', [held_row()])
    assert len(bus.rows) == 1
    calendar._retry_story_hold_provenance(store, 'chateau123', [held_row()])
    assert len(bus.rows) == 1
    before = deepcopy(bus.rows)
    store.rows_in_range = lambda *a: [held_row(image_url='https://cdn.test/ready.png', media_not_ready_reason=None)]
    calendar._retry_story_hold_provenance(store, 'chateau123', [held_row()])
    assert bus.rows == before


def observe(bus):
    return proof.observe('story_calendar_media_ready', gym_key=CLIENT,
                         request_key='a' * 64, merged_sha='b' * 40,
                         params=PARAMS, deps={'read': bus._get},
                         now=datetime(2026, 10, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize('patch', [{}, {'image_url': ''}, {'format': 'image'},
                                  {'status': 'denied'}, {'media_not_ready_reason': 'hold'},
                                  {'account': 'facebook'}, {'post_date': '2026-10-03'},
                                  {'variant_status': 'candidate'}, {'media_not_ready_reason': ''},
                                  {'created_at': DAY + 'T13:00:00+00:00'}])
def test_only_exact_recovered_shared_story_row_proves_outcome_without_worker_db(monkeypatch, tmp_path, patch):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'absent-worker.db'))
    bus = MemoryBus()
    bus.calendar = [held_row(image_url='https://cdn.test/story.png', media_not_ready_reason=None)]
    bus.calendar[0].update(patch)
    before = deepcopy(bus.calendar)
    result = observe(bus)
    assert result['verified'] is (patch == {})
    assert (result['symptom_resolved'] is True) is (patch == {})
    assert bus.calendar == before and not (tmp_path / 'absent-worker.db').exists()


def test_replaced_or_wrong_tenant_row_cannot_prove_original_target():
    bus = MemoryBus()
    bus.calendar = [held_row(id='44444444-4444-4444-8444-444444444444',
                             image_url='https://cdn.test/story.png', media_not_ready_reason=None)]
    assert observe(bus)['verified'] is False
    bus.calendar = [held_row(gym_id='othergym', image_url='https://cdn.test/story.png', media_not_ready_reason=None)]
    assert observe(bus)['verified'] is False


def test_seed_to_authenticated_observer_binds_request_and_refuses_new_inbound():
    bus = MemoryBus()
    row = seeds.persist(seeds.prepare_story_hold_seed(held_row(), bus=bus),
                        seeds.story_hold_message(held_row()), bus=bus)['ticket']
    plan = row['verification_before']['fixer']['business_check']
    bus.calendar = [held_row(image_url='https://cdn.test/story.png', media_not_ready_reason=None)]
    request = {'schema_version': 1, 'contract_version': seeds.CONTRACT_VERSION,
               'ticket_id': row['id'], 'client_id': CLIENT,
               'request_key': plan['request_key'], 'merged_sha': 'b' * 40,
               'check_id': plan['check_id'], 'params': plan['params']}
    status, result = fixer_ops._run_business_evidence(json.dumps(request).encode(),
                           {'business_evidence': {'read': bus._get}},
                           now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert status == 200 and result['verified'] is True and result['symptom_resolved'] is True
    assert result['request_key'] == plan['request_key'] and result['params'] == PARAMS
    bus.messages.append({'id': 'new-request', 'ticket_id': row['id'], 'direction': 'inbound',
                         'author_type': 'client', 'body': 'new requirement', 'created_at': DAY})
    status, result = fixer_ops._run_business_evidence(json.dumps(request).encode(),
                           {'business_evidence': {'read': bus._get}})
    assert status == 409 and result['error'] == 'request_identity_mismatch'


def test_http_boundary_accepts_exact_shared_target_and_refuses_malformed_params():
    assert fixer_ops._business_params_valid('story_calendar_media_ready', PARAMS)
    for patch in ({'row_id': 'bad'}, {'post_date': '2026-02-31'}, {'extra': True}):
        assert not fixer_ops._business_params_valid('story_calendar_media_ready', {**PARAMS, **patch})


def test_studio_crosspost_deferral_is_scoped_and_restored_on_failure(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(ops_alerts.config, 'support_channel_id', lambda: 'C-support')
    from agent import ops_triage
    monkeypatch.setattr(ops_triage, 'classify', lambda text: ops_triage.NEEDS_TRIAGE)
    monkeypatch.setattr(ops_triage, 'is_systemic', lambda text: False)
    sent = []
    poster = SimpleNamespace(_chat_post=lambda **kw: sent.append(kw))
    with pytest.raises(RuntimeError):
        with ops_alerts.story_hold_scope(True):
            ops_alerts._maybe_cross_post_ops_fix('render failed', poster)
            assert not sent
            raise RuntimeError('failed render')
    ops_alerts._maybe_cross_post_ops_fix('feed render failed', poster)
    assert len(sent) == 1


class CalendarHTTP:
    """Model shared persistence, guarded PATCH, and ordinary month deletion."""
    def __init__(self):
        self.rows = []
        self.calls = []
        self.bus = None

    def get(self, url, params, **kw):
        if url.endswith("support_tickets"):
            return SimpleNamespace(status_code=200, json=lambda: list(self.bus.rows.values()))
        rows = [deepcopy(r) for r in self.rows
                if all(r.get(k) == v[3:] for k, v in params.items()
                       if isinstance(v, str) and v.startswith('eq.'))]
        return SimpleNamespace(status_code=200, json=lambda: rows)

    def post(self, url, json, **kw):
        self.calls.append('insert')
        rows = [held_row(**r) for r in json]
        self.rows.extend(deepcopy(rows))
        return SimpleNamespace(status_code=201, json=lambda: rows)

    def delete(self, url, params, **kw):
        self.calls.append('delete')
        assert params['and'] in {'(or(format.neq.story,format.is.null))',
                                 '(or(format.neq.story,format.is.null,media_not_ready_reason.is.null))'}
        protect_all = params['and'] == '(or(format.neq.story,format.is.null))'
        ids = params.get('id', 'not.in.()')[8:-1].split(',')
        removed = [r for r in self.rows if r['id'] not in ids and
                   not ((protect_all or r.get('media_not_ready_reason') is not None)
                        and r['format'] == 'story') and r['status'] == 'pending']
        self.rows = [r for r in self.rows if r not in removed]
        return SimpleNamespace(status_code=200, json=lambda: removed)

    def patch(self, url, params, json, **kw):
        self.calls.append('recover')
        matched = []
        for row in self.rows:
            def agrees(k, v):
                return row.get(k) is None if v == 'is.null' else row.get(k) == v[3:]
            if all(agrees(k, v) for k, v in params.items()):
                row.update(json)
                matched.append(deepcopy(row))
        return SimpleNamespace(status_code=200, json=lambda: matched)


def setup_calendar(monkeypatch, bus):
    enable(monkeypatch)
    for name in ('_stage_belts',):
        monkeypatch.setattr(calendar, name, lambda key, rows: rows)
    monkeypatch.setattr(calendar, '_media_stage_belt', lambda store, key, rows: rows)
    monkeypatch.setattr(calendar, '_dedupe_slots', lambda store, key, rows: rows)
    from agent import plan_horizon
    monkeypatch.setattr(plan_horizon, 'belt_filter', lambda key, rows: (rows, 0))
    monkeypatch.setattr(calendar.config, 'caption_cooldown_enabled', lambda: False)
    record = ops_alerts.record_story_hold
    monkeypatch.setattr(ops_alerts, 'record_story_hold', lambda row: record(row, bus=bus))
    http = CalendarHTTP()
    http.bus = bus
    store = calendar.SupabaseCalendarStore(url='https://db.test', service_key='test-key', http=http)
    return store, http


def test_failed_seed_ordinary_rebuild_retries_before_delete_and_recovers_same_generation(monkeypatch):
    bus = MemoryBus()
    bus.tokens = []
    store, http = setup_calendar(monkeypatch, bus)
    assert store.insert_rows('chateau123', [held_row()])[0]['id'] == ROW_ID
    assert not bus.rows and len(http.rows) == 1
    original_stamp = http.rows[0]['created_at']
    bus.tokens = [{'gym_id': CLIENT, 'echo_account_key': 'chateau123'}]
    # The normal mirror/month lifecycle is delete_month followed by insert_rows.
    store.delete_month('chateau123', '2026-10')
    assert len(bus.rows) == 1, 'seed retry must precede normal rebuild deletion'
    assert http.rows[0]['id'] == ROW_ID
    ready = held_row(image_url='https://cdn.test/recovered.png', media_not_ready_reason=None)
    returned = store.insert_rows('chateau123', [ready])
    assert returned[0]['id'] == ROW_ID and returned[0]['created_at'] == original_stamp
    assert len(http.rows) == 1 and http.calls.count('insert') == 1
    bus.calendar = deepcopy(http.rows)
    assert observe(bus)['verified'] is True
    # A subsequent ordinary rebuild must also retain the now READY target.
    store.delete_month('chateau123', '2026-10')
    store.insert_rows('chateau123', [ready])
    assert len(http.rows) == 1 and http.rows[0]['id'] == ROW_ID
    assert http.rows[0]['created_at'] == original_stamp


def test_failed_new_render_never_downgrades_existing_ready_story(monkeypatch):
    bus = MemoryBus()
    store, http = setup_calendar(monkeypatch, bus)
    http.rows = [held_row(image_url='https://cdn.test/ready.png', media_not_ready_reason=None)]
    result = store.insert_rows('chateau123', [held_row()])
    assert result[0]['image_url'] == 'https://cdn.test/ready.png'
    assert http.calls == [] and not bus.rows


def test_same_request_cannot_substitute_ready_sibling_or_forged_source_pointer():
    bus = MemoryBus()
    row = seeds.persist(seeds.prepare_story_hold_seed(held_row(), bus=bus),
                        seeds.story_hold_message(held_row()), bus=bus)['ticket']
    plan = row['verification_before']['fixer']['business_check']
    sibling = '44444444-4444-4444-8444-444444444444'
    bus.calendar.append(held_row(id=sibling, image_url='https://cdn.test/ready.png', media_not_ready_reason=None))
    request = {'schema_version': 1, 'contract_version': seeds.CONTRACT_VERSION,
               'ticket_id': row['id'], 'client_id': CLIENT,
               'request_key': plan['request_key'], 'merged_sha': 'b' * 40,
               'check_id': plan['check_id'], 'params': {**plan['params'], 'row_id': sibling}}
    status, result = fixer_ops._run_business_evidence(json.dumps(request).encode(),
                                                    {'business_evidence': {'read': bus._get}})
    assert status == 409 and result['error'] == 'business_pointer_mismatch'
    request['params'] = plan['params']
    bus.rows[row['id']]['verification_before']['fixer']['source_event']['row_id'] = sibling
    status, result = fixer_ops._run_business_evidence(json.dumps(request).encode(),
                                                    {'business_evidence': {'read': bus._get}})
    assert status == 409 and result['error'] == 'business_pointer_mismatch'


@pytest.mark.parametrize("proposals", [[], [held_row(image_url='https://cdn.test/new.png', media_not_ready_reason=None)]])
def test_unrelated_ready_story_keeps_ordinary_rebuild_and_omission_behavior(monkeypatch, proposals):
    bus = MemoryBus()
    store, http = setup_calendar(monkeypatch, bus)
    http.rows = [held_row(image_url='https://cdn.test/old.png', media_not_ready_reason=None)]
    assert store.delete_month('chateau123', '2026-10') == 1
    store.insert_rows('chateau123', proposals)
    assert len(http.rows) == len(proposals)
    if proposals:
        assert http.rows[0]['image_url'] == 'https://cdn.test/new.png'


@pytest.mark.parametrize("duplicate_existing", [False, True])
def test_ambiguous_story_slot_does_not_patch_or_insert_sibling(monkeypatch, duplicate_existing):
    bus = MemoryBus()
    store, http = setup_calendar(monkeypatch, bus)
    http.rows = [held_row()]
    proposed = [held_row(image_url='https://cdn.test/new.png', media_not_ready_reason=None)]
    if duplicate_existing:
        http.rows.append(held_row(id='44444444-4444-4444-8444-444444444444'))
    else:
        proposed *= 2
    assert store.insert_rows('chateau123', proposed) == []
    assert not http.calls
    assert all(r['image_url'] == '' for r in http.rows)


@pytest.mark.parametrize("partial", [False, True])
def test_story_read_failure_protects_delete_and_refuses_new_story(monkeypatch, partial):
    bus = MemoryBus()
    store, http = setup_calendar(monkeypatch, bus)
    http.rows = [held_row()]
    def bad_read(*a):
        if partial:
            return [{}] * 1000
        raise RuntimeError('shared read unavailable')
    monkeypatch.setattr(store, 'rows_in_range', bad_read)
    assert store.delete_month('chateau123', '2026-10') == 0
    assert store.insert_rows('chateau123', [held_row(image_url='https://cdn.test/new.png', media_not_ready_reason=None)]) == []
    assert http.rows[0]['image_url'] == '' and 'recover' not in http.calls


def test_human_approval_race_refuses_pending_story_patch(monkeypatch):
    bus = MemoryBus()
    store, http = setup_calendar(monkeypatch, bus)
    http.rows = [held_row()]
    patch = http.patch
    def human_first(*a, **kw):
        http.rows[0].update(status='approved', caption='Human edit')
        return patch(*a, **kw)
    monkeypatch.setattr(http, 'patch', human_first)
    assert store.insert_rows('chateau123', [held_row(image_url='https://cdn.test/new.png', media_not_ready_reason=None)]) == []
    assert http.rows[0]['status'] == 'approved' and http.rows[0]['caption'] == 'Human edit'
    assert http.rows[0]['image_url'] == '' and 'insert' not in http.calls


def test_preexisting_human_owned_story_keeps_edit_and_slot(monkeypatch):
    bus = MemoryBus()
    dedupe = calendar._dedupe_slots
    store, http = setup_calendar(monkeypatch, bus)
    monkeypatch.setattr(calendar, '_dedupe_slots', dedupe)
    http.rows = [held_row(status='approved', caption='Human edit', image_url='https://cdn.test/human.png', media_not_ready_reason=None)]
    assert store.delete_month('chateau123', '2026-10') == 0
    assert store.insert_rows('chateau123', [held_row(image_url='https://cdn.test/new.png', media_not_ready_reason=None)]) == []
    assert http.rows[0]['caption'] == 'Human edit' and http.rows[0]['image_url'] == 'https://cdn.test/human.png'


def test_unavailable_provenance_read_keeps_ready_story_target(monkeypatch):
    bus = MemoryBus()
    store, http = setup_calendar(monkeypatch, bus)
    http.rows = [held_row(image_url='https://cdn.test/old.png', media_not_ready_reason=None)]
    get = http.get
    def failed_source(url, **kw):
        if url.endswith('support_tickets'):
            return SimpleNamespace(status_code=503, json=lambda: None)
        return get(url, **kw)
    monkeypatch.setattr(http, 'get', failed_source)
    assert store.delete_month('chateau123', '2026-10') == 0
    assert http.rows[0]['id'] == ROW_ID


def test_hold_inserted_between_month_read_and_delete_is_protected(monkeypatch):
    bus = MemoryBus()
    store, http = setup_calendar(monkeypatch, bus)
    delete = http.delete
    def new_hold_first(*a, **kw):
        http.rows.append(held_row())
        return delete(*a, **kw)
    monkeypatch.setattr(http, 'delete', new_hold_first)
    assert store.delete_month('chateau123', '2026-10') == 0
    assert http.rows[0]['id'] == ROW_ID
