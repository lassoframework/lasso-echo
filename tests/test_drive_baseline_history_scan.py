"""Deterministic Drive captions must search unused approved facts before dropping a photo."""
from types import SimpleNamespace

import pytest

from agent import caption_ledger, client_month_run as cmr
from agent.voice import VoiceDoc


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setenv('AGENT_CAPTION_COOLDOWN', 'true')
    monkeypatch.setattr(cmr.config, 'sb7_enabled', lambda: False)


def source(index, text=None):
    return SimpleNamespace(text=text or f'Approved training fact number {index} describes the gym coaching members through each session.',
                           category='service')


def feed():
    return SimpleNamespace(caption='Old approved copy', hashtags=[], creative_path='real_photo.jpg',
                           creative_public_url='https://cdn.example/real_photo.jpg',
                           source_media_asset_id='photo-1', category='service', status='pending',
                           day_key='2026-10-15', caption_grounding={'creative_name': 'real_photo.jpg', 'verified': {'ok': True}})


def voice():
    return VoiceDoc(raw='Approved gym voice', hashtags=['#Gym'], ctas=['Ask us about training.'])


def invoke(draft, v, logs, banned=(), day_captions=()):
    return cmr._retry_drive_caption_history(
        SimpleNamespace(key='swift_ig', platform='instagram'), draft, v, 'swift', 'swift_ig',
        '2026-10-15', 0, banned, day_captions, logs.append)


@pytest.mark.parametrize('target_day', ['2026-10-15', '2026-10-16', '2026-10-20'])
def test_scan_recovers_beyond_first_seven_used_facts_with_same_photo(monkeypatch, target_day):
    candidates = [source(i) for i in range(20)]
    draft, v, logs = feed(), voice(), []
    account = SimpleNamespace(key='swift_ig', platform='instagram')
    for candidate in candidates[:12]:
        cap, _ = cmr.client_content.compose_caption(account, candidate, v, draft.creative_path)
        caption_ledger.record_staged('swift', cap, '2026-10-10')
    scans, generated = [], []
    monkeypatch.setattr(cmr, '_drive_history_sources',
                        lambda *args: scans.append(args[-1]) or candidates)
    real_make = cmr.client_content.make_caption
    def make(*args, **kwargs):
        generated.append((args[1], args[3], kwargs))
        return real_make(*args, **kwargs)
    monkeypatch.setattr(cmr.client_content, 'make_caption', make)
    # make_caption resolves config from the same module, so SB7 remains truly off.
    assert cmr._retry_drive_caption_history(
        account, draft, v, 'swift', 'swift_ig', target_day, 0, (), (), logs.append)
    assert scans == [64] and len(generated) == 1
    assert generated[0][0] is candidates[12]
    assert generated[0][1] == draft.creative_path == 'real_photo.jpg'
    assert generated[0][2]['verified'] == {'ok': True}
    assert draft.source_media_asset_id == 'photo-1' and draft.status == 'pending'
    assert draft.day_key == '2026-10-15'  # retries cannot redate or stage the draft
    assert any('history_180d' in log and '12' in log for log in logs)
    assert all(candidate.text not in '\n'.join(logs) for candidate in candidates)


def test_baseline_scan_uses_exact_photo_cta_key(monkeypatch):
    draft, v, logs = feed(), voice(), []
    candidate = source(1)
    keys=[]
    real_compose = cmr.client_content.compose_caption
    def compose(*args):
        keys.append(args[3])
        return real_compose(*args)
    monkeypatch.setattr(cmr.client_content, 'compose_caption', compose)
    monkeypatch.setattr(cmr, '_drive_history_sources', lambda *a: [candidate])
    assert invoke(draft, v, logs)
    assert keys == ['real_photo.jpg', 'real_photo.jpg']


def test_baseline_scan_rejects_banned_and_normalized_same_day_copy(monkeypatch):
    draft, v, logs = feed(), voice(), []
    candidates=[source(0, 'Forbidden training copy from the approved material.'), source(1)]
    account=SimpleNamespace(key='swift_ig', platform='instagram')
    same, _ = cmr.client_content.compose_caption(account, candidates[1], v, draft.creative_path)
    monkeypatch.setattr(cmr, '_drive_history_sources', lambda *a: candidates)
    monkeypatch.setattr(cmr.client_content, 'make_caption', lambda *a, **kw: pytest.fail('rejected baseline must not regenerate'))
    assert not invoke(draft, v, logs, banned=('forbidden',), day_captions=('  '+same.upper()+'  ',))
    assert draft.caption == 'Old approved copy'
    assert any('banned_word' in log and 'same_day' in log for log in logs)
    assert 'Forbidden training copy' not in '\n'.join(logs)


def test_baseline_scan_is_bounded_even_when_remaining_fact_is_fresh(monkeypatch):
    candidates=[source(i) for i in range(65)]
    draft, v, logs=feed(), voice(), []
    account=SimpleNamespace(key='swift_ig', platform='instagram')
    for candidate in candidates[:64]:
        caption, _=cmr.client_content.compose_caption(account, candidate, v, draft.creative_path)
        caption_ledger.record_staged('swift', caption, '2026-10-10')
    monkeypatch.setattr(cmr, '_drive_history_sources', lambda *a: candidates)
    monkeypatch.setattr(cmr.client_content, 'make_caption', lambda *a, **kw: pytest.fail('all 64 baselines repeat'))
    assert not invoke(draft, v, logs)
    assert any('checked 64 facts, found 0 candidates' in log for log in logs)
    assert draft.source_media_asset_id == 'photo-1' and draft.status == 'pending'


def test_generated_mode_remains_bounded_and_reports_real_gate_reason(monkeypatch):
    monkeypatch.setattr(cmr.config, 'sb7_enabled', lambda: True)
    candidates=[source(i) for i in range(64)]
    draft, v, logs=feed(), voice(), []
    scans, calls=[], []
    monkeypatch.setattr(cmr, '_drive_history_sources', lambda *a: scans.append(a[-1]) or candidates)
    monkeypatch.setattr(cmr.client_content, 'compose_caption', lambda *a: pytest.fail('generated copy cannot be predicted from baseline'))
    def make(*args, **kw):
        calls.append(args[1])
        return f'Thin {len(calls)}', []
    monkeypatch.setattr(cmr.client_content, 'make_caption', make)
    assert not invoke(draft, v, logs)
    assert scans == [7] and len(calls) == 7
    assert sum('rejected (a_plus)' in log for log in logs) == 7
    assert not any('history_180d' in log for log in logs)
    assert draft.source_media_asset_id == 'photo-1' and draft.status == 'pending'


def test_generated_history_rejection_is_distinguished_from_quality(monkeypatch):
    monkeypatch.setattr(cmr.config, 'sb7_enabled', lambda: True)
    candidate=source(1)
    draft, v, logs=feed(), voice(), []
    repeated='This approved training caption describes real coaching and guidance for gym members each day.'
    caption_ledger.record_staged('swift', repeated, '2026-10-10')
    monkeypatch.setattr(cmr, '_drive_history_sources', lambda *a: [candidate])
    monkeypatch.setattr(cmr.client_content, 'make_caption', lambda *a, **kw: (repeated, []))
    assert not invoke(draft, v, logs)
    assert any('rejected (history_180d)' in log for log in logs)
    assert not any('a_plus' in log for log in logs)


def test_baseline_mode_recaptions_remain_bounded_when_generator_cannot_change_copy(monkeypatch):
    candidates=[source(i) for i in range(40)]
    draft, v, logs=feed(), voice(), []
    calls=[]
    monkeypatch.setattr(cmr, '_drive_history_sources', lambda *a: candidates)
    monkeypatch.setattr(cmr, '_recaption_drive_draft', lambda *a, **kw: calls.append(kw['source']) or False)
    assert not invoke(draft, v, logs)
    assert len(calls) == 7 and calls == candidates[:7]
    assert any('exhausted 7 candidate facts' in log for log in logs)
    assert draft.caption == 'Old approved copy' and draft.source_media_asset_id == 'photo-1'


def test_expanded_source_walk_preserves_deterministic_distinct_approved_order(monkeypatch):
    from agent import client_sources
    pools={cat: [source(i + offset) for i in range(40)]
           for cat, offset in [('about', 0), ('service', 40), ('testimonial', 80)]}
    monkeypatch.setattr(cmr.client_content, '_pillars_for', lambda key: list(pools))
    monkeypatch.setattr(client_sources, 'approved_sources', lambda key, category=None: pools[category])
    monkeypatch.setattr(cmr, '_gym_drive_source_for', lambda *a: pools['about'][0])
    normal=cmr._drive_history_sources('swift_ig', '2026-10-15', 0)
    expanded=cmr._drive_history_sources('swift_ig', '2026-10-15', 0, 64)
    assert len(normal) == 7 and len(expanded) == 64
    assert expanded[:7] == normal
    assert len({s.text for s in expanded}) == 64
    assert all(s is not pools['about'][0] for s in expanded)
    assert expanded == cmr._drive_history_sources('swift_ig', '2026-10-15', 0, 64)
