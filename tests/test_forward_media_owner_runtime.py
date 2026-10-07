"""Isolated owner one-pass factory and cleanup; no DB/network credentials used."""
import json
import os
from types import SimpleNamespace

import pytest

from agent import forward_media_owner as owner, forward_media_owner_worker as worker
from agent import forward_media_owner_transport as transport_module
from agent import forward_media_source_verifier as source_module


@pytest.fixture
def runtime(monkeypatch):
    for key in list(os.environ):
        if owner._FORBIDDEN_ENV_NAME.search(key) or key in worker._FORBIDDEN:
            monkeypatch.delenv(key)
    monkeypatch.setenv(worker.WORKER_ENV, 'true')
    monkeypatch.setenv(worker.TENANTS_ENV, 'gym')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_DSN', 'postgres://isolated-owner@example.invalid/x')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_ROLE', 'isolated_owner')
    events = []
    state = {'fail': None, 'close_fail': False}
    hosted, drive, transport = object(), object(), object()
    class Connection:
        closed = False
        def close(self):
            events.append('close')
            self.closed = True
            if state['close_fail']:
                raise RuntimeError('private-close-driver-error')
    conn = Connection()
    persistence = SimpleNamespace(_conn=conn)
    report = {'status': 'partial_hold', 'rows': [{'status': 'hold', 'decision': 'hold_uncertain'}]}
    def step(name):
        events.append(name)
        if state['fail'] == name:
            raise RuntimeError('private-factory-driver-error')
    def hosted_factory():
        step('hosted')
        return hosted
    def connect(*, reader):
        assert reader is hosted
        step('connect')
        return persistence
    def transport_factory(actual):
        assert actual is persistence
        step('transport')
        return transport
    def drive_factory():
        step('drive')
        return drive
    def adapter(**kwargs):
        assert kwargs == {'transport': transport, 'persistence': persistence,
                          'reader': hosted, 'drive_reader': drive}
        step('adapter')
        return report
    monkeypatch.setattr(owner, 'HostedObjectReader', hosted_factory)
    monkeypatch.setattr(owner.ForwardMediaOwnerPersistence, 'connect_from_environment', connect)
    monkeypatch.setattr(transport_module, 'DedicatedOwnerTransport', transport_factory)
    monkeypatch.setattr(source_module, 'OriginalDriveReader', drive_factory)
    monkeypatch.setattr(worker, 'run_adapter', adapter)
    return SimpleNamespace(events=events, state=state, conn=conn, report=report)


def test_one_pass_composes_only_isolated_readers_and_owner_connection(runtime):
    assert worker.run_once() is runtime.report
    assert runtime.events == ['hosted', 'connect', 'transport', 'drive', 'adapter', 'close']
    assert runtime.conn.closed


def test_default_off_constructs_nothing_even_with_missing_owner_configuration(runtime, monkeypatch):
    monkeypatch.delenv(worker.WORKER_ENV)
    monkeypatch.delenv('FORWARD_MEDIA_OWNER_DSN')
    monkeypatch.setenv('ZERNIO_API_KEY', 'private-forbidden-key')
    assert worker.run_once() == {'status': 'disabled', 'rows': []}
    assert runtime.events == []


@pytest.mark.parametrize('key,value', [
    ('FORWARD_MEDIA_OWNER_DSN', ''), ('FORWARD_MEDIA_OWNER_ROLE', ''),
    ('FORWARD_MEDIA_OWNER_ROLE', 'service_role'),
    ('FORWARD_MEDIA_OWNER_ROLE', 'fixer_forward_media_attester_20261006'),
    (worker.TENANTS_ENV, ''), (worker.TENANTS_ENV, 'bad tenant'),
    ('AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE', '101'),
    ('AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE', 'not-a-number'),
    ('SUPABASE_SERVICE_ROLE_KEY', 'private-forbidden-key'),
    ('ZERNIO_API_KEY', 'private-forbidden-key'),
    ('AGENT_SOCIALAPI_KEY', 'private-forbidden-key'),
    ('AGENT_FORWARD_MEDIA_ATTESTER_DSN', 'private-forbidden-key'),
    ('AGENT_VISUAL_RECEIPT_OWNER_DSN', 'private-forbidden-key'),
])
def test_invalid_environment_never_opens_or_reads(runtime, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    report = worker.run_once()
    assert report['status'] == 'hold' and report['rows'] == []
    assert report['reason'] in {'owner_environment_invalid', 'explicit_tenant_allowlist_required', 'worker_bounds_invalid'}
    assert runtime.events == []
    assert 'private-' not in json.dumps(report)


@pytest.mark.parametrize('phase', ['hosted', 'connect', 'transport', 'drive', 'adapter'])
def test_every_factory_failure_is_static_and_closes_if_open(runtime, phase):
    runtime.state['fail'] = phase
    assert worker.run_once() == {'status': 'hold', 'reason': 'owner_transport_unavailable', 'rows': []}
    expected_closed = phase in {'transport', 'drive', 'adapter'}
    assert runtime.conn.closed is expected_closed
    assert runtime.events.count('close') == int(expected_closed)


def test_uncertain_commit_is_not_retried_and_owned_connection_closes(runtime, monkeypatch):
    def uncertain(**kwargs):
        runtime.events.append('adapter')
        raise owner.UncertainCommitError('private-unknown-commit')
    monkeypatch.setattr(worker, 'run_adapter', uncertain)
    assert worker.run_once() == {'status': 'hold', 'reason': 'uncertain_authority_commit', 'rows': []}
    assert runtime.events.count('adapter') == 1 and runtime.events.count('close') == 1


def test_cleanup_failure_preserves_acknowledged_hold_receipts_and_stays_static(runtime):
    runtime.state['close_fail'] = True
    report = worker.run_once()
    assert report == {'status': 'hold', 'reason': 'owner_transport_unavailable', 'rows': runtime.report['rows']}
    assert runtime.events.count('close') == 1 and runtime.conn.closed
    assert 'private-' not in json.dumps(report)


@pytest.mark.parametrize('report,exit_code', [
    ({'status': 'disabled', 'rows': []}, 0),
    ({'status': 'complete', 'rows': []}, 0),
    ({'status': 'complete', 'rows': [{'status': 'persisted'}]}, 2),
    ({'status': 'complete', 'rows': None}, 2),
    ({'status': 'partial_hold', 'rows': [{'status': 'hold'}]}, 2),
    ({'status': 'hold', 'reason': 'owner_transport_unavailable', 'rows': []}, 2),
])
def test_cli_success_is_only_disabled_or_confirmed_empty_pass(monkeypatch, capsys, report, exit_code):
    monkeypatch.setattr(worker, 'run_once', lambda: report)
    assert worker.main([]) == exit_code
    assert json.loads(capsys.readouterr().out) == report
