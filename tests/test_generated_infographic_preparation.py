import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest

from agent import generated_infographic_preparation as prep, media_host, config
from agent import client_infographic_fill as fill


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'hosting_enabled', lambda: True)
    monkeypatch.setattr(config, 'S3_PUBLIC_BASE_URL', 'https://images.example.test')
    request = dict(gym_id='same-gym', local_date='2026-10-08',
                   logical_post_id=str(uuid.uuid4()), copy_revision='copy-v1',
                   palette_revision='palette-v1', inventory_revision='inventory-v1',
                   history_revision='history-v1')
    snapshot = {**request, 'photo_inventory_complete': True, 'eligible_photo_count': 0,
                'history_complete': True, 'copy_approved': True,
                'copy': dict(headline='Build strength', facts=['Practice with guidance'],
                             cta='Start today', footer='gym.example.test'),
                'palette': dict(gym_id='same-gym', verified=True,
                                evidence_ref='approved-brand-record-v1',
                                colors=['#112233', '#44AA77'])}
    output = io.BytesIO()
    Image.new('RGB', (1024, 1280), '#112233').save(output, format='PNG')
    data = output.getvalue()
    class Provider:
        calls = 0
        def create(self, brief, job_id):
            self.calls += 1
            assert 'LASSO' not in json.dumps(snapshot['copy'])
            assert '#112233' in brief
            return dict(id='resp_original', model='gpt-6-astra', status='completed',
                        metadata={'echo_generation_job_id': job_id},
                        output=[dict(type='image_generation_call', id='ig_original',
                                     result=base64.b64encode(data).decode())])
    class Reviewer:
        calls = 0
        response_id = 'resp_review'
        def ask_image(self, observed, question):
            self.calls += 1
            assert observed == data
            assert '#112233' in question
            return json.dumps(dict(scores=dict(prep_scores()), copy_complete=True,
                                   copy_accurate=True, placement_safe=True,
                                   placement_violations=[], issues=[], palette_matches=True))
    class Storage:
        objects = None
        calls = 0
        def __init__(self): self.objects = {}
        def put_bytes_if_absent(self, key, observed):
            self.calls += 1
            if key in self.objects: raise RuntimeError('precondition')
            self.objects[key] = observed
        def get_bytes(self, key): return self.objects.get(key)
    return SimpleNamespace(request=request, snapshot=snapshot, data=data,
                           jobs=prep.SQLiteGenerationJobs(tmp_path/'jobs.sqlite'),
                           provider=Provider(), reviewer=Reviewer(), storage=Storage())


def prep_scores():
    return dict(concept=20, hierarchy=20, readability=20, craft=15, originality=15, integration=10)


def run(c, **kw):
    return prep.prepare_candidate(c.request, c.snapshot, jobs=c.jobs, provider=c.provider,
                                  reviewer=c.reviewer, storage=c.storage, enabled=True, **kw)


def test_fresh_original_preserved_replay_and_payload(case):
    first = run(case)
    assert first['ok']
    candidate = first['candidate']
    assert prep.validate_candidate(candidate, case.data) == candidate
    assert case.storage.objects[candidate['storage_key']] == case.data
    assert candidate['source_type'] == 'generated_astra_infographic'
    assert 'needs_client_safe_review' not in json.dumps(candidate)
    assert run(case) == first
    assert case.provider.calls == case.reviewer.calls == 1


def test_different_date_is_new_job(case):
    a = run(case)['candidate']
    case.request['local_date'] = case.snapshot['local_date'] = '2026-10-09'
    case.request['logical_post_id'] = case.snapshot['logical_post_id'] = str(uuid.uuid4())
    b = run(case)['candidate']
    assert a['job_id'] != b['job_id'] and case.provider.calls == 2
    # Deliberately identical provider pixels remain the same byte identity.
    # The final owner must refuse cross-date reservation, never mint by URL/date.
    assert a['original_sha256'] == b['original_sha256']


@pytest.mark.parametrize('field,value,reason', [
    ('photo_inventory_complete', False, 'generated_photo_inventory_uncertain'),
    ('eligible_photo_count', None, 'generated_photo_inventory_uncertain'),
    ('eligible_photo_count', False, 'generated_photo_inventory_uncertain'),
    ('eligible_photo_count', 1, 'generated_photo_available'),
    ('history_complete', False, 'generated_history_uncertain'),
    ('copy_approved', False, 'generated_copy_unapproved'),
    ('palette_revision', 'changed', 'generated_snapshot_binding_changed'),
])
def test_uncertain_evidence_blocks_before_provider(case, field, value, reason):
    case.snapshot[field] = value
    assert run(case)['reason'] == reason
    assert case.provider.calls == 0


@pytest.mark.parametrize('change', ['url-only','cross-gym','bad-colors','no-evidence'])
def test_palette_not_guessable(case, change):
    p = case.snapshot['palette']
    if change == 'url-only': p.update(verified=False, source_url='https://gym.example.test')
    if change == 'cross-gym': p['gym_id'] = 'other-gym'
    if change == 'bad-colors': p['colors'] = ['navy']
    if change == 'no-evidence': p['evidence_ref'] = ''
    assert run(case)['reason'] == 'generated_palette_unverified'
    assert case.provider.calls == 0


def test_bad_copy_style_never_rewritten(case):
    case.snapshot['copy']['headline'] = 'Build strength: today'
    assert run(case)['reason'] == 'generated_copy_style_invalid'
    assert case.provider.calls == 0


def test_disabled_has_no_external_effect(case):
    out = prep.prepare_candidate(case.request, case.snapshot, jobs=None,
                                 provider=None, reviewer=None, enabled=False)
    assert out['reason'] == 'generated_preparation_disabled'
    assert case.provider.calls == 0


def test_ambiguous_provider_failure_never_regenerates(case):
    def failure(*args):
        case.provider.calls += 1
        raise TimeoutError('private provider body')
    case.provider.create = failure
    assert run(case)['reason'] == 'generated_preparation_unavailable'
    assert run(case)['reason'] == 'generated_execution_pending_reconciliation'
    assert case.provider.calls == 1


def test_concurrent_job_claim_has_one_new_worker(case):
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: case.jobs.start('job', {'v': 1})['state'], range(2)))
    assert sorted(states) == ['generating', 'new']


def test_binding_collision_held(case):
    case.jobs.start('job', {'v': 1})
    with pytest.raises(prep.PreparationHold, match='binding_changed'):
        case.jobs.start('job', {'v': 2})


def test_changed_remote_bytes_invalidate_replay(case):
    candidate = run(case)['candidate']
    case.storage.objects[candidate['storage_key']] = b'changed'
    assert run(case)['reason'] == 'generated_storage_readback_failed'
    assert case.provider.calls == 1


def test_review_can_resume_same_original(case):
    original = case.reviewer.ask_image
    case.reviewer.ask_image = lambda *a: '{}'
    assert run(case)['reason'] == 'generated_automated_review_failed'
    assert not case.storage.objects
    case.reviewer.ask_image = original
    assert run(case)['ok']
    assert case.provider.calls == 1


def test_palette_review_required(case):
    original = case.reviewer.ask_image
    case.reviewer.ask_image = lambda *a: original(*a).replace('"palette_matches": true', '"palette_matches": false')
    assert run(case)['reason'] == 'generated_automated_review_failed'
    assert not case.storage.objects


@pytest.mark.parametrize('change', ['model', 'metadata', 'output', 'bytes'])
def test_provider_response_requires_actual_fresh_astra_inline_original(case, change):
    original = case.provider.create
    def bad(brief, job_id):
        out = original(brief, job_id)
        if change == 'model': out['model'] = 'gemini'
        if change == 'metadata': out['metadata'] = {}
        if change == 'output': out['output'] *= 2
        if change == 'bytes': out['output'][0]['result'] = base64.b64encode(b'bad').decode()
        return out
    case.provider.create = bad
    assert run(case)['reason'] == 'generated_original_invalid'
    assert not case.storage.objects


@pytest.mark.parametrize('field,value', [('original_sha256', '0'*64), ('original_phash', 'bad'),
                                        ('storage_key','other'), ('model','gemini'),
                                        ('local_date','2026-10-09'), ('width',True)])
def test_candidate_shape_and_byte_rebinding_refused(case, field, value):
    candidate = run(case)['candidate']
    candidate[field] = value
    with pytest.raises(prep.PreparationHold, match='candidate_invalid'):
        prep.validate_candidate(candidate, case.data)


def test_candidate_extra_authority_field_refused(case):
    candidate = run(case)['candidate']
    candidate['approved'] = True
    with pytest.raises(prep.PreparationHold): prep.validate_candidate(candidate)


def test_wrapper_photo_first_no_coach_or_calendar_side_effect(case, monkeypatch):
    monkeypatch.setattr(fill, 'real_media_status', lambda *a, **k: (fill.MEDIA_AVAILABLE, 'photo'))
    out = fill.prepare_verified_infographic_candidate(case.request, case.snapshot,
        jobs=case.jobs, provider=case.provider, reviewer=case.reviewer, storage=case.storage, enabled=True)
    assert out['reason'] == 'generated_photo_available' and case.provider.calls == 0
    monkeypatch.setattr(fill, 'real_media_status', lambda *a, **k: (fill.MEDIA_DEPLETED, 'complete'))
    out = fill.prepare_verified_infographic_candidate(case.request, case.snapshot,
        jobs=case.jobs, provider=case.provider, reviewer=case.reviewer, storage=case.storage, enabled=True)
    assert out['ok']


def test_astra_provider_raw_original_before_resize(case):
    def transport(url, headers, payload):
        assert payload['model'] == 'gpt-6-astra'
        assert payload['tools'][0]['action'] == 'generate'
        assert payload['tools'][0]['size'] == '1024x1280'
        return 200, json.dumps(case.provider.create('fresh #112233', payload['metadata']['echo_generation_job_id']))
    provider = prep.AstraOriginalProvider('never-return', transport=transport)
    response = provider.create('fresh brief', 'job')
    data, output = prep.original_bytes(response, 'job')
    assert data == case.data and output == 'ig_original'


def test_completed_job_survives_worker_restart(case):
    first = run(case)
    case.jobs = prep.SQLiteGenerationJobs(case.jobs.path)
    assert run(case) == first
    assert case.provider.calls == 1


def test_conditional_storage_required_and_no_overwrite_fallback(case):
    class MutableStorage:
        writes = 0
        def put(self, *a): self.writes += 1
        def get_bytes(self, *a): return case.data
    storage = MutableStorage()
    assert media_host.host_generated_original(case.data, 'same-gym', client=storage) is None
    assert storage.writes == 0


def test_snapshot_mutation_during_provider_cannot_rebind_copy(case):
    original = case.provider.create
    expected = prep.digest(case.snapshot['copy'])
    def mutate(brief, job_id):
        case.snapshot['copy']['headline'] = 'Unapproved change'
        return original(brief, job_id)
    case.provider.create = mutate
    assert run(case)['candidate']['copy_digest'] == expected


def test_review_cannot_reuse_generation_response(case):
    case.reviewer.response_id = 'resp_original'
    assert run(case)['reason'] == 'generated_automated_review_failed'
    assert not case.storage.objects


@pytest.mark.parametrize('status', [400, 429])
def test_definite_rejection_backoff_then_same_job_retry_succeeds(case, status):
    clock = [1000]
    case.jobs = prep.SQLiteGenerationJobs(case.jobs.path, clock=lambda: clock[0])
    calls, job_ids = [], []
    old_provider = case.provider
    def transport(url, headers, payload):
        calls.append(payload)
        job_ids.append(payload['metadata']['echo_generation_job_id'])
        if len(calls) == 1:
            return status, '{"error":"private body"}'
        return 200, json.dumps(old_provider.create('fresh #112233', job_ids[-1]))
    case.provider = prep.AstraOriginalProvider('never-return', transport=transport)
    assert run(case)['reason'] == 'generated_provider_rejected'
    assert run(case)['reason'] == 'generated_provider_retry_delayed'
    assert len(calls) == 1
    clock[0] += 60
    case.jobs = prep.SQLiteGenerationJobs(case.jobs.path, clock=lambda: clock[0])
    assert run(case)['ok']
    assert len(calls) == 2 and job_ids[0] == job_ids[1]
    assert run(case)['ok'] and len(calls) == 2


@pytest.mark.parametrize('status', [400, 429])
def test_definite_rejections_have_persisted_capped_backoff(case, status):
    clock, calls = [1000], []
    case.jobs = prep.SQLiteGenerationJobs(case.jobs.path, clock=lambda: clock[0])
    def rejected(url, headers, payload):
        calls.append(payload)
        return status, '{}'
    case.provider = prep.AstraOriginalProvider('never-return', transport=rejected)
    for index, advance in enumerate([60, 120, 240]):
        assert run(case)['reason'] == 'generated_provider_rejected'
        assert len(calls) == index + 1
        expected = 'generated_provider_retry_exhausted' if index == 2 else 'generated_provider_retry_delayed'
        assert run(case)['reason'] == expected
        assert len(calls) == index + 1
        clock[0] += advance
    case.jobs = prep.SQLiteGenerationJobs(case.jobs.path, clock=lambda: clock[0])
    assert run(case)['reason'] == 'generated_provider_retry_exhausted'
    assert len(calls) == prep.MAX_PROVIDER_ATTEMPTS


def test_definite_retry_claim_is_atomic_across_workers(case):
    clock = [1000]
    case.jobs = prep.SQLiteGenerationJobs(case.jobs.path, clock=lambda: clock[0])
    case.jobs.start('retry', {'v': 1})
    case.jobs.provider_rejected('retry')
    clock[0] += 60
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: case.jobs.start('retry', {'v': 1})['state'], range(2)))
    assert sorted(states) == ['generating', 'new']


@pytest.mark.parametrize('status', [500, 502, 503])
def test_ambiguous_http_failure_never_reexecutes(case, status):
    calls = []
    def ambiguous(url, headers, payload):
        calls.append(payload)
        return status, '{}'
    case.provider = prep.AstraOriginalProvider('never-return', transport=ambiguous)
    assert run(case)['reason'] == 'generated_provider_unavailable'
    assert run(case)['reason'] == 'generated_execution_pending_reconciliation'
    assert len(calls) == 1


@pytest.mark.parametrize('phash', [None, '', 0, False])
def test_shape_only_validator_requires_nonempty_canonical_phash(case, phash):
    candidate = run(case)['candidate']
    candidate['original_phash'] = phash
    with pytest.raises(prep.PreparationHold, match='generated_candidate_invalid'):
        prep.validate_candidate(candidate)


def test_existing_journal_upgrade_preserves_ambiguous_job(tmp_path):
    import sqlite3
    path = tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE generated_jobs (job_id TEXT PRIMARY KEY, binding TEXT NOT NULL, state TEXT NOT NULL, response TEXT, candidate TEXT)')
        con.execute('INSERT INTO generated_jobs(job_id,binding,state) VALUES (?,?,?)',
                    ('legacy', prep.canonical({'v': 1}), 'generating'))
    jobs = prep.SQLiteGenerationJobs(path)
    assert jobs.start('legacy', {'v': 1})['state'] == 'generating'
