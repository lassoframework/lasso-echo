from urllib.parse import urlencode

import pytest

from agent import fixer_evidence as evidence
from agent import fixer_ops


TICKET = '234b8861-9fa9-5673-a2bd-10bbdc412b86'
GYM = 'crossfitchateau813e78'
FOLDER = 'exact-drive-folder'


def fixture():
    tables = {
        'support_tickets': [{'id': TICKET, 'product': 'echo', 'client_id': 'portal-uuid'}],
        'media_source': [{'id': 'source-1', 'gym_id': GYM, 'kind': 'gym_drive',
                          'folder_id': FOLDER, 'active': True,
                          'revoked_externally': False, 'sync_status': 'ready',
                          'sync_finished_at': '2026-09-18T10:00:00Z',
                          'sync_error': None}],
        'media_asset': [{'id': 'asset-1', 'gym_id': GYM, 'source_id': 'source-1'}],
    }
    calls = []
    def read(table, params):
        calls.append((table, params))
        if table == 'media_asset':
            rows = sorted(tables[table], key=lambda row: row['id'])
            if 'id' in params:
                rows = [row for row in rows if row['id'] > params['id'][3:]]
            return rows[:int(params['limit'])]
        return tables[table]
    return {'read': read, 'resolve': lambda _: 'portal-uuid'}, tables, calls


def test_valid_readback_is_bounded_and_read_only(monkeypatch):
    monkeypatch.setenv('FIXER_OPS_SECRET', 'test-secret')
    deps, _, calls = fixture()
    path = '/ops/actions/evidence/media-source/' + TICKET + '?' + urlencode({'gym_key': GYM, 'folder_id': FOLDER})
    assert fixer_ops.handle('GET', path, lambda *_: '')[0] == 401
    status, body = fixer_ops.handle('GET', path, lambda *_: 'test-secret', deps={'evidence': deps})
    assert status == 200
    assert body['source'] == {'status': 'found', 'active': True, 'revoked_externally': False,
                              'sync_status': 'ready', 'sync_finished_at': '2026-09-18T10:00:00Z',
                              'sync_error_present': False}
    assert body['assets'] == {'status': 'available', 'count': 1}
    assert [t for t, _ in calls] == ['support_tickets', 'media_source', 'media_asset', 'media_asset']
    assert calls[1][1]['folder_id'] == 'eq.' + FOLDER
    assert calls[2][1]['source_id'] == 'eq.source-1'
    assert calls[3][1]['id'] == 'gt.asset-1'


def test_wrong_tenant_and_ambiguous_folder_fail_closed():
    deps, tables, calls = fixture()
    tables['support_tickets'][0]['client_id'] = 'foreign'
    with pytest.raises(evidence.EvidenceError, match='ticket_tenant_mismatch'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)
    assert [t for t, _ in calls] == ['support_tickets']
    tables['support_tickets'][0]['client_id'] = 'portal-uuid'
    tables['media_source'].append(dict(tables['media_source'][0], id='source-2'))
    with pytest.raises(evidence.EvidenceError, match='media_folder_ambiguous'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)


def test_store_unavailable_and_source_asset_mismatch():
    deps, tables, _ = fixture()
    def unavailable(*_):
        raise evidence.EvidenceError('store_unavailable')
    deps['read'] = unavailable
    with pytest.raises(evidence.EvidenceError, match='store_unavailable'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)
    deps, tables, _ = fixture()
    tables['media_source'][0]['gym_id'] = 'foreign'
    with pytest.raises(evidence.EvidenceError, match='media_source_tenant_mismatch'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)
    tables['media_source'][0]['gym_id'] = GYM
    tables['media_asset'][0]['source_id'] = 'foreign'
    with pytest.raises(evidence.EvidenceError, match='media_asset_scope_mismatch'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)


def test_missing_source_is_unknown_and_error_text_is_withheld():
    deps, tables, _ = fixture()
    tables['media_source'] = []
    result = evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)
    assert result['source'] == {'status': 'unknown'}
    assert result['assets'] == {'status': 'unknown', 'count': None}
    tables['media_source'] = fixture()[1]['media_source']
    tables['media_source'][0]['sync_error'] = 'credential secret'
    result = evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)
    assert result['source']['sync_error_present'] is True
    assert 'credential secret' not in str(result)


def test_server_row_cap_never_masquerades_as_exact_asset_count():
    deps, tables, calls = fixture()
    tables['media_asset'] = [dict(tables['media_asset'][0], id=f'asset-{n:04d}')
                             for n in range(1201)]
    original = deps['read']
    def capped(table, params):
        rows = original(table, params)
        return rows[:400] if table == 'media_asset' else rows
    deps['read'] = capped
    result = evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)
    assert result['assets'] == {'status': 'available', 'count': 1201}
    assert [params.get('id') for table, params in calls if table == 'media_asset'] == [
        None, 'gt.asset-0399', 'gt.asset-0799', 'gt.asset-1199', 'gt.asset-1200']


@pytest.mark.parametrize('bad_row', [
    {'id': 'asset-2', 'gym_id': 'foreign', 'source_id': 'source-1'},
    {'id': 'asset-2', 'gym_id': GYM, 'source_id': 'foreign'},
])
def test_later_page_scope_mismatch_fails_closed(bad_row):
    deps, tables, _ = fixture()
    tables['media_asset'] = [dict(tables['media_asset'][0], id=f'asset-{n:04d}')
                             for n in range(evidence.MEDIA_PAGE_SIZE)]
    bad_row['id'] = 'asset-0500'
    tables['media_asset'].append(bad_row)
    with pytest.raises(evidence.EvidenceError, match='media_asset_scope_mismatch'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)


def test_nonadvancing_or_unbounded_pages_fail_closed():
    deps, tables, _ = fixture()
    original = deps['read']
    def stale(table, params):
        if table == 'media_asset':
            return tables['media_asset']
        return original(table, params)
    deps['read'] = stale
    with pytest.raises(evidence.EvidenceError, match='media_asset_count_unavailable'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)

    deps, tables, _ = fixture()
    tables['media_asset'] = [dict(tables['media_asset'][0], id=f'asset-{n:05d}')
                             for n in range(evidence.MEDIA_MAX_ASSETS + 1)]
    with pytest.raises(evidence.EvidenceError, match='media_asset_count_unavailable'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)


def test_count_timeout_and_later_read_error_never_return_partial_count(monkeypatch):
    deps, tables, _ = fixture()
    tables['media_asset'] = [dict(tables['media_asset'][0], id=f'asset-{n:04d}')
                             for n in range(evidence.MEDIA_PAGE_SIZE + 1)]
    original = deps['read']
    def later_error(table, params):
        if table == 'media_asset' and 'id' in params:
            raise evidence.EvidenceError('store_unavailable')
        return original(table, params)
    deps['read'] = later_error
    with pytest.raises(evidence.EvidenceError, match='store_unavailable'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)

    clock = iter([0, 0, evidence.MEDIA_MAX_SECONDS + 1])
    monkeypatch.setattr(evidence.time, 'monotonic', lambda: next(clock))
    deps, tables, _ = fixture()
    with pytest.raises(evidence.EvidenceError, match='media_asset_count_unavailable'):
        evidence.inspect_media_source(TICKET, GYM, FOLDER, deps=deps)
