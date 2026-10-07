"""Runtime integration checks use offline provider/storage/DB fixtures only."""
import copy
import hashlib
from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from agent import generated_infographic_runtime as runtime
from agent import generated_infographic_preparation as prep
from agent import forward_media_guard as guard, forward_media_owner as owner
from test_generated_infographic_preparation import case

REAL_RESERVE_GENERATED = guard.reserve_generated


class Conn:
    autocommit = False
    def __init__(self):
        self.events = []
        self.info = SimpleNamespace(transaction_status=0)
    def rollback(self): self.events.append('rollback')
    def commit(self): self.events.append('commit')


@pytest.fixture
def system(case, monkeypatch):
    monkeypatch.setenv(runtime.FLAG, 'true')
    monkeypatch.setattr(guard, 'enabled', lambda: True)
    monkeypatch.setattr(owner, 'check_environment', lambda: None)
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',
                        lambda: (('same-gym',), 25))
    conn = Conn()
    persistence = owner.ForwardMediaOwnerPersistence(conn, 'isolated_owner', None)
    row_id = str(uuid.uuid4())
    caption = case.snapshot['copy']['facts'][0]
    palette = case.snapshot['palette']
    # Loader builds exact approved words and supplies palette from controlled
    # source. Provider/reviewer/storage are the real A path with offline clients.
    case.snapshot['copy'] = dict(headline=caption, facts=[caption], cta='', footer='')
    snap = {**case.request, 'account': 'instagram', 'format': 'feed', 'group_key': 'vg_bound',
            'copy': dict(gym_id='same-gym', local_date=case.request['local_date'],
                         logical_post_id=case.request['logical_post_id'], group_key='vg_bound',
                         caption=caption),
            'photo_inventory_complete': True, 'eligible_photo_count': 0,
            'history_complete': True,
            'history': dict(rows=[], scope_complete=True, spine_digest='history-v1')}
    monkeypatch.setattr(guard, 'generated_snapshot', lambda p, rid: copy.deepcopy(snap))
    source = SimpleNamespace(id=1, account_key='same-gym_ig', text=caption, status='approved',
                             citation='approved website',category='educational',created_at='2026-10-01')
    account = SimpleNamespace(key='same-gym_ig', platform='instagram')
    def read(url):
        assert conn.events[-1] == 'rollback', 'network read must have no open snapshot transaction'
        if '/echo-generated-originals/' in url:
            return case.storage.get_bytes(url.split('images.example.test/')[1])
        return case.data
    loader = runtime.OwnerSnapshotLoader(persistence, sources=lambda key: [source],
        palette_loader=lambda base, key: (palette, 'palette-v1'), reader=read)
    reserved = []
    def reserve(p, rid, candidate, current, **kwargs):
        assert p is persistence and rid == row_id
        prep.validate_candidate(candidate, kwargs['read_bytes'](candidate['original_url']))
        assert current['palette_verified'] and current['copy_verified']
        assert current['eligible_photo_count'] == 0
        assert candidate['logical_post_id'] == current['logical_post_id']
        reserved.append(candidate)
        return dict(reserved=True, receipt_ref='offline_owner_receipt')
    monkeypatch.setattr(guard, 'reserve_generated', reserve)
    return SimpleNamespace(case=case, conn=conn, persistence=persistence, row_id=row_id,
        snap=snap, source=source, account=account, palette=palette, loader=loader, reserved=reserved)


def run(s):
    c = s.case
    return runtime.run_calendar_row('same-gym', s.account, s.row_id,
        persistence=s.persistence, loader=s.loader, jobs=c.jobs, provider=c.provider,
        reviewer=c.reviewer, storage=c.storage)


def row_for(s):
    c = s.reserved[-1]
    return dict(id=s.row_id, caption=s.source.text, gym_id=c['gym_id'], post_date=c['local_date'], logical_post_id=c['logical_post_id'],
        account='instagram', format='feed', source_media_asset_id=runtime.PREFIX+c['job_id'],
        image_url=c['original_url'], source_media_url=c['original_url'], thumbnail_url=None,
        visual_group_key='vg_bound', render_manifest_digest='manifest-1')


def binding_for(s, row, **overrides):
    """Immutable SQL reservation readback for this row; build while approved."""
    c = s.reserved[-1]
    copy, source_revision = runtime.approved_copy(row['gym_id'], row['caption'],
                                                  lambda key: [s.source])
    binding = dict(job_id=c['job_id'], calendar_row_id=str(row['id']), gym_id=c['gym_id'],
        account=row['account'], local_date=c['local_date'], logical_post_id=c['logical_post_id'],
        group_key=row['visual_group_key'], original_url=c['original_url'],
        manifest_digest=row['render_manifest_digest'], source_revision=source_revision,
        copy_digest=prep.digest(copy), palette_revision='palette-v1',
        palette_digest=prep.digest(s.palette), receipt_ref='offline_owner_receipt')
    binding.update(overrides)
    return lambda row_id: dict(binding)


def publish_args(s, row, **overrides):
    return dict(readback=binding_for(s, row, **overrides),
        palette_loader=lambda base, key: (s.palette, 'palette-v1'), sources=lambda key: [s.source])


def test_row_to_fresh_candidate_owner_reservation_and_normal_publish_binding(system):
    s = system
    result = run(s)
    assert result['ok'] and result['reserved']
    assert s.conn.events[-1] == 'commit'
    assert len(s.reserved) == s.case.provider.calls == s.case.reviewer.calls == 1
    row = row_for(s)
    assert runtime.validate_publish_palette(row, **publish_args(s, row))
    assert 'coach_review' not in json.dumps(s.reserved)
    assert 'needs_client_safe_review' not in json.dumps(s.reserved)
    # Replay consumes prepared original; no second provider job or review.
    assert run(s)['ok'] and s.case.provider.calls == s.case.reviewer.calls == 1


@pytest.mark.parametrize('field,value,reason', [
    ('gym_id', 'other-gym', 'generated_calendar_binding_changed'),
    ('account', 'facebook', 'generated_calendar_binding_changed'),
    ('format', 'story', 'generated_calendar_binding_changed'),
    ('photo_inventory_complete', False, 'generated_photo_inventory_uncertain'),
    ('eligible_photo_count', 1, 'generated_photo_available'),
    ('history_complete', False, 'generated_history_uncertain'),
])
def test_owner_facts_block_before_paid_generation(system, field, value, reason):
    system.snap[field] = value
    result = run(system)
    assert result['held'] and result['reason'] == reason
    assert system.case.provider.calls == 0 and not system.reserved


def test_copy_needs_exact_current_same_gym_approved_source(system):
    system.source.account_key = 'other-gym_ig'
    assert run(system)['reason'] == 'generated_approved_copy_receipt_missing'
    assert system.case.provider.calls == 0


def test_caption_changed_after_generation_blocks_reservation(system):
    create = system.case.provider.create
    def change(brief, job_id):
        result = create(brief, job_id)
        system.snap['copy']['caption'] = 'Different caption'
        return result
    system.case.provider.create = change
    assert run(system)['reason'] == 'generated_approved_copy_receipt_missing'
    assert len(system.reserved) == 0


def test_late_photo_blocks_owner_reserve(system):
    create = system.case.provider.create
    def change(brief, job_id):
        result = create(brief, job_id)
        system.snap['eligible_photo_count'] = 1
        return result
    system.case.provider.create = change
    assert run(system)['reason'] == 'generated_photo_available'
    assert not system.reserved


def test_complete_history_observed_before_provider_and_change_holds(system):
    item = dict(history_key='calendar-image:old', published_binding_ref='bound',
                visual_url='https://images.example.test/old.png', visual_sha256='sha256:'+'f'*64)
    system.snap['history']['rows'] = [item]
    assert run(system)['reason'] == 'generated_history_bytes_changed'
    assert system.case.provider.calls == 0


def test_provider_ambiguity_keeps_durable_job_and_does_not_regenerate(system):
    calls = []
    def fail(brief, job):
        calls.append(job)
        raise TimeoutError()
    system.case.provider.create = fail
    assert run(system)['reason'] == 'generated_preparation_unavailable'
    assert run(system)['reason'] == 'generated_execution_pending_reconciliation'
    assert len(calls) == 1 and not system.reserved


def test_uncertain_owner_commit_does_not_report_completion(system):
    def fail(): raise TimeoutError()
    system.conn.commit = fail
    assert run(system)['reason'] == 'generated_owner_commit_uncertain'
    assert system.case.provider.calls == 1


@pytest.mark.parametrize('field,value', [('storage_key','echo-generated-originals/other.png'),
    ('storage_readback_sha256','f'*64), ('job_id',str(uuid.uuid4()))])
def test_strict_A_validation_precedes_B_reserve(system, monkeypatch, field, value):
    real = prep.prepare_candidate
    def corrupted(*args, **kwargs):
        result = real(*args, **kwargs)
        result['candidate'][field] = value
        return result
    monkeypatch.setattr(prep, 'prepare_candidate', corrupted)
    assert run(system)['reason'] == 'generated_candidate_invalid'
    assert not system.reserved


@pytest.mark.parametrize('field,value', [('post_date','2026-10-09'),
    ('gym_id','other-gym'), ('logical_post_id',str(uuid.uuid4())),
    ('image_url','https://images.example.test/changed.png'), ('thumbnail_url','https://images.example.test/thumb.png'),
    ('visual_group_key','vg_other'), ('render_manifest_digest','manifest-2'), ('account','facebook')])
def test_publish_date_gym_post_and_original_binding(system, field, value):
    assert run(system)['ok']
    row = row_for(system)
    args = publish_args(system, row)
    row[field] = value
    with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
        runtime.validate_publish_palette(row, **args)


@pytest.mark.parametrize('field,reason', [
    ('job_id', 'generated_publish_binding_unavailable'),
    ('calendar_row_id', 'generated_publish_binding_unavailable'),
    ('gym_id', 'generated_publish_binding_unavailable'),
    ('account', 'generated_publish_binding_unavailable'),
    ('local_date', 'generated_publish_binding_unavailable'),
    ('logical_post_id', 'generated_publish_binding_unavailable'),
    ('group_key', 'generated_publish_binding_unavailable'),
    ('original_url', 'generated_publish_binding_unavailable'),
    ('manifest_digest', 'generated_publish_binding_unavailable'),
    ('source_revision', 'generated_approved_source_changed'),
    ('copy_digest', 'generated_approved_source_changed'),
    ('palette_revision', 'generated_palette_changed'),
    ('palette_digest', 'generated_palette_changed')])
def test_sql_readback_binding_mismatch_fails_closed(system, field, reason):
    assert run(system)['ok']
    row = row_for(system)
    with pytest.raises(runtime.RuntimeHold, match=reason):
        runtime.validate_publish_palette(row, **publish_args(system, row, **{field: 'changed-ref'}))


def test_current_palette_revision_blocks_before_send(system):
    assert run(system)['ok']
    row = row_for(system)
    with pytest.raises(runtime.RuntimeHold, match='generated_palette_changed'):
        runtime.validate_publish_palette(row, readback=binding_for(system, row),
            palette_loader=lambda base,key: (system.palette,'new-revision'), sources=lambda key:[system.source])


def test_absent_sql_readback_fail_closed_without_journal_authority(system):
    # Negative SQL authority: a complete matching local owner journal on this
    # volume can never substitute for the persisted reservation readback.
    assert run(system)['ok']
    journal = Path(system.case.jobs.path)
    assert journal.exists()
    row = row_for(system)
    loaders = dict(palette_loader=lambda base, key: (system.palette, 'palette-v1'),
                   sources=lambda key: [system.source])
    with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
        runtime.validate_publish_palette(row, **loaders)
    with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
        runtime.validate_publish_palette(row, readback=lambda row_id: None, **loaders)
    def failing(row_id):
        raise TimeoutError()
    with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
        runtime.validate_publish_palette(row, readback=failing, **loaders)


def test_separate_owner_volume_journal_absent_still_passes_on_sql_readback(system):
    # Cross-volume layout: the publisher host cannot see the owner's SQLite
    # journal at all. The immutable SQL readback alone is the binding.
    assert run(system)['ok']
    journal = Path(system.case.jobs.path)
    journal.rename(str(journal) + '.owner-volume-only')
    row = row_for(system)
    assert runtime.validate_publish_palette(row, **publish_args(system, row))


def test_malformed_sql_readback_fail_closed(system):
    assert run(system)['ok']
    row = row_for(system)
    loaders = dict(palette_loader=lambda base, key: (system.palette, 'palette-v1'),
                   sources=lambda key: [system.source])
    for bad in ([], 'binding', dict(), dict(binding_for(system, row)('x'), receipt_ref='')):
        with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
            runtime.validate_publish_palette(row, readback=lambda row_id: bad, **loaders)


def test_normal_photo_row_needs_no_generated_journal_or_flag(monkeypatch):
    monkeypatch.delenv(runtime.FLAG, raising=False)
    assert runtime.validate_publish_palette(dict(source_media_asset_id='drive_asset'))


def test_flag_off_has_no_owner_provider_or_store_calls(system, monkeypatch):
    monkeypatch.delenv(runtime.FLAG, raising=False)
    assert run(system)['reason'] == 'generated_runtime_disabled'
    assert system.case.provider.calls == 0 and not system.conn.events


def test_legacy_fill_not_called_when_fresh_lane_is_enabled(monkeypatch):
    from agent import client_media_sync, client_infographic_fill
    monkeypatch.setenv(runtime.FLAG, 'true')
    def forbidden(*a, **k): raise AssertionError('Legacy fallback must not run')
    monkeypatch.setattr(client_infographic_fill, 'fill_gaps', forbidden)
    monkeypatch.setattr(client_infographic_fill, 'real_media_status', lambda *a,**k: (client_infographic_fill.MEDIA_DEPLETED, {}))
    result = client_media_sync._maybe_infographic_fill('same-gym',
        SimpleNamespace(key='same-gym_ig',platform='instagram'), object(), lambda text: None)
    assert result['reason'] == 'generated_gap_owner_transport_missing'


def test_publish_bridge_checks_palette_before_authority_claim(system, monkeypatch):
    from agent import forward_media_publish as bridge
    assert run(system)['ok']
    row = dict(row_for(system), id=system.row_id, status='publishing', publish_claim_token=str(uuid.uuid4()))
    store = SimpleNamespace(get_row=lambda base, rid: row)
    seen = {}
    def check(row, *, store=None, **kwargs):
        seen['store'] = store
        raise runtime.RuntimeHold('generated_palette_changed')
    monkeypatch.setattr(runtime, 'validate_publish_palette', check)
    with pytest.raises(guard.ForwardMediaVerificationHold, match='generated_palette_changed'):
        bridge.authorize(store, row, row['publish_claim_token'])
    assert seen['store'] is store, 'provider boundary must receive the publisher store'


def test_palette_evidence_file_revision_and_missing_notes(tmp_path, monkeypatch):
    from agent import astra_prompt as ap
    path = tmp_path/'brand_colors.json'
    raw = dict(colors=['#112233'],source_url='https://gym.example.test',source_note='Official site CSS')
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(ap, 'load_gym_brand_palette', lambda key: dict(colors=['#112233'],path=str(path)))
    first, revision = runtime.verified_palette('same-gym','same-gym_ig')
    assert first['verified'] and first['gym_id'] == 'same-gym'
    raw['source_note'] = 'Rechecked official site CSS'
    path.write_text(json.dumps(raw))
    assert runtime.verified_palette('same-gym','same-gym_ig')[1] != revision
    raw.pop('source_note')
    path.write_text(json.dumps(raw))
    with pytest.raises(runtime.RuntimeHold, match='generated_palette_unverified'):
        runtime.verified_palette('same-gym','same-gym_ig')


def test_own_successful_reservation_history_revision_never_creates_another_job(system):
    assert run(system)['ok']
    system.snap['history_revision'] = 'history-after-own-reservation'
    system.snap['history']['spine_digest'] = 'history-after-own-reservation'
    assert run(system)['ok']
    assert system.case.provider.calls == 1
    assert system.reserved[0]['job_id'] == system.reserved[1]['job_id']


def test_uncertain_owner_commit_is_durable_and_never_retried(system):
    calls = []
    def fail():
        calls.append(True)
        raise TimeoutError()
    system.conn.commit = fail
    assert run(system)['reason'] == 'generated_owner_commit_uncertain'
    assert run(system)['reason'] == 'generated_owner_commit_uncertain'
    assert system.case.provider.calls == 1 and len(calls) == 1 and len(system.reserved) == 1


def test_fresh_flag_off_keeps_existing_scheduler_entry(monkeypatch):
    from agent import client_media_sync, client_infographic_fill, voice
    monkeypatch.delenv(runtime.FLAG, raising=False)
    monkeypatch.setattr(client_infographic_fill, 'fill_enabled', lambda: True)
    monkeypatch.setattr(voice, 'load_voice', lambda path: object())
    monkeypatch.setattr(client_media_sync, '_resolve_client_voice_path', lambda base,path: path)
    calls = []
    monkeypatch.setattr(client_infographic_fill, 'fill_gaps', lambda *a,**k: calls.append((a,k)))
    account = SimpleNamespace(key='same-gym_ig',platform='instagram',voice_doc_path=lambda: 'voice.md')
    client_media_sync._maybe_infographic_fill('same-gym', account, object(), lambda text: None)
    assert len(calls) == 1


def test_forward_authority_flag_off_blocks_generated_candidate_and_send(system, monkeypatch):
    monkeypatch.setattr(guard, 'enabled', lambda: False)
    assert run(system)['reason'] == 'generated_forward_authority_disabled'
    assert system.case.provider.calls == 0


def test_existing_owner_tenant_allowlist_is_required(system, monkeypatch):
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',
                        lambda: (('other-gym',), 25))
    assert run(system)['reason'] == 'generated_owner_tenant_not_allowed'
    assert system.case.provider.calls == 0


def test_ambiguous_generation_bound_before_provider_survives_history_drift(system):
    calls = []
    def timeout(brief, job_id):
        # The row mapping is already durable at first provider invocation.
        record = runtime._runtime_record(system.case.jobs, system.row_id)
        assert record['job_id'] == job_id and record['job_state'] == 'generating'
        calls.append(job_id)
        raise TimeoutError()
    system.case.provider.create = timeout
    assert run(system)['reason'] == 'generated_preparation_unavailable'
    system.snap['history_revision'] = 'changed-history'
    system.snap['history']['spine_digest'] = 'changed-history'
    assert run(system)['reason'] == 'generated_execution_pending_reconciliation'
    assert len(calls) == 1


def test_reviewing_execution_with_new_history_never_generates_again(system):
    reviewer = system.case.reviewer.ask_image
    system.case.reviewer.ask_image = lambda *a: (_ for _ in ()).throw(TimeoutError())
    assert run(system)['reason'] == 'generated_automated_review_failed'
    assert runtime._runtime_record(system.case.jobs, system.row_id)['job_state'] == 'reviewing'
    system.snap['history_revision'] = 'changed-history'
    system.snap['history']['spine_digest'] = 'changed-history'
    system.case.reviewer.ask_image = reviewer
    assert run(system)['reason'] == 'generated_execution_pending_reconciliation'
    assert system.case.provider.calls == 1 and not system.reserved


def test_sql_issued_history_cache_avoids_old_object_reads(system):
    from agent.visual_scene import scene_fingerprint
    system.snap['history']['rows'] = [dict(history_key='sealed:baseline:old',
        published_binding_ref='sealed-binding', visual_url='https://images.example.test/unreadable-old.png',
        visual_sha256='sha256:'+hashlib.sha256(system.case.data).hexdigest(),
        phash=scene_fingerprint(system.case.data), history_proof_ref='generated-history:sha256:'+'a'*64)]
    read = system.loader.reader
    def only_original(url):
        assert 'unreadable-old' not in url
        return read(url)
    system.loader.reader = only_original
    assert run(system)['ok']
    assert system.case.provider.calls == 1


def test_unresolved_history_duplicate_urls_observed_once(system):
    system.snap['history']['rows'] = [dict(history_key='old:'+str(i), published_binding_ref='b'+str(i),
        visual_url='https://images.example.test/old.png', visual_sha256=None,
        phash=None, history_proof_ref=None) for i in range(3)]
    calls = []
    read = system.loader.reader
    def count(url):
        calls.append(url)
        return read(url)
    system.loader.reader = count
    assert run(system)['ok']
    assert calls.count('https://images.example.test/old.png') == 1


def test_invalid_cached_proof_holds(system):
    system.snap['history']['rows'] = [dict(history_key='old', published_binding_ref='bound',
        visual_url='https://images.example.test/old.png', visual_sha256='sha256:'+'b'*64,
        phash='scene:phash64:abc', history_proof_ref='producer-says-cached')]
    assert run(system)['reason'] == 'generated_history_cache_invalid'
    assert system.case.provider.calls == 0


def test_no_sources_legacy_seed_suppressed_under_new_on_flag(monkeypatch):
    from agent import client_media_sync, no_media_astra_seed, client_infographic_fill
    monkeypatch.setenv(runtime.FLAG,'true')
    monkeypatch.setattr(no_media_astra_seed,'enabled',lambda: True)
    monkeypatch.setattr(client_infographic_fill, 'real_media_status', lambda *a,**k: (client_infographic_fill.MEDIA_DEPLETED, {}))
    monkeypatch.setattr(no_media_astra_seed,'seed_gaps',lambda *a,**k: pytest.fail('legacy seed ran'))
    result = client_media_sync._maybe_seed_no_media_astra('same-gym',
        SimpleNamespace(key='same-gym_ig',platform='instagram'), object(), lambda text: None)
    assert result['reason'] == 'generated_gap_owner_transport_missing'


def test_approved_source_revocation_or_metadata_change_blocks_send(system):
    assert run(system)['ok']
    row = row_for(system)
    readback = binding_for(system, row)
    loaders = dict(palette_loader=lambda base,key:(system.palette,'palette-v1'),
                   sources=lambda key:[system.source])
    system.source.status = 'pending'
    with pytest.raises(runtime.RuntimeHold, match='generated_approved_copy_receipt_missing'):
        runtime.validate_publish_palette(row, readback=readback, **loaders)
    system.source.status = 'approved'
    system.source.citation = 'changed source record'
    with pytest.raises(runtime.RuntimeHold, match='generated_approved_source_changed'):
        runtime.validate_publish_palette(row, readback=readback, **loaders)


def test_same_day_facebook_sibling_reuses_original_after_history_drift(system, monkeypatch):
    assert run(system)['ok']
    old_job = system.reserved[0]['job_id']
    sibling = str(uuid.uuid4())
    system.snap['account'] = 'facebook'
    system.snap['history_revision'] = 'history-after-instagram-reservation'
    system.snap['history']['spine_digest'] = system.snap['history_revision']
    reserved = []
    monkeypatch.setattr(guard, 'reserve_generated', lambda p,rid,candidate,current,**kw:
                        reserved.append(candidate) or dict(receipt_ref='sibling'))
    result = runtime.run_calendar_row('same-gym', SimpleNamespace(key='same-gym_fb',platform='facebook_page'),
        sibling, persistence=system.persistence, loader=system.loader, jobs=system.case.jobs,
        provider=system.case.provider,reviewer=system.case.reviewer,storage=system.case.storage)
    assert result['ok'] and reserved[0]['job_id'] == old_job
    assert system.case.provider.calls == system.case.reviewer.calls == 1


def test_sibling_ambiguous_provider_never_mints_second_job(system):
    calls = []
    system.case.provider.create = lambda brief,job: calls.append(job) or (_ for _ in ()).throw(TimeoutError())
    assert run(system)['reason'] == 'generated_preparation_unavailable'
    system.snap['account'] = 'facebook'
    system.snap['history_revision'] = 'new-history'
    system.snap['history']['spine_digest'] = 'new-history'
    result = runtime.run_calendar_row('same-gym', SimpleNamespace(key='same-gym_fb',platform='facebook_page'),
        str(uuid.uuid4()),persistence=system.persistence,loader=system.loader,jobs=system.case.jobs,
        provider=system.case.provider,reviewer=system.case.reviewer,storage=system.case.storage)
    assert result['reason'] == 'generated_execution_pending_reconciliation' and len(calls)==1


def test_same_logical_sibling_with_new_copy_receipt_holds_old_job(system):
    assert run(system)['ok']
    system.source.citation='changed approved metadata'
    system.snap['account']='facebook'
    result=runtime.run_calendar_row('same-gym',SimpleNamespace(key='same-gym_fb',platform='facebook_page'),
        str(uuid.uuid4()),persistence=system.persistence,loader=system.loader,jobs=system.case.jobs,
        provider=system.case.provider,reviewer=system.case.reviewer,storage=system.case.storage)
    assert result['reason']=='generated_logical_binding_changed'
    assert system.case.provider.calls==1


@pytest.mark.parametrize("revision", [None, "", "sha256:" + "a" * 64,
                                        "client-source:sha256:bad"])
def test_owner_reservation_refuses_missing_or_non_source_approval_revision(system, monkeypatch, revision):
    s = system
    snapshot, _ = s.loader.load(s.row_id, 'same-gym', s.account)
    snapshot['approved_source_revision'] = revision
    monkeypatch.setattr(s.persistence, '_assert_owner_identity', lambda: None)
    candidate = {**s.case.request, 'schema_version': 1,
                 'source_type': 'generated_astra_infographic', 'provider': 'astra',
                 'model': 'gpt-6-astra'}
    with pytest.raises(guard.ForwardMediaVerificationHold,
                       match='verified approved source revision required'):
        REAL_RESERVE_GENERATED(s.persistence, s.row_id, candidate, snapshot,
                               history_visuals=[], read_bytes=lambda url: pytest.fail('unexpected read'))


def test_database_copy_digest_cannot_substitute_for_approved_source_revision(system):
    s = system
    assert run(s)['ok']
    row = row_for(s)
    # Reproduces the old SQL readback source_revision=c.copy_revision mismatch.
    with pytest.raises(runtime.RuntimeHold, match='generated_approved_source_changed'):
        runtime.validate_publish_palette(row, **publish_args(
            s, row, source_revision=s.reserved[-1]['copy_revision']))


def test_null_sql_readback_refuses_stale_python_feed_row(system):
    s = system
    assert run(s)['ok']
    row = row_for(s)
    assert row['format'] == 'feed'
    # SQL returns NULL when the live row drifted to Story; the stale Python
    # feed snapshot must not independently authorize the generated visual.
    args = publish_args(s, row)
    args['readback'] = lambda row_id: None
    with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
        runtime.validate_publish_palette(row, **args)
