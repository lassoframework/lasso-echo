"""Provider identity refresh cron lane: default OFF, attestation-only, no capture.

Offline synthetic authorities/transports only; no production plane is reachable.
"""
import threading
import time
from dataclasses import asdict

import pytest

from agent.source_brand_ingest import CaptureIngestError
from agent.source_brand_provider_refresh_job import main, run_job

from test_source_brand_capture_runner import GYM, PROFILE, environment, setup
from test_source_brand_startup import authority

OTHER_GYM = 'a1b2c3d4-1111-2222-3333-444455556666'


def _gym(i):
    return f'b2c3d4e5-{i:04d}-2222-3333-444455556666'


GYMS_10 = [_gym(i) for i in range(10)]
GYMS_11 = [_gym(i) for i in range(11)]


def _mapping(key='k'):
    return type('M', (), {'echo_account_key': key})()


class _ConcurrentIdentity:
    """Thread-safe fake identity reader: records calls and peak concurrency."""

    def __init__(self, delay_by_gym=None):
        self.calls = []
        self.max_concurrent = 0
        self._current = 0
        self._lock = threading.Lock()
        self._delay = delay_by_gym or {}

    def __call__(self, gym_id, key):
        with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
            self.calls.append(gym_id)
        try:
            time.sleep(self._delay.get(gym_id, 0))
            return {'profile_id': PROFILE}
        finally:
            with self._lock:
                self._current -= 1


def job_env(tmp_path, overrides=None):
    original, storage, zernio, apify, _ = setup(tmp_path)
    entries = [asdict(x) for x in original.collector._resolve._approved.values()]
    path = authority(tmp_path, entries)
    env = dict(environment(),
        ECHO_SOURCE_PROVIDER_REFRESH_JOB_ENABLED='true',
        ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST=GYM,
        ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE=str(path))
    env.update(overrides or {})
    return env, original, storage, zernio


def test_default_off_no_io(tmp_path):
    assert run_job(environ={}) == {'state': 'off'}
    assert not list(tmp_path.iterdir())


def test_cli_default_off_exits_zero(capsys):
    assert main() == 0


def test_armed_without_collector_flag_holds(tmp_path):
    env, *_ = job_env(tmp_path, {'ECHO_SOURCE_COLLECTOR_ENABLED': 'false'})
    assert run_job(environ=env)['hold'] == 'source_collector_disabled'


@pytest.mark.parametrize('raw', [None, '', 'not-a-uuid', GYM + ',' + GYM])
def test_allowlist_missing_or_invalid_holds_without_io(tmp_path, raw):
    env, _, storage, zernio = job_env(
        tmp_path, {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST': raw} if raw is not None else {})
    if raw is None:
        env.pop('ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST', None)
    assert run_job(environ=env)['hold'] == 'tenant_allowlist_required'
    assert not storage.posts and not zernio.calls


def test_max_gyms_malformed_holds(tmp_path):
    env, *_ = job_env(tmp_path, {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_MAX_GYMS': 'abc'})
    assert run_job(environ=env)['hold'] == 'max_gyms_invalid'


def test_missing_authority_holds_without_fallback(tmp_path):
    env, *_ = job_env(tmp_path)
    env['ECHO_SOURCE_CAPTURE_APPROVED_MAPPINGS_FILE'] = ''
    assert run_job(environ=env)['hold'] == 'private_mapping_authority_required'


def test_missing_zernio_key_holds(tmp_path):
    env, *_ = job_env(tmp_path, {'ZERNIO_API_KEY': ''})
    assert run_job(environ=env)['hold'] == 'zernio_account_credential_required'


def test_one_allowlisted_gym_refreshed_with_attestation_readback(tmp_path):
    env, _, storage, zernio = job_env(tmp_path)
    receipt = run_job(environ=env, http=storage, identity_http=zernio)
    assert receipt['state'] == 'complete' and receipt['refreshed'] == 1
    assert receipt['held'] == 0 and receipt['errors'] == 0
    entry = receipt['gyms'][0]
    assert entry == {'gym_id': GYM, 'status': 'refreshed', 'profile_id': PROFILE}
    # Exactly one attestation RPC write with exact readback; no capture ingest.
    assert len(storage.attestations) == 1
    row = storage.attestations[0]
    assert row['gym_id'] == GYM
    assert row['attestation']['lookup_status'] == 'complete'
    assert row['attestation']['source'] == 'zernio_authenticated_accounts'
    assert all(url.endswith('echo_source_brand_attest_provider')
               for url, _ in storage.posts)
    # Authenticated provider evidence: health authority + paired account list.
    endpoints = [url for url, _ in zernio.calls]
    assert endpoints == ['https://api.zernio.com/v1/accounts/health',
                         'https://api.zernio.com/v1/accounts']


def test_no_cross_tenant_work_outside_allowlist(tmp_path):
    env, *_ = job_env(
        tmp_path, {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST': GYM + ',' + OTHER_GYM})
    receipt = run_job(environ=env, _identity=lambda g, k: {'profile_id': PROFILE},
                      _approved={GYM: type('M', (), {'echo_account_key': 'k'})()})
    assert receipt['refreshed'] == 1 and receipt['held'] == 1
    assert receipt['gyms'][1] == {'gym_id': OTHER_GYM, 'status': 'held',
                                  'hold': 'gym_not_in_approved_mapping'}


def test_tenant_token_mismatch_holds_before_identity_call(tmp_path):
    env, _, storage, zernio = job_env(tmp_path)

    class Tokens:
        def __call__(self, table, params):
            assert table == 'echo_intake_tokens'
            assert params == {'gym_id': 'eq.' + GYM,
                              'select': 'gym_id,echo_account_key'}
            return [{'gym_id': GYM, 'echo_account_key': 'someone-else'}]

    from agent.source_brand_collector import AuthenticatedZernioIdentityReader
    identity = AuthenticatedZernioIdentityReader(read_rows=Tokens(), environ=env,
                                                 http=zernio)
    approved = {GYM: type('M', (), {'echo_account_key': 'swiftrivercrossfite5c9db'})()}
    receipt = run_job(environ=env, _identity=identity, _approved=approved)
    assert receipt['gyms'][0]['hold'] == 'current_tenant_mapping_mismatch'
    assert not zernio.calls  # identity call never reached the provider


def test_per_gym_failure_isolation_negative_attestation_persisted(tmp_path):
    env, _, storage, zernio = job_env(tmp_path)
    zernio.health_rows = []  # unhealthy/missing authority -> hold, never proof
    receipt = run_job(environ=env, http=storage, identity_http=zernio)
    assert receipt['state'] == 'complete' and receipt['held'] == 1
    assert receipt['gyms'][0]['hold'] == \
        'authenticated_social_status_unavailable_or_incomplete'
    # The reader persisted the partial lookup as a hold attestation.
    assert len(storage.attestations) == 1
    assert storage.attestations[0]['attestation']['lookup_status'] != 'complete'


def test_oversize_allowlist_holds_fail_closed(tmp_path):
    env, *_ = job_env(tmp_path, {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_MAX_GYMS': '1',
        'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST': GYM + ',' + OTHER_GYM})
    approved = {g: type('M', (), {'echo_account_key': 'k'})() for g in (GYM, OTHER_GYM)}
    receipt = run_job(environ=env, _identity=lambda g, k: {'profile_id': PROFILE},
                      _approved=approved)
    assert receipt['hold'] == 'allowlist_exceeds_limit' and receipt['gyms'] == []


@pytest.mark.parametrize('raw_max', [None, '50'])
def test_overlimit_allowlist_holds_before_any_provider_work(tmp_path, raw_max):
    """More than 10 gyms per invocation holds BEFORE any provider call."""
    overrides = {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST': ','.join(GYMS_11)}
    if raw_max is not None:
        overrides['ECHO_SOURCE_PROVIDER_REFRESH_JOB_MAX_GYMS'] = raw_max
    env, *_ = job_env(tmp_path, overrides)
    identity = _ConcurrentIdentity()
    approved = {g: _mapping() for g in GYMS_11}
    receipt = run_job(environ=env, _identity=identity, _approved=approved)
    assert receipt['hold'] == 'allowlist_exceeds_limit'
    assert receipt['gyms'] == [] and receipt['refreshed'] == 0
    assert identity.calls == []  # capacity gate held before all provider work


def test_slow_gym_does_not_serially_starve_another(tmp_path):
    """A blocked first gym must not prevent a second gym from refreshing."""
    env, *_ = job_env(
        tmp_path, {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST': GYM + ',' + OTHER_GYM})
    other_done = threading.Event()
    calls = []

    def gated(gym_id, key):
        calls.append(gym_id)
        if gym_id == GYM:
            # Serial execution would deadlock/timeout here: GYM is allowlisted
            # first and never releases until OTHER_GYM has refreshed.
            assert other_done.wait(10), 'serial starvation: other gym never ran'
        else:
            other_done.set()
        return {'profile_id': PROFILE}

    approved = {GYM: _mapping(), OTHER_GYM: _mapping()}
    receipt = run_job(environ=env, _identity=gated, _approved=approved)
    assert receipt['state'] == 'complete' and receipt['refreshed'] == 2
    assert receipt['errors'] == 0
    assert set(calls) == {GYM, OTHER_GYM}


def test_concurrency_is_bounded_and_receipt_preserves_allowlist_order(tmp_path):
    """Peak workers <= 10, gyms really overlap, and entries stay in allowlist
    order even when later gyms finish before earlier ones."""
    env, *_ = job_env(
        tmp_path, {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST': ','.join(GYMS_10)})
    # Earlier gyms sleep longer, so completion order is the reverse of the
    # allowlist order; a correct implementation still returns allowlist order.
    delays = {g: 0.05 * (len(GYMS_10) - i) for i, g in enumerate(GYMS_10)}
    identity = _ConcurrentIdentity(delay_by_gym=delays)
    approved = {g: _mapping() for g in GYMS_10}
    receipt = run_job(environ=env, _identity=identity, _approved=approved)
    assert receipt['state'] == 'complete' and receipt['refreshed'] == 10
    assert receipt['held'] == 0 and receipt['errors'] == 0
    assert identity.max_concurrent <= 10
    assert identity.max_concurrent >= 2  # gyms demonstrably ran concurrently
    assert [e['gym_id'] for e in receipt['gyms']] == GYMS_10
    assert sorted(identity.calls) == sorted(GYMS_10)  # exactly one call per gym


def test_concurrent_run_no_cross_tenant_data(tmp_path):
    """Under concurrency each gym sees only its own approved binding; one
    cross-tenant/unmapped gym holds without affecting the other."""
    env, *_ = job_env(
        tmp_path, {'ECHO_SOURCE_PROVIDER_REFRESH_JOB_ALLOWLIST': GYM + ',' + OTHER_GYM})
    seen = []

    def identity(gym_id, key):
        seen.append((gym_id, key))
        return {'profile_id': PROFILE}

    receipt = run_job(environ=env, _identity=identity, _approved={GYM: _mapping('k1')})
    assert receipt['refreshed'] == 1 and receipt['held'] == 1
    assert seen == [(GYM, 'k1')]  # unmapped tenant never reached provider work
    statuses = {e['gym_id']: e['status'] for e in receipt['gyms']}
    assert statuses == {GYM: 'refreshed', OTHER_GYM: 'held'}


def test_unexpected_per_gym_error_isolated_and_typed_only(tmp_path):
    env, *_ = job_env(tmp_path)

    def boom(gym_id, key):
        raise RuntimeError('synthetic transport secret-detail')

    approved = {GYM: type('M', (), {'echo_account_key': 'k'})()}
    receipt = run_job(environ=env, _identity=boom, _approved=approved)
    assert receipt['errors'] == 1
    assert receipt['gyms'][0] == {'gym_id': GYM, 'status': 'error',
                                  'error': 'RuntimeError'}


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    env, _, storage, zernio = job_env(tmp_path)

    monkeypatch.setattr('os.environ', env)
    from agent import source_brand_provider_refresh_job as job
    monkeypatch.setattr(job, 'run_job',
                        lambda *a, **kw: run_job(environ=env, http=storage,
                                                 identity_http=zernio))
    assert job.main() == 0
    zernio.health_rows = []
    assert job.main() == 1
