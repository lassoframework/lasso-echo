import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from agent import fixer_business_evidence as be

GYM = 'testgym'
OTHER = 'othergym'
REQ = 'req-' + 'a' * 32
SHA = 'b' * 40
NOW = datetime(2026, 9, 18, 12, 30, tzinfo=timezone.utc)
ROW = {'id': 'row123', 'gym_id': GYM, 'status': 'published'}
GRADE = {'gym_id': GYM, 'window': 'forward_book', 'total': 92,
         'graded_at': '2026-09-18T00:00:00Z'}
SOURCE_ROW = {'id': 'src1', 'gym_id': GYM, 'folder_id': 'folderABC',
              'active': True, 'revoked_externally': False, 'sync_status': 'ready'}


def make_reader(tables):
    """Fake PostgREST-style reader: honors eq. filters, records every call."""
    calls = []

    def read(table, params):
        calls.append((table, dict(params)))
        out = []
        for row in tables.get(table, []):
            match = True
            for key, val in params.items():
                if key in ('select', 'order', 'limit'):
                    continue
                if isinstance(val, str) and val.startswith('eq.'):
                    if row.get(key) != val[3:]:
                        match = False
            if match:
                out.append(dict(row))
        return out

    read.calls = calls
    return read


_DEFAULT_DEPS = object()


def run(check_id, tables, deps=_DEFAULT_DEPS, **kw):
    """deps=_DEFAULT_DEPS installs the fake reader; an explicit deps (including
    None) is passed through untouched so missing-reader paths stay reachable."""
    if deps is _DEFAULT_DEPS:
        reader = make_reader(tables)
        deps = {'read': reader}
    else:
        reader = deps.get('read') if isinstance(deps, dict) else None
    args = dict(gym_key=GYM, request_key=REQ, merged_sha=SHA, deps=deps, now=NOW)
    args.update(kw)
    result = be.observe(check_id, **args)
    assert json.dumps(result)  # every record must be durable
    return result, reader


# -- verification only on genuine readback agreement -------------------------------

def test_calendar_row_verified_record_matches_consumer_shape():
    result, reader = run('calendar_row_status', {'content_calendar': [ROW]},
                         params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.VERIFIED
    # The exact field contract slack_convo/outbox._verified_fix_notice consumes.
    assert result['source'] == 'independent_business_check'
    assert result['verified'] is True and result['symptom_resolved'] is True
    assert result['check_id'] == 'calendar_row_status'
    assert result['evidence'] == 'calendar_row:row123:published'
    assert result['request_key'] == REQ and result['merged_sha'] == SHA
    assert result['gym_key'] == GYM and result['reason'] == ''
    assert result['captured_at'] == NOW.isoformat()
    assert reader.calls == [('content_calendar', {
        'id': 'eq.row123', 'gym_id': 'eq.testgym',
        'select': 'id,gym_id,status', 'limit': '2'})]
    assert be.binding_matches(result, gym_key=GYM, request_key=REQ, merged_sha=SHA)


def test_grade_and_media_source_verify_on_readback():
    grade, _ = run('forward_book_grade_at_least', {'gym_social_grades': [GRADE]},
                   params={'min_total': 90})
    assert grade['outcome'] == be.VERIFIED and grade['evidence'] == 'forward_book_grade:92'
    media, _ = run('media_source_active', {'media_source': [SOURCE_ROW]},
                   params={'folder_id': 'folderABC'})
    assert media['outcome'] == be.VERIFIED
    assert media['evidence'] == 'media_source:folderABC:ready'


# -- unknown checks fail closed ----------------------------------------------------

@pytest.mark.parametrize('check_id', [
    'no_such_check', '', 'eval("1")', '__import__', '../config',
    'calendar_row_status; drop table', 'Calendar_Row_Status'])
def test_unknown_check_fails_closed_and_runs_nothing(check_id):
    result, reader = run(check_id, {'content_calendar': [ROW]},
                         params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.UNVERIFIED
    assert result['verified'] is False and result['symptom_resolved'] is None
    assert result['reason'] == 'unknown_check' and result['evidence'] == ''
    assert reader.calls == []


def test_registry_is_frozen_and_catalog_is_explicit():
    with pytest.raises(TypeError):
        be.CHECKS['injected'] = object()
    catalog = be.catalog()
    assert catalog['ok'] is True
    assert {c['check_id'] for c in catalog['checks']} == set(be.CHECKS)
    assert all(c['description'] for c in catalog['checks'])


# -- no free-text proof ------------------------------------------------------------

def test_no_prose_proof_channel_exists():
    with pytest.raises(TypeError):
        be.observe('calendar_row_status', gym_key=GYM, request_key=REQ,
                   merged_sha=SHA, proof='trust me, it is fixed')
    with pytest.raises(TypeError):
        be.observe('calendar_row_status', gym_key=GYM, request_key=REQ,
                   merged_sha=SHA, verified=True)


@pytest.mark.parametrize('params', [
    {'row_id': 'row123', 'expected_status': 'it is fixed now'},
    {'row_id': 'row123', 'expected_status': 'PUBLISHED'},
    {'row_id': 'row123'},
    {'row_id': 'the row that had the bug', 'expected_status': 'published'},
    {'row_id': 123, 'expected_status': 'published'},
])
def test_prose_or_malformed_params_can_never_verify(params):
    result, reader = run('calendar_row_status', {'content_calendar': [ROW]},
                         params=params)
    assert result['outcome'] == be.UNVERIFIED and result['verified'] is False
    assert result['reason'] == 'bad_params'
    assert reader.calls == []


def test_check_returning_match_without_evidence_cannot_verify(monkeypatch):
    lazy = be.CheckSpec('lazy', lambda ctx: be.Observation(True, True, ''))
    monkeypatch.setattr(be, 'CHECKS', {'lazy': lazy})
    result, _ = run('lazy', {})
    assert result['outcome'] == be.UNKNOWN and result['verified'] is False
    assert result['reason'] == 'result_invalid'


# -- readback disagreement is UNVERIFIED, never a guess -----------------------------

def test_calendar_row_status_mismatch_reports_actual_state():
    row = {**ROW, 'status': 'pending'}
    result, _ = run('calendar_row_status', {'content_calendar': [row]},
                    params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.UNVERIFIED
    assert result['verified'] is False and result['symptom_resolved'] is False
    assert result['reason'] == 'status_mismatch'
    assert result['evidence'] == 'calendar_row:row123:pending'


def test_calendar_row_absent_and_unreadable_status():
    absent, _ = run('calendar_row_status', {'content_calendar': []},
                    params={'row_id': 'row123', 'expected_status': 'published'})
    assert absent['reason'] == 'row_not_found' and absent['verified'] is False
    bad = {**ROW, 'status': None}
    unreadable, _ = run('calendar_row_status', {'content_calendar': [bad]},
                        params={'row_id': 'row123', 'expected_status': 'published'})
    assert unreadable['reason'] == 'status_unreadable'
    assert unreadable['verified'] is False


def test_grade_below_minimum_and_grade_absent():
    below, _ = run('forward_book_grade_at_least',
                   {'gym_social_grades': [{**GRADE, 'total': 89}]},
                   params={'min_total': 90})
    assert below['outcome'] == be.UNVERIFIED
    assert below['reason'] == 'grade_below_minimum'
    assert below['evidence'] == 'forward_book_grade:89'
    none, _ = run('forward_book_grade_at_least', {'gym_social_grades': []},
                  params={'min_total': 90})
    assert none['reason'] == 'grade_not_found' and none['verified'] is False


@pytest.mark.parametrize('minimum', [90.5, '90', True, -1, 101, None])
def test_grade_minimum_must_be_an_int_0_to_100(minimum):
    result, reader = run('forward_book_grade_at_least',
                         {'gym_social_grades': [GRADE]}, params={'min_total': minimum})
    assert result['reason'] == 'bad_params' and result['verified'] is False
    assert reader.calls == []


@pytest.mark.parametrize('row,reason', [
    ({**SOURCE_ROW, 'active': False}, 'source_inactive'),
    ({**SOURCE_ROW, 'revoked_externally': True}, 'source_revoked'),
    ({**SOURCE_ROW, 'sync_status': 'failed'}, 'source_sync_failed'),
])
def test_media_source_unhealthy_variants(row, reason):
    result, _ = run('media_source_active', {'media_source': [row]},
                    params={'folder_id': 'folderABC'})
    assert result['outcome'] == be.UNVERIFIED and result['verified'] is False
    assert result['reason'] == reason


def test_media_source_absent_and_ambiguous():
    absent, _ = run('media_source_active', {'media_source': []},
                    params={'folder_id': 'folderABC'})
    assert absent['reason'] == 'source_not_found' and absent['verified'] is False
    dup = [SOURCE_ROW, {**SOURCE_ROW, 'id': 'src2'}]
    ambiguous, _ = run('media_source_active', {'media_source': dup},
                       params={'folder_id': 'folderABC'})
    assert ambiguous['outcome'] == be.UNKNOWN
    assert ambiguous['reason'] == 'source_ambiguous'


# -- reader unavailable / failing / partial -> UNKNOWN -------------------------------

@pytest.mark.parametrize('deps', [None, {}, {'read': None}, {'read': 'not-callable'}])
def test_missing_reader_is_unknown_not_unverified(deps):
    result, _ = run('calendar_row_status', {}, deps=deps,
                    params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.UNKNOWN and result['verified'] is False
    assert result['symptom_resolved'] is None
    assert result['reason'] == 'reader_unavailable'


def test_failing_reader_is_unknown_and_leaks_nothing():
    def boom(_table, _params):
        raise RuntimeError('supabase service key sk-live-secret refused')

    result, _ = run('calendar_row_status', {}, deps={'read': boom},
                    params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.UNKNOWN and result['verified'] is False
    assert result['reason'] == 'reader_unavailable'
    dumped = json.dumps(result)
    assert 'sk-live-secret' not in dumped and 'supabase' not in dumped


@pytest.mark.parametrize('rows', [
    [ROW, {**ROW, 'id': 'other'}, {**ROW, 'id': 'third'}],  # over the limit
    {'id': 'row123'},                                        # not a list
    ['row123'],                                              # not dict rows
    [None],
])
def test_partial_or_shape_violating_reader_is_unknown(rows):
    result, _ = run('calendar_row_status', {}, deps={'read': lambda _t, _p: rows},
                    params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.UNKNOWN and result['verified'] is False
    assert result['reason'] == 'reader_partial'


def test_check_fault_is_unknown_never_exception_as_proof(monkeypatch):
    def crash(_ctx):
        raise ValueError('deliberate')

    monkeypatch.setattr(be, 'CHECKS',
                        {'crash': be.CheckSpec('crash', crash)})
    result, _ = run('crash', {})
    assert result['outcome'] == be.UNKNOWN and result['verified'] is False
    assert result['reason'] == 'check_fault'
    assert 'deliberate' not in json.dumps(result)


@pytest.mark.parametrize('seen', [
    {'available': True, 'matched': True},               # not an Observation
    be.Observation(True, True, 'x' * 200),              # evidence over the bound
    be.Observation('yes', True, 'calendar_row:a:b'),    # non-bool flags
    be.Observation(False, False, '', 'reader_gave_up'), # declared unavailable
])
def test_malformed_check_results_are_unknown(monkeypatch, seen):
    spec = be.CheckSpec('fake', lambda _ctx: seen)
    monkeypatch.setattr(be, 'CHECKS', {'fake': spec})
    result, _ = run('fake', {})
    assert result['outcome'] == be.UNKNOWN and result['verified'] is False


# -- tenant binding -----------------------------------------------------------------

@pytest.mark.parametrize('check_id,params,table', [
    ('calendar_row_status', {'row_id': 'row123', 'expected_status': 'published'},
     'content_calendar'),
    ('forward_book_grade_at_least', {'min_total': 90}, 'gym_social_grades'),
    ('media_source_active', {'folder_id': 'folderABC'}, 'media_source'),
])
def test_foreign_tenant_rows_are_never_evidence(check_id, params, table):
    # A mis-scoped reader that returns the requesting gym's row to a DIFFERENT
    # tenant's observation must poison the readback, not satisfy it.
    foreign_row = {'content_calendar': ROW, 'gym_social_grades': GRADE,
                   'media_source': SOURCE_ROW}[table]
    result, _ = run(check_id, {}, deps={'read': lambda _t, _p: [dict(foreign_row)]},
                    gym_key=OTHER, params=params)
    assert result['outcome'] == be.UNKNOWN
    assert result['verified'] is False and result['symptom_resolved'] is None
    assert result['reason'] == 'scope_mismatch'
    assert result['gym_key'] == OTHER


def test_tenant_reads_are_scoped_and_bounded():
    other_row = {**ROW, 'gym_id': OTHER, 'status': 'published'}
    tables = {'content_calendar': [other_row]}
    result, reader = run('calendar_row_status', tables,
                         params={'row_id': 'row123', 'expected_status': 'published'})
    # The fake honors the eq. gym filter, so the foreign row is not returned;
    # the request gym sees an honest row_not_found, never the other tenant's row.
    assert result['reason'] == 'row_not_found' and result['verified'] is False
    assert reader.calls[0][1]['gym_id'] == 'eq.testgym'
    assert reader.calls[0][1]['limit'] == '2'


# -- request / release binding -------------------------------------------------------

@pytest.mark.parametrize('gym_key', ['x', '', 'has space', 'a' * 81, None, 5])
def test_bad_gym_key_refused_before_any_read(gym_key):
    result, reader = run('calendar_row_status', {'content_calendar': [ROW]},
                         gym_key=gym_key,
                         params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.UNVERIFIED and result['reason'] == 'bad_gym_key'
    assert result['verified'] is False and reader.calls == []


@pytest.mark.parametrize('request_key', ['', 'has space', 'x' * 201, None])
def test_bad_request_key_refused_before_any_read(request_key):
    result, reader = run('calendar_row_status', {'content_calendar': [ROW]},
                         request_key=request_key,
                         params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['reason'] == 'bad_request_key' and reader.calls == []


@pytest.mark.parametrize('merged_sha', [
    '', 'ab', 'not a sha', 'z' * 129, None,
    'a' * 39, 'a' * 41, 'a' * 63, 'a' * 65, 'A' * 40,
])
def test_bad_release_id_refused_before_any_read(merged_sha):
    result, reader = run('calendar_row_status', {'content_calendar': [ROW]},
                         merged_sha=merged_sha,
                         params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['reason'] == 'bad_release_id' and reader.calls == []


def test_exact_64_lowercase_hex_sha_is_a_valid_release_binding():
    result, reader = run('calendar_row_status', {'content_calendar': [ROW]},
                         merged_sha='c' * 64,
                         params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['outcome'] == be.VERIFIED
    assert result['merged_sha'] == 'c' * 64
    assert reader.calls


def test_binding_mismatch_on_any_identity_axis():
    result, _ = run('calendar_row_status', {'content_calendar': [ROW]},
                    params={'row_id': 'row123', 'expected_status': 'published'})
    assert result['verified'] is True
    assert not be.binding_matches(result, gym_key=OTHER, request_key=REQ, merged_sha=SHA)
    assert not be.binding_matches(result, gym_key=GYM, request_key='req-' + 'c' * 32,
                                  merged_sha=SHA)
    assert not be.binding_matches(result, gym_key=GYM, request_key=REQ,
                                  merged_sha='d' * 40)
    assert not be.binding_matches(result, gym_key='', request_key=REQ, merged_sha=SHA)
    assert not be.binding_matches('not a record', gym_key=GYM, request_key=REQ,
                                  merged_sha=SHA)
    forged = {**result, 'request_key': 'req-' + 'c' * 32}
    assert not be.binding_matches(forged, gym_key=GYM, request_key=REQ, merged_sha=SHA)


# -- durability ----------------------------------------------------------------------

def test_record_is_json_durable_and_timestamped():
    result, _ = run('calendar_row_status', {'content_calendar': [ROW]},
                    params={'row_id': 'row123', 'expected_status': 'published'})
    roundtrip = json.loads(json.dumps(result))
    assert roundtrip == result
    assert roundtrip['captured_at'] == '2026-09-18T12:30:00+00:00'
    assert roundtrip['schema_version'] == 1


def test_module_source_has_no_dynamic_execution_network_or_env():
    src = Path(be.__file__).read_text()
    for token in ('eval(', 'exec(', '__import__', 'importlib', 'requests',
                  'socket', 'subprocess', 'os.environ', 'open('):
        assert token not in src
