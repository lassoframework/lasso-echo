"""Drive feeds must pass the final caption belt before acquiring companions."""
from datetime import date
from types import SimpleNamespace

import pytest

from agent import caption_ledger, client_month_run as cmr, gym_media_builder
from agent.voice import VoiceDoc


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setenv('AGENT_CAPTION_COOLDOWN', 'true')
    monkeypatch.setattr(cmr.config, 'sb7_enabled', lambda: False)


def setup(monkeypatch, candidates):
    account = SimpleNamespace(key='swift_ig', platform='instagram')
    voice = VoiceDoc(raw='Approved voice', hashtags=[], ctas=['Ask us about training.'])
    original = SimpleNamespace(text='Our coaches support members through each training session.', category='service')
    repeated, _ = cmr.client_content.compose_caption(account, original, voice, 'safe.jpg')
    monkeypatch.setattr(cmr, '_gym_drive_source_for', lambda *a: original)
    monkeypatch.setattr(cmr, '_drive_history_sources', lambda *a: candidates)
    built, finished, rolled_back, generated = [], [], [], []

    def build(account, day, *a, **kw):
        feed = SimpleNamespace(caption=repeated, hashtags=[], creative_path='safe.jpg',
                               creative_public_url=f'https://cdn/{day}.jpg',
                               source_media_asset_id=f'photo-{day}', category='service',
                               status='pending', day_key=day, is_story=False,
                               caption_grounding={'creative_name': 'member training',
                                                  'verified': {'ok': True}})
        built.append(feed)
        return feed

    def finish(account, feed, *a, **kw):
        finished.append((feed.source_media_asset_id, feed.caption))
        return [feed]

    real_make = cmr.client_content.make_caption
    def make(*args, **kwargs):
        generated.append((args[1], args[3], kwargs))
        return real_make(*args, **kwargs)

    monkeypatch.setattr(gym_media_builder, 'build_gym_media_draft', build)
    monkeypatch.setattr(cmr, '_finish_feed_with_story', finish)
    monkeypatch.setattr(cmr, '_rollback_drive_asset', lambda d, *a: rolled_back.append(d.source_media_asset_id))
    monkeypatch.setattr(cmr.client_content, 'make_caption', make)
    return account, voice, repeated, built, finished, rolled_back, generated


def fact(text):
    return SimpleNamespace(text=text, category='service')


def test_drive_cross_day_retries_use_unused_approved_facts_and_same_photo(monkeypatch):
    candidates = [fact('Small group classes give members focused coaching.'),
                  fact('Members can build strength with consistent practice.'),
                  fact('Training sessions help members learn movement skills.')]
    account, voice, repeated, built, finished, rollback, generated = setup(monkeypatch, candidates)
    # Persisted history is a separate constraint; the first alternative is still blocked.
    old, _ = cmr.client_content.compose_caption(account, candidates[0], voice, 'safe.jpg')
    caption_ledger.record_staged('swift', cmr._drive_staged_caption(old), '2026-10-01')
    extra = cmr.append_gym_drive_drafts(account, 'swift', date(2026, 10, 15), 3, voice,
                                      log=lambda m: None, covered_days=())
    assert len(extra) == len(finished) == len(built) == 3
    assert len({cmr._drive_build_caption_hash(d.caption) for d in extra}) == 3
    assert extra[0].caption == repeated
    assert extra[1].caption.startswith(candidates[1].text)
    assert extra[2].caption.startswith(candidates[2].text)
    assert not rollback
    assert all(d.source_media_asset_id == f'photo-{d.day_key}' for d in extra)
    assert all(path == 'safe.jpg' and kw['verified'] == {'ok': True}
               for _, path, kw in generated)
    assert all(d.status == 'pending' for d in extra)
    # Keep the downstream belt active: all three logical posts and their FB
    # mirrors must survive its independent within-batch and ledger checks.
    from agent.portal_calendar_store import _stage_belts
    payload = [dict(gym_id='swift', post_date=d.day_key, format='feed',
                    account=platform, status='pending', image_url=d.creative_public_url,
                    caption=cmr._drive_staged_caption(d.caption))
               for d in extra for platform in ('instagram', 'facebook')]
    assert _stage_belts('swift', payload) == payload
    assert not any(caption_ledger.is_verbatim_blocked('swift', d.caption, d.day_key) for d in extra)


def test_drive_seeded_prior_lane_caption_triggers_retry_after_final_hook_normalization(monkeypatch):
    candidates = [fact('Our coaches help members practice safe movement.')]
    account, voice, _, built, finished, rollback, _ = setup(monkeypatch, candidates)
    # Store formatting rewrites semicolons and bounds the long opening line.
    prefix = 'Members practice strength and movement with patient coaching ' * 5
    raw = prefix + 'Coaches guide practice; members learn movement.'
    prior = prefix + 'Coaches guide practice, members learn movement.'
    assert raw != prior
    assert cmr._drive_build_caption_hash(raw) == cmr._drive_build_caption_hash(prior)
    original_builder = gym_media_builder.build_gym_media_draft
    def build(*a, **kw):
        feed = original_builder(*a, **kw)
        feed.caption = raw
        return feed
    monkeypatch.setattr(gym_media_builder, 'build_gym_media_draft', build)
    extra = cmr.append_gym_drive_drafts(account, 'swift', date(2026, 10, 15), 1, voice,
                                      log=lambda m: None, covered_days=(),
                                      day_captions_seed={'2026-10-14': [prior]})
    assert len(extra) == len(finished) == 1
    assert cmr._drive_build_caption_hash(extra[0].caption) != cmr._drive_build_caption_hash(prior)
    assert extra[0].source_media_asset_id == built[0].source_media_asset_id
    assert not rollback


def test_drive_exhausted_build_copy_rolls_back_without_finishing_duplicate(monkeypatch):
    account, voice, repeated, built, finished, rollback, _ = setup(monkeypatch, [])
    extra = cmr.append_gym_drive_drafts(account, 'swift', date(2026, 10, 15), 2, voice,
                                      log=lambda m: None, covered_days=())
    assert len(extra) == len(finished) == 1
    assert extra[0].caption == repeated
    assert rollback == ['photo-2026-10-16']
    assert built[1] not in extra


@pytest.mark.parametrize('collision_path', ['initial', 'single_retry'])
def test_drive_normalized_history_collision_retries_before_companions(monkeypatch, collision_path):
    candidates = [fact('Our coaches help members practice safe movement.')]
    account, voice, repeated, built, finished, rollback, generated = setup(monkeypatch, candidates)
    raw = 'Coaches guide practice; members learn movement.'
    historical = cmr._drive_staged_caption(raw)
    assert raw != historical
    caption_ledger.record_staged('swift', historical, '2026-10-01')
    assert not caption_ledger.is_verbatim_blocked('swift', raw, '2026-10-15')
    assert caption_ledger.is_verbatim_blocked('swift', historical, '2026-10-15')
    kwargs = {}
    if collision_path == 'initial':
        original_builder = gym_media_builder.build_gym_media_draft
        def build(*a, **kw):
            feed = original_builder(*a, **kw)
            feed.caption = raw
            return feed
        monkeypatch.setattr(gym_media_builder, 'build_gym_media_draft', build)
    else:
        # A build duplicate triggers one ordinary regeneration, which returns
        # raw copy whose formatted value is already present in the ledger.
        kwargs['day_captions_seed'] = {'2026-10-14': [repeated]}
        original_make = cmr.client_content.make_caption
        calls = []
        def make(*a, **kw):
            calls.append(kw)
            return (raw, []) if len(calls) == 1 else original_make(*a, **kw)
        monkeypatch.setattr(cmr.client_content, 'make_caption', make)
    extra = cmr.append_gym_drive_drafts(account, 'swift', date(2026, 10, 15), 1, voice,
                                      log=lambda m: None, covered_days=(), **kwargs)
    assert len(extra) == len(finished) == 1
    assert cmr._drive_staged_caption(extra[0].caption) != historical
    assert not cmr._drive_caption_repeats_history('swift', extra[0].caption, extra[0].day_key)
    assert extra[0].source_media_asset_id == built[0].source_media_asset_id
    assert not rollback
    if collision_path == 'single_retry':
        assert len(calls) == 2
        assert extra[0].caption.startswith(candidates[0].text)


def test_drive_build_duplicates_do_not_retry_when_caption_cooldown_is_off(monkeypatch):
    account, voice, repeated, built, finished, rollback, generated = setup(monkeypatch, [])
    monkeypatch.setenv('AGENT_CAPTION_COOLDOWN', 'false')
    caption_ledger.record_staged('swift', repeated, '2026-10-01')
    extra = cmr.append_gym_drive_drafts(
        account, 'swift', date(2026, 10, 15), 3, voice,
        log=lambda m: None, covered_days=(),
        day_captions_seed={'2026-10-14': [repeated]})
    assert len(extra) == len(built) == len(finished) == 3
    assert all(d.caption == repeated for d in extra)
    assert not generated, 'flag-off duplicates must not invoke recaption'
    assert not rollback
    from agent.portal_calendar_store import _stage_belts
    payload = [dict(gym_id='swift', post_date=d.day_key, format='feed',
                    account='instagram', status='pending', image_url=d.creative_public_url,
                    caption=d.caption) for d in extra]
    assert _stage_belts('swift', payload) == payload


def test_missing_protected_caption_reader_refuses_only_when_slots_exist():
    store = SimpleNamespace()
    assert cmr._protected_feed_captions('swift', date(2026, 10, 15), 1, store,
                                       lambda m: None, require_protected=False) == {}
    with pytest.raises(RuntimeError, match='protected caption reader unavailable'):
        cmr._protected_feed_captions('swift', date(2026, 10, 15), 1, store,
                                     lambda m: None, require_protected=True)
