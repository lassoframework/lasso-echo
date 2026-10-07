"""Swift River one-day duplicate captions must retry or fail before deletion."""
from datetime import date
from types import SimpleNamespace

import pytest
from agent import client_month_run as cmr, caption_ledger, portal_calendar_store as pcs


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('AGENT_DB_PATH', str(tmp_path / 'echo.db'))
    monkeypatch.setenv('AGENT_CAPTION_COOLDOWN', 'true')
    monkeypatch.setenv('AGENT_SB7_ENABLED', 'false')
    monkeypatch.setenv('AGENT_PLAN_HORIZON_DAYS', '0')
    monkeypatch.setattr(cmr.config, 'sb7_enabled', lambda: False)
    monkeypatch.setattr(pcs, '_media_stage_belt', lambda store, gym, rows, **kw: rows)
    monkeypatch.setattr(pcs, '_preserve_held_slots', lambda store, gym, rows: rows)
    monkeypatch.setattr(pcs, '_dedupe_slots', lambda store, gym, rows, **kw: rows)
    monkeypatch.setattr(pcs, 'preserve_and_prune', lambda store, gym, months, rows: (rows, {}))
    from agent import cadence, ops_alerts
    monkeypatch.setattr(cadence, 'resolve_posts_per_day', lambda *a, **kw: 1)
    monkeypatch.setattr(ops_alerts, 'alert', lambda *a, **kw: None)


class AdmissionStore:
    preflight_cadence_rows = pcs.SupabaseCalendarStore.preflight_cadence_rows

    def __init__(self):
        self.deleted = []
        self.inserted = []

    def delete_month(self, gym, month, preserve_dates=()):
        self.deleted.append(month)
        return 0

    def insert_rows(self, gym, rows, *, required_feed_slots=None, prevalidated_cadence=False):
        assert prevalidated_cadence
        assert required_feed_slots
        self.inserted.extend(rows)
        return rows


def rows(day, caption):
    common = dict(gym_id='swift', post_date=day, slot_index=0, caption=caption,
                  image_url='https://cdn.example/photo.jpg', status='pending')
    return [{**common, 'account': account, 'format': fmt}
            for account, fmt in [('instagram', 'feed'), ('facebook', 'feed'),
                                 ('instagram', 'story')]]


@pytest.mark.parametrize('day', ['2026-10-15', '2026-10-16'])
def test_one_day_verbatim_block_fails_before_delete(day):
    caption_ledger.record_staged('swift', 'Real approved caption', '2026-10-10')
    store = AdmissionStore()
    result = cmr._apply('swift', rows(day, 'Real approved caption'),
                        date.fromisoformat(day), 1, store, lambda msg: None)
    assert not result['ok']
    assert result['reason'] == 'incomplete cadence preflight'
    assert result['expected_feed_slots'] == 1
    assert result['admitted_feed_slots'] == result['inserted'] == result['deleted'] == 0
    assert store.deleted == store.inserted == []


def test_one_day_unique_caption_inserts_all_three_photo_companions():
    caption_ledger.record_staged('swift', 'Old approved caption', '2026-10-10')
    store = AdmissionStore()
    result = cmr._apply('swift', rows('2026-10-18', 'Fresh approved caption'),
                        date(2026, 10, 18), 1, store, lambda msg: None)
    assert result['ok'] and result['inserted'] == 3
    assert {r['image_url'] for r in store.inserted} == {'https://cdn.example/photo.jpg'}
    assert all(r['status'] == 'pending' for r in store.inserted)


def draft(caption):
    return SimpleNamespace(caption=caption, hashtags=[], creative_path='photo.jpg',
                           category='service', scheduled_for='2026-10-15T12:00:00Z')


def test_uploaded_source_walk_checks_history_on_real_target_date(monkeypatch):
    caption_ledger.record_staged('swift', 'Old approved caption', '2026-10-16')
    calls = []
    def build(account, day, *a, **kw):
        calls.append(day)
        return draft('Old approved caption' if len(calls) == 1 else 'Fresh approved caption')
    monkeypatch.setattr(cmr.client_content, 'build_client_draft', build)
    picked, reason = cmr._clean_draft_for_day(
        SimpleNamespace(key='swift_ig'), '2026-10-15', object(), 'library', (),
        lambda msg: None)
    assert picked.caption == 'Fresh approved caption'
    assert picked.day_key == '2026-10-15' and reason is None
    assert calls == ['2026-10-15', '2026-10-16']


@pytest.mark.parametrize('retry_caption, expected', [('Fresh approved caption', True),
                                                   ('Old approved caption', False)])
def test_drive_history_retry_keeps_same_photo_and_requires_unique_copy(
        monkeypatch, retry_caption, expected):
    caption_ledger.record_staged('swift', 'Old approved caption', '2026-10-10')
    feed = draft('Old approved caption')
    feed.source_media_asset_id = 'photo-1'
    retry = []
    rollback = []
    def recaption(account, d, *a):
        retry.append(d)
        d.caption = retry_caption
        return True
    monkeypatch.setattr(cmr, '_recaption_drive_draft', recaption)
    monkeypatch.setattr(cmr, '_rollback_drive_asset', lambda *a: rollback.append(a))
    monkeypatch.setattr(cmr, '_finish_feed_with_story', lambda account, d, *a, **kw: [d])
    failed, extra, covered = set(), [], set()
    result = cmr._stage_drive_draft(
        SimpleNamespace(key='swift_ig'), 'swift', 'swift_ig', 'instagram', feed,
        '2026-10-15', 0, 1, object(), (), [], 'library', lambda msg: None,
        failed, extra, covered, 'service', False)
    assert result is expected
    assert retry == [feed] and feed.source_media_asset_id == 'photo-1'
    assert bool(rollback) is not expected


def test_real_drive_recaption_uses_next_approved_source_and_original_grounding(monkeypatch):
    feed = draft('Old approved caption')
    feed.source_media_asset_id = 'photo-1'
    feed.caption_grounding = {'creative_name': 'member lifting', 'verified': {'ok': True}}
    approved = SimpleNamespace(text='Approved small group training fact')
    source_calls, generated = [], []
    def source_for(account_key, day_key, slot):
        source_calls.append((account_key, day_key, slot))
        return approved
    def make_caption(account, source, voice, creative_key, **kwargs):
        generated.append((source, creative_key, kwargs))
        return 'Fresh approved caption', ['#Gym']
    monkeypatch.setattr(cmr, '_gym_drive_source_for', source_for)
    monkeypatch.setattr(cmr.client_content, 'make_caption', make_caption)
    assert cmr._recaption_drive_draft(SimpleNamespace(key='swift_ig'), feed, object(),
                                    'swift_ig', '2026-10-15', 0, lambda msg: None)
    assert source_calls == [('swift_ig', '2026-10-15', 1)]
    assert generated[0][0] is approved and generated[0][1] == 'photo.jpg'
    assert generated[0][2]['verified'] == {'ok': True}
    assert generated[0][2]['avoid_openings']
    assert feed.source_media_asset_id == 'photo-1'
    assert feed.caption == 'Fresh approved caption' and feed.hashtags == ['#Gym']


def test_history_guard_off_does_not_retry(monkeypatch):
    monkeypatch.setenv('AGENT_CAPTION_COOLDOWN', 'false')
    monkeypatch.setattr(caption_ledger, 'is_verbatim_blocked',
                        lambda *a: pytest.fail('ledger must stay unused'))
    assert not cmr._caption_repeats_history('swift', 'Copy', '2026-10-15')


def test_recaption_does_not_invent_copy_without_approved_source(monkeypatch):
    feed = draft('Old approved caption')
    monkeypatch.setattr(cmr, '_gym_drive_source_for', lambda *a: None)
    monkeypatch.setattr(cmr.client_content, 'make_caption',
                        lambda *a, **kw: pytest.fail('no approved source'))
    assert not cmr._recaption_drive_draft(SimpleNamespace(key='swift_ig'), feed, object(),
                                        'swift_ig', '2026-10-15', 0, lambda msg: None)
    assert feed.caption == 'Old approved caption'
