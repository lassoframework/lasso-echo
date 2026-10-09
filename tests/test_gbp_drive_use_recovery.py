"""Original listener journal + real binding adapter, synthetic PG RPC transport."""
from copy import deepcopy
from pathlib import Path
import importlib
import sqlite3
import uuid

import pytest

from agent import db, gbp_drive_use_journal as journal, gbp_planner, gym_media_selector
from agent.jobs import gbp_drive_use_recovery as recovery
from tests.test_gbp_drive_use_journal import make_request, _receipt
from tests.test_gbp_staged_journal_binding import (
    FLAG, _FakeHttp, _store, _make_stage_entry, _status_payload, _terminal_receipt)


@pytest.fixture
def case(tmp_path, monkeypatch):
    path = tmp_path / 'listener.db'
    monkeypatch.setenv('AGENT_DB_PATH', str(path))
    monkeypatch.setenv(recovery.FLAG, 'true')
    monkeypatch.setenv(FLAG, 'true')
    monkeypatch.setenv('AGENT_REMOTE_DRIVE_USE_CAS_ENABLED', 'true')
    # Simulate an original mounted durable volume, never weaken production checks.
    monkeypatch.setattr(recovery, 'EPHEMERAL_ROOTS', ())
    request = make_request(gym='gym1')
    entry = journal.record_write_intent(journal.prepare(request)['use_id'])
    with db.connect() as conn:
        conn.execute("INSERT INTO socialapi_claims(draft_id,account_key,status) VALUES(?,?,'in_flight')",
                     (entry['claim_id'], 'gym1_gbp'))
        conn.commit()
    member = str(uuid.uuid4())
    stage = _make_stage_entry(member, row=entry['calendar_row'])
    journal.record_forward_stage_intent(stage)
    identity = str(uuid.uuid4())
    recovery.pin_original_journal(identity)
    monkeypatch.setenv(recovery.JOURNAL_ID_ENV, identity)
    bound = journal.get_forward_stage(stage['batch_id'])
    active = dict(entry['calendar_row'], id=member, variant_status='active',
                  media_not_ready_reason=None)
    http = _FakeHttp()
    http.status_payload = _status_payload(bound, 'finalized', _terminal_receipt(bound))
    store = _store(http)
    reads = []
    def read(gym, proposed):
        reads.append((gym, deepcopy(proposed)))
        return [deepcopy(active)]
    monkeypatch.setattr(store, 'authoritative_rows_for_keys', read, raising=False)
    calls = []
    monkeypatch.setattr(gym_media_selector, 'stamp_use',
                        lambda *a, **kw: calls.append(kw['use_id']))
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt', lambda uid: None)
    return dict(path=path, entry=entry, bound=bound, active=active,
                http=http, store=store, calls=calls, reads=reads)


def run(case, **kw):
    return recovery.run(store=case['store'], media_store=object(), logger=lambda m: None, **kw)


def test_skipped_existing_does_not_block_lost_ack_recovery(case, monkeypatch):
    from agent.gbp_dogfood import plan_gbp_dogfood
    monkeypatch.setattr(case['store'], 'future_gbp_rows', lambda *a: [case['active']], raising=False)
    result = plan_gbp_dogfood('gym1', 'gym1', voice=object(), library_path='',
                             city='City', store=case['store'])
    assert result['skipped_existing']
    assert run(case)['held'] == 1  # only downstream remote receipt is absent
    assert journal.get_forward_stage(case['bound']['batch_id'])['state'] == 'finalized'
    assert case['calls'] == [case['entry']['use_id']]
    assert len(case['reads']) == 2
    assert journal.get(case['entry']['use_id'])['state'] == 'consumption_pending'
    assert run(case)['held'] == 1
    assert case['calls'] == [case['entry']['use_id']] * 2
    assert len(case['http'].status_calls) == 2  # each restart freshly reads PG


@pytest.mark.parametrize('state', ['missing', 'unknown', 'staged', 'expired'])
def test_nonterminal_receipts_never_consume(case, state):
    if state == 'missing':
        case['http'].status_payload = None
    else:
        case['http'].status_payload = _status_payload(case['bound'], state)
    assert run(case)['held'] == 1
    assert case['calls'] == []
    assert journal.get(case['entry']['use_id'])['state'] == 'write_intent'


@pytest.mark.parametrize('field', ['tenant_id', 'request_digest', 'member_row_ids'])
def test_wrong_pg_binding_never_consumes(case, field):
    payload = case['http'].status_payload
    payload[field] = [str(uuid.uuid4())] if field == 'member_row_ids' else 'wrong'
    assert run(case)['held'] == 1
    assert case['calls'] == []


def test_frozen_cross_tenant_binding_never_queries_pg(case):
    with __import__('sqlite3').connect(case['path']) as conn:
        conn.execute("UPDATE gbp_forward_stage_journal SET tenant_id='other'")
    assert run(case)['held'] == 1
    assert case['http'].status_calls == []
    assert case['calls'] == []


@pytest.mark.parametrize('change', ['candidate', 'wrong_bytes', 'missing'])
def test_active_readback_required_after_terminal_bind(case, monkeypatch, change):
    def read(gym, rows):
        case['reads'].append(1)
        active = deepcopy(case['active'])
        if len(case['reads']) == 2:
            if change == 'missing':
                return []
            if change == 'candidate':
                active['variant_status'] = 'candidate'
            else:
                active['image_url'] = 'https://wrong'
        return [active]
    monkeypatch.setattr(case['store'], 'authoritative_rows_for_keys', read)
    assert run(case)['held'] == 1
    assert case['calls'] == []


def test_receipt_ack_then_claim_ack_restart_is_terminal_once(case, monkeypatch):
    entry = case['entry']
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt', lambda uid: _receipt(entry))
    # Lost claim completion acknowledgment: receipt stays durable, restart completes
    # the original claim without issuing another remote use.
    complete = gbp_planner._complete_drive_claim
    monkeypatch.setattr(gbp_planner, '_complete_drive_claim', lambda pick: None)
    assert run(case)['held'] == 1
    assert journal.get(entry['use_id'])['state'] == 'receipt_confirmed'
    monkeypatch.setattr(gbp_planner, '_complete_drive_claim', complete)
    assert run(case)['recovered'] == 1
    assert journal.get(entry['use_id'])['state'] == 'claim_done'
    assert case['calls'] == [entry['use_id']]
    assert run(case)['examined'] == 0


@pytest.mark.parametrize('flag', ['false', 'perhaps'])
def test_default_off_and_ambiguous_flag_no_reads(case, monkeypatch, flag):
    monkeypatch.setenv(recovery.FLAG, flag)
    summary = run(case)
    assert summary['examined'] == 0
    assert case['reads'] == [] and case['calls'] == []
    assert summary['ok'] is (flag == 'false')


def test_missing_or_ephemeral_original_journal_no_pg(case, monkeypatch):
    monkeypatch.setenv('AGENT_DB_PATH', str(case['path'].parent / 'missing.db'))
    assert not run(case)['ok']
    assert not (case['path'].parent / 'missing.db').exists()
    monkeypatch.setenv('AGENT_DB_PATH', str(case['path']))
    monkeypatch.setattr(recovery, 'EPHEMERAL_ROOTS', (str(case['path'].parent),))
    assert not run(case)['ok']
    assert case['http'].status_calls == []


def test_row_bound_and_no_planning_dependency(case, monkeypatch):
    for i in range(3):
        journal.record_write_intent(journal.prepare(make_request(gym='gym1', logical=f'lp-{i+2}'))['use_id'])
    monkeypatch.setattr(gbp_planner, 'plan_gbp_month', lambda *a, **kw: pytest.fail('new content'))
    assert run(case, row_limit=2)['examined'] == 2
    assert run(case, row_limit=33)['examined'] == 0


def test_daily_recovery_precedes_month_sweep_even_when_sweep_off():
    source = Path(gbp_planner.__file__).with_name('runner.py').read_text()
    assert source.index('recovery = _gbp_use_recovery_run()') < source.index('if config.gbp_month_sweep_enabled():')


def test_daily_no_voice_recovers_existing_use_before_voice_load(case, monkeypatch):
    from agent import runner
    calls = []
    original_run = recovery.run
    def recover():
        calls.append('recover')
        return original_run(store=case['store'], media_store=object(), logger=lambda m: None)
    def voice(*a):
        calls.append('voice')
        return None
    class Poster:
        def post_notice(self, message):
            calls.append('notice')
    monkeypatch.setattr(runner, '_trust_startup_warning', lambda: None)
    monkeypatch.setattr(runner.config, 'master_enabled', lambda: True)
    monkeypatch.setattr(runner.config, 'lasso_three_feed_enabled', lambda: False)
    monkeypatch.setattr(runner, 'load_voice', voice)
    monkeypatch.setattr(recovery, 'run', recover)
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt',
                        lambda uid: _receipt(case['entry']))
    assert runner.run_daily(poster=Poster())['status'] == 'no_voice'
    assert calls == ['recover', 'voice', 'notice']
    assert journal.get(case['entry']['use_id'])['state'] == 'claim_done'


def test_daily_master_off_never_runs_recovery(case, monkeypatch):
    from agent import runner
    monkeypatch.setattr(runner, '_trust_startup_warning', lambda: None)
    monkeypatch.setattr(runner.config, 'master_enabled', lambda: False)
    monkeypatch.setattr(recovery, 'run', lambda: pytest.fail('disarmed recovery'))
    assert runner.run_daily()['status'] == 'disabled'


@pytest.mark.parametrize('pin', ['missing', 'mismatch'])
def test_independent_identity_pin_required(case, monkeypatch, pin):
    if pin == 'missing':
        monkeypatch.delenv(recovery.JOURNAL_ID_ENV)
    else:
        monkeypatch.setenv(recovery.JOURNAL_ID_ENV, str(uuid.uuid4()))
    assert run(case)['ok'] is False
    assert case['http'].status_calls == [] and case['calls'] == []


def test_fresh_populated_absolute_database_never_auto_enrolls(case, monkeypatch):
    path = case['path'].with_name('replacement.db')
    monkeypatch.setenv('AGENT_DB_PATH', str(path))
    journal.record_write_intent(journal.prepare(make_request(gym='gym1'))['use_id'])
    journal.get_forward_stage(str(uuid.uuid4()))  # replacement has journal schemas
    result = run(case)
    assert not result['ok'] and result['examined'] == 0
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='gbp_drive_use_recovery_owner'").fetchall()
    assert case['http'].status_calls == [] and case['calls'] == []


def test_pin_step_cannot_silently_replace_an_existing_identity(case):
    with pytest.raises(ValueError, match='already pinned'):
        recovery.pin_original_journal(str(uuid.uuid4()))
    assert recovery._durable_path() == case['path']


def test_33rd_valid_use_recovers_across_restart_despite_32_held(case, monkeypatch):
    held_ids = []
    for i in range(32):
        held_ids.append(journal.record_write_intent(journal.prepare(
            make_request(gym='gym1', logical=f'held-{i}'))['use_id'])['use_id'])
    with sqlite3.connect(case['path']) as conn:
        conn.execute("UPDATE gbp_drive_use_journal SET created_at='2000-01-01' WHERE use_id<>?",
                     (case['entry']['use_id'],))
        conn.execute("UPDATE gbp_drive_use_journal SET created_at='2030-01-01' WHERE use_id=?",
                     (case['entry']['use_id'],))
    first = run(case)
    assert first['examined'] == first['held'] == 32
    assert case['calls'] == [] and case['http'].status_calls == []
    with sqlite3.connect(case['path']) as conn:
        cursor = conn.execute('SELECT cursor_created_at,cursor_use_id FROM gbp_drive_use_recovery_owner').fetchone()
    assert cursor[0] == '2000-01-01'
    importlib.reload(recovery)  # no in-memory cursor survives
    monkeypatch.setattr(recovery, 'EPHEMERAL_ROOTS', ())
    # Restarted store has no prior attempt state; only SQLite and PG receipts survive.
    case['store'] = _store(case['http'])
    monkeypatch.setattr(case['store'], 'authoritative_rows_for_keys',
                        lambda *a: [deepcopy(case['active'])], raising=False)
    monkeypatch.setattr(gbp_planner, '_recorded_remote_receipt',
                        lambda uid: _receipt(case['entry']))
    second = run(case)
    assert second['examined'] == 32 and second['recovered'] == 1
    assert second['held'] == 31
    assert case['calls'] == [case['entry']['use_id']]
    assert journal.get(case['entry']['use_id'])['state'] == 'claim_done'
    assert all(journal.get(uid)['state'] == 'write_intent' for uid in held_ids)


@pytest.mark.parametrize('bound', ['MAX_TENANTS', 'MAX_BATCHES'])
def test_tenant_and_batch_work_bounds(case, monkeypatch, bound):
    monkeypatch.setattr(recovery, bound, 0)
    summary = run(case)
    assert summary['examined'] == summary['held'] == 0
    assert summary['reason'] in ('tenant work bound', 'batch work bound')
    assert case['http'].status_calls == [] and case['calls'] == []


@pytest.mark.parametrize('kind,total,cap', [('tenants', 9, 8), ('batches', 17, 16)])
def test_budget_deferred_use_is_first_after_restart(case, monkeypatch, kind, total, cap):
    entries = [case['entry']]
    active = {('gym1', case['entry']['logical_post_id']): case['active']}
    payloads = {case['bound']['batch_id']: case['http'].status_payload}
    for i in range(1, total):
        tenant = f'gym{i+1}' if kind == 'tenants' else 'gym1'
        logical = f'lp-{i+1}'
        entry = journal.record_write_intent(journal.prepare(
            make_request(gym=tenant, logical=logical))['use_id'])
        entries.append(entry)
        member_id = str(uuid.uuid4())
        stage = _make_stage_entry(member_id, tenant=tenant, logical=logical,
                                  row=entry['calendar_row'])
        journal.record_forward_stage_intent(stage)
        bound = journal.get_forward_stage(stage['batch_id'])
        payloads[bound['batch_id']] = _status_payload(bound, 'finalized', _terminal_receipt(bound))
        active[(tenant, logical)] = dict(entry['calendar_row'], id=member_id,
                                         variant_status='active', media_not_ready_reason=None)
        if kind == 'tenants':
            with db.connect() as conn:
                conn.execute("INSERT INTO socialapi_claims(draft_id,account_key,status) VALUES(?,?,'in_flight')",
                             (entry['claim_id'], tenant + '_gbp'))
                conn.commit()
    with sqlite3.connect(case['path']) as conn:
        for i, entry in enumerate(entries):
            conn.execute('UPDATE gbp_drive_use_journal SET created_at=? WHERE use_id=?',
                         (f'2030-01-01T00:00:{i:02d}', entry['use_id']))
    http = case['http']
    original_post = http.post
    def post(url, json=None, **kw):
        http.status_payload = payloads[json['p_batch_id']]
        return original_post(url, json=json, **kw)
    monkeypatch.setattr(http, 'post', post)
    def reader(tenant, proposed):
        return [deepcopy(active[(tenant, proposed[0]['logical_post_id'])])]
    monkeypatch.setattr(case['store'], 'authoritative_rows_for_keys', reader)
    first = run(case)
    assert first['examined'] == first['held'] == cap
    assert first['reason'] == ('tenant work bound' if kind == 'tenants' else 'batch work bound')
    assert first[kind] == cap
    assert case['calls'] == [entry['use_id'] for entry in entries[:cap]]
    with sqlite3.connect(case['path']) as conn:
        cursor_id = conn.execute('SELECT cursor_use_id FROM gbp_drive_use_recovery_owner').fetchone()[0]
    assert cursor_id == entries[cap-1]['use_id']  # never pass the deferred row
    importlib.reload(recovery)
    monkeypatch.setattr(recovery, 'EPHEMERAL_ROOTS', ())
    case['store'] = _store(http)
    monkeypatch.setattr(case['store'], 'authoritative_rows_for_keys', reader, raising=False)
    second = run(case)
    assert second['examined'] == second['held'] == cap
    assert second[kind] == cap
    assert case['calls'][cap] == entries[-1]['use_id']  # first attempt after restart
    assert all(journal.get(entry['use_id'])['state'] == 'consumption_pending' for entry in entries)


@pytest.mark.parametrize('kind', ['missing', 'wrong_asset', 'wrong_account'])
def test_original_claim_safety_before_any_pg_or_remote_call(case, kind):
    with db.connect() as conn:
        if kind == 'missing':
            conn.execute('DELETE FROM socialapi_claims')
        elif kind == 'wrong_asset':
            conn.execute("UPDATE socialapi_claims SET post_id='foreign'")
        else:
            conn.execute("UPDATE socialapi_claims SET account_key='other_gbp'")
        conn.commit()
    assert run(case)['held'] == 1
    assert case['http'].status_calls == [] and case['calls'] == []


def test_unavailable_pg_holds_even_if_old_local_proof_exists(case, monkeypatch):
    journal.record_forward_finalized(case['bound']['batch_id'], case['http'].status_payload)
    def unavailable(*a):
        raise ConnectionError('PG unavailable')
    monkeypatch.setattr(case['store'], 'bind_forward_finalization', unavailable)
    assert run(case)['held'] == 1
    assert case['calls'] == []
