from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.slack_convo import classifier as c
from agent import media_reuse_policy as p


@pytest.mark.parametrize('text', [
    'I just uploaded some creative from a recent photo shoot we did. Can we replace the photos on the website?',
    'Please update the photos on our website.',
    'Could you swap the images on the homepage?',
])
def test_website_media_requests_are_work(text):
    assert c.classify(text, has_open_ticket=False, identity_product='portal') == c.CODE_FIX


@pytest.mark.parametrize('text', [
    'How do I replace photos on the website?',
    'Can you explain how to change website photos?',
    'Can we replace photos on Instagram?',
    'Can we replace the photos on the website and Facebook?',
])
def test_how_to_and_other_products_keep_existing_route(text):
    assert c.classify(text, has_open_ticket=False, identity_product='portal') == c.QUESTION


GYM = 'zanshinfitness630e22'
NOW = datetime(2026, 9, 27, 20, tzinfo=timezone.utc)


def test_nine_months_is_calendar_months():
    assert p.months_before(datetime(2026, 11, 30), 9) == datetime(2026, 2, 28)
    assert p.months_before(NOW, 9).isoformat() == '2025-12-27T20:00:00+00:00'
    assert p.reuse_months(GYM + '_fb') == 9
    assert p.reuse_months('other') == 0


@pytest.mark.parametrize('platform', ['facebook', 'instagram', 'googlebusiness'])
def test_published_photo_blocks_other_platform_and_same_day(platform):
    row = dict(id='new', gym_id=GYM, image_url='https://new/photo.jpg', account=platform)
    old = dict(id='old', gym_id=GYM, image_url='https://old/photo.jpg?x=1',
               status='published', published_at=NOW.isoformat())
    seen = []
    def history(gym, cutoff):
        seen.append((gym, cutoff))
        return [old]
    store = SimpleNamespace(list_media_publish_history=history)
    assets = SimpleNamespace(list_assets=lambda gym: [])
    assert p.publish_hold_reason(row, GYM, store, now=NOW, media_store=assets) == 'media_reuse_nine_month_hold'
    assert seen == [(GYM, '2025-12-27T20:00:00+00:00')]


def test_duplicate_asset_hash_and_read_outage():
    assets = [dict(id=x, gym_id=GYM, content_hash='same') for x in ['a', 'b']]
    row = dict(id='new', source_media_asset_id='b')
    old = dict(id='old', gym_id=GYM, source_media_asset_id='a')
    media = SimpleNamespace(list_assets=lambda gym: assets)
    store = SimpleNamespace(list_media_publish_history=lambda *args: [old])
    assert p.publish_hold_reason(row, GYM, store, now=NOW, media_store=media) == 'media_reuse_nine_month_hold'
    assert p.publish_hold_reason(row, GYM, object(), now=NOW, media_store=media) == 'media_reuse_history_unavailable'
    assert p.publish_hold_reason(row, 'other', object()) is None


def test_new_photo_and_same_row_retry():
    row = dict(id='new', gym_id=GYM, image_url='https://cdn/new.jpg')
    store = SimpleNamespace(list_media_publish_history=lambda *args: [row])
    media = SimpleNamespace(list_assets=lambda gym: [])
    assert p.publish_hold_reason(row, GYM, store, now=NOW, media_store=media) is None
