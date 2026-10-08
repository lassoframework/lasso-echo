"""Runtime integration checks use offline provider/storage/DB fixtures only."""
import copy
import hashlib
from datetime import datetime, timedelta, timezone
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
from test_generated_canonical_owner import active, derive, observe, frozen, identity

REAL_RESERVE_GENERATED = guard.reserve_generated


class Conn:
    autocommit = False
    def __init__(self):
        self.events = []
        self.info = SimpleNamespace(transaction_status=0)
    def rollback(self): self.events.append('rollback')
    def commit(self): self.events.append('commit')


@pytest.fixture
def system(case, active, monkeypatch):
    monkeypatch.setenv(runtime.FLAG, 'true')
    monkeypatch.setattr(guard, 'enabled', lambda: True)
    monkeypatch.setattr(owner, 'check_environment', lambda: None)
    monkeypatch.setattr('agent.forward_media_owner_worker.settings_from_environment',
                        lambda: (('same-gym',), 25))
    conn = Conn()
    persistence = owner.ForwardMediaOwnerPersistence(conn, 'isolated_owner', None)
    row_id = str(uuid.uuid4())
    caption = case.snapshot['copy']['facts'][0]
    authority=derive(active)
    palette = authority['palette']
    # Loader builds exact approved words and supplies palette from controlled
    # source. Provider/reviewer/storage are the real A path with offline clients.
    case.snapshot['copy'] = dict(headline=caption, facts=[caption], cta='', footer='')
    snap = {**case.request, 'account': 'instagram', 'format': 'feed', 'group_key': 'vg_bound',
            'copy': dict(gym_id='same-gym', local_date=case.request['local_date'],
                         logical_post_id=case.request['logical_post_id'], group_key='vg_bound',
                         caption=caption),
            'photo_inventory_complete': True, 'eligible_photo_count': 0,
            'local_census_current': True,
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
    loader = runtime.OwnerSnapshotLoader(persistence, bundle_reader=lambda base:copy.deepcopy(active), reader=read)
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
    # Default trusted census: newest current-epoch row at the exact inventory
    # revision is complete, zero-supply and fresh (legitimate exhausted case).
    census = dict(enabled=True, receipt_id=str(uuid.uuid4()), local_complete=True,
                  local_available=0, observed_at=datetime.now(timezone.utc))
    monkeypatch.setattr(runtime, '_latest_local_census', lambda *args, **kwargs: dict(census))
    return SimpleNamespace(case=case, conn=conn, persistence=persistence, row_id=row_id,
        snap=snap, source=source, account=account, palette=palette, loader=loader, reserved=reserved, active=active,
        census=census)


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
    authority=derive(s.active,caption=row['caption'])
    copy, source_revision = authority['copy'], authority['source_revision']
    binding = dict(schema_version=2,job_id=c['job_id'], calendar_row_id=str(row['id']), gym_id=c['gym_id'],
        account=row['account'], local_date=c['local_date'], logical_post_id=c['logical_post_id'],
        group_key=row['visual_group_key'], original_url=c['original_url'],
        manifest_digest=row['render_manifest_digest'], source_revision=source_revision,
        copy_digest=prep.digest(copy), palette_revision=authority['palette_revision'],
        palette_digest=prep.digest(s.palette), receipt_ref='offline_owner_receipt',
        authority_pins=authority['authority_pins'],copy_derivation_receipt=authority['copy_derivation_receipt'])
    binding.update(overrides)
    return lambda row_id: dict(binding)


def publish_args(s, row, **overrides):
    return dict(readback=binding_for(s, row, **overrides),
        bundle_reader=lambda base:copy.deepcopy(s.active))


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


@pytest.mark.parametrize('change,reason', [
    (dict(local_available=1), 'generated_local_photo_available'),
    (dict(local_complete=False), 'generated_local_census_incomplete'),
    (dict(receipt_id=None), 'generated_local_census_unavailable'),
    (dict(enabled=False), 'generated_local_census_unavailable'),
    (dict(observed_at=datetime.now(timezone.utc) - timedelta(minutes=11)),
     'generated_local_census_stale'),
    (dict(observed_at=lambda: datetime.now(timezone.utc) + timedelta(minutes=1)),
     'generated_local_census_stale'),
    (dict(observed_at='not-a-timestamp'), 'generated_local_census_stale'),
])
def test_latest_local_census_governs_before_paid_generation(system, change, reason):
    """A positive/incomplete/stale/missing newest census holds pre-generation."""
    change = {key: value() if callable(value) else value for key, value in change.items()}
    system.census.update(change)
    result = run(system)
    assert result['held'] and result['reason'] == reason
    assert system.case.provider.calls == 0 and not system.reserved


def test_late_local_arrival_after_first_preflight_still_blocks_rerun(system):
    """Zero-then-positive at the same revision: a later run sees only the newest
    census and never reaches provider I/O or owner reservation."""
    first = run(system)
    assert first['ok'] and first['reserved']
    system.census.update(local_available=2)
    second = run(system)
    assert second['held'] and second['reason'] == 'generated_local_photo_available'
    assert system.case.provider.calls == 1 and len(system.reserved) == 1


def test_newest_zero_census_after_positive_restores_exhausted_fallback(system):
    """A newer complete zero census again governs: legitimate photo-exhausted
    generated fallback proceeds through reservation."""
    system.census.update(local_available=1)
    assert run(system)['reason'] == 'generated_local_photo_available'
    system.census.update(local_available=0,
                         observed_at=datetime.now(timezone.utc))
    result = run(system)
    assert result['ok'] and result['reserved']
    assert system.case.provider.calls == 1


def test_census_rpc_failure_fails_closed_before_provider(system, monkeypatch):
    def unavailable(*args, **kwargs):
        raise runtime.RuntimeHold('generated_local_census_unavailable')
    monkeypatch.setattr(runtime, '_latest_local_census', unavailable)
    result = run(system)
    assert result['held'] and result['reason'] == 'generated_local_census_unavailable'
    assert system.case.provider.calls == 0 and not system.reserved


def test_copy_needs_exact_current_same_gym_approved_source(system):
    system.active['bundle']['echo_account_key'] = 'other-gym'
    assert run(system)['reason'] == 'generated_bundle_evidence_invalid'
    assert system.case.provider.calls == 0


def test_caption_changed_after_generation_blocks_reservation(system):
    create = system.case.provider.create
    def change(brief, job_id):
        result = create(brief, job_id)
        system.snap['copy']['caption'] = 'Different caption'
        return result
    system.case.provider.create = change
    assert run(system)['reason'] == 'generated_bundle_caption_unsupported'
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
    ('source_revision', 'generated_bundle_publish_binding_changed'),
    ('copy_digest', 'generated_bundle_publish_binding_changed'),
    ('palette_revision', 'generated_bundle_publish_binding_changed'),
    ('palette_digest', 'generated_bundle_publish_binding_changed')])
def test_sql_readback_binding_mismatch_fails_closed(system, field, reason):
    assert run(system)['ok']
    row = row_for(system)
    with pytest.raises(runtime.RuntimeHold, match=reason):
        runtime.validate_publish_palette(row, **publish_args(system, row, **{field: 'changed-ref'}))


def test_current_palette_revision_blocks_before_send(system):
    assert run(system)['ok']
    row = row_for(system)
    binding=binding_for(system,row)
    system.active['observation']['id']+=1
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_publish_binding_changed'):
        runtime.validate_publish_palette(row,readback=binding,bundle_reader=lambda base:system.active)


def test_absent_sql_readback_fail_closed_without_journal_authority(system):
    # Negative SQL authority: a complete matching local owner journal on this
    # volume can never substitute for the persisted reservation readback.
    assert run(system)['ok']
    journal = Path(system.case.jobs.path)
    assert journal.exists()
    row = row_for(system)
    loaders = dict(bundle_reader=lambda base:copy.deepcopy(system.active))
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
    loaders = dict(bundle_reader=lambda base:copy.deepcopy(system.active))
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


def test_owner_loader_fails_closed_without_guarded_local_census(system):
    system.snap['local_census_current'] = False
    with pytest.raises(runtime.RuntimeHold, match='generated_local_census_unverified'):
        system.loader.load(system.row_id, 'same-gym', system.account)
    assert system.case.provider.calls == 0 and system.conn.events == ['rollback']


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


def test_palette_byte_evidence_revision_and_missing_tokens(system):
    first=derive(system.active)
    system.active['observation']['id']+=1
    second=derive(system.active)
    assert second['authority_pins']!=first['authority_pins']
    observe(system.active,lambda snapshot:snapshot['palette'].update(primary_byte_offset=0))
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_evidence_invalid'):
        derive(system.active)


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


def test_configuration_revocation_or_observation_change_blocks_send(system):
    assert run(system)['ok']
    row = row_for(system)
    readback = binding_for(system, row)
    loaders = dict(bundle_reader=lambda base:copy.deepcopy(system.active))
    system.active['fact_validation']='pending_collector_validation'
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_fact_validation_required'):
        runtime.validate_publish_palette(row, readback=readback, **loaders)
    system.active['fact_validation']='supported_uncontradicted'
    system.active['observation']['id']+=1
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_publish_binding_changed'):
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
    system.active['observation']['id']+=1
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
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_publish_binding_changed'):
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


# --- Schema-v2 source_policy + provider attestation consumption ------------

import base64

V2_WEBSITE_ONLY = 'website_only_no_connected_instagram_v2'
V2_BOTH = 'website_and_social_v2'
IG_OWNER = '17841400000000001'


def _v2_connection(**overrides):
    connection = dict(id='zernio-account-1', platform_user_id=IG_OWNER, handle='verified_gym')
    connection.update(overrides)
    return connection


def _v2_attestation(gym, base, observed_at, *, connected=False, **overrides):
    if connected:
        connection = _v2_connection()
        instagram = dict(connected=True, account_id=connection['id'],
            platform_user_id=connection['platform_user_id'], handle=connection['handle'])
    else:
        instagram = dict(connected=False, account_id=None, platform_user_id=None, handle=None)
    attestation = dict(provider='zernio', source='zernio_authenticated_accounts',
        gym_id=gym, echo_account_key=base, profile_id='profile-1', mapping_revision='map-v1',
        lookup_status='complete', authenticated=True, observed_at=observed_at.isoformat(),
        response_sha256='a'*64, instagram=instagram)
    attestation.update(overrides)
    return attestation


def _v2_receipt(receipt_id, attestation):
    return dict(id=receipt_id, observed_at=attestation['observed_at'],
                response_sha256=attestation['response_sha256'])


def _v2_snapshot(gym, base, now, mode, attestation, receipt_id=1, kinds=None):
    text = 'Practice with guidance'
    website = b'<style>#112233 #44AA77</style> ' + text.encode()
    social = ('{"caption":"' + text + '"}').encode()
    if kinds is None:
        kinds = ['website'] if mode == V2_WEBSITE_ONLY else ['website', 'social']
    captures = []
    for kind in kinds:
        data = website if kind == 'website' else social
        captures.append(dict(id=identity(), gym_id=gym, echo_account_key=base,
            source_kind=kind, source_url='https://gym.example.test/' if kind == 'website' else 'https://api.apify.com/v2/datasets/test/items',
            source_locator=None if kind == 'website' else 'https://www.instagram.com/verified_gym/',
            capture_provider='direct' if kind == 'website' else 'apify',
            provider_response_id=None if kind == 'website' else 'dataset:test:1',
            provider_account_id=None if kind == 'website' else IG_OWNER,
            source_revision='capture-v1', mapping_revision='map-v1', mapping_evidence={'binding':'gym'},
            fetched_at=now.isoformat(), bytes_sha256=hashlib.sha256(data).hexdigest(),
            bytes_base64=base64.b64encode(data).decode()))
    website_capture = captures[0]
    instagram = attestation['instagram']
    connection = (dict(id=instagram['account_id'], platform_user_id=instagram['platform_user_id'],
                       handle=instagram['handle']) if instagram['connected'] else None)
    return dict(schema_version=2, gym_id=gym, echo_account_key=base,
        fact_policy='delegated_supported_facts', captures=captures,
        palette=dict(capture_id=website_capture['id'], bytes_sha256=website_capture['bytes_sha256'],
            primary='#112233',secondary='#44AA77',primary_byte_offset=7,secondary_byte_offset=15),
        selected_facts=[dict(key='coaching',capture_id=website_capture['id'],
            bytes_sha256=website_capture['bytes_sha256'], source_locator=website_capture['source_url'],
            byte_offset=website.index(text.encode()),byte_length=len(text.encode()),text=text)],
        source_policy=dict(version=2, mode=mode, gym_id=gym, echo_account_key=base,
            provider_identity=dict(provider=attestation['provider'], source=attestation['source'],
                profile_id=attestation['profile_id'], mapping_revision=attestation['mapping_revision']),
            instagram_connection=connection),
        provider_status_receipt=_v2_receipt(receipt_id, attestation))


def _v2_active(mode, *, connected, now=None, current='same', kinds=None, gym=None, base='same-gym'):
    now = now or datetime.now(timezone.utc)
    gym = gym or identity()
    frozen_at = now - timedelta(minutes=2)
    attestation = _v2_attestation(gym, base, frozen_at, connected=connected)
    raw, sha = frozen(_v2_snapshot(gym, base, frozen_at, mode, attestation, kinds=kinds))
    bundle_id = identity()
    bundle = dict(id=bundle_id,gym_id=gym,echo_account_key=base,schema_version=2,version=1,
        capture_ids=[c['id'] for c in json.loads(raw)['captures']],snapshot_bytes=raw,content_sha256=sha,
        source_revision='bundle-source-v1',palette_revision='bundle-palette-v1',created_at=frozen_at.isoformat())
    receipt = dict(id=1,gym_id=gym,bundle_id=bundle_id,bundle_version=1,content_sha256=sha,
        actor_clerk_user_id='user_authenticated',actor_authority='blake',action='approve',
        purpose='echo_source_brand_configuration',request_id=identity(),created_at=frozen_at.isoformat())
    observation = dict(id=1,gym_id=gym,bundle_id=bundle_id,configuration_sha256=sha,
        snapshot_bytes=raw,content_sha256=sha,validator_revision='trusted-validator-v1',
        validation_report=dict(selected_facts_status='supported_uncontradicted',identity_status='verified'),
        created_at=frozen_at.isoformat())
    result = dict(bundle=bundle,approval_receipt=receipt,observation=observation,
        fact_approval_mode='delegated_policy',fact_validation='supported_uncontradicted')
    if current == 'same':
        result['provider_status'] = _v2_attestation(gym, base, now, connected=connected)
    elif current is not None:
        result['provider_status'] = current
    return result


def test_v2_both_source_consumes_with_fresh_connected_attestation():
    authority = derive(_v2_active(V2_BOTH, connected=True))
    assert authority['caption'] == 'Practice with guidance'
    assert authority['copy_approved'] is False
    runtime.validate_authority_pins(authority['authority_pins'], 'same-gym')


def test_v2_website_only_consumes_with_fresh_affirmative_negative():
    authority = derive(_v2_active(V2_WEBSITE_ONLY, connected=False))
    assert authority['caption'] == 'Practice with guidance'
    assert authority['copy_verified'] is True


def test_v2_website_only_snapshot_with_social_capture_holds():
    active = _v2_active(V2_WEBSITE_ONLY, connected=False, kinds=['website', 'social'])
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_website_only_capture_set_required'):
        derive(active)


def test_v2_website_only_fresh_connected_attestation_requires_social():
    active = _v2_active(V2_WEBSITE_ONLY, connected=False)
    gym = active['bundle']['gym_id']
    active['provider_status'] = _v2_attestation(gym, 'same-gym',
        datetime.now(timezone.utc), connected=True)
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_connected_instagram_requires_social'):
        derive(active)


@pytest.mark.parametrize('current', [
    None, 'attacker-supplied',
])
def test_v2_missing_or_malformed_current_attestation_holds(current):
    active = _v2_active(V2_BOTH, connected=True, current=current)
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_provider_status_unavailable'):
        derive(active)


@pytest.mark.parametrize('overrides', [
    dict(lookup_status='partial'),
    dict(lookup_status='unavailable'),
    dict(authenticated=False),
    dict(response_sha256='not-a-hash'),
    dict(profile_id=''),
])
def test_v2_partial_unavailable_or_unauthenticated_attestation_holds(overrides):
    active = _v2_active(V2_BOTH, connected=True)
    active['provider_status'] = _v2_attestation(active['bundle']['gym_id'], 'same-gym',
        datetime.now(timezone.utc), connected=True, **overrides)
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_provider_status_unavailable'):
        derive(active)


def test_v2_stale_attestation_older_than_fifteen_minutes_holds():
    active = _v2_active(V2_BOTH, connected=True)
    stale = datetime.now(timezone.utc) - timedelta(minutes=16)
    active['provider_status'] = _v2_attestation(active['bundle']['gym_id'], 'same-gym',
        stale, connected=True)
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_provider_status_unavailable'):
        derive(active)


def test_v2_attestation_for_other_tenant_holds():
    active = _v2_active(V2_BOTH, connected=True)
    active['provider_status'] = _v2_attestation(identity(), 'same-gym',
        datetime.now(timezone.utc), connected=True)
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_provider_status_unavailable'):
        derive(active)


@pytest.mark.parametrize('overrides', [
    dict(profile_id='profile-2'),
    dict(mapping_revision='map-v2'),
])
def test_v2_provider_identity_drift_holds(overrides):
    active = _v2_active(V2_BOTH, connected=True)
    active['provider_status'] = _v2_attestation(active['bundle']['gym_id'], 'same-gym',
        datetime.now(timezone.utc), connected=True, **overrides)
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_stale_source_policy'):
        derive(active)


@pytest.mark.parametrize('overrides', [
    dict(provider='other'),
    dict(source='other_source'),
])
def test_v2_foreign_provider_attestation_holds(overrides):
    active = _v2_active(V2_BOTH, connected=True)
    active['provider_status'] = _v2_attestation(active['bundle']['gym_id'], 'same-gym',
        datetime.now(timezone.utc), connected=True, **overrides)
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_provider_status_unavailable'):
        derive(active)


@pytest.mark.parametrize('mutate', [
    lambda ig: ig.update(account_id='zernio-account-2'),
    lambda ig: ig.update(platform_user_id='17841400000000002'),
    lambda ig: ig.update(handle='other_gym'),
    lambda ig: ig.update(connected=False, account_id=None, platform_user_id=None, handle=None),
])
def test_v2_both_source_connection_drift_holds(mutate):
    active = _v2_active(V2_BOTH, connected=True)
    attestation = _v2_attestation(active['bundle']['gym_id'], 'same-gym',
        datetime.now(timezone.utc), connected=True)
    mutate(attestation['instagram'])
    active['provider_status'] = attestation
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_stale_source_policy'):
        derive(active)


def test_v2_social_capture_numeric_owner_mismatch_holds():
    active = _v2_active(V2_BOTH, connected=True)
    observe(active, lambda s: s['captures'][1].update(provider_account_id='17841400000000002'))
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_provider_instagram_identity_mismatch'):
        derive(active)


def test_v2_observation_must_retain_frozen_policy_exactly():
    active = _v2_active(V2_BOTH, connected=True)
    observe(active, lambda s: s['source_policy']['instagram_connection'].update(handle='other_gym'))
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_stale_source_policy'):
        derive(active)


def test_v2_website_only_frozen_with_connected_identity_is_invalid():
    active = _v2_active(V2_BOTH, connected=True)
    observe(active, lambda s: s['source_policy'].update(mode=V2_WEBSITE_ONLY))
    with pytest.raises(runtime.RuntimeHold,
                       match='generated_bundle_website_only_capture_set_required'):
        derive(active)


def test_v2_missing_provider_status_receipt_is_invalid():
    active = _v2_active(V2_BOTH, connected=True)
    observe(active, lambda s: s.pop('provider_status_receipt'))
    with pytest.raises(runtime.RuntimeHold, match='generated_bundle_evidence_invalid'):
        derive(active)


def test_v1_consumption_ignores_unrelated_policy_readback(active):
    active['provider_status'] = 'attacker-supplied'
    assert derive(active)['caption'] == 'Practice with guidance'


def test_v2_observation_binds_newest_receipt_without_new_approval():
    active = _v2_active(V2_BOTH, connected=True)
    newer = _v2_attestation(active['bundle']['gym_id'], 'same-gym',
        datetime.now(timezone.utc), connected=True, response_sha256='b'*64)
    observe(active, lambda s: s.update(provider_status_receipt=_v2_receipt(2, newer)))
    authority = derive(active)
    assert authority['authority_pins']['configuration_sha256'] == active['bundle']['content_sha256']
