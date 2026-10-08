"""Finite cron job lane for trusted semantic observation: default OFF,
allowlist-only, fail-closed. Offline synthetic authorities/transports only;
no production plane, generation, or publishing path is reachable.
"""
import base64
import copy
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent.source_brand_ingest import CaptureIngestError
from agent.source_brand_observation import (ObservationHold,
    TrustedSourceObservationProducer)
from agent.source_brand_observation_job import main, run_job

from test_source_brand_capture_runner import GYM, KEY, URL, environment, setup

OTHER_GYM = 'a1b2c3d4-1111-2222-3333-444455556666'
SHA = 'ab' * 32


def job_env(overrides=None):
    env = dict(environment(),
        ECHO_SOURCE_OBSERVATION_ENABLED='true',
        ECHO_SOURCE_OBSERVATION_JOB_ENABLED='true',
        ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST=GYM)
    env.update(overrides or {})
    return env


class FakeProducer:
    """Scripted per-gym observation outcomes; records every observe() call.

    `report` deliberately carries synthetic private detail so tests can prove
    the job's receipt never forwards producer report bodies.
    """
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = outcomes or {}

    def observe(self, gym_id):
        self.calls.append(gym_id)
        outcome = self.outcomes.get(gym_id)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        return {'state': 'verified', 'observation_id': 1,
                'content_sha256': SHA,
                'report': {'synthetic_secret': 'raw source body'}}


def test_off_zero_io_and_exit_zero(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('composition must not run while OFF')
    monkeypatch.setattr('agent.source_brand_observation_job.initialize_source_capture',
                        _boom)
    producer = FakeProducer()
    assert run_job(environ={}, _producer=producer, _approved_gyms=(GYM,)) == {'state': 'off'}
    assert producer.calls == []
    assert not list(tmp_path.iterdir())
    monkeypatch.delenv('ECHO_SOURCE_OBSERVATION_JOB_ENABLED', raising=False)
    assert main([]) == 0


def test_allowlist_missing_empty_malformed_duplicate_holds_without_io(
        tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('no fallback composition on invalid allowlist')
    monkeypatch.setattr('agent.source_brand_observation_job.initialize_source_capture',
                        _boom)
    producer = FakeProducer()
    for raw in (None, '', '  ', 'not-a-uuid', f'{GYM},bogus', f'{GYM},{GYM}'):
        env = job_env({'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST': raw})
        if raw is None:
            del env['ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST']
        receipt = run_job(environ=env,
                          _producer=producer, _approved_gyms=(GYM,))
        assert receipt['state'] == 'held'
        assert receipt['hold'] == 'tenant_allowlist_required'
        assert receipt['gyms'] == [] and receipt['observed'] == 0
        assert receipt['held'] == 1 and receipt['errors'] == 0
    assert producer.calls == []
    assert not list(tmp_path.iterdir())


def test_oversize_allowlist_holds_fail_closed_never_truncates():
    producer = FakeProducer()
    receipt = run_job(
        environ=job_env({'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST': f'{GYM},{OTHER_GYM}',
                         'ECHO_SOURCE_OBSERVATION_JOB_MAX_GYMS': '1'}),
        _producer=producer, _approved_gyms=(GYM, OTHER_GYM))
    assert receipt['state'] == 'held'
    assert receipt['hold'] == 'allowlist_exceeds_limit'
    assert producer.calls == []  # no partial prefix observation


def test_allowlisted_gym_without_approved_mapping_held_no_cross_tenant():
    producer = FakeProducer()
    receipt = run_job(environ=job_env(),
                      _producer=producer, _approved_gyms=(OTHER_GYM,))
    assert receipt['state'] == 'complete'
    assert receipt['observed'] == 0 and receipt['held'] == 1
    entry = receipt['gyms'][0]
    assert entry['gym_id'] == GYM and entry['status'] == 'held'
    assert entry['hold'] == 'gym_not_in_approved_mapping'
    assert producer.calls == []  # no cross-tenant observe


def test_independent_gym_continuation_hold_and_error_do_not_stop_others():
    producer = FakeProducer({
        GYM: ObservationHold('capture_transport_authentication_required')})
    class Crash:
        def __init__(self):
            self.inner = FakeProducer()
        def observe(self, gym_id):
            self.inner.calls.append(gym_id)
            if gym_id == GYM:
                raise ObservationHold('capture_transport_authentication_required')
            if gym_id == OTHER_GYM:
                raise RuntimeError('synthetic private detail')
            return {'state': 'verified', 'observation_id': 2,
                    'content_sha256': SHA, 'report': {}}
    third = 'b2c3d4e5-1111-2222-3333-444455556666'
    crashing = Crash()
    receipt = run_job(
        environ=job_env({'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST':
                         f'{GYM},{OTHER_GYM},{third}'}),
        _producer=crashing, _approved_gyms=(GYM, OTHER_GYM, third))
    assert receipt['state'] == 'complete'
    assert receipt['observed'] == 1 and receipt['held'] == 1 and receipt['errors'] == 1
    assert crashing.inner.calls == [GYM, OTHER_GYM, third]  # loop never stopped
    statuses = {e['gym_id']: e['status'] for e in receipt['gyms']}
    assert statuses == {GYM: 'held', OTHER_GYM: 'error', third: 'observed'}
    held = next(e for e in receipt['gyms'] if e['gym_id'] == GYM)
    assert held['hold'] == 'capture_transport_authentication_required'
    error = next(e for e in receipt['gyms'] if e['gym_id'] == OTHER_GYM)
    assert error['error'] == 'RuntimeError'
    assert 'synthetic private detail' not in str(receipt)


def test_startup_capture_ingest_error_holds_whole_run(monkeypatch):
    def _missing_config(*a, **k):
        raise CaptureIngestError('private_mapping_authority_required')
    monkeypatch.setattr('agent.source_brand_observation_job.initialize_source_capture',
                        _missing_config)
    receipt = run_job(environ=job_env())
    assert receipt['state'] == 'held'
    assert receipt['hold'] == 'private_mapping_authority_required'
    assert receipt['gyms'] == []


def test_producer_disabled_gate_is_armed_hold_and_nonzero_exit(monkeypatch):
    # Producer's own ECHO_SOURCE_OBSERVATION_ENABLED gate fires inside
    # observe(); a per-gym source_observation_disabled hold is an armed hold.
    producer = FakeProducer({GYM: ObservationHold('source_observation_disabled')})
    env = job_env({'ECHO_SOURCE_OBSERVATION_ENABLED': 'false'})
    receipt = run_job(environ=env, _producer=producer, _approved_gyms=(GYM,))
    assert receipt['state'] == 'complete'
    assert receipt['held'] == 1
    assert receipt['gyms'][0]['hold'] == 'source_observation_disabled'
    monkeypatch.setattr('agent.source_brand_observation_job.run_job',
                        lambda *a, **k: receipt)
    assert main([]) == 1


def test_enabled_job_holds_when_source_capture_startup_is_off():
    env = job_env({'ECHO_SOURCE_CAPTURE_RUNNER_ENABLED': 'false'})
    receipt = run_job(environ=env)
    assert receipt == {'state': 'held', 'hold': 'source_capture_runner_disabled',
                       'gyms': [], 'observed': 0, 'held': 1, 'errors': 0}


def test_unavailable_store_or_stale_capture_holds_are_armed_nonzero(monkeypatch):
    # Fail-closed producer surfaces (unavailable assessor/store, stale or
    # mismatched capture) all arrive as ObservationHold fixed codes; each is an
    # armed hold and must exit nonzero.
    for code in ('semantic_assessor_credentials_missing',
                 'current_capture_identity_or_bytes_changed',
                 'capture_transport_authentication_required',
                 'semantic_assessor_unavailable_or_uncertain'):
        producer = FakeProducer({GYM: ObservationHold(code)})
        receipt = run_job(environ=job_env(), _producer=producer,
                          _approved_gyms=(GYM,))
        assert receipt['held'] == 1
        assert receipt['gyms'][0]['status'] == 'held'
        assert receipt['gyms'][0]['hold'] == code
        monkeypatch.setattr('agent.source_brand_observation_job.run_job',
                            lambda *a, _r=receipt, **k: _r)
        assert main([]) == 1, code


def test_receipt_is_sanitized_no_report_bodies_or_bytes():
    producer = FakeProducer()
    receipt = run_job(environ=job_env(), _producer=producer,
                      _approved_gyms=(GYM,))
    assert receipt['state'] == 'complete' and receipt['observed'] == 1
    entry = receipt['gyms'][0]
    assert entry['status'] == 'observed' and entry['state'] == 'verified'
    assert entry['observation_id'] == 1 and entry['content_sha256'] == SHA
    assert 'report' not in entry
    assert 'synthetic_secret' not in str(receipt)
    assert 'raw source body' not in str(receipt)


def test_held_observation_state_is_sanitized_held_entry():
    # A completed-but-held producer result is a hold, while its observation
    # identity remains available for safe follow-up. Report bodies never leak.
    producer = FakeProducer({GYM: {'state': 'held', 'observation_id': 1,
                                   'content_sha256': SHA,
                                   'report': {'selected_facts_status': 'contradicted'}}})
    receipt = run_job(environ=job_env(), _producer=producer,
                      _approved_gyms=(GYM,))
    assert receipt['observed'] == 0 and receipt['held'] == 1
    entry = receipt['gyms'][0]
    assert entry['status'] == 'held'
    assert entry['hold'] == 'observation_state_held'
    assert entry['observation_id'] == 1 and entry['content_sha256'] == SHA
    assert 'report' not in entry and 'contradicted' not in str(receipt)


def test_held_observation_does_not_stop_verified_gym():
    third = 'b2c3d4e5-1111-2222-3333-444455556666'
    producer = FakeProducer({
        GYM: {'state': 'held', 'observation_id': 7, 'content_sha256': SHA,
              'report': {'selected_facts_status': 'contradicted'}},
        OTHER_GYM: {'state': 'verified', 'observation_id': 8,
                    'content_sha256': 'cd' * 32,
                    'report': {'selected_facts_status': 'supported_uncontradicted'}},
    })
    receipt = run_job(
        environ=job_env({'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST': f'{GYM},{OTHER_GYM}'}),
        _producer=producer, _approved_gyms=(GYM, OTHER_GYM, third))
    assert producer.calls == [GYM, OTHER_GYM]
    assert receipt['observed'] == 1 and receipt['held'] == 1 and receipt['errors'] == 0
    held = next(entry for entry in receipt['gyms'] if entry['gym_id'] == GYM)
    verified = next(entry for entry in receipt['gyms'] if entry['gym_id'] == OTHER_GYM)
    assert held['status'] == 'held' and held['observation_id'] == 7
    assert held['content_sha256'] == SHA
    assert verified['status'] == 'observed' and verified['state'] == 'verified'
    assert 'contradicted' not in str(receipt)


def test_cli_exit_codes(monkeypatch):
    cases = [
        ({'state': 'off'}, 0),
        ({'state': 'held', 'hold': 'tenant_allowlist_required', 'gyms': [],
          'observed': 0, 'held': 1, 'errors': 0}, 1),
        ({'state': 'complete', 'observed': 0, 'held': 0, 'errors': 1,
          'gyms': [{'gym_id': GYM, 'status': 'error', 'error': 'RuntimeError'}]}, 1),
        ({'state': 'complete', 'observed': 0, 'held': 1, 'errors': 0,
          'gyms': [{'gym_id': GYM, 'status': 'held',
                    'hold': 'gym_not_in_approved_mapping'}]}, 1),
        ({'state': 'complete', 'observed': 2, 'held': 0, 'errors': 0,
          'gyms': []}, 0),
    ]
    for receipt, expected in cases:
        monkeypatch.setattr('agent.source_brand_observation_job.run_job',
                            lambda *a, _r=receipt, **k: _r)
        assert main([]) == expected, receipt


def test_cli_gym_narrows_configured_allowlist(monkeypatch):
    seen = {}

    def _fake_run_job(*a, **k):
        seen['allowlist'] = os.environ.get('ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST')
        return {'state': 'complete', 'observed': 1, 'held': 0, 'errors': 0,
                'gyms': []}

    monkeypatch.setattr('agent.source_brand_observation_job.run_job', _fake_run_job)
    monkeypatch.setenv('ECHO_SOURCE_OBSERVATION_JOB_ENABLED', 'true')
    monkeypatch.setenv('ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST', f'{GYM},{OTHER_GYM}')
    assert main(['--gym', GYM]) == 0
    assert seen['allowlist'] == GYM  # narrowed, never replaced/widened


def test_cli_gym_outside_allowlist_rejected_before_run(monkeypatch):
    called = []

    def _fake_run_job(*a, **k):
        called.append(True)
        return {'state': 'off'}

    monkeypatch.setattr('agent.source_brand_observation_job.run_job', _fake_run_job)
    monkeypatch.setenv('ECHO_SOURCE_OBSERVATION_JOB_ENABLED', 'true')
    monkeypatch.setenv('ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST', GYM)
    assert main(['--gym', OTHER_GYM]) == 2
    assert main(['--gym', 'not-a-uuid']) == 2
    assert called == []  # job never ran


def _observation_store_and_assessor(rows, gym, key, url):
    """Frozen approved configuration + positive assessor over real collected
    captures (mirrors test_source_brand_capture_runner's producer wiring)."""
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    website = next(c for c in rows if c['source_kind'] == 'website')
    raw = bytes.fromhex(website['raw_bytes'][2:])
    snap = {'schema_version': 1, 'gym_id': gym, 'echo_account_key': key,
        'fact_policy': 'delegated_supported_facts',
        'captures': [{**{k: v for k, v in c.items() if k not in ('raw_bytes', 'captured_at')},
                      'bytes_base64': base64.b64encode(bytes.fromhex(c['raw_bytes'][2:])).decode()}
                     for c in rows],
        'selected_facts': [{'key': 'gym', 'capture_id': website['id'],
            'bytes_sha256': website['bytes_sha256'], 'source_locator': url,
            'byte_offset': raw.index(b'Real gym evidence'), 'byte_length': 17,
            'text': 'Real gym evidence'}],
        'palette': {'capture_id': website['id'], 'bytes_sha256': website['bytes_sha256'],
            'primary': '#123456', 'secondary': '#ffffff',
            'primary_byte_offset': raw.index(b'#123456'),
            'secondary_byte_offset': raw.index(b'#ffffff')}}
    frozen = canonical(snap)
    now = datetime.now(timezone.utc)
    bundle = {'id': str(uuid.uuid4()), 'gym_id': gym, 'echo_account_key': key,
        'version': 1, 'capture_ids': [c['id'] for c in rows],
        'snapshot_bytes': frozen,
        'content_sha256': hashlib.sha256(frozen.encode()).hexdigest(),
        'created_at': (now - timedelta(days=2)).isoformat()}
    approval = {'id': 1, 'gym_id': gym, 'bundle_id': bundle['id'],
        'bundle_version': 1, 'content_sha256': bundle['content_sha256'],
        'request_id': str(uuid.uuid4()),
        'purpose': 'echo_source_brand_configuration', 'action': 'approve',
        'actor_authority': 'blake', 'actor_clerk_user_id': 'synthetic-server-user',
        'created_at': (now - timedelta(days=1)).isoformat()}
    config = {'bundle': bundle, 'approval_receipt': approval}

    class Store:
        observation = None
        def latest(self, g, prior):
            assert g == gym
            return copy.deepcopy(next(c for c in rows if c['id'] == prior['id']))
        def rpc(self, name, params):
            assert params['p_gym'] == gym
            if name == 'echo_source_brand_configuration':
                return copy.deepcopy(config)
            if name == 'echo_source_brand_revalidate':
                self.observation = {'id': 1, 'gym_id': gym,
                    'bundle_id': bundle['id'],
                    'configuration_sha256': bundle['content_sha256'],
                    'snapshot_bytes': frozen,
                    'content_sha256': bundle['content_sha256'],
                    'validator_revision': params['p_validator_revision'],
                    'validation_report': params['p_validation_report']}
                return copy.deepcopy(self.observation)
            assert name == 'echo_source_brand_active'
            return {**copy.deepcopy(config),
                    'observation': copy.deepcopy(self.observation),
                    'fact_validation': 'supported_uncontradicted'}

    class Assessor:
        def assess(self, evidence):
            return {'evidence_sha256': hashlib.sha256(
                        canonical(evidence).encode()).hexdigest(),
                    'selected_facts_status': 'supported_uncontradicted'}

    return Store(), Assessor()


def test_durable_collector_journal_capture_flows_through_job(tmp_path, monkeypatch):
    # Synthetic-but-durable: a real CaptureReceiptJournal (strict perms in
    # tmp_path), real collector/resolver with fake transports, a real capture,
    # then the REAL TrustedSourceObservationProducer composed exactly like the
    # job's production path (collector._resolve + collector.authenticate_capture).
    for forbidden in ('agent.generation_log',):
        sys.modules.pop(forbidden, None)
    runner, storage, zernio, apify, _ = setup(tmp_path)
    assert os.stat(tmp_path).st_mode & 0o777 == 0o700  # strict journal dir perms
    runner.capture(GYM, 'observe-job-durable')
    rows = list(storage.rows.values())
    store, assessor = _observation_store_and_assessor(rows, GYM, KEY, URL)
    producer = TrustedSourceObservationProducer(
        resolve_mapping=runner.collector._resolve,
        authenticate_capture=runner.collector.authenticate_capture,
        store=store, assessor=assessor,
        environ={'ECHO_SOURCE_OBSERVATION_ENABLED': 'true'})
    receipt = run_job(environ=job_env(), _producer=producer,
                      _approved_gyms=(GYM,))
    assert receipt['state'] == 'complete'
    assert receipt['observed'] == 1 and receipt['held'] == 0 and receipt['errors'] == 0
    entry = receipt['gyms'][0]
    assert entry['gym_id'] == GYM and entry['status'] == 'observed'
    assert entry['state'] == 'verified' and entry['observation_id'] == 1
    int(entry['content_sha256'], 16)  # digest only
    # Sanitized: no report body, no raw source bytes, no credentials.
    rendered = str(receipt)
    assert 'report' not in entry
    assert 'Real gym evidence' not in rendered
    assert 'synthetic-service' not in rendered
    assert 'zernio_fixture_secret' not in rendered
    # No generation or publishing call occurred: the generation log module was
    # never imported, let alone invoked.
    assert 'agent.generation_log' not in sys.modules
