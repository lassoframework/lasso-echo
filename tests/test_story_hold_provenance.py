"""Offline Story hold provenance, idempotency and real-store recovery checks."""
import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent import fixer_business_seed as seeds, fixer_business_evidence as proof
from agent import fixer_ops, ops_alerts, stories
from agent.drafter import Draft, DraftStatus, _make_id
from agent.slack_convo.bus import Bus, BusError

CLIENT = '22222222-2222-4222-8222-222222222222'
ACCOUNT = 'chateau123_ig'
DAY = '2026-10-02'
DRAFT = _make_id(ACCOUNT, 'story', DAY)
PARAMS = {'account_key': ACCOUNT, 'draft_id': DRAFT, 'day_key': DAY}


class MemoryBus(Bus):
    def __init__(self):
        self.rows = {}
        self.tokens = [{'gym_id': CLIENT, 'echo_account_key': 'chateau123'}]

    def _get(self, table, params):
        if table == 'echo_intake_tokens':
            return [deepcopy(row) for row in self.tokens
                    if all(row.get(k) == v[3:] for k, v in params.items()
                           if k not in {'select', 'limit'})]
        if table == 'support_tickets':
            return [deepcopy(row) for row in self.rows.values()
                    if row['id'] == params['id'][3:]]
        raise AssertionError(table)

    def _insert(self, table, row):
        assert table == 'support_tickets'
        if row['id'] in self.rows:
            return None, True
        self.rows[row['id']] = deepcopy(row)
        return deepcopy(row), False


def held():
    return Draft(draft_id=DRAFT, account_key=ACCOUNT, platform='instagram',
                 caption='', hashtags=[], creative_path='', creative_public_url='',
                 scheduled_for=DAY + 'T12:00:00+00:00', status=DraftStatus.BLOCKED,
                 blocked_reason='Story media not ready: render unavailable',
                 is_story=True, day_key=DAY, draft_type='story', needs_media=True,
                 force_approval=True)


def test_story_source_has_stable_tenant_submission_and_scout_request_key():
    bus = MemoryBus()
    seed = seeds.prepare_story_hold_seed(held(), bus=bus)
    a = seeds.persist(seed, 'Story held for review', bus=bus)
    b = seeds.persist(seed, 'Story held for review', bus=bus)
    row = a['ticket']
    assert a['duplicate'] is False and b['duplicate'] is True and len(bus.rows) == 1
    assert row['client_id'] == CLIENT and row['submission_key'] == row['id']
    assert row['reporter'] == 'echo_story_hold'
    plan = row['verification_before']['fixer']['business_check']
    assert plan['params'] == PARAMS and plan['check_id'] == 'story_draft_media_ready'
    identity = [row['id'], row['created_at'], row['raw_text']]
    assert plan['request_key'] == hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    assert seed == seeds.prepare_story_hold_seed(held(), bus=bus)


@pytest.mark.parametrize('field,value', [('draft_id', 'aaaaaaaaaa'), ('client_id', 'bad'),
                                       ('source_event_id', 'a' * 64), ('params', {'day_key': DAY})])
def test_forged_or_incomplete_story_seed_cannot_persist(field, value):
    bus = MemoryBus()
    seed = seeds.prepare_story_hold_seed(held(), bus=bus)
    seed[field] = value
    with pytest.raises(seeds.SeedError):
        seeds.persist(seed, 'Story held', bus=bus)
    assert not bus.rows


def test_remapped_or_ambiguous_tenant_refuses_story_ticket():
    bus = MemoryBus()
    seed = seeds.prepare_story_hold_seed(held(), bus=bus)
    bus.tokens.append({'gym_id': CLIENT, 'echo_account_key': 'othergym'})
    with pytest.raises(BusError):
        seeds.persist(seed, 'Story held', bus=bus)
    assert not bus.rows


def test_text_alert_survives_seed_failure_without_creating_unbound_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'audit.db'))
    monkeypatch.setattr(ops_alerts.config, 'ops_fix_triage_enabled', lambda: True)
    monkeypatch.setattr(ops_alerts.config, 'ops_alerts_enabled', lambda: True)
    monkeypatch.setattr(ops_alerts.config, 'ops_alerts_noise_filter_enabled', lambda: False)
    poster = SimpleNamespace(post_notice=lambda text: {'ok': True},
                             _chat_post=lambda **kw: pytest.fail('unbound cross-post'))
    bus = MemoryBus()
    bus.tokens = []
    assert ops_alerts.alert('Story render failed', force=True, poster=poster,
                            story_hold=held(), seed_bus=bus) == {'ok': True}
    assert not bus.rows


def test_reported_failure_hold_records_provenance_without_a_second_alert(monkeypatch):
    records = []
    monkeypatch.setattr(ops_alerts, 'record_story_hold', lambda draft, text: records.append(draft))
    monkeypatch.setattr(ops_alerts, 'alert', lambda *a, **kw: pytest.fail('duplicate alert'))
    result = stories._needs_media_hold(SimpleNamespace(key=ACCOUNT, platform='instagram'),
                                      DAY, DRAFT, None, [], 'quality hold')
    assert records == [result] and result.needs_media is True


def write_draft(path, patch=None):
    data = {'draft_id': DRAFT, 'account_key': ACCOUNT, 'day_key': DAY,
            'draft_type': 'story', 'status': 'pending', 'is_story': True,
            'needs_media': False, 'blocked_reason': '',
            'creative_public_url': 'https://cdn.test/story.png'}
    data.update(patch or {})
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE drafts(draft_id TEXT,account_key TEXT,day_key TEXT,draft_type TEXT,status TEXT,data TEXT)')
        c.execute('INSERT INTO drafts VALUES(?,?,?,?,?,?)', (DRAFT, ACCOUNT, DAY, 'story',
                                                            data['status'], json.dumps(data)))


def observe(path, monkeypatch, params=None, client=CLIENT):
    monkeypatch.setenv('AGENT_DB_PATH', str(path))
    token = {'gym_id': CLIENT, 'echo_account_key': 'chateau123'}
    return proof.observe('story_draft_media_ready', gym_key=client,
                         request_key='a' * 64, merged_sha='b' * 40,
                         params=params or PARAMS, deps={'read': lambda *a: [token]},
                         now=datetime(2026, 10, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize('patch', [{}, {'needs_media': True}, {'creative_public_url': ''},
                                  {'is_story': False}, {'status': 'blocked'},
                                  {'blocked_reason': 'missing media'}, {'account_key': 'other_ig'}])
def test_only_real_recovered_story_proves_outcome_read_only(tmp_path, monkeypatch, patch):
    path = tmp_path / 'echo.db'
    write_draft(path, patch)
    before = path.read_bytes()
    result = observe(path, monkeypatch)
    assert result['verified'] is (patch == {})
    assert (result['symptom_resolved'] is True) is (patch == {})
    assert path.read_bytes() == before


def test_absent_store_cannot_create_database_or_fake_recovery(tmp_path, monkeypatch):
    path = tmp_path / 'missing.db'
    assert observe(path, monkeypatch)['verified'] is False
    assert not path.exists()


def test_http_boundary_accepts_exact_story_target_and_refuses_wrong_slot():
    assert fixer_ops._business_params_valid('story_draft_media_ready', PARAMS)
    for patch in ({'draft_id': 'a' * 10}, {'day_key': '2026-02-31'}, {'extra': True}):
        assert not fixer_ops._business_params_valid('story_draft_media_ready', {**PARAMS, **patch})


def test_seed_to_authenticated_observer_binds_request_and_refuses_new_inbound(tmp_path, monkeypatch):
    bus = MemoryBus()
    row = seeds.persist(seeds.prepare_story_hold_seed(held(), bus=bus), 'Story held', bus=bus)['ticket']
    plan = row['verification_before']['fixer']['business_check']
    path = tmp_path / 'echo.db'
    write_draft(path)
    monkeypatch.setenv('AGENT_DB_PATH', str(path))
    messages = []
    def read(table, params):
        if table == 'support_messages':
            return messages
        return bus._get(table, params)
    request = {'schema_version': 1, 'contract_version': seeds.CONTRACT_VERSION,
               'ticket_id': row['id'], 'client_id': CLIENT,
               'request_key': plan['request_key'], 'merged_sha': 'b' * 40,
               'check_id': plan['check_id'], 'params': plan['params']}
    status, result = fixer_ops._run_business_evidence(json.dumps(request).encode(),
                           {'business_evidence': {'read': read}},
                           now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert status == 200 and result['verified'] is True and result['symptom_resolved'] is True
    assert result['request_key'] == plan['request_key'] and result['params'] == PARAMS
    messages.append({'id': 'new-request', 'ticket_id': row['id'], 'direction': 'inbound',
                     'author_type': 'client', 'body': 'new requirement', 'created_at': DAY})
    status, result = fixer_ops._run_business_evidence(json.dumps(request).encode(),
                           {'business_evidence': {'read': read}})
    assert status == 409 and result['error'] == 'request_identity_mismatch'


def test_studio_crosspost_deferral_is_scoped_and_restored_on_failure(monkeypatch):
    monkeypatch.setattr(ops_alerts.config, 'ops_fix_triage_enabled', lambda: True)
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
