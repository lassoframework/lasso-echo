"""Synthetic lower publishers and fake RPC acknowledgements; no real sends.

The disposable PG companion separately exercises the actual lease SQL. The
fixtures use the delegated v2 contract and do not establish live authorization.
"""
from types import SimpleNamespace
import uuid

import pytest
from agent import generated_infographic_runtime as runtime, generated_infographic_preparation as prep
from agent import forward_media_publish as bridge, forward_media_send_context as scope
from test_forward_media_lower_publishers import lane


@pytest.fixture
def generated(lane, monkeypatch):
    monkeypatch.setenv(runtime.FLAG, 'true')
    monkeypatch.setattr(runtime, 'validate_publish_palette', lambda *a, **kw: True)
    def factory(provider='zernio', platform='instagram'):
        row, token, authority, draft, account, vendor, publish, requests = lane(provider, platform)
        row['source_media_asset_id'] = runtime.PREFIX + str(uuid.uuid4())
        graphic_copy=dict(headline=row['caption'],facts=[row['caption']],cta='',footer='')
        derivation=dict(policy='verbatim_selected_fact_v1',caption=row['caption'],copy_digest=prep.digest(graphic_copy),
            fact_witness=dict(key='SYNTHETIC fact',capture_id=str(uuid.uuid4()),bytes_sha256='a'*64,
                source_locator='https://synthetic.test/',byte_offset=0,byte_length=len(row['caption'].encode()),text=row['caption']))
        row['generated_authority_pins'] = dict(mode='delegated_policy',gym_id=str(uuid.uuid4()),
            echo_account_key=row['gym_id'],bundle_id=str(uuid.uuid4()),bundle_version=1,
            configuration_sha256='b'*64,configuration_receipt_sha256='c'*64,observation_id=1,
            observation_sha256='d'*64,validator_revision='SYNTHETIC validator',derivation_sha256=prep.digest(derivation))
        authority.row.update(row)
        events=[]
        control={'begin':'ok','acquire':'ok','validate':'ok','outcome':'ok'}
        original=authority.post
        def post(path, **kwargs):
            args=kwargs['json']
            if path.endswith('fixer_generated_publish_readback_20261007'):
                result={'schema_version':2,'authority_pins':row['generated_authority_pins'],
                    'copy_derivation_receipt':derivation,'copy_digest':prep.digest(graphic_copy)}
            elif 'generated_send_' in path:
                events.append((path,args))
                kind = path.split('generated_send_')[1].split('_')[0]
                if control[kind]=='lost':
                    raise TimeoutError('synthetic response loss')
                result={'attempt_token':args['p_attempt'],'authorize_send':kind in ('begin','validate')}
                if kind=='acquire':
                    result.update(state='reserved',replayed=control[kind]=='replay')
                elif kind in ('begin','validate'): result.update(state='inflight')
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
    kinds=[e[0].split('generated_send_')[1].split('_')[0] for e in s.events]
    assert [kind for kind in kinds if kind!='validate']==['acquire','begin','outcome']
    assert kinds.count('validate')==len(s.requests)-1
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
