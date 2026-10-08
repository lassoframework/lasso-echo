"""Single-process capture->observation pipeline job: default OFF, exact
matching allowlists and explicit enablement for BOTH phases, capture strictly
before observation, no observation after a capture hold/error. Offline
synthetic seams only; no production plane, generation, or publishing path is
reachable.
"""
import sys

import pytest

from agent.source_brand_pipeline_job import main, run_job

GYM = 'e5c9db81-110d-4308-9bb7-3ad3bf563a0b'
OTHER_GYM = 'a1b2c3d4-1111-2222-3333-444455556666'
JOURNAL = '/private/echo-source-journal'


def pipeline_env(overrides=None):
    env = {
        'ECHO_SOURCE_PIPELINE_JOB_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_JOB_ENABLED': 'true',
        'ECHO_SOURCE_OBSERVATION_JOB_ENABLED': 'true',
        'ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST': GYM,
        'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST': GYM,
        'ECHO_SOURCE_CAPTURE_JOURNAL_DIR': JOURNAL,
    }
    env.update(overrides or {})
    return env


def capture_complete(captured=1):
    return {'state': 'complete', 'day': '2026-10-08', 'limit': 50,
            'captured': captured, 'held': 0, 'errors': 0,
            'gyms': [{'gym_id': GYM, 'request_id': 'source-capture:x:2026-10-08',
                      'status': 'captured', 'captures': 2}]}


def observe_complete():
    return {'state': 'complete', 'limit': 50, 'observed': 1, 'held': 0,
            'errors': 0,
            'gyms': [{'gym_id': GYM, 'status': 'observed', 'state': 'verified',
                      'observation_id': 1, 'content_sha256': 'ab' * 32}]}


class Recorder:
    """Scripted phase callable; records invocation order and received env."""
    def __init__(self, name, receipt, calls):
        self.name, self.receipt, self.calls = name, receipt, calls

    def __call__(self, env):
        self.calls.append((self.name, dict(env)))
        return self.receipt


def test_off_zero_io_and_exit_zero(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('no phase may run while OFF')
    monkeypatch.setattr('agent.source_brand_pipeline_job.capture_phase.run_job', _boom)
    monkeypatch.setattr('agent.source_brand_pipeline_job.observation_phase.run_job', _boom)
    assert run_job(environ={}) == {'state': 'off'}
    assert run_job(environ={'ECHO_SOURCE_PIPELINE_JOB_ENABLED': 'yes'}) == {'state': 'off'}
    assert not list(tmp_path.iterdir())
    monkeypatch.delenv('ECHO_SOURCE_PIPELINE_JOB_ENABLED', raising=False)
    assert main([]) == 0


def test_no_global_unlock_both_phase_flags_required():
    calls = []
    for overrides in (
        {'ECHO_SOURCE_CAPTURE_JOB_ENABLED': 'false'},
        {'ECHO_SOURCE_OBSERVATION_JOB_ENABLED': 'false'},
        {'ECHO_SOURCE_CAPTURE_JOB_ENABLED': 'false',
         'ECHO_SOURCE_OBSERVATION_JOB_ENABLED': 'false'},
    ):
        receipt = run_job(environ=pipeline_env(overrides),
                          _capture=Recorder('capture', capture_complete(), calls),
                          _observe=Recorder('observe', observe_complete(), calls))
        assert receipt['state'] == 'held'
        assert receipt['hold'] == 'phase_enablement_required'
    assert calls == []  # no phase ever ran


def test_both_phases_share_same_env_and_journal_dir():
    calls = []
    receipt = run_job(environ=pipeline_env(),
                      _capture=Recorder('capture', capture_complete(), calls),
                      _observe=Recorder('observe', observe_complete(), calls))
    assert receipt['state'] == 'complete'
    assert [name for name, _ in calls] == ['capture', 'observe']
    capture_env, observe_env = calls[0][1], calls[1][1]
    # Same environ mapping content to both phases — same private durable
    # journal dir, one service, one volume.
    assert capture_env == observe_env
    assert capture_env['ECHO_SOURCE_CAPTURE_JOURNAL_DIR'] == JOURNAL
    assert observe_env['ECHO_SOURCE_CAPTURE_JOURNAL_DIR'] == JOURNAL


def test_capture_runs_before_observation():
    calls = []
    run_job(environ=pipeline_env(),
            _capture=Recorder('capture', capture_complete(), calls),
            _observe=Recorder('observe', observe_complete(), calls))
    assert [name for name, _ in calls] == ['capture', 'observe']


def test_no_observation_after_capture_hold_or_error():
    for capture_receipt in (
        {'state': 'held', 'hold': 'tenant_allowlist_required', 'gyms': [],
         'captured': 0, 'held': 1, 'errors': 0},
        {'state': 'complete', 'captured': 1, 'held': 1, 'errors': 0,
         'gyms': [{'gym_id': GYM, 'status': 'held',
                   'hold': 'gym_not_in_approved_mapping'}]},
        {'state': 'complete', 'captured': 1, 'held': 0, 'errors': 1,
         'gyms': [{'gym_id': GYM, 'status': 'error', 'error': 'RuntimeError'}]},
        {'state': 'off'},
    ):
        calls = []
        receipt = run_job(environ=pipeline_env(),
                          _capture=Recorder('capture', capture_receipt, calls),
                          _observe=Recorder('observe', observe_complete(), calls))
        assert [name for name, _ in calls] == ['capture'], capture_receipt
        assert receipt['state'] == 'held'
        assert receipt['hold'] == 'capture_phase_incomplete'
        assert receipt['capture'] == capture_receipt
        assert receipt['observation'] is None


def test_allowlist_mismatch_holds_before_any_phase():
    calls = []
    receipt = run_job(
        environ=pipeline_env({'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST': OTHER_GYM}),
        _capture=Recorder('capture', capture_complete(), calls),
        _observe=Recorder('observe', observe_complete(), calls))
    assert receipt['state'] == 'held'
    assert receipt['hold'] == 'pipeline_allowlist_mismatch'
    assert calls == []


def test_missing_or_malformed_allowlist_holds_before_any_phase():
    calls = []
    for overrides in (
        {'ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST': ''},
        {'ECHO_SOURCE_OBSERVATION_JOB_ALLOWLIST': 'not-a-uuid'},
        {'ECHO_SOURCE_CAPTURE_JOB_ALLOWLIST': f'{GYM},{GYM}'},
    ):
        receipt = run_job(environ=pipeline_env(overrides),
                          _capture=Recorder('capture', capture_complete(), calls),
                          _observe=Recorder('observe', observe_complete(), calls))
        assert receipt['state'] == 'held'
        assert receipt['hold'] == 'tenant_allowlist_required'
    assert calls == []


def test_observation_hold_or_error_exits_nonzero_after_capture(monkeypatch):
    calls = []
    obs = {'state': 'complete', 'limit': 50, 'observed': 0, 'held': 1,
           'errors': 0,
           'gyms': [{'gym_id': GYM, 'status': 'held',
                     'hold': 'source_observation_disabled'}]}
    receipt = run_job(environ=pipeline_env(),
                      _capture=Recorder('capture', capture_complete(), calls),
                      _observe=Recorder('observe', obs, calls))
    assert [name for name, _ in calls] == ['capture', 'observe']
    assert receipt['state'] == 'held'
    assert receipt['hold'] == 'observation_phase_incomplete'
    monkeypatch.setattr('agent.source_brand_pipeline_job.run_job',
                        lambda *a, **k: receipt)
    assert main([]) == 1


def test_receipt_bounded_sanitized_and_exit_codes(monkeypatch, capsys):
    receipt = run_job(environ=pipeline_env(),
                      _capture=Recorder('capture', capture_complete(), []),
                      _observe=Recorder('observe', observe_complete(), []))
    assert receipt['state'] == 'complete'
    assert len(receipt['capture']['gyms']) == 1
    assert len(receipt['observation']['gyms']) == 1
    rendered = str(receipt)
    for forbidden in ('zernio', 'service_role', 'api_key', 'raw source body'):
        assert forbidden not in rendered.lower()
    monkeypatch.setattr('agent.source_brand_pipeline_job.run_job',
                        lambda *a, **k: receipt)
    assert main([]) == 0
    out = capsys.readouterr().out
    assert 'source-pipeline-job' in out
    for held in ({'state': 'held', 'hold': 'capture_phase_incomplete',
                  'capture': None, 'observation': None},
                 {'state': 'off'}):
        monkeypatch.setattr('agent.source_brand_pipeline_job.run_job',
                            lambda *a, _r=held, **k: _r)
        assert main([]) == (0 if held['state'] == 'off' else 1)


def test_production_defaults_wire_real_phase_modules(monkeypatch):
    # Without test seams, run_job must call the real phase modules' run_job
    # with the same environ, in order.
    calls = []
    env = pipeline_env()
    monkeypatch.setattr('agent.source_brand_pipeline_job.capture_phase.run_job',
                        lambda environ=None, **k: calls.append(('capture', environ))
                        or capture_complete())
    monkeypatch.setattr('agent.source_brand_pipeline_job.observation_phase.run_job',
                        lambda environ=None, **k: calls.append(('observe', environ))
                        or observe_complete())
    receipt = run_job(environ=env)
    assert receipt['state'] == 'complete'
    assert [name for name, _ in calls] == ['capture', 'observe']
    assert calls[0][1] == env == calls[1][1]
    assert 'agent.generation_log' not in sys.modules
