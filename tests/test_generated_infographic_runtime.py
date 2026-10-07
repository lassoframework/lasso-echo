"""Runtime integration checks use offline provider/storage/DB fixtures only."""
import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from agent import generated_infographic_runtime as runtime
from agent import generated_infographic_preparation as prep
from agent import forward_media_guard as guard, forward_media_owner as owner
from test_generated_infographic_preparation import case


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
    source = SimpleNamespace(account_key='same-gym_ig', text=caption, status='approved')
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
    return dict(gym_id=c['gym_id'], post_date=c['local_date'], logical_post_id=c['logical_post_id'],
        account='instagram', format='feed', source_media_asset_id=runtime.PREFIX+c['job_id'],
        image_url=c['original_url'], source_media_url=c['original_url'], thumbnail_url=None)


def test_row_to_fresh_candidate_owner_reservation_and_normal_publish_binding(system):
    s = system
    result = run(s)
    assert result['ok'] and result['reserved']
    assert s.conn.events[-1] == 'commit'
    assert len(s.reserved) == s.case.provider.calls == s.case.reviewer.calls == 1
    row = row_for(s)
    assert runtime.validate_publish_palette(row, jobs_path=str(s.case.jobs.path),
        palette_loader=lambda base, key: (s.palette, 'palette-v1'))
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
    ('image_url','https://images.example.test/changed.png'), ('thumbnail_url','https://images.example.test/thumb.png')])
def test_publish_date_gym_post_and_original_binding(system, field, value):
    assert run(system)['ok']
    row = row_for(system)
    row[field] = value
    with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
        runtime.validate_publish_palette(row, jobs_path=str(system.case.jobs.path),
            palette_loader=lambda base,key: (system.palette,'palette-v1'))


def test_current_palette_revision_blocks_before_send(system):
    assert run(system)['ok']
    with pytest.raises(runtime.RuntimeHold, match='generated_palette_changed'):
        runtime.validate_publish_palette(row_for(system), jobs_path=str(system.case.jobs.path),
            palette_loader=lambda base,key: (system.palette,'new-revision'))


def test_missing_journal_fail_closed_without_creating_file(system, tmp_path):
    assert run(system)['ok']
    path = tmp_path/'missing.sqlite'
    with pytest.raises(runtime.RuntimeHold, match='generated_publish_binding_unavailable'):
        runtime.validate_publish_palette(row_for(system), jobs_path=str(path))
    assert not path.exists()


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
    result = client_media_sync._maybe_infographic_fill('same-gym',
        SimpleNamespace(key='same-gym_ig',platform='instagram'), object(), lambda text: None)
    assert result['reason'] == 'generated_gap_owner_transport_missing'


def test_publish_bridge_checks_palette_before_authority_claim(system, monkeypatch):
    from agent import forward_media_publish as bridge
    assert run(system)['ok']
    row = dict(row_for(system), id=system.row_id, status='publishing', publish_claim_token=str(uuid.uuid4()))
    store = SimpleNamespace(get_row=lambda base, rid: row)
    monkeypatch.setattr(runtime, 'validate_publish_palette',
        lambda row: (_ for _ in ()).throw(runtime.RuntimeHold('generated_palette_changed')))
    with pytest.raises(guard.ForwardMediaVerificationHold, match='generated_palette_changed'):
        bridge.authorize(store, row, row['publish_claim_token'])


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
