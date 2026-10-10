"""Focused tests for the automatic_reel_controls_repaired business check and
the worker-owned upload-form probe. Fail-closed on every missing or mismatched
piece of evidence; never accepts caller URLs, tokens, or completion claims."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import fixer_business_evidence as be
from agent import fixer_ops

NOW = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)
GYM = '11111111-1111-4111-8111-111111111111'
RID = '33333333-3333-4333-8333-333333333333'
SHA = 'b' * 40
CHECK = 'automatic_reel_controls_repaired'
JOB = {'request_id': RID, 'status': 'held', 'reason': 'portrait clip requires a crop decision',
       'updated_at': NOW.isoformat(), 'clip_count': 2, 'next_attempt_at': None}


def tables():
    return {
        'echo_intake_tokens': [{'gym_id': GYM, 'echo_account_key': 'gym'}],
        'auto_reel_status': [{'gym_id': 'gym', 'updated_at': NOW.isoformat(),
                              'snapshot': {'gym': 'gym', 'ok': True,
                                           'jobs': [copy.deepcopy(JOB)]}}],
    }


def read_factory(data):
    def read(table, params):
        return [copy.deepcopy(r) for r in data.get(table, []) if all(
            key in ('select', 'limit', 'order') or not value.startswith('eq.')
            or str(r.get(key)) == value[3:]
            for key, value in params.items())]
    return read


def deps_for(data, **overrides):
    deps = {'read': read_factory(data),
            'job_status_probe': lambda gym, rid: copy.deepcopy(
                data['auto_reel_status'][0]['snapshot']['jobs'][0]),
            'form_probe': lambda gym: True}
    deps.update(overrides)
    return deps


def observe(data, deps, params=None, now=NOW):
    return be.observe(CHECK, gym_key=GYM, request_key='a' * 64, merged_sha=SHA,
                      ticket_id='22222222-2222-4222-8222-222222222222',
                      params=params if params is not None else {'request_id': RID},
                      deps=deps, now=now)


def test_portrait_media_hold_with_two_clips_passes():
    data, deps = tables(), None
    deps = deps_for(data)
    result = observe(data, deps)
    assert result['verified'] is True and result['outcome'] == 'verified'
    assert result['symptom_resolved'] is True
    assert result['evidence'].startswith(f'reel_controls:{RID}:')


def test_production_portrait_reason_is_a_hold_not_a_completion_claim():
    data = tables()
    job = data['auto_reel_status'][0]['snapshot']['jobs'][0]
    job['status'] = 'exhausted'
    job['reason'] = 'Automatic reel held: The complete athlete will not fit a portrait crop'
    result = observe(data, deps_for(data))
    assert result['verified'] is True and result['evidence'].endswith(':exhausted')


@pytest.mark.parametrize('params', [
    {}, {'request_id': RID, 'asset_ids': ['a1', 'a2']},
    {'request_id': RID, 'approved_cta': 'No Sweat'},
    {'request_id': 'not-a-uuid'}, {'request_id': 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'},
    {'asset_ids': ['a1', 'a2']}, {'request_id': 123},
])
def test_params_must_be_only_original_request_uuid(params):
    data = tables()
    result = observe(data, deps_for(data), params=params)
    assert result['verified'] is False and result['outcome'] == 'unverified'
    assert result['reason'] == 'bad_params'
    assert be.automatic_reel_controls_params_valid(params) is False


@pytest.mark.parametrize('status', ['unknown', 'completed', 'published', 'posted', 'done'])
def test_unknown_or_completion_status_rejected(status):
    data = tables()
    data['auto_reel_status'][0]['snapshot']['jobs'][0]['status'] = status
    result = observe(data, deps_for(data))
    assert result['verified'] is False and result['outcome'] == 'unverified'
    assert result['symptom_resolved'] is False


@pytest.mark.parametrize('reason', [
    'queued for team review', 'reviewing by staff', 'overlay_text_final too long',
    'end-frame carries 0 ask(s)', 'no approved gym call to action',
    'caption needs exactly one approved CTA',
])
def test_raw_team_reviewing_overlay_cta_error_text_rejected(reason):
    data = tables()
    data['auto_reel_status'][0]['snapshot']['jobs'][0]['reason'] = reason
    result = observe(data, deps_for(data))
    assert result['verified'] is False and result['outcome'] == 'unverified'


@pytest.mark.parametrize('reason', ['reel completed and delivered to Instagram',
                                    'published to provider'])
def test_false_completed_claims_rejected(reason):
    data = tables()
    data['auto_reel_status'][0]['snapshot']['jobs'][0]['reason'] = reason
    result = observe(data, deps_for(data))
    assert result['verified'] is False and result['reason'] == 'completion_claim_rejected'


def test_worker_mirror_mismatch_is_unknown():
    data = tables()
    deps = deps_for(data, job_status_probe=lambda *a: {'request_id': RID, 'status': 'held'})
    result = observe(data, deps)
    assert result['outcome'] == 'unknown' and result['verified'] is False


def test_worker_probe_unavailable_is_unknown():
    data = tables()
    result = observe(data, deps_for(data, job_status_probe=lambda *a: None))
    assert result['outcome'] == 'unknown'
    result = observe(data, deps_for(data, job_status_probe=None))
    assert result['outcome'] == 'unknown'


def test_form_probe_unavailable_is_unknown_and_failed_is_unverified():
    data = tables()
    assert observe(data, deps_for(data, form_probe=lambda *a: None))['outcome'] == 'unknown'
    assert observe(data, deps_for(data, form_probe=None))['outcome'] == 'unknown'
    result = observe(data, deps_for(data, form_probe=lambda *a: False))
    assert result['outcome'] == 'unverified' and result['verified'] is False


def test_stale_or_foreign_status_row_is_unknown():
    data = tables()
    data['auto_reel_status'][0]['updated_at'] = (NOW - timedelta(hours=1)).isoformat()
    assert observe(data, deps_for(data))['outcome'] == 'unknown'
    data = tables()
    data['auto_reel_status'][0]['gym_id'] = 'other'
    assert observe(data, deps_for(data))['outcome'] == 'unknown'
    data = tables()
    data['auto_reel_status'][0]['snapshot']['ok'] = False
    assert observe(data, deps_for(data))['outcome'] == 'unknown'


def test_missing_job_is_unknown():
    data = tables()
    data['auto_reel_status'][0]['snapshot']['jobs'] = []
    assert observe(data, deps_for(data))['outcome'] == 'unknown'


def test_tenant_binding_failure_is_unknown():
    data = tables()
    data['echo_intake_tokens'] = []
    assert observe(data, deps_for(data))['outcome'] == 'unknown'


def test_registered_in_catalog_and_ops_params_valid():
    catalog = be.catalog()
    entry = [c for c in catalog['checks'] if c['check_id'] == CHECK]
    assert entry and set(entry[0]['params']) == {'request_id'}
    assert fixer_ops._business_params_valid(CHECK, {'request_id': RID}) is True
    assert fixer_ops._business_params_valid(CHECK, {'request_id': RID, 'x': 1}) is False
    assert fixer_ops._business_params_valid(CHECK, {}) is False


def test_business_endpoint_defaults_off_for_new_check(monkeypatch):
    body = json.dumps({
        'schema_version': 1, 'contract_version': fixer_ops.BUSINESS_EVIDENCE_CONTRACT,
        'ticket_id': '22222222-2222-4222-8222-222222222222',
        'client_id': GYM, 'request_key': 'a' * 64, 'merged_sha': SHA,
        'check_id': CHECK, 'params': {'request_id': RID},
    }).encode()
    monkeypatch.delenv('ECHO_REEL_CONTROLS_PROOF', raising=False)
    assert fixer_ops._run_business_evidence(body, {}) == (
        503, {'error': 'business_check_disabled'})
    monkeypatch.setenv('ECHO_REEL_CONTROLS_PROOF', 'true')
    assert fixer_ops._run_business_evidence(body, {}) != (
        503, {'error': 'business_check_disabled'})


GOOD_FORM = (
    '<html><body>'
    '<input id="filepick" type="file" accept="image/*,video/mp4,video/quicktime" multiple hidden>'
    '<button class="send" id="send" disabled>Send it to LASSO</button>'
    '<script>sendBtn.disabled = pending.length===0;</script>'
    '</body></html>').encode()
TEST_TOKEN = 'ZW5n.abcdefghijklmnopqrstuv'


class Response:
    def __init__(self, status=200, ctype='text/html', content=GOOD_FORM, length=None):
        self.status_code = status
        self.headers = {'Content-Type': ctype,
                        'Content-Length': str(len(content) if length is None else length)}
        self.content = content
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, _): yield self.content


def probe(response, calls=None, token=TEST_TOKEN, action_url=None, action_ok=True):
    def get(url, **kw):
        if calls is not None:
            calls.append((url, kw))
        if '/studio/story?' in url:
            target = action_url or f'{fixer_ops._UPLOAD_FORM_ORIGIN}/u/{token}'
            body = json.dumps({'ok': True, 'automatic_reels': {
                'enabled': True, 'ok': action_ok, 'upload_action': {
                    'url': target, 'label': 'Upload video clips'}}}).encode()
            return Response(200, ctype='application/json', content=body)
        return response
    return fixer_ops.probe_upload_form(
        'gym', http=SimpleNamespace(get=get), token_resolver=lambda _: token)


def test_form_probe_happy_path_bounded_no_redirect_token_never_returned():
    calls = []
    assert probe(Response(), calls) is True
    assert len(calls) == 2
    assert calls[0][0] == (f'{fixer_ops._UPLOAD_FORM_ORIGIN}/portal/'
                           f'{TEST_TOKEN}/studio/story?automatic=1')
    assert calls[1][0] == f'{fixer_ops._UPLOAD_FORM_ORIGIN}/u/{TEST_TOKEN}'
    assert all(kw == {'stream': True, 'timeout': (5, 5), 'allow_redirects': False}
               for _, kw in calls)


def test_form_probe_rejects_wrong_client_upload_action():
    assert probe(Response(), action_url=f'https://other.example/u/{TEST_TOKEN}') is False
    assert probe(Response(), action_ok=False) is False


@pytest.mark.parametrize('response', [
    Response(301), Response(302), Response(403), Response(404), Response(500),
    Response(200, ctype='application/json'),
    Response(200, content=GOOD_FORM.replace(b'video/mp4,video/quicktime', b'image/png')),
    Response(200, content=GOOD_FORM.replace(b' disabled', b' ')),
    Response(200, content=GOOD_FORM.replace(b'sendBtn.disabled = pending.length===0;',
                                            b'sendBtn.disabled = false;')),
    Response(200, length=fixer_ops._UPLOAD_FORM_MAX_BYTES + 1),
])
def test_form_probe_rejects_broken_redirected_or_oversized_forms(response):
    result = probe(response)
    assert result is not True and TEST_TOKEN not in str(result)
    if response.status_code == 200:
        assert result is False


def test_form_probe_faults_and_missing_token_are_unavailable():
    def boom(url, **kw):
        raise RuntimeError('network down')
    assert fixer_ops.probe_upload_form(
        'gym', http=SimpleNamespace(get=boom), token_resolver=lambda _: TEST_TOKEN) is None
    assert fixer_ops.probe_upload_form('gym', token_resolver=lambda _: None) is None
    assert fixer_ops.probe_upload_form('gym', token_resolver=lambda _: '') is None
