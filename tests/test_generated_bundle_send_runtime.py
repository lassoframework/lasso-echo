"""Exact delegated lower-send boundary, synthetic RPCs and no provider calls."""
from types import SimpleNamespace
import uuid

import pytest
from agent import generated_infographic_preparation as prep, generated_infographic_runtime as runtime
from agent.generated_send_lease import GeneratedSendLease


@pytest.fixture
def lease(monkeypatch):
    monkeypatch.setenv(runtime.FLAG,'true')
    caption='SYNTHETIC selected training fact'
    copy=dict(headline=caption,facts=[caption],cta='',footer='')
    witness=dict(key='training',capture_id=str(uuid.uuid4()),bytes_sha256='a'*64,
        source_locator='https://synthetic.test/',byte_offset=0,byte_length=len(caption.encode()),text=caption)
    derivation=dict(policy='verbatim_selected_fact_v1',caption=caption,copy_digest=prep.digest(copy),fact_witness=witness)
    pins=dict(mode='delegated_policy',gym_id=str(uuid.uuid4()),echo_account_key='gym',bundle_id=str(uuid.uuid4()),
        bundle_version=1,configuration_sha256='b'*64,configuration_receipt_sha256='c'*64,
        observation_id=1,observation_sha256='d'*64,validator_revision='SYNTHETIC validator',derivation_sha256=prep.digest(derivation))
    row=dict(id=str(uuid.uuid4()),gym_id='gym',source_media_asset_id=runtime.PREFIX+str(uuid.uuid4()),
        caption=caption,generated_authority_pins=pins)
    binding=dict(schema_version=2,authority_pins=pins,copy_derivation_receipt=derivation,copy_digest=prep.digest(copy))
    monkeypatch.setattr(runtime,'_sql_publish_readback',lambda *_:binding)
    events=[];control={}
    class Store:
        def _client(self):return self
        def _rest(self,path):return path
        def _headers(self,headers):return headers
        def post(self,path,**kw):
            kind=path.split('generated_send_')[1].split('_')[0];args=kw['json'];events.append((kind,args))
            if control.get(kind)=='lost':raise TimeoutError('SYNTHETIC lost response')
            state=dict(acquire='reserved',begin='inflight',validate='inflight').get(kind,args.get('p_outcome'))
            value=dict(attempt_token=args['p_attempt'],state=state,authorize_send=kind in ('begin','validate'),replayed=False)
            if control.get(kind)=='deny':value['authorize_send']=False
            if control.get(kind)=='replay':value['replayed']=True
            return SimpleNamespace(status_code=200,json=lambda:value)
    return SimpleNamespace(value=GeneratedSendLease(Store(),row,str(uuid.uuid4())),row=row,binding=binding,
        pins=pins,derivation=derivation,events=events,control=control)


def test_delegated_verbatim_receipt_retained_and_every_mutation_validated(lease):
    s=lease;s.value.acquire();s.value.begin('meta');s.value.begin('meta')
    assert [kind for kind,_ in s.events]==['acquire','begin','validate']
    assert s.events[0][1]['p_pins']==s.pins
    result=SimpleNamespace(ok=True,mode='published',media_id='SYNTHETIC post')
    assert s.value.finish(result=result)=='sent'


def test_legacy_schema_holds_before_send_decision(lease):
    s=lease;s.binding['schema_version']=1
    with pytest.raises(runtime.RuntimeHold):s.value.acquire()
    assert not s.events


def test_legacy_pins_hold_before_send_decision(lease):
    s=lease;s.binding['authority_pins']=dict(tenant_id='gym',epoch=1,source_id='old',source_revision=1,palette_key='old',palette_revision=1)
    with pytest.raises(runtime.RuntimeHold):s.value.acquire()
    assert not s.events


@pytest.mark.parametrize('field,value',[('caption','fabricated'),('copy_digest','0'*64),('policy','approval_bypass')])
def test_altered_derivation_holds_before_send_decision(lease,field,value):
    s=lease;s.binding['copy_derivation_receipt']={**s.derivation,field:value}
    with pytest.raises(runtime.RuntimeHold):s.value.acquire()
    assert not s.events


def test_mismatched_outgoing_caption_holds_before_send_decision(lease):
    s=lease;s.row['caption']='SYNTHETIC extra unsupported claim'
    with pytest.raises(runtime.RuntimeHold):s.value.acquire()
    assert not s.events


def test_lost_begin_never_retries_or_fabricates_cancellation(lease):
    s=lease;s.value.acquire();s.control['begin']='lost'
    with pytest.raises(runtime.RuntimeHold):s.value.begin('meta')
    with pytest.raises(runtime.RuntimeHold):s.value.finish(error=TimeoutError())
    assert [kind for kind,_ in s.events]==['acquire','begin']


def test_changed_authority_hold_before_second_mutation(lease):
    s=lease;s.value.acquire();s.value.begin('meta');s.control['validate']='deny'
    with pytest.raises(runtime.RuntimeHold):s.value.begin('meta')
    assert s.value.finish(error=TimeoutError())=='unknown'


def test_changed_provider_never_validated_as_same_attempt(lease):
    s=lease;s.value.acquire();s.value.begin('meta')
    with pytest.raises(runtime.RuntimeHold):s.value.begin('zernio')
    assert [kind for kind,_ in s.events]==['acquire','begin']
