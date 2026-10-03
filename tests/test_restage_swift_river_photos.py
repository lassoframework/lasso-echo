from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path

_PATH = Path(__file__).parents[1] / 'scripts' / 'restage_swift_river_photos.py'
_SPEC = importlib.util.spec_from_file_location('restage_swift_river_photos', _PATH)
script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(script)


def _rows():
    rows = []
    for day in script._TARGET_DAYS:
        for account, fmt in [('instagram', 'feed'), ('instagram', 'story'),
                             ('facebook', 'feed'), ('googlebusiness', 'update')]:
            rid = f'{day}-{account}-{fmt}'
            rows.append({'id': rid, 'gym_id': script._BASE, 'post_date': day,
                         'account': account, 'format': fmt, 'status': 'pending',
                         'image_url': f'https://cdn/igfill_{day}.jpg',
                         'source_media_url': None, 'source_media_asset_id': None,
                         'media_not_ready_reason': 'held'})
    return rows


class Calendar:
    def __init__(self, rows, fail_at=None):
        self.rows = {r['id']: r for r in rows}
        self.fail_at = fail_at
        self.stages = 0
        self.releases = 0

    def list_month(self, _gym, month):
        return [dict(r) for r in self.rows.values() if r['post_date'].startswith(month)]

    def get_row(self, _gym, rid):
        return dict(self.rows[rid])

    def restage_held_media(self, _gym, current, *, image_url=None, source_media_url=None,
                           extra_fields=None, release=False):
        rid = current['id']
        live = self.rows[rid]
        if live != current:
            return None
        if release:
            self.releases += 1
            live['media_not_ready_reason'] = None
        else:
            self.stages += 1
            if self.stages == self.fail_at:
                return None
            live.update(image_url=image_url, source_media_url=source_media_url,
                        **extra_fields)
        return dict(live)


class Media:
    def available(self):
        return True

    def get_asset(self, asset_id):
        return {'id': asset_id}


def _ctx(rows, fail_at=None):
    return {'account': object(), 'voice': object(), 'calendar': Calendar(rows, fail_at),
            'media': Media(), 'banned_words': (), 'library_path': '/unused'}


def _photos():
    return [{'id': str(i), 'gym_id': script._BASE, 'kind': 'photo',
             'moderation_json': {'observed_at': '2026-10-02T01:00:00+00:00'}}
            for i in range(9)]


def _run(monkeypatch, ctx, **kw):
    monkeypatch.setattr(script, '_new_pickable_photos', lambda *_: _photos())
    return script.run(ctx=ctx, start=date(2026, 10, 3), days=9,
                      moderated_since=datetime(2026, 10, 2, tzinfo=timezone.utc), **kw)


def test_missing_target_and_no_rebuild(monkeypatch):
    out = _run(monkeypatch, _ctx([]), apply=True, expected_digest='x')
    assert not out['ok'] and '36-row' in out['reason']


def test_unrelated_future_hold_does_not_block(monkeypatch):
    rows = _rows() + [{'id': 'later', 'gym_id': script._BASE,
                       'post_date': '2026-10-20', 'status': 'pending',
                       'image_url': 'https://cdn/other.jpg',
                       'media_not_ready_reason': 'hold'}]
    out = _run(monkeypatch, _ctx(rows))
    assert out['dry_run'] and out['target_rows'] == 36


def test_digest_race_fails_closed(monkeypatch):
    ctx = _ctx(_rows())
    digest = _run(monkeypatch, ctx)['expected_digest']
    ctx['calendar'].rows[script._TARGET_DAYS[0] + '-instagram-feed']['image_url'] = 'https://cdn/changed.jpg'
    out = _run(monkeypatch, ctx, apply=True, expected_digest=digest)
    assert not out['ok'] and ctx['calendar'].stages == 0


def test_partial_staging_retains_every_hold(monkeypatch):
    from agent import media_swap
    ctx = _ctx(_rows(), fail_at=2)
    digest = _run(monkeypatch, ctx)['expected_digest']
    def pick(_base, row, **kwargs):
        result = {'ok': True, 'image_url': 'https://cdn/new.jpg',
                  'source_media_url': None, 'source_media_asset_id': 'fresh',
                  'thumbnail_url': None, 'source': 'drive'}
        result['siblings'] = {str(s['id']): dict(result) for s in kwargs['siblings']}
        return result
    monkeypatch.setattr(media_swap, 'pick_replacement', pick)
    out = _run(monkeypatch, ctx, apply=True, expected_digest=digest)
    assert not out['ok'] and out['reason'] == 'partial staging; holds retained'
    assert ctx['calendar'].releases == 0
    assert all(r['media_not_ready_reason'] == 'held' for r in ctx['calendar'].rows.values())


def test_success_uses_distinct_daily_photos_and_reserves_before_release(monkeypatch):
    from agent import media_swap, gym_media_selector
    ctx = _ctx(_rows())
    digest = _run(monkeypatch, ctx)['expected_digest']
    stamps = []

    def pick(base, row, **kwargs):
        asset_id = kwargs['candidates_fn'](base, row)[0]['key']
        result = {'ok': True, 'image_url': f'https://cdn/{asset_id}.jpg',
                  'source_media_url': None, 'source_media_asset_id': asset_id,
                  'thumbnail_url': None, 'source': 'drive'}
        result['siblings'] = {str(s['id']): dict(result) for s in kwargs['siblings']}
        return result

    def stamp(asset, base, day, **kwargs):
        assert ctx['calendar'].releases == 0
        stamps.append((asset['id'], base, day))

    monkeypatch.setattr(media_swap, 'pick_replacement', pick)
    monkeypatch.setattr(gym_media_selector, 'stamp_use', stamp)
    out = _run(monkeypatch, ctx, apply=True, expected_digest=digest)
    assert out['ok'] and len(out['released_ids']) == 36
    assert len(stamps) == 9 and len({asset for asset, _, _ in stamps}) == 9
    assert all(r['media_not_ready_reason'] is None for r in ctx['calendar'].rows.values())
