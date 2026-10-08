"""Offline isolated worker contract: no sender/database/network is activated."""
import json
import uuid

import pytest

from agent import forward_media_attester_worker as worker

ROW = str(uuid.UUID(int=1))
OTHER_ROW = str(uuid.UUID(int=2))
EVIDENCE = str(uuid.UUID(int=3))
REVISION = 'a' * 32
SECRET = 'postgresql://service_role:never-log-this@private.example/db'


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name in worker._FORBIDDEN_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(worker.WORKER_ENV, 'true')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_GUARD', 'true')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_DSN', 'postgresql://dedicated-attester')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_ROLE', worker.guard.ROLE)
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce,gritx')


def snapshot(row=ROW, tenant='pierce', revision=REVISION):
    return {'calendar_row_id': row, 'tenant_id': tenant, 'revision': revision,
            'source_url': SECRET}  # snapshots' URL/string fields must never be logged


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.conn.calls.append((query, params))
        if query == 'select current_user':
            self.result = (self.conn.role,)
        elif 'forward_schedule_preparation_eligible_20261008' in query:
            self.result = (self.conn.eligible.get(params[0]),)
        elif 'attestation_request_20261006' in query:
            self.result = (self.conn.snapshots.get(params[0]),)
        elif 'pg_try_advisory_lock' in query:
            self.result = (self.conn.lock,)
        elif 'pending_attestations_20261006' in query:
            rows = [(row,) for row in self.conn.pending.get(params[0], [])
                    if params[2] is None or (isinstance(row, dict)
                                              and row.get('calendar_row_id', '') > params[2])]
            self.result = rows if self.conn.over_bound else rows[:params[1]]
        else:
            raise AssertionError('unexpected database access')

    def fetchone(self):
        return self.result

    def fetchmany(self, count):
        return self.result[:count]


class Connection:
    def __init__(self, pending=None, role=None, lock=True, over_bound=False,
                 eligible=None, snapshots=None):
        self.pending = pending or {}
        self.eligible = eligible or {}
        self.snapshots = snapshots or {}
        self.role = role or worker.guard.ROLE
        self.lock = lock
        self.over_bound = over_bound
        self.calls = []
        self.closed = False
        self.commits = 0

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_disabled_never_connects_or_reads_credentials(monkeypatch):
    monkeypatch.setenv(worker.WORKER_ENV, 'false')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', SECRET)
    result = worker.run_once(connection_factory=lambda: pytest.fail('must not connect'))
    assert result == {'status': 'disabled', 'rows': []}


@pytest.mark.parametrize('overrides,reason', [
    ({worker.TENANTS_ENV: ''}, 'explicit_tenant_allowlist_required'),
    ({worker.TENANTS_ENV: 'pierce; select secret'}, 'explicit_tenant_allowlist_required'),
    ({'AGENT_FORWARD_MEDIA_ATTESTER_ROLE': 'service_role'}, 'dedicated_attester_configuration_required'),
    ({'AGENT_FORWARD_MEDIA_ATTESTER_DSN': ''}, 'dedicated_attester_configuration_required'),
    ({'SUPABASE_SERVICE_ROLE_KEY': SECRET}, 'publisher_or_service_credentials_present'),
    ({'ZERNIO_API_KEY': SECRET}, 'publisher_or_service_credentials_present'),
    ({'AGENT_FORWARD_MEDIA_ATTESTER_BATCH_SIZE': '101'}, 'worker_bounds_invalid'),
    ({'AGENT_FORWARD_MEDIA_ATTESTER_INTERVAL_SECONDS': '0'}, 'worker_bounds_invalid'),
])
def test_configuration_holds_before_database_access(monkeypatch, overrides, reason):
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    result = worker.run_once(connection_factory=lambda: pytest.fail('must not connect'))
    assert result['status'] == 'hold'
    assert result['reason'] == reason
    assert SECRET not in json.dumps(result)


def test_production_dispatch_has_no_callback_or_connection_override(monkeypatch):
    conns = []
    calls = []
    def connect():
        conn = Connection({'pierce': [snapshot()], 'gritx': []})
        conns.append(conn)
        return conn
    def attest(row_id, expected_revision):
        calls.append((row_id, expected_revision))
        return {'evidence_id': EVIDENCE, 'revision': expected_revision}
    monkeypatch.setattr(worker.guard, '_connect', connect)
    monkeypatch.setattr(worker.guard, 'attest', attest)
    result = worker.run_once()
    assert calls == [(ROW, REVISION)]
    assert result['status'] == 'complete'
    assert result['rows'][0]['status'] == 'attested'
    assert result['rows'][0]['evidence_id'] == EVIDENCE
    assert all(conn.closed for conn in conns)
    assert all(conn.commits == 1 for conn in conns)
    queries = [q for conn in conns for q, _ in conn.calls]
    assert all('content_calendar' not in q for q in queries)
    assert SECRET not in json.dumps(result)


def test_wrong_role_and_busy_tenant_never_attest():
    conns = iter([Connection(role='service_role'), Connection(lock=False)])
    result = worker.run_once(connection_factory=lambda: next(conns),
                             attest_fn=lambda *_: pytest.fail('must not attest'))
    assert result['status'] == 'partial_hold'
    assert result['tenants'][0]['reason'] == 'attester_role_mismatch'
    assert result['tenants'][1]['status'] == 'busy'


def test_per_row_hold_continues_and_does_not_log_exception_text(monkeypatch):
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce')
    conn = Connection({'pierce': [snapshot(), snapshot(OTHER_ROW)]})
    calls = []
    def attest(row, revision):
        calls.append(row)
        if row == ROW:
            raise worker.guard.ForwardMediaVerificationHold(SECRET)
        return {'evidence_id': EVIDENCE, 'revision': revision}
    result = worker.run_once(connection_factory=lambda: conn, attest_fn=attest)
    assert calls == [ROW, OTHER_ROW]
    assert result['status'] == 'partial_hold'
    assert [row['status'] for row in result['rows']] == ['hold', 'attested']
    assert result['rows'][0]['reason'] == 'verification_hold'
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize('bad', [snapshot(tenant='other'), snapshot(row=SECRET),
                                  snapshot(revision=SECRET), {}, 'not-a-row'])
def test_malformed_or_cross_tenant_rows_fail_closed(bad, monkeypatch):
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce')
    result = worker.run_once(connection_factory=lambda: Connection({'pierce': [bad]}),
                             attest_fn=lambda *_: pytest.fail('must not attest'))
    assert result['rows'][0]['status'] == 'hold'
    assert SECRET not in json.dumps(result)


def test_duplicate_discovery_and_repeated_pass_do_not_replay_committed_proof(monkeypatch):
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce')
    pending = {'pierce': [snapshot(), snapshot()]}
    calls = []
    def attest(row, revision):
        calls.append((row, revision))
        pending['pierce'] = []  # SQL discovery removes the committed revision
        return {'evidence_id': EVIDENCE, 'revision': revision}
    connect = lambda: Connection(pending)
    first = worker.run_once(connection_factory=connect, attest_fn=attest)
    second = worker.run_once(connection_factory=connect, attest_fn=attest)
    assert calls == [(ROW, REVISION)]
    assert first['rows'][1]['reason'] == 'duplicate_pending_revision'
    assert second['rows'] == []


def test_uncertain_commit_is_not_retried_from_a_local_queue(monkeypatch):
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce')
    pending = {'pierce': [snapshot()]}
    calls = []
    def attest(row, revision):
        calls.append(row)
        pending['pierce'] = []
        raise OSError(SECRET)  # proof landed, client lost its response
    first = worker.run_once(connection_factory=lambda: Connection(pending), attest_fn=attest)
    second = worker.run_once(connection_factory=lambda: Connection(pending), attest_fn=attest)
    assert first['rows'][0]['status'] == 'hold'
    assert second['rows'] == []
    assert calls == [ROW]
    assert SECRET not in json.dumps(first)


def test_batch_bound_and_rotation(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_BATCH_SIZE', '1')
    queried = []
    def connect():
        conn = Connection({'pierce': [snapshot()], 'gritx': [snapshot(tenant='gritx')]})
        queried.append(conn)
        return conn
    attest = lambda row, revision: {'evidence_id': EVIDENCE, 'revision': revision}
    first = worker.run_once(connection_factory=connect, attest_fn=attest)
    second = worker.run_once(connection_factory=connect, attest_fn=attest, tenant_offset=1)
    assert len(first['rows']) == len(second['rows']) == 1
    assert first['rows'][0]['tenant'] == 'pierce'
    assert second['rows'][0]['tenant'] == 'gritx'


def test_held_row_does_not_starve_later_revision_across_passes(monkeypatch):
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_BATCH_SIZE', '1')
    pending = {'pierce': [snapshot(), snapshot(OTHER_ROW)]}
    cursors = {}
    calls = []
    def attest(row, revision):
        calls.append(row)
        if row == ROW:
            raise worker.guard.ForwardMediaVerificationHold('not ready')
        return {'evidence_id': EVIDENCE, 'revision': revision}
    connect = lambda: Connection(pending)
    first = worker.run_once(connection_factory=connect, attest_fn=attest, cursors=cursors)
    second = worker.run_once(connection_factory=connect, attest_fn=attest, cursors=cursors)
    third = worker.run_once(connection_factory=connect, attest_fn=attest, cursors=cursors)
    assert calls == [ROW, OTHER_ROW]
    assert first['rows'][0]['status'] == 'hold'
    assert second['rows'][0]['status'] == 'attested'
    assert third['rows'] == []
    assert cursors == {}


def test_pending_rpc_exceeding_requested_bound_holds_entire_tenant(monkeypatch):
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce')
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_BATCH_SIZE', '1')
    result = worker.run_once(connection_factory=lambda: Connection({'pierce': [snapshot(), snapshot(OTHER_ROW)]}, over_bound=True),
                             attest_fn=lambda *_: pytest.fail('must not attest'))
    assert result['tenants'][0]['reason'] == 'pending_batch_bound_exceeded'
    assert result['rows'] == []


def test_settings_cannot_expand_environment_allowlist():
    result = worker.run_once(settings=worker.Settings(('not-configured',)),
                             connection_factory=lambda: pytest.fail('must not connect'))
    assert result['reason'] == 'tenant_outside_configured_allowlist'


def test_background_loop_stops_when_disabled(monkeypatch):
    monkeypatch.setenv(worker.WORKER_ENV, 'false')
    logs = []
    worker.run_forever(logger=logs.append)
    assert json.loads(logs[0])['status'] == 'disabled'


def test_attester_invalid_revision_response_is_hold(monkeypatch):
    monkeypatch.setenv(worker.TENANTS_ENV, 'pierce')
    result = worker.run_once(connection_factory=lambda: Connection({'pierce': [snapshot()]}),
                             attest_fn=lambda *_: {'evidence_id': EVIDENCE, 'revision': 'b' * 32})
    assert result['rows'][0]['status'] == 'hold'
    assert 'evidence_id' not in result['rows'][0]


def test_discovery_exception_is_redacted():
    def connect():
        raise OSError(SECRET)
    result = worker.run_once(connection_factory=connect)
    assert result['status'] == 'partial_hold'
    assert all(t['reason'] == 'discovery_unavailable' for t in result['tenants'])
    assert SECRET not in json.dumps(result)


def test_background_loop_rotates_allowlist_and_waits_at_bounded_interval(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_ATTESTER_BATCH_SIZE', '1')
    passes = []
    logs = []
    class Stop:
        waits = []
        def is_set(self):
            return len(self.waits) >= 2
        def wait(self, seconds):
            self.waits.append(seconds)
    stop = Stop()
    def run_once(**kwargs):
        passes.append(kwargs['tenant_offset'])
        return {'status': 'complete', 'rows': []}
    monkeypatch.setattr(worker, 'run_once', run_once)
    worker.run_forever(stop=stop, logger=logs.append)
    assert passes == [0, 1]
    assert stop.waits == [60, 60]
    assert all(json.loads(log)['status'] == 'complete' for log in logs)


def test_cli_once_configuration_hold_has_nonzero_exit_and_no_secret(monkeypatch, capsys):
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', SECRET)
    assert worker.main(['--once']) == 1
    output = capsys.readouterr().out
    assert json.loads(output)['reason'] == 'publisher_or_service_credentials_present'
    assert SECRET not in output


# --------------------------------------------------------------------------
# Staged preparation + missing-visual-role recovery lanes (DRAFT 20261008).
# Authorization comes ONLY from the SQL predicate; the stage marker alone is
# never consulted (the worker never reads content_calendar directly).
# --------------------------------------------------------------------------

BATCH = str(uuid.UUID(int=9))


def eligible_result(eligible=True, mode='staged', tenant='pierce', batch=BATCH,
                    reason=None):
    return {'eligible': eligible, 'mode': mode, 'tenant_id': tenant,
            'batch_id': batch, 'reason': reason}


def staged_conn(**kwargs):
    kwargs.setdefault('eligible', {ROW: eligible_result()})
    kwargs.setdefault('snapshots', {ROW: snapshot()})
    return Connection(**kwargs)


def staged_settings(rows=(ROW,), recoveries=()):
    return worker.Settings(('pierce',), staged_rows=rows, recoveries=recoveries)


def attest_ok(row_id, revision):
    return {'evidence_id': EVIDENCE, 'revision': revision}


def test_staged_member_attests_with_sql_predicate_authorization():
    conn = staged_conn()
    result = worker.run_once(settings=staged_settings(),
                             connection_factory=lambda: conn, attest_fn=attest_ok)
    row = result['rows'][0]
    assert row['status'] == 'attested' and row['mode'] == 'staged'
    assert row['batch_id'] == BATCH and row['evidence_id'] == EVIDENCE
    queries = [q for q, _ in conn.calls]
    assert any('forward_schedule_preparation_eligible_20261008' in q for q in queries)
    assert all('content_calendar' not in q for q in queries)
    assert result['status'] == 'complete'


def test_predicate_ineligible_or_marker_only_never_attests():
    for outcome in (eligible_result(eligible=False, mode=None, reason='not a staged schedule candidate'),
                    eligible_result(eligible=True, mode='active', batch=None),
                    eligible_result(eligible=False, mode=None, batch=None, reason='unregistered staged row')):
        conn = staged_conn(eligible={ROW: outcome})
        result = worker.run_once(settings=staged_settings(),
                                 connection_factory=lambda: conn,
                                 attest_fn=lambda *_: pytest.fail('must not attest'))
        assert result['rows'][0]['status'] == 'hold'
        assert result['rows'][0]['reason'] == 'preparation_ineligible'
        assert result['status'] == 'partial_hold'


def test_staged_tenant_outside_allowlist_never_attests():
    conn = staged_conn(eligible={ROW: eligible_result(tenant='other')})
    result = worker.run_once(settings=staged_settings(),
                             connection_factory=lambda: conn,
                             attest_fn=lambda *_: pytest.fail('must not attest'))
    assert result['rows'][0]['reason'] == 'preparation_ineligible'


def test_malformed_predicate_result_holds_without_attesting():
    for bad in (None, {'eligible': 'yes'}, {'eligible': True, 'mode': None},
                {'eligible': False, 'mode': 'staged'}, 'junk'):
        conn = staged_conn(eligible={ROW: bad})
        result = worker.run_once(settings=staged_settings(),
                                 connection_factory=lambda: conn,
                                 attest_fn=lambda *_: pytest.fail('must not attest'))
        assert result['rows'][0]['status'] == 'hold'
        assert result['rows'][0]['reason'] == 'attestation_unavailable_or_invalid'


def test_staged_terminal_batch_or_changed_binding_holds():
    outcome = eligible_result(eligible=False, mode=None, reason='content/media binding changed')
    conn = staged_conn(eligible={ROW: outcome})
    result = worker.run_once(settings=staged_settings(),
                             connection_factory=lambda: conn,
                             attest_fn=lambda *_: pytest.fail('must not attest'))
    assert result['rows'][0]['reason'] == 'preparation_ineligible'
    assert result['rows'][0]['sql_reason'] == 'content/media binding changed'


def test_staged_revision_mismatch_from_attester_holds():
    conn = staged_conn()
    result = worker.run_once(settings=staged_settings(), connection_factory=lambda: conn,
                             attest_fn=lambda *_: {'evidence_id': EVIDENCE, 'revision': 'b' * 32})
    assert result['rows'][0]['reason'] == 'attestation_unavailable_or_invalid'


@pytest.mark.parametrize('env_value', ['not-a-uuid', ROW + ',' + ROW, ROW + ':extra'])
def test_staged_rows_configuration_bounds_fail_closed(env_value):
    with pytest.raises(worker.WorkerConfigurationHold, match='worker_bounds_invalid'):
        worker.settings_from_environment({
            'AGENT_FORWARD_MEDIA_ATTESTER_DSN': 'postgresql://dedicated-attester',
            'AGENT_FORWARD_MEDIA_ATTESTER_ROLE': worker.guard.ROLE,
            worker.TENANTS_ENV: 'pierce',
            worker.STAGED_ROWS_ENV: env_value})


def test_recovery_requires_armed_visual_index():
    conn = staged_conn()
    settings = staged_settings(rows=(), recoveries=((ROW, EVIDENCE),))
    result = worker.run_once(settings=settings, connection_factory=lambda: conn,
                             recover_fn=lambda *_: pytest.fail('must not recover'))
    assert result['rows'][0]['reason'] == 'visual_index_disabled'


def test_recovery_reuses_same_lineage_and_appends_missing_roles(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '1')
    ids = {role: str(uuid.uuid4()) for role in ('original', 'delivered', 'thumbnail')}
    calls = []
    def recover(row_id, revision, lineage, *, tenant_key=None):
        calls.append((row_id, revision, lineage, tenant_key))
        return {'attestation_ids': ids, 'tenant_key': 'pierce',
                'appended': ['thumbnail'], 'recovered': True}
    conn = staged_conn(eligible={ROW: eligible_result(mode='active', batch=None)})
    settings = staged_settings(rows=(), recoveries=((ROW, EVIDENCE),))
    result = worker.run_once(settings=settings, connection_factory=lambda: conn,
                             recover_fn=recover)
    assert calls == [(ROW, REVISION, EVIDENCE, 'pierce')]
    row = result['rows'][0]
    assert row['status'] == 'recovered' and row['appended'] == ['thumbnail']
    assert row['mode'] == 'active'


def test_recovery_ineligible_row_never_touches_lineage(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '1')
    conn = staged_conn(eligible={ROW: eligible_result(eligible=False, mode=None)})
    settings = staged_settings(rows=(), recoveries=((ROW, EVIDENCE),))
    result = worker.run_once(settings=settings, connection_factory=lambda: conn,
                             recover_fn=lambda *_: pytest.fail('must not recover'))
    assert result['rows'][0]['reason'] == 'preparation_ineligible'


def test_recovery_malformed_result_holds_without_claiming(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_VISUAL_INDEX', '1')
    conn = staged_conn(eligible={ROW: eligible_result(mode='active', batch=None)})
    settings = staged_settings(rows=(), recoveries=((ROW, EVIDENCE),))
    result = worker.run_once(settings=settings, connection_factory=lambda: conn,
                             recover_fn=lambda *_: {'attestation_ids': {'original': EVIDENCE},
                                                    'tenant_key': 'pierce'})
    assert result['rows'][0]['reason'] == 'attestation_unavailable_or_invalid'


def test_recovery_pairs_configuration_bounds_fail_closed():
    with pytest.raises(worker.WorkerConfigurationHold, match='worker_bounds_invalid'):
        worker.settings_from_environment({
            'AGENT_FORWARD_MEDIA_ATTESTER_DSN': 'postgresql://dedicated-attester',
            'AGENT_FORWARD_MEDIA_ATTESTER_ROLE': worker.guard.ROLE,
            worker.TENANTS_ENV: 'pierce',
            worker.RECOVER_ROWS_ENV: ROW})


def test_staged_lane_wrong_role_holds_everything():
    conn = staged_conn(role='service_role')
    result = worker.run_once(settings=staged_settings(), connection_factory=lambda: conn,
                             attest_fn=lambda *_: pytest.fail('must not attest'))
    assert result['rows'] == []
    assert result['tenants'][-1]['reason'] == 'attester_role_mismatch'
    assert result['status'] == 'partial_hold'
