from agent.portal_calendar_store import SupabaseCalendarStore
import pytest


class Response:
    status_code = 200
    text = ''
    def __init__(self, rows):
        self.rows = rows
    def json(self):
        return self.rows


class HTTP:
    def __init__(self, row, race=False):
        self.row = dict(row)
        self.race = race
        self.calls = []
    def patch(self, _url, *, params, headers, json, timeout):
        self.calls.append((params, json))
        if self.race:
            return Response([])
        self.row.update(json)
        return Response([dict(self.row)])


def _row():
    return {'id': 'r1', 'gym_id': 'swift', 'post_date': '2026-10-03',
            'status': 'pending', 'variant_status': 'active', 'account': 'instagram',
            'format': 'story', 'image_url': 'https://cdn/igfill_old.jpg',
            'source_media_url': 'https://cdn/igfill_old.jpg',
            'source_media_asset_id': None, 'media_not_ready_reason': 'hold'}


def test_stage_retains_hold_and_clears_stale_source():
    row = _row()
    http = HTTP(row)
    store = SupabaseCalendarStore(url='https://example.test', service_key='key', http=http)
    result = store.restage_held_media('swift', row, image_url='https://cdn/new.jpg',
                                      source_media_url=None,
                                      extra_fields={'source_media_asset_id': 'drive1'})
    assert result['media_not_ready_reason'] == 'hold'
    assert result['source_media_url'] is None
    assert http.calls[0][0]['image_url'] == 'eq.https://cdn/igfill_old.jpg'
    assert http.calls[0][0]['media_not_ready_reason'] == 'eq.hold'
    released = store.restage_held_media('swift', result, release=True)
    assert released['media_not_ready_reason'] is None
    assert http.calls[1][1] == {'media_not_ready_reason': None}


def test_server_race_returns_no_match():
    row = _row()
    http = HTTP(row, race=True)
    store = SupabaseCalendarStore(url='https://example.test', service_key='key', http=http)
    assert store.restage_held_media('swift', row, image_url='https://cdn/new.jpg') is None
    assert http.row == row


def test_historical_hold_is_complete_row_cas_and_changes_only_reason():
    row = _row()
    row['media_not_ready_reason'] = None
    http = HTTP(row)
    store = SupabaseCalendarStore(url='https://example.test', service_key='key', http=http)
    reason = 'Historical igfill card held: archive versus real-photo restage decision pending'
    result = store.hold_pending_media('swift', row, reason)
    assert result['media_not_ready_reason'] == reason
    params, payload = http.calls[0]
    assert payload == {'media_not_ready_reason': reason}
    assert params['status'] == 'eq.pending'
    assert params['image_url'] == 'eq.https://cdn/igfill_old.jpg'
    assert params['source_media_url'] == 'eq.https://cdn/igfill_old.jpg'
    assert params['media_not_ready_reason'] == 'is.null'


def test_pending_media_read_includes_variants_and_requires_exact_count():
    class ReadHTTP:
        def __init__(self, count):
            self.count = count
        def get(self, _url, *, params, headers, timeout):
            assert params['status'] == 'eq.pending'
            assert 'variant_status' not in params
            response = Response([_row()])
            response.headers = {'Content-Range': f'0-0/{self.count}'}
            return response
    store = SupabaseCalendarStore(url='https://example.test', service_key='key', http=ReadHTTP(1))
    assert len(store.list_pending_media_between('swift', '2026-10-01', '2026-10-03')) == 1
    store = SupabaseCalendarStore(url='https://example.test', service_key='key', http=ReadHTTP(2))
    with pytest.raises(ValueError, match='incomplete'):
        store.list_pending_media_between('swift', '2026-10-01', '2026-10-03')
