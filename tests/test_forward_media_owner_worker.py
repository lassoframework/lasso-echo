"""Offline owner adapter boundary tests; no production transport or credentials."""
from contextlib import contextmanager
import hashlib
import io
import json

import pytest
from PIL import Image

from agent import forward_media_attester as attester
from agent import forward_media_owner as owner
from agent import forward_media_owner_worker as worker

ROW = '00000000-0000-0000-0000-000000000001'


class Reader:
    def __init__(self, objects):
        self.objects = objects

    def read(self, url):
        return self.objects[url]


class Persistence:
    def __init__(self):
        self.saved = []
        self.fail = False

    def _assert_owner_identity(self):
        pass

    def persist(self, *tuples):
        if self.fail:
            raise owner.UncertainCommitError('private DSN must not leak')
        replayed = tuples in self.saved
        if not replayed:
            self.saved.append(tuples)
        return {'replayed': replayed}


class Transport(worker.OwnerTransport):
    def __init__(self, current, candidate):
        self.current = current
        self.candidate = candidate
        self.records = {}
        self.history = True
        self.decision = 'hold_uncertain'
        self.locked = False
        self.record_ok = True
        self.pending_calls = 0

    def pending(self, tenants, limit):
        self.pending_calls += 1
        return [self.candidate]

    @contextmanager
    def locked_current(self, candidate):
        self.locked = True
        try:
            yield self.current
        finally:
            self.locked = False

    def verified_history(self, original):
        assert self.locked
        if not self.history:
            return None
        return {'original': original.row(), 'decision': self.decision,
                'history_evidence_ref': 'owner-reviewed-fleet-history',
                'production_evidence_ref': 'owner-reviewed-generation'}

    def record(self, candidate, outcome):
        assert self.locked
        key = tuple(candidate.values())
        # Fixture durably reuses an exactly identical successful outcome.
        self.records[key] = outcome
        return self.record_ok


@pytest.fixture
def lane(monkeypatch):
    for name in list(__import__('os').environ):
        if owner._FORBIDDEN_ENV_NAME.search(name) or name in worker._FORBIDDEN:
            monkeypatch.delenv(name)
    monkeypatch.setenv(worker.WORKER_ENV, 'true')
    monkeypatch.setenv(worker.TENANTS_ENV, 'gym')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_DSN', 'postgres://owner@example.invalid/x')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_ROLE', 'isolated_owner')
    buf = io.BytesIO()
    Image.new('RGB', (1200, 400), 'blue').save(buf, 'PNG')
    source = buf.getvalue()
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    image = attester.replay_still_recipe(source, recipe)['image_bytes']
    observation = {'schema_version': 1, 'tenant': 'gym', 'source_asset_id': 'asset',
                   'source_exact_url': 'https://host/source',
                   'delivered_exact_url': 'https://host/image', 'recipe': recipe,
                   'provenance_status': 'unverified', 'used_count': 0}
    digest = hashlib.sha256(json.dumps(observation, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False).encode()).hexdigest()
    observation['observation_digest'] = digest
    candidate = {'calendar_row_id': ROW, 'revision': 'a' * 32, 'observation_digest': digest}
    current = {'revision': 'a' * 32, 'observation': observation,
               'render_evidence_ref': 'owner-render-read',
               'calendar': {'id': ROW, 'gym_id': 'gym', 'status': 'pending',
                            'variant_status': 'active', 'post_date': '2026-10-09',
                            'visual_group_key': 'group', 'source_media_asset_id': 'asset',
                            'source_media_url': 'https://host/source', 'image_url': 'https://host/image'},
               'asset': {'id': 'asset', 'gym_id': 'gym', 'source_url': 'https://host/source',
                         'registry_evidence_ref': 'owner-verified-registry'}}
    return Transport(current, candidate), Persistence(), Reader({'https://host/source': source,
                                                               'https://host/image': image})


def execute(lane):
    transport, persistence, reader = lane
    return worker.run_adapter(transport=transport, persistence=persistence, reader=reader)


def test_default_off_never_discovers_or_constructs_transport(lane, monkeypatch):
    monkeypatch.delenv(worker.WORKER_ENV)
    assert execute(lane)['status'] == 'disabled'
    assert lane[0].pending_calls == 0
    assert worker.run_once()['status'] == 'disabled'


def test_production_factory_failure_is_static_and_has_no_fallback(lane, monkeypatch):
    def unavailable(**kwargs):
        raise owner.OwnerPersistenceError('private DSN must not leak')
    monkeypatch.setattr(owner.ForwardMediaOwnerPersistence, 'connect_from_environment', unavailable)
    assert worker.run_once() == {'status': 'hold', 'reason': 'owner_transport_unavailable', 'rows': []}


def test_exact_replay_and_hold_decision_are_persisted_under_lock(lane):
    first = execute(lane)
    assert first['status'] == 'complete'
    assert first['rows'][0]['decision'] == 'hold_uncertain'
    assert len(lane[1].saved) == 1
    assert execute(lane)['rows'][0]['replayed'] is True
    assert len(lane[1].saved) == 1
    assert len(lane[0].records) == 1


def test_explicit_verified_history_clearance_is_required(lane):
    lane[0].history = False
    assert execute(lane)['rows'][0]['reason'] == 'verified_byte_history_required'
    assert not lane[1].saved
    lane[0].history = True
    lane[0].decision = 'cleared_unused'
    assert execute(lane)['rows'][0]['decision'] == 'cleared_unused'


@pytest.mark.parametrize('field,value', [('gym_id', 'another'), ('source_media_asset_id', 'other'),
                                        ('published_at', '2026-10-01'), ('publish_claim_token', 'token'),
                                        ('late_post_id', 'submitted'), ('variant_status', 'archived'),
                                        ('render_manifest_digest', 'bound')])
def test_current_row_changes_hold_before_persistence(lane, field, value):
    lane[0].current['calendar'][field] = value
    assert execute(lane)['rows'][0]['status'] == 'hold'
    assert not lane[1].saved


def test_stale_revision_and_foreign_asset_hold(lane):
    lane[0].current['revision'] = 'b' * 32
    assert execute(lane)['rows'][0]['reason'] == 'canonical_revision_changed'
    lane[0].current['revision'] = 'a' * 32
    lane[0].current['asset']['gym_id'] = 'other'
    assert execute(lane)['rows'][0]['reason'] == 'canonical_tenant_asset_mismatch'
    assert not lane[1].saved


def test_source_and_derivative_mismatches_and_mutated_recipe_hold(lane):
    transport, persistence, reader = lane
    reader.objects['https://host/image'] = b'wrong delivered bytes'
    assert execute(lane)['rows'][0]['reason'] == 'render_bytes_mismatch'
    transport.current['observation']['recipe']['image']['name'] = 'identity'
    assert execute(lane)['rows'][0]['reason'] == 'candidate_canonical_binding_invalid'
    assert not persistence.saved


def test_thumbnail_without_observation_contract_holds(lane):
    lane[0].current['calendar']['thumbnail_url'] = 'https://host/thumb'
    assert execute(lane)['rows'][0]['reason'] == 'thumbnail_candidate_contract_missing'
    assert not lane[1].saved


@pytest.mark.parametrize('name', ['SUPABASE_SERVICE_ROLE_KEY', 'ZERNIO_API_KEY',
                                 'AGENT_FORWARD_MEDIA_ATTESTER_DSN'])
def test_mixed_credentials_stop_before_discovery(lane, monkeypatch, name):
    monkeypatch.setenv(name, 'private-secret')
    result = execute(lane)
    assert result['reason'] == 'owner_environment_invalid'
    assert lane[0].pending_calls == 0
    assert 'private-secret' not in json.dumps(result)


def test_uncertain_authority_commit_stops_without_reporting_persisted(lane):
    lane[1].fail = True
    result = execute(lane)
    assert result['reason'] == 'uncertain_authority_commit'
    assert result['rows'] == []
    assert lane[0].records == {}
    assert 'private DSN' not in json.dumps(result)


def test_progress_commit_failure_is_explicit_and_stops(lane):
    lane[0].record_ok = False
    result = execute(lane)
    assert result['reason'] == 'durable_progress_commit_unverified'
    assert result['rows'] == []
    assert len(lane[1].saved) == 1  # Must reconcile; no claim that nothing committed.


def test_batch_bounds_and_role_guard(lane, monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE', '101')
    assert execute(lane)['reason'] == 'worker_bounds_invalid'
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE', '1')
    monkeypatch.setenv('FORWARD_MEDIA_OWNER_ROLE', 'service_role')
    assert execute(lane)['reason'] == 'owner_environment_invalid'


def test_current_role_check_runs_before_candidate_discovery(lane):
    def denied():
        raise owner.OwnerPersistenceError('private-role-DSN')
    lane[1]._assert_owner_identity = denied
    result = execute(lane)
    assert result['reason'] == 'owner_transport_unavailable'
    assert lane[0].pending_calls == 0
    assert 'private-role-DSN' not in json.dumps(result)


def test_verified_history_must_bind_exact_bytes(lane):
    def wrong_history(original):
        different = dict(original.row(), source_fingerprint='md5:' + '0' * 32)
        return {'original': different, 'decision': 'cleared_unused',
                'history_evidence_ref': 'h', 'production_evidence_ref': 'p', 'verified': True}
    lane[0].verified_history = wrong_history
    assert execute(lane)['rows'][0]['reason'] == 'verified_byte_history_required'
    assert not lane[1].saved


def test_history_clearance_cannot_omit_production_receipt(lane):
    lane[0].verified_history = lambda original: {'original': original.row(),
                                               'decision': 'cleared_unused',
                                               'history_evidence_ref': 'h'}
    assert execute(lane)['rows'][0]['reason'] == 'fresh_receipts_required'
    assert not lane[1].saved


def test_discovery_cannot_exceed_bound(lane, monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_OWNER_BATCH_SIZE', '1')
    lane[0].pending = lambda *_: [lane[0].candidate, lane[0].candidate]
    assert execute(lane)['reason'] == 'candidate_batch_invalid'
    assert not lane[1].saved


def test_transport_errors_cannot_leak_credentials(lane):
    def unavailable(*args):
        raise worker.OwnerWorkerHold('postgres://private-secret')
    lane[0].pending = unavailable
    result = execute(lane)
    assert result['reason'] == 'owner_transport_unavailable'
    assert 'private-secret' not in json.dumps(result)


def test_source_mutation_after_byte_history_check_holds(lane):
    transport, persistence, reader = lane
    verify = transport.verified_history
    def change_after_history(original):
        result = verify(original)
        buf = io.BytesIO()
        Image.new('RGB', (1200, 400), 'red').save(buf, 'PNG')
        reader.objects['https://host/source'] = buf.getvalue()
        reader.objects['https://host/image'] = attester.replay_still_recipe(
            buf.getvalue(), transport.current['observation']['recipe'])['image_bytes']
        return result
    transport.verified_history = change_after_history
    assert execute(lane)['rows'][0]['reason'] == 'source_changed_after_history_verification'
    assert not persistence.saved


def test_malformed_commit_response_stops_for_reconciliation(lane):
    lane[1].persist = lambda *args: {'replayed': 'not-a-boolean'}
    result = execute(lane)
    assert result['reason'] == 'authority_commit_unverified'
    assert result['rows'] == []
    assert not lane[0].records


def test_durable_success_outcome_is_stable_across_authority_replay(lane):
    execute(lane)
    first = dict(next(iter(lane[0].records.values())))
    execute(lane)
    assert next(iter(lane[0].records.values())) == first
    assert 'replayed' not in first


def test_atomic_transport_uses_stage_and_reports_only_after_context_exit(lane):
    transport, persistence, reader = lane
    transport.atomic_authority_outcome = True
    calls = []
    old_lock = transport.locked_current
    @contextmanager
    def atomic_lock(candidate):
        with old_lock(candidate) as current:
            yield current
            calls.append('commit')
    transport.locked_current = atomic_lock
    def stage(candidate, actual_persistence, tuples):
        assert actual_persistence is persistence and transport.locked
        calls.append('authority')
        return {'replayed': False}
    transport.stage_authority = stage
    persistence.persist = lambda *args: pytest.fail('must not call independently committing persist')
    assert execute(lane)['rows'][0]['status'] == 'persisted'
    assert calls == ['authority','commit']


def test_atomic_commit_failure_never_reports_staged_success(lane):
    transport, persistence, reader = lane
    transport.atomic_authority_outcome = True
    old_lock = transport.locked_current
    @contextmanager
    def lost_commit(candidate):
        with old_lock(candidate) as current:
            yield current
            raise owner.UncertainCommitError('private DSN')
    transport.locked_current = lost_commit
    transport.stage_authority = lambda *args: {'replayed': False}
    result = execute(lane)
    assert result == {'status':'hold','reason':'uncertain_authority_commit','rows':[]}


def test_atomic_stage_error_is_never_converted_to_successful_hold(lane):
    transport, persistence, reader = lane
    transport.atomic_authority_outcome = True
    def failed(*args):
        raise RuntimeError('SQL transaction aborted private DSN')
    transport.stage_authority = failed
    result = execute(lane)
    assert result == {'status':'hold','reason':'authority_commit_unverified','rows':[]}
    assert not transport.records


def test_actual_asset_missing_source_receipt_holds_before_byte_read(lane):
    transport, persistence, reader = lane
    transport.current['asset'].pop('source_url')
    transport.current['asset']['used_count'] = 0
    reader.read = lambda *_: pytest.fail('missing source contract must hold before byte reads')
    assert execute(lane)['rows'][0]['reason'] == 'owner_asset_source_binding_missing'
    assert not persistence.saved


def test_real_persistence_rejects_boolean_only_transaction_claim(lane):
    transport, fake_persistence, reader = lane
    transport.atomic_authority_outcome = True
    real = owner.ForwardMediaOwnerPersistence(None, 'isolated_owner', reader)
    report = worker.run_adapter(transport=transport, persistence=real, reader=reader)
    assert report == {'status':'hold','reason':'owner_transaction_contract_required','rows':[]}
    assert transport.pending_calls == 0


def test_closed_local_cache_cannot_mutate_or_fall_back_to_remote():
    from dataclasses import FrozenInstanceError
    from agent.forward_media_owner_transport import FrozenObjectReader
    objects = {'https://host/source': b'exact bytes'}
    local = FrozenObjectReader(objects)
    objects['https://host/source'] = b'changed'
    assert local.read('https://host/source') == b'exact bytes'
    with pytest.raises(FrozenInstanceError):
        local.read = lambda url: b'remote replacement'
    with pytest.raises(TypeError):
        local._objects['https://host/source'] = b'changed'
    with pytest.raises(worker.OwnerWorkerHold, match='verified_local_byte_cache_required'):
        local.read('https://host/unverified')


def test_final_authority_stage_rejects_a_network_reader(lane):
    from agent.forward_media_owner_transport import DedicatedOwnerTransport
    persistence = owner.ForwardMediaOwnerPersistence(None,'isolated_owner',lane[2])
    transport = object.__new__(DedicatedOwnerTransport)
    transport.persistence = persistence
    transport._active = (*worker._identity(lane[0].candidate), 'token')
    transport._final_phase = True
    with pytest.raises(worker.OwnerWorkerHold, match='verified_local_byte_cache_required'):
        transport.stage_authority(lane[0].candidate,persistence,(),local_reader=lane[2])


# ---- staged lane (DRAFT worker discovery 20261008) -------------------------

STAGED_BATCH = '00000000-0000-0000-0000-0000000000b1'


def staged_lane(lane, monkeypatch, state=None):
    """Dedicated-transport fixture with the staged discovery/predicate seam."""
    from agent.forward_media_owner_transport import DedicatedOwnerTransport
    monkeypatch.setenv(worker.STAGED_ENV, 'true')
    persistence = owner.ForwardMediaOwnerPersistence(None, 'isolated_owner', lane[2])
    dedicated = object.__new__(DedicatedOwnerTransport)
    dedicated.persistence = persistence
    dedicated._conn = None
    dedicated._broken = False
    dedicated._active = None
    dedicated._recorded = False
    dedicated._final_phase = False
    candidate = dict(lane[0].candidate, batch_id=STAGED_BATCH, tenant_id='gym')
    calls = {'reserved': 0}
    dedicated.pending_staged = lambda tenants, limit: [candidate]
    dedicated.preparation_eligible = lambda row_id: (
        state if state is not None else
        {'eligible': True, 'mode': 'staged', 'tenant_id': 'gym',
         'batch_id': STAGED_BATCH, 'reason': None})

    def forbidden_reserve(_candidate):
        calls['reserved'] += 1
        raise AssertionError('unauthorized staged candidate must never be reserved')
    dedicated.reserved_current = forbidden_reserve
    return dedicated, persistence, lane[2], candidate, calls


def test_staged_pass_is_default_off(lane, monkeypatch):
    transport, persistence, reader = lane
    monkeypatch.delenv(worker.WORKER_ENV, raising=False)
    monkeypatch.delenv(worker.STAGED_ENV, raising=False)
    report = worker.run_staged_pass(transport=transport, persistence=persistence, reader=reader)
    assert report == {'status': 'disabled', 'rows': []}
    assert transport.pending_calls == 0


def test_staged_pass_requires_dedicated_shared_transaction(lane, monkeypatch):
    transport, _, reader = lane
    monkeypatch.setenv(worker.STAGED_ENV, 'true')
    real = owner.ForwardMediaOwnerPersistence(None, 'isolated_owner', reader)
    report = worker.run_staged_pass(transport=transport, persistence=real, reader=reader)
    assert report == {'status': 'hold', 'reason': 'owner_transaction_contract_required', 'rows': []}
    assert transport.pending_calls == 0


def test_staged_marker_alone_never_authorizes(lane, monkeypatch):
    # Predicate says the row is not eligible: forged/unregistered marker,
    # terminal batch and drifted binding all collapse to the same static hold
    # and no reservation or progress write ever happens.
    for state in ({'eligible': False, 'mode': None, 'tenant_id': 'gym',
                   'batch_id': None, 'reason': 'unregistered staged row'},
                  {'eligible': False, 'mode': None, 'tenant_id': 'gym',
                   'batch_id': STAGED_BATCH, 'reason': 'stage batch terminal or unavailable'},
                  {'eligible': False, 'mode': None, 'tenant_id': 'gym',
                   'batch_id': STAGED_BATCH, 'reason': 'content/media binding changed'}):
        dedicated, persistence, reader, candidate, calls = staged_lane(lane, monkeypatch, state=state)
        report = worker.run_staged_pass(transport=dedicated, persistence=persistence, reader=reader)
        assert report['rows'] == [{'calendar_row_id': ROW, 'revision': 'a' * 32,
                                   'status': 'hold', 'reason': 'staged_preparation_not_eligible'}]
        assert report['status'] == 'partial_hold'
        assert calls['reserved'] == 0


def test_staged_active_mode_and_tenant_or_batch_mismatch_hold(lane, monkeypatch):
    # An ordinary ACTIVE row (mode 'active'), a foreign canonical tenant and a
    # batch readback mismatch must never be admitted by the staged lane.
    for state in ({'eligible': True, 'mode': 'active', 'tenant_id': 'gym',
                   'batch_id': None, 'reason': None},
                  {'eligible': True, 'mode': 'staged', 'tenant_id': 'other-tenant',
                   'batch_id': STAGED_BATCH, 'reason': None},
                  {'eligible': True, 'mode': 'staged', 'tenant_id': 'gym',
                   'batch_id': '00000000-0000-0000-0000-0000000000b2', 'reason': None}):
        dedicated, persistence, reader, candidate, calls = staged_lane(lane, monkeypatch, state=state)
        report = worker.run_staged_pass(transport=dedicated, persistence=persistence, reader=reader)
        assert report['rows'][0]['reason'] == 'staged_preparation_not_eligible'
        assert calls['reserved'] == 0


def test_staged_candidate_batch_identity_must_parse(lane, monkeypatch):
    dedicated, persistence, reader, candidate, calls = staged_lane(lane, monkeypatch)
    dedicated.pending_staged = lambda tenants, limit: [dict(candidate, batch_id='not-a-uuid')]
    report = worker.run_staged_pass(transport=dedicated, persistence=persistence, reader=reader)
    assert report['rows'][0]['reason'] == 'staged_candidate_batch_invalid'
    assert calls['reserved'] == 0


def test_staged_authorized_candidate_proceeds_to_quarantine(lane, monkeypatch):
    dedicated, persistence, reader, candidate, calls = staged_lane(lane, monkeypatch)
    def halted(_candidate):
        calls['reserved'] += 1
        raise worker.OwnerWorkerHold('owner_manual_reconciliation_required')
    dedicated.reserved_current = halted
    report = worker.run_staged_pass(transport=dedicated, persistence=persistence, reader=reader)
    assert calls['reserved'] == 1
    assert report == {'status': 'hold', 'reason': 'owner_manual_reconciliation_required', 'rows': []}


def test_staged_candidate_context_binds_marker_tenant_and_state(lane):
    transport, _, _ = lane
    candidate = transport.candidate
    current = json.loads(json.dumps(transport.current))
    row = current['calendar']
    row['variant_status'] = 'candidate'
    row['media_not_ready_reason'] = 'forward_reservation_staged'
    row['status'] = 'pending'
    context = worker._candidate_context(candidate, current, ('gym',), mode='staged',
                                        canonical_tenant='gym')
    assert context[0]['id'] == ROW
    # Marker drift, active variant, approved status or an unallowlisted
    # canonical tenant all hold; Python never overrides the SQL predicate.
    for mutate, tenant in (
            (lambda r: r.update(media_not_ready_reason=None), 'gym'),
            (lambda r: r.update(variant_status='active'), 'gym'),
            (lambda r: r.update(status='approved'), 'gym'),
            (lambda r: None, 'other-tenant')):
        tampered = json.loads(json.dumps(current))
        mutate(tampered['calendar'])
        with pytest.raises(worker.OwnerWorkerHold):
            worker._candidate_context(candidate, tampered, ('gym',), mode='staged',
                                      canonical_tenant=tenant)


# ---- running loop (bounded interval, stop-event controlled) ----------------

def test_run_forever_repeats_bounded_passes_until_stopped(monkeypatch):
    import threading
    stop = threading.Event()
    calls = []

    def fake_once():
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        return {'status': 'disabled', 'rows': []}

    monkeypatch.setenv('AGENT_FORWARD_MEDIA_OWNER_INTERVAL_SECONDS', '5')
    monkeypatch.setattr(worker, 'run_once', fake_once)
    worker.run_forever(stop=stop, sleep=lambda _s: None)
    assert len(calls) == 3


def test_run_forever_rejects_out_of_bounds_interval(monkeypatch):
    monkeypatch.setenv('AGENT_FORWARD_MEDIA_OWNER_INTERVAL_SECONDS', '1')
    with pytest.raises(worker.OwnerWorkerHold, match='worker_bounds_invalid'):
        worker.run_forever(sleep=lambda _s: None)


def test_main_loop_is_default_off(monkeypatch, capsys):
    monkeypatch.delenv(worker.WORKER_ENV, raising=False)
    assert worker.main(['--loop']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'disabled'
