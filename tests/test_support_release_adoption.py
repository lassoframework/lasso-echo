import copy
import hashlib
import json
from datetime import datetime, timezone, timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from agent import fixer_business_evidence as be
from agent import support_release_adoption as adoption
from agent.slack_convo.bus import Bus
from agent.support_thumbnail_probe import probe_thumbnail
from tests.gym_media_fakes import bound_review_fields

NOW = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)
GYM = '11111111-1111-4111-8111-111111111111'
TID = '22222222-2222-4222-8222-222222222222'
RID = '33333333-3333-4333-8333-333333333333'
SHA = 'b' * 40
PRIMARY = 'https://github.com/lassoframework/lasso-echo/pull/12'
PORTAL = 'https://github.com/LASSO-FRAMEWORK/lasso-ops-portal/pull/34'
OLD = 'https://github.com/lassoframework/lasso-echo/pull/11'
PARAMS = {'request_id':RID, 'asset_ids':['a1', 'a2', 'a3'], 'approved_cta':'No Sweat Intro'}


def tables():
    return {'echo_intake_tokens':[{'gym_id':GYM, 'echo_account_key':'gym'}],
        'auto_reel_status':[{'gym_id':'gym', 'updated_at':NOW.isoformat(), 'snapshot':{
            'gym':'gym', 'ok':True, 'jobs':[{'request_id':RID, 'status':'staged'}]}}],
        'content_calendar':[{'id':RID, 'gym_id':'gym', 'status':'pending', 'account':'instagram', 'format':'feed',
            'image_url':'https://media.example/reel.mp4', 'media_not_ready_reason':None,
            'caption':'Expert coaching for busy adults.\n\nNo Sweat Intro\n\n#Gym #Fitness #Coaching'}],
        'story_render':[{'request_id':RID, 'gym_id':'gym', 'segment_plan':[{'asset_id':v} for v in PARAMS['asset_ids']]}],
        'media_asset':[{'id':v, 'gym_id':'gym', 'kind':'video', 'eligible':True, 'excluded_by_coach':False,
                        **bound_review_fields(v, 'gym')} for v in PARAMS['asset_ids']]}


class FakeBus(Bus):
    def __init__(self):
        self.current = {'id':TID, 'product':'echo', 'source':'slack_conversation', 'status':'verification',
            'classification':'code_fix', 'client_id':GYM, 'request_version':1, 'identity_kind':'client',
            'bot_identity':'echo', 'slack_user_id':'UCLIENT', 'slack_channel_id':'CGYM', 'slack_thread_ts':'123.45',
            'escalated':False, 'hold_tier':None, 'fix_pr_url':OLD,
            'verification_before':{'fixer':{'claimed_at':'original'}},
            'verification_after':{'verifier':'github-actions', 'run_id':123, 'sha':'a' * 40,
                                  'fixer':{'client_note':'Old note'}}}
        self.data = tables()
        self.inbound = [{'id':'message', 'created_at':NOW.isoformat(), 'body':'Reported issue',
                         'direction':'inbound', 'author_type':'client'}]
        self.patch_calls = []
        self.race = None
    def ticket(self, _tid):
        return copy.deepcopy(self.current)
    def messages(self, _tid, limit):
        return copy.deepcopy(self.inbound)
    def inbound_count(self, _tid):
        return len(self.inbound)
    def _get(self, table, params):
        return [copy.deepcopy(r) for r in self.data.get(table, []) if all(
            key in ('select', 'limit', 'order') or not value.startswith('eq.') or str(r.get(key)) == value[3:]
            for key, value in params.items())]
    def _patch(self, table, match, fields):
        self.patch_calls.append((table, match, fields))
        if self.race:
            self.race(self)
        for key, value in match.items():
            actual = self.current.get(key)
            if key in ('verification_before', 'verification_after') and value != 'is.null':
                equal = actual == json.loads(value[3:])
            elif value == 'is.null':
                equal = actual is None
            else:
                equal = str(actual).lower() == value[3:].lower()
            if not equal:
                return None
        self.current.update(copy.deepcopy(fields))
        return self.ticket(TID)


def setup():
    from agent.slack_convo.outbox import _current_fixer_request_key
    bus = FakeBus()
    releases = [{'pr_url':PRIMARY, 'merge_sha':SHA}, {'pr_url':PORTAL, 'merge_sha':'c' * 40}]
    plan = {'ticket_id':TID, 'request_version':1, 'superseded_pr_url':OLD, 'releases':releases,
            'business_params':copy.deepcopy(PARAMS), 'client_note':'The fixes were independently verified. This ticket is closed.'}
    review = {'schema_version':1, 'ticket_id':TID, 'request_version':1,
              'request_key':_current_fixer_request_key(bus, bus.current), 'verified':True,
              'reviewer_id':'independent-reviewer', 'builder_id':'builder', 'checked_at':NOW.isoformat(),
              'release_identifiers':releases, 'business_params':PARAMS, 'essential_failures':[], 'limitations':['Client approval is still required.']}
    receipts = [{**r, 'repo':repo, 'head_sha':'d' * 40,
                 'deployment_check':{'verified':True, 'sha':r['merge_sha'], 'evidence':[{'sha':'e' * 40}]}}
                for r, repo in zip(releases, ['lassoframework/lasso-echo', 'lasso-framework/lasso-ops-portal'])]
    deps = {'read':bus._get, 'thumbnail_probe':lambda gym, aid:True, 'approved_cta_probe':lambda gym, ask:True,
            'job_status_probe':lambda gym, rid:copy.deepcopy(bus.data['auto_reel_status'][0]['snapshot']['jobs'][0])}
    return bus, plan, review, receipts, deps


def run_adopt(bus, plan, review, receipts, deps, **kw):
    return adoption.adopt_release(bus, bus.ticket(TID), plan, json.dumps(review).encode(), now=NOW,
                                 release_reader=lambda _:receipts, deps=deps, **kw)


def test_dry_run_has_no_writes_and_explicit_adoption_preserves_original_receipts():
    bus, plan, review, receipts, deps = setup()
    original = bus.ticket(TID)
    assert run_adopt(bus, plan, review, receipts, deps)['write'] is False
    assert bus.patch_calls == []
    result = run_adopt(bus, plan, review, receipts, deps, write=True)
    assert result['status'] == 'merged'
    after = bus.current['verification_after']
    assert after['verifier'] == adoption.SOURCE and 'run_id' not in after
    history = bus.current['verification_before']['independent_release_adoptions'][0]
    assert history['superseded_verification_after'] == original['verification_after']
    assert history['superseded_verification_before'] == original['verification_before']
    assert after['fixer']['merged_sha'] == SHA
    assert after['fixer']['deployment_check']['evidence'][0]['sha'] == 'e' * 40
    assert bus.current['request_version'] == 1 and bus.current['status'] != 'resolved'


@pytest.mark.parametrize('field,value', [('request_version',2), ('client_id','other'), ('fix_pr_url','other'),
                                       ('slack_channel_id','COTHER'), ('verification_after',{'new_receipt':True}),
                                       ('verification_before',{'new_note':True})])
def test_final_cas_preserves_concurrent_request_route_and_receipt_writer(field, value):
    bus, plan, review, receipts, deps = setup()
    bus.race = lambda b:b.current.update({field:value})
    with pytest.raises(adoption.AdoptionRefused, match='cas_not_confirmed'):
        run_adopt(bus, plan, review, receipts, deps, write=True)
    assert bus.current[field] == value and bus.current['status'] == 'verification'


def test_changed_request_during_external_verification_refuses_before_cas():
    bus, plan, review, receipts, deps = setup()
    def external(_):
        bus.inbound.append({**bus.inbound[0], 'id':'new-message', 'body':'Changed request'})
        return receipts
    with pytest.raises(adoption.AdoptionRefused, match='ticket_changed'):
        adoption.adopt_release(bus, bus.ticket(TID), plan, json.dumps(review).encode(), now=NOW,
                               release_reader=external, deps=deps, write=True)
    assert not bus.patch_calls


@pytest.mark.parametrize('change', ['reviewer', 'tenant', 'business', 'deployment', 'stale_review'])
def test_no_adoption_on_failed_or_incomplete_scope(change):
    bus, plan, review, receipts, deps = setup()
    if change == 'reviewer':review['reviewer_id'] = review['builder_id']
    if change == 'tenant':bus.current['client_id'] = 'other'
    if change == 'business':deps['thumbnail_probe'] = lambda *a:False
    if change == 'deployment':receipts[1]['deployment_check']['verified'] = False
    if change == 'stale_review':review['checked_at'] = (NOW - timedelta(hours=2)).isoformat()
    with pytest.raises(adoption.AdoptionRefused):run_adopt(bus, plan, review, receipts, deps, write=True)
    assert not bus.patch_calls


@pytest.mark.parametrize('fault', ['status', 'stale', 'foreign', 'mirror', 'thumbnail', 'moderation', 'voice'])
def test_registered_check_fails_closed_when_actual_scope_fails(fault):
    bus, plan, review, receipts, deps = setup()
    if fault == 'status':bus.data['auto_reel_status'][0]['snapshot']['jobs'][0]['status'] = 'unknown'
    if fault == 'stale':bus.data['auto_reel_status'][0]['updated_at'] = (NOW - timedelta(hours=1)).isoformat()
    if fault == 'foreign':bus.data['media_asset'][0]['gym_id'] = 'other'
    if fault == 'mirror':deps['job_status_probe'] = lambda *a:{'request_id':RID, 'status':'held'}
    if fault == 'thumbnail':deps['thumbnail_probe'] = lambda *a:False
    if fault == 'voice':deps['approved_cta_probe'] = lambda *a:False
    if fault == 'moderation':bus.data['media_asset'][0]['moderation_status'] = 'pending'
    result = be.observe(adoption.CHECK, gym_key=GYM, request_key='a' * 64, merged_sha=SHA,
                        ticket_id=TID, params=PARAMS, deps=deps, now=NOW)
    assert result['verified'] is False and result['symptom_resolved'] is not True


class Response:
    def __init__(self, status=200, ctype='image/png', content=b'', length=None):
        self.status_code=status
        self.headers={'Content-Type':ctype, 'Content-Length':str(len(content) if length is None else length)}
        self.content=content
    def __enter__(self):return self
    def __exit__(self, *args):pass
    def iter_content(self, _):yield self.content


@pytest.mark.parametrize('status,ctype,valid,length', [(200,'image/png',True,None), (404,'image/png',True,None),
    (403,'image/png',True,None), (200,'video/mp4',True,None), (200,'image/png',False,None),
    (200,'image/png',True,2 * 1024 * 1024)])
def test_actual_thumbnail_probe_is_bounded_decodes_and_never_returns_token(status, ctype, valid, length):
    out=BytesIO()
    Image.new('RGB',(3,3)).save(out,format='PNG')
    response=Response(status,ctype,out.getvalue() if valid else b'not an image',length)
    calls=[]
    def get(url, **kw):
        calls.append((url,kw))
        return response
    result=probe_thumbnail('gym','a1',http=SimpleNamespace(get=get),token_resolver=lambda _: 'secret-token')
    assert result is (True if status == 200 and ctype == 'image/png' and valid and length is None else False) or result is None
    assert 'secret-token' not in str(result)
    assert calls[0][1] == {'stream':True, 'timeout':(5,5), 'allow_redirects':False}


def test_dispatch_reobserves_composite_before_customer_notice():
    from agent.slack_convo.outbox import _verified_fix_notice
    bus, plan, review, receipts, deps = setup()
    run_adopt(bus, plan, review, receipts, deps, write=True)
    import agent.slack_convo.outbox as outbox
    original = outbox._business_evidence_deps
    try:
        outbox._business_evidence_deps = lambda *a:deps
        att={'resolve_notice':True, 'pr_url':PRIMARY}
        assert _verified_fix_notice(bus.current, att, 'status', bus=bus, now=NOW)
        deps['thumbnail_probe'] = lambda *a:False
        assert not _verified_fix_notice(bus.current, att, 'status', bus=bus, now=NOW)
    finally:
        outbox._business_evidence_deps = original


@pytest.mark.parametrize('state', ['held', 'uncertain', 'exhausted', 'running', 'waiting_pool'])
def test_reported_fix_can_verify_while_real_reel_safety_hold_remains(state):
    bus, plan, review, receipts, deps = setup()
    bus.data['auto_reel_status'][0]['snapshot']['jobs'][0].update(
        status=state, next_attempt_at=123, reason='Portrait framing could not verify the complete athlete')
    result = run_adopt(bus, plan, review, receipts, deps)
    assert result['proof']['verified'] is True
    assert 'staged' not in result['proof']['evidence']
    assert bus.current['status'] == 'verification'


def test_provider_receipts_are_read_from_exact_pr_head_and_current_deployments():
    bus, plan, review, receipts, deps = setup()
    plan['railway_project_id'] = GYM
    calls=[]
    def command(args):
        calls.append(args)
        if args[:3] == ['railway', 'status', '--json']:return {'id':GYM}
        if args[0] == 'railway':return [{'id':'deployment','createdAt':NOW.isoformat(),'status':'SUCCESS','meta':{'commitHash':SHA}}]
        if args[:2] == ['vercel', 'inspect']:return {'id':'dpl_test'}
        if args[0] == 'vercel':return {'id':'dpl_test','target':'production','readyState':'READY','alias':['ops.lassoframework.com'],'gitSource':{'sha':'c' * 40}}
        path=args[2]
        if '/pulls/' in path:
            return {'merged':True,'merge_commit_sha':SHA if '/lasso-echo/' in path else 'c' * 40,'head':{'sha':'d' * 40}}
        if '/check-runs' in path:
            name='pytest' if '/lasso-echo/' in path else 'portal-gate'
            return {'total_count':1,'check_runs':[{'id':42,'name':name,'status':'completed','conclusion':'success'}]}
        raise AssertionError(args)
    result=adoption.verify_releases(plan, command=command, http=SimpleNamespace(get=lambda *a,**k:Response()))
    assert len(result)==2 and all(r['deployment_check']['verified'] for r in result)
    assert any('/commits/' + 'd' * 40 + '/check-runs' in a[2] for a in calls if a[0]=='gh')
    assert not any('--project' in a for a in calls)


@pytest.mark.parametrize('fault', ['merge', 'checks', 'deployment', 'project'])
def test_adoption_provider_reader_refuses_unconfirmed_release(fault):
    _, plan, _, _, _=setup()
    plan['railway_project_id']=GYM
    def command(args):
        if args[:3] == ['railway','status','--json']:return {'id':'wrong' if fault=='project' else GYM}
        if args[0]=='railway':return [{'id':'deployment','createdAt':NOW.isoformat(), 'status':'FAILED' if fault=='deployment' else 'SUCCESS','meta':{'commitHash':SHA}}]
        if args[0]=='gh' and '/pulls/' in args[2]:return {'merged':fault!='merge','merge_commit_sha':SHA,'head':{'sha':'d' * 40}}
        if args[0]=='gh':return {'total_count':1,'check_runs':[{'name':'pytest','status':'completed','conclusion':'failure' if fault=='checks' else 'success'}]}
        raise AssertionError(args)
    with pytest.raises(adoption.AdoptionRefused):
        adoption.verify_releases(plan,command=command,http=SimpleNamespace(get=lambda *a,**k:Response()))
