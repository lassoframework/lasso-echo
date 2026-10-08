"""Finite cron job lane: default OFF, allowlist-only, deterministic request IDs.

Offline synthetic authorities/transports only; no production plane is reachable.
"""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent.source_brand_ingest import CaptureIngestError
from agent.source_brand_capture_job import (durable_request_id, main,
    parse_allowlist, parse_max_gyms, run_job)

import re as _re  # noqa: E402  (used by test_cli_exit_codes default arg)
from test_source_brand_capture_runner import GYM, environment, setup
from test_source_brand_startup import authority

OTHER_GYM = 'a1b2c3d4-1111-2222-3333-444455556666'


def job_env(tmp_path, monkeypatch, overrides=None):
    original, storage, zernio, apify, _ = setup(tmp_path)
    entries = [asdict(x) for x in original.collector._resolve._approved.values()]
    path = authority(tmp_path, entries)
    env = dict(environment(),
        ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
        ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=GYM,
        ECHO_SOURCE_CAPTURE_JOB_DAY='2026-10-08',
        ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path),
        ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE=str(path))
    env.update(overrides or {})
    # Deterministic offline capture: the runner composed here by the trusted
    # startup path must not depend on real DNS/network for the allowlisted
    # gym's website (CI runs network-disabled; DNS for the mapped host is not
    # a test input). Swap in the same synthetic website transport the runner
    # tests use — pinned-IP assert, no socket I/O. Production behavior and the
    # fail-closed CaptureError path are unchanged.
    real_initialize = run_job.__globals__['initialize_source_capture']
    def wrapped(**kw):
        runner = real_initialize(**kw)
        if runner is not None:
            runner.collector._website_factory = original.collector._website_factory
            runner.collector._min_host_delay = 0
        return runner
    monkeypatch.setattr('agent.source_brand_capture_job.initialize_source_capture',
                        wrapped)
    return env, original, storage, zernio, apify


class FakeRunner:
    """Approved-gym universe + per-gym scripted outcome; records capture calls."""
    def __init__(self, approved, fail=()):
        self.collector = SimpleNamespace(_resolve=SimpleNamespace(
            _approved={g: object() for g in approved}))
        self.fail = set(fail)
        self.calls = []

    def capture(self, gym_id, request_id):
        self.calls.append((gym_id, request_id))
        if gym_id in self.fail:
            raise CaptureIngestError('synthetic_hold')
        return {'captures': [{'id': 'c1'}]}


def test_off_no_io_and_exit_clean(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('composition must not run while OFF')
    monkeypatch.setattr('agent.source_brand_capture_job.initialize_source_capture', _boom)
    assert run_job(environ={}) == {'state': 'off'}
    assert not list(tmp_path.iterdir())
    assert main(['--day', '2026-10-08']) == 0


def test_allowlist_missing_or_invalid_holds_without_io(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('no fallback composition on invalid allowlist')
    monkeypatch.setattr('agent.source_brand_capture_job.initialize_source_capture', _boom)
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path))
    for raw in (None, '', '  ', 'not-a-uuid', f'{GYM},bogus', f'{GYM},{GYM}'):
        overrides = {} if raw is None else {'ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST': raw}
        receipt = run_job(environ=dict(env, **overrides))
        assert receipt['state'] == 'held'
        assert receipt['hold'] == 'tenant_allowlist_required'
    assert parse_allowlist(GYM) == (GYM,)
    assert parse_allowlist(f' {GYM} , {OTHER_GYM} ') == (GYM, OTHER_GYM)
    assert not list(tmp_path.iterdir())


def test_max_gyms_defaults_only_when_unset_and_holds_malformed_before_io(
        tmp_path, monkeypatch):
    assert parse_max_gyms(None) == 50
    assert parse_max_gyms('14') == 14
    for raw in ('', '  ', 'many', '1.5'):
        assert parse_max_gyms(raw) is None

    def _boom(*a, **k):
        raise AssertionError('malformed max gyms must hold before composition')
    monkeypatch.setattr('agent.source_brand_capture_job.initialize_source_capture', _boom)
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=GYM,
               ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path))
    for raw in ('', '  ', 'many', '1.5'):
        receipt = run_job(environ=dict(env,
            ECHO_SOURCE_CAPTURE_JOB_MAX_GYMS=raw))
        assert receipt['state'] == 'held'
        assert receipt['hold'] == 'max_gyms_invalid'
    assert not list(tmp_path.iterdir())


def test_enabled_missing_authority_holds_without_fallback(tmp_path):
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=GYM,
               ECHO_SOURCE_CAPTURE_JOURNAL_DIR=str(tmp_path))
    receipt = run_job(environ=env)
    assert receipt['state'] == 'held'
    assert receipt['hold'] == 'private_mapping_authority_required'
    assert not list(tmp_path.iterdir())  # no journals, no untrusted fallback


def test_one_allowlisted_gym_captured(tmp_path, monkeypatch):
    env, original, storage, zernio, apify = job_env(tmp_path, monkeypatch)
    receipt = run_job(environ=env, http=storage, identity_http=zernio,
                      apify_client=apify)
    assert receipt['state'] == 'complete'
    assert receipt['captured'] == 1 and receipt['held'] == 0
    entry = receipt['gyms'][0]
    assert entry['gym_id'] == GYM and entry['status'] == 'captured'
    assert entry['request_id'] == durable_request_id(GYM, '2026-10-08')
    assert len(storage.rows) == 2  # one website + one social capture, stored


def test_retry_same_scheduled_day_reuses_durable_request(tmp_path, monkeypatch):
    # job_env already pins the synthetic offline website transport.
    env, original, storage, zernio, apify = job_env(tmp_path, monkeypatch)
    first = run_job(environ=env, http=storage, identity_http=zernio,
                    apify_client=apify)
    posts = [m for m, _ in apify.calls].count('POST')
    second = run_job(environ=env, http=storage, identity_http=zernio,
                     apify_client=apify)
    assert first['gyms'][0]['request_id'] == second['gyms'][0]['request_id']
    assert durable_request_id(GYM, '2026-10-08') != durable_request_id(GYM, '2026-10-09')
    # Retry replays the same durable receipt binding: no new Apify run, same rows.
    assert [m for m, _ in apify.calls].count('POST') == posts
    assert len(storage.rows) == 2


def test_no_cross_tenant_work_outside_allowlist():
    runner = FakeRunner([GYM, OTHER_GYM])
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=GYM,
               ECHO_SOURCE_CAPTURE_JOB_DAY='2026-10-08')
    receipt = run_job(environ=env, _runner=runner, _approved_gyms=(GYM, OTHER_GYM))
    assert receipt['captured'] == 1
    assert [g for g, _ in runner.calls] == [GYM]
    # An allowlisted gym with NO approved mapping holds; nothing is invented.
    runner2 = FakeRunner([OTHER_GYM])
    receipt2 = run_job(environ=env, _runner=runner2, _approved_gyms=(OTHER_GYM,))
    assert receipt2['captured'] == 0 and receipt2['held'] == 1
    assert receipt2['gyms'][0]['hold'] == 'gym_not_in_approved_mapping'
    assert runner2.calls == []


def test_failure_isolation_one_gym_holds_others(tmp_path):
    runner = FakeRunner([GYM, OTHER_GYM], fail={GYM})
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=f'{GYM},{OTHER_GYM}',
               ECHO_SOURCE_CAPTURE_JOB_DAY='2026-10-08')
    receipt = run_job(environ=env, _runner=runner, _approved_gyms=(GYM, OTHER_GYM))
    assert receipt['state'] == 'complete'
    assert receipt['captured'] == 1 and receipt['held'] == 1 and receipt['errors'] == 0
    statuses = {e['gym_id']: e['status'] for e in receipt['gyms']}
    assert statuses == {GYM: 'held', OTHER_GYM: 'captured'}
    assert all('synthetic_hold' not in str(e) for e in receipt['gyms']
               if e['status'] != 'held' or 'hold' not in e)


def _fourteen_gyms():
    return tuple(
        f'a1b2c3d4-1111-2222-3333-44445555{i:04d}' for i in range(14))


def test_oversize_allowlist_holds_fail_closed_instead_of_deferring():
    runner = FakeRunner([GYM, OTHER_GYM])
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=f'{GYM},{OTHER_GYM}',
               ECHO_SOURCE_CAPTURE_JOB_MAX_GYMS='1',
               ECHO_SOURCE_CAPTURE_JOB_DAY='2026-10-08')
    receipt = run_job(environ=env, _runner=runner, _approved_gyms=(GYM, OTHER_GYM))
    # Fail-closed: an oversized selection holds the whole run rather than
    # silently starving the tail of the allowlist with a permanent prefix.
    assert receipt['state'] == 'held'
    assert receipt['hold'] == 'allowlist_exceeds_limit'
    assert runner.calls == []  # no partial prefix capture, no silent deferral


def test_default_limit_covers_14_gym_rollout_across_days():
    gyms = _fourteen_gyms()
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=','.join(gyms))
    receipts = {}
    for day in ('2026-10-08', '2026-10-09'):
        runner = FakeRunner(list(gyms))
        receipts[day] = run_job(
            environ=dict(env, ECHO_SOURCE_CAPTURE_JOB_DAY=day),
            _runner=runner, _approved_gyms=gyms)
    for day, receipt in receipts.items():
        assert receipt['state'] == 'complete'
        assert receipt['captured'] == 14 and receipt['held'] == 0
        assert {e['gym_id'] for e in receipt['gyms']} == set(gyms)
    # Multi-day: same gym binds a different durable request per scheduled day.
    d1 = {e['gym_id']: e['request_id'] for e in receipts['2026-10-08']['gyms']}
    d2 = {e['gym_id']: e['request_id'] for e in receipts['2026-10-09']['gyms']}
    probe = gyms[0]
    assert d1[probe] != d2[probe]
    assert d1[probe] == durable_request_id(probe, '2026-10-08')
    assert d2[probe] == durable_request_id(probe, '2026-10-09')


def test_unexpected_per_gym_error_isolated_and_typed_only():
    class Crash:
        def __init__(self):
            self.collector = SimpleNamespace(_resolve=SimpleNamespace(
                _approved={GYM: object()}))
        def capture(self, gym_id, request_id):
            raise RuntimeError('synthetic private detail')
    env = dict(environment(), ECHO_SOURCE_CAPTURE_JOB_ENABLED='true',
               ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST=GYM,
               ECHO_SOURCE_CAPTURE_JOB_DAY='2026-10-08')
    receipt = run_job(environ=env, _runner=Crash(), _approved_gyms=(GYM,))
    assert receipt['errors'] == 1
    entry = receipt['gyms'][0]
    assert entry['status'] == 'error' and entry['error'] == 'RuntimeError'
    assert 'synthetic private detail' not in str(receipt)


def test_cli_gym_narrows_configured_allowlist(monkeypatch):
    seen = {}

    def _fake_run_job(*a, **k):
        import os as _os
        seen['allowlist'] = _os.environ.get('ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST')
        return {'state': 'complete', 'captured': 1, 'held': 0, 'errors': 0,
                'gyms': []}

    monkeypatch.setattr('agent.source_brand_capture_job.run_job', _fake_run_job)
    monkeypatch.setenv('ECHO_SOURCE_CAPTURE_JOB_ENABLED', 'true')
    monkeypatch.setenv('ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST', f'{GYM},{OTHER_GYM}')
    assert main(['--gym', GYM]) == 0
    assert seen['allowlist'] == GYM  # narrowed, not replaced/widened


def test_cli_gym_outside_allowlist_rejected_before_run(monkeypatch):
    called = []

    def _fake_run_job(*a, **k):
        called.append(True)
        return {'state': 'off'}

    monkeypatch.setattr('agent.source_brand_capture_job.run_job', _fake_run_job)
    monkeypatch.setenv('ECHO_SOURCE_CAPTURE_JOB_ENABLED', 'true')
    monkeypatch.setenv('ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST', GYM)
    rc = main(['--gym', OTHER_GYM])
    assert rc != 0
    assert called == []  # job never ran


def test_cli_exit_codes(monkeypatch):
    cases = [
        ({'state': 'off'}, 0),
        ({'state': 'held', 'hold': 'private_mapping_authority_required',
          'gyms': []}, 1),
        ({'state': 'complete', 'captured': 0, 'held': 0, 'errors': 1,
          'gyms': [{'gym_id': GYM, 'status': 'error',
                    'error': 'RuntimeError'}]}, 1),
        ({'state': 'complete', 'captured': 0, 'held': 1, 'errors': 0,
          'gyms': [{'gym_id': GYM, 'status': 'held',
                    'hold': 'gym_not_in_approved_mapping'}]}, 1),
        ({'state': 'unexpected', 'captured': 0, 'held': 0, 'errors': 0,
          'gyms': []}, 1),
        ({'state': 'complete', 'captured': 2, 'held': 0, 'errors': 0,
          'gyms': []}, 0),
    ]
    for receipt, expected in cases:
        monkeypatch.setattr('agent.source_brand_capture_job.run_job',
                            lambda *a, _r=receipt, **k: _r)
        assert main([]) == expected, receipt
