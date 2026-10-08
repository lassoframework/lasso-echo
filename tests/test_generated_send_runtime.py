"""Synthetic lower publishers and fake RPC acknowledgements; no real sends.

The disposable PG companion separately exercises the actual lease SQL. The
owner canonical generation route remains unwired and cannot mint these fixtures.
"""
from types import SimpleNamespace
import uuid

import pytest
from agent import generated_infographic_runtime as runtime
from agent import forward_media_publish as bridge, forward_media_send_context as scope
from test_forward_media_lower_publishers import lane


@pytest.fixture
def generated(lane, monkeypatch):
    monkeypatch.setenv(runtime.FLAG, 'true')
    monkeypatch.setattr(runtime, 'validate_publish_palette', lambda *a, **kw: True)
    def factory(provider='zernio', platform='instagram'):
        row, token, authority, draft, account, vendor, publish, requests = lane(provider, platform)
        row['source_media_asset_id'] = runtime.PREFIX + str(uuid.uuid4())
        row['generated_authority_pins'] = dict(tenant_id=row['gym_id'],epoch=1,source_id='src',
            source_revision=1,palette_key='brand',palette_revision=1)
        authority.row.update(row)
        events=[]
        control={'begin':'ok','acquire':'ok','outcome':'ok'}
        original=authority.post
        def post(path, **kwargs):
            args=kwargs['json']
            if path.endswith('fixer_generated_publish_readback_20261007'):
                result={'authority_pins':row['generated_authority_pins']}
            elif 'generated_send_' in path:
                events.append((path,args))
                kind = path.split('generated_send_')[1].split('_')[0]
                if control[kind]=='lost':
                    raise TimeoutError('synthetic response loss')
                result={'attempt_token':args['p_attempt'],'authorize_send':kind=='begin'}
                if kind=='acquire':
                    result.update(state='reserved',replayed=control[kind]=='replay')
                elif kind=='begin': result.update(state='inflight')
                else: result.update(state=args['p_outcome'])
            else: return original(path, **kwargs)
            return SimpleNamespace(status_code=200,json=lambda:result)
        authority.post=post
        return SimpleNamespace(row=row,token=token,authority=authority,draft=draft,account=account,
            vendor=vendor,publish=publish,requests=requests,events=events,control=control)
    return factory


@pytest.mark.parametrize('provider',['meta','socialapi','zernio'])
@pytest.mark.parametrize('platform',['instagram','facebook_page'])
def test_generated_lower_send_one_begin_then_settlement(generated,provider,platform):
    s=generated(provider,platform)
    with bridge.authorized_send(s.authority,s.row,s.token):
        assert s.publish().ok
    assert s.requests
    assert [e[0].split('generated_send_')[1].split('_')[0] for e in s.events]==['acquire','begin','outcome']
    assert s.events[-1][1]['p_outcome']=='sent'
    assert ':post:' in s.events[-1][1]['p_evidence']['receipt_ref']


@pytest.mark.parametrize('stage',['acquire','begin'])
def test_lost_commit_never_calls_provider_or_cancels(generated,stage):
    s=generated();s.control[stage]='lost'
    with pytest.raises(scope.ProviderSendHold):
        with bridge.authorized_send(s.authority,s.row,s.token): s.publish()
    assert s.requests==[]
    assert not any('outcome' in path for path,args in s.events)


def test_replayed_attempt_holds_without_second_send(generated):
    s=generated();s.control['acquire']='replay'
    with pytest.raises(scope.ProviderSendHold):
        with bridge.authorized_send(s.authority,s.row,s.token): s.publish()
    assert s.requests==[]


def test_missing_canonical_pins_holds(generated):
    s=generated();s.row['generated_authority_pins']=None
    with pytest.raises(scope.ProviderSendHold):
        with bridge.authorized_send(s.authority,s.row,s.token): s.publish()
    assert s.requests==[] and s.events==[]


def test_unknown_provider_exception_quarantines(generated):
    s=generated()
    def ambiguous(*a,**kw):
        s.requests.append('attempt'); raise TimeoutError('synthetic provider response loss')
    s.vendor.create_post=ambiguous
    with pytest.raises(TimeoutError):
        with bridge.authorized_send(s.authority,s.row,s.token): s.publish()
    assert s.requests==['attempt']
    assert s.events[-1][1]['p_outcome']=='unknown'
    assert s.events[-1][1]['p_reconcile'] is False


def test_lost_terminal_settlement_retains_claim(generated):
    s=generated();s.control['outcome']='lost'
    with pytest.raises(scope.ProviderSendHold) as error:
        with bridge.authorized_send(s.authority,s.row,s.token): s.publish()
    assert s.requests and error.value.definitive_no_post is False


@pytest.mark.parametrize('provider',['meta','socialapi'])
def test_appended_hashtags_hold_before_mutation(generated,provider):
    s=generated(provider);s.draft.hashtags=['#synthetic']
    with pytest.raises(scope.ProviderSendHold):
        with bridge.authorized_send(s.authority,s.row,s.token): s.publish()
    assert s.requests==[]
    # Rejection before begin is durably cancelled, never a sent receipt.
    assert s.events[-1][1]['p_outcome']=='not_sent'


def test_cached_published_result_cannot_settle_new_lease(generated,monkeypatch):
    from agent import socialapi_publisher as social
    s=generated('socialapi')
    monkeypatch.setattr(social,'_already_published',lambda *_:(True,'cached-post'))
    with pytest.raises(scope.ProviderSendHold):
        with bridge.authorized_send(s.authority,s.row,s.token): s.publish()
    assert s.requests==[]
    assert not any('outcome' in path for path,args in s.events)
