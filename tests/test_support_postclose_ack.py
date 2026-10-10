"""Bounded historical follow-on; synthetic observer is never live projection proof."""
import copy
import hashlib
from datetime import datetime, timezone
import pytest
from agent import support_historical_closeout as H
from agent.slack_convo import outbox as O, identities as IDS
from tests.test_historical_receipt_recovery import _case, _validator, _run
from tests.test_slack_convo import FakeBus, _posted

class Bus(FakeBus):
    def record_outbound(self, **kw):
        result=super().record_outbound(**kw)
        if kw.get('message_id'):
            self.msgs[-1]['id']=kw['message_id'];result['id']=kw['message_id']
        return result

@pytest.fixture
def case(monkeypatch):
    monkeypatch.setenv('SLACK_CONVO_ECHO_CLIENT_REPLY','true')
    monkeypatch.setenv('AGENT_FIXER_CHANNEL_ID','C_FIXER')
    bus=Bus();tid,notice,key=_case(bus);_validator(bus,True)
    calls,summary=_run(bus,exact_message_id=notice['id'])
    assert summary['posted']==1
    bus.tickets[tid]['resolved_at']='2026-10-10T10:00:00Z'
    def observe(ticket,completion_id):
        return {**copy.deepcopy(ticket),'ticket_id':ticket['id'],
            'authenticated_client_id':ticket['client_id'],'completion_message_id':completion_id,
            'request_key':key,'display_status':'done','client_delivery_confirmed':True,
            'observation_ref':'authenticated:test','observed_at':datetime.now(timezone.utc).isoformat()}
    bus.read_authenticated_client_closed_projection=observe
    scope='the answer to your publishing question'
    body=O.POSTCLOSE_BODY.format(recipient='U_CLIENT',scope=scope)
    review=dict(ticket_id=tid,request_version=bus.ticket(tid)['request_version'],
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),independently_verified_by='reviewer',
        evidence_ref='review:test',production_evidence_refs=['production:test'],verified_scope=scope)
    return bus,tid,notice['id'],review

def prep(case):
    bus,tid,notice,review=case
    return H.prepare_postclose_acknowledgement(bus,expected_ticket=bus.ticket(tid),
        completion_message_id=notice,review=review)

def test_missing_authenticated_projection_denies_preparation(case):
    bus,tid,notice,review=case;del bus.read_authenticated_client_closed_projection
    before=len(bus.msgs)
    with pytest.raises(H.HistoricalCloseoutError):prep(case)
    assert len(bus.msgs)==before

@pytest.mark.parametrize('field,value',[('display_status','working'),('client_delivery_confirmed',False),
    ('authenticated_client_id','other'),('completion_message_id','wrong'),('observed_at','2000-01-01T00:00:00Z')])
def test_wrong_projection_denies(case,field,value):
    bus=case[0];observe=bus.read_authenticated_client_closed_projection
    bus.read_authenticated_client_closed_projection=lambda *args:{**observe(*args),field:value}
    with pytest.raises(H.HistoricalCloseoutError):prep(case)

def test_exact_ack_posts_once_without_close_side_effect(case):
    bus,tid,notice,review=case;before=copy.deepcopy(bus.ticket(tid));original=copy.deepcopy(bus.message(notice))
    row=prep(case);assert prep(case)['id']==row['id']
    assert row['id']==H.postclose_notice_id(before)
    calls,result=_run(bus,exact_message_id=row['id'])
    assert result['posted']==1 and result['resolved']==0
    assert len(calls)==1 and calls[0]['text']==row['body']
    assert calls[0]['text'].startswith('<@U_CLIENT> Your ticket is closed.')
    assert calls[0]['channel']=='C_CLIENT' and calls[0]['thread_ts']=='1.0'
    assert bus.ticket(tid)==before and bus.message(notice)==original
    posted=bus.message(row['id'])
    assert posted['attachments']['delivery_readback_verified'] is True
    assert O._support_send_completed(posted)
    calls,result=_run(bus,exact_message_id=row['id']);assert calls==[]


def test_projection_revoked_after_preparation_prevents_send(case):
    row=prep(case);del case[0].read_authenticated_client_closed_projection
    calls,result=_run(case[0],exact_message_id=row['id'])
    assert calls==[] and result['posted']==0


def test_ack_is_not_sent_by_broad_queue_sweep(case):
    row=prep(case);calls,result=_run(case[0])
    assert not any(c['channel']=='C_CLIENT' for c in calls)
    assert case[0].message(row['id'])['delivery_status']=='ready'


def test_lost_post_response_never_reposts(case):
    bus=case[0];row=prep(case);post,calls=_posted()
    def lost(*args,**kwargs):
        post(*args,**kwargs);raise TimeoutError('ACK lost after Slack accepted')
    lost.readback=post.readback;lost.verify_sender=post.verify_sender
    result=O.run_once(bus,lost,identity=IDS.get('echo'),member_check=lambda *a:True,
        exact_message_id=row['id'],log=lambda *a:None)
    assert len(calls)==1 and result['posted']==0
    # Original intent survives. An exact retry cannot produce another POST.
    result=O.run_once(bus,lost,identity=IDS.get('echo'),member_check=lambda *a:True,
        exact_message_id=row['id'],log=lambda *a:None)
    assert len(calls)==1
    # Existing readback reconciler may finish that original provider message.
    summary={'resolved':0,'reconciled_posted':0}
    O._reconcile_held_fixer(bus,IDS.get('echo'),post.readback,lambda *a:None,summary)
    assert len(calls)==1 and summary['resolved']==0
    reconciled=bus.message(row['id'])
    # Admission's unknown outcome stays held until its guarded reconciliation.
    assert reconciled['delivery_status']=='held'
    assert reconciled['attachments'].get('fixer_slack_delivery_intent')


def test_same_version_correction_during_final_observation_stops_post(case):
    bus=case[0];row=prep(case);observe=bus.read_authenticated_client_closed_projection
    def correction(*args):
        result=observe(*args)
        next(m for m in bus.msgs if m['direction']=='inbound')['body']='different request'
        return result
    bus.read_authenticated_client_closed_projection=correction
    calls,result=_run(bus,exact_message_id=row['id'])
    assert calls==[] and result['posted']==0


@pytest.mark.parametrize('mutation',['tenant','route','body','receipt','surface'])
def test_exact_bound_proof_change_stops_send(case,mutation):
    bus,tid,notice,review=case;row=prep(case)
    if mutation=='tenant':bus.tickets[tid]['client_id']='other'
    elif mutation=='route':bus.tickets[tid]['slack_channel_id']='C_OTHER'
    elif mutation=='surface':next(m for m in bus.msgs if m['id']==row['id'])['attachments']['surface']='im'
    elif mutation=='body':next(m for m in bus.msgs if m['id']==row['id'])['body']='Ticket closed'
    else:next(m for m in bus.msgs if m['id']==notice)['attachments']['delivery_readback_verified']=False
    calls,result=_run(bus,exact_message_id=row['id'])
    assert calls==[] and result['posted']==0


def test_competing_claim_loser_cannot_post(case,monkeypatch):
    bus=case[0];row=prep(case)
    original=bus.claim_fixer_message
    def competitor(*args,**kwargs):
        original(*args,**kwargs)
        return None
    monkeypatch.setattr(bus,'claim_fixer_message',competitor)
    calls,result=_run(bus,exact_message_id=row['id'])
    assert calls==[] and result['posted']==0


def test_closed_projection_revoked_during_send_admission_stops_post(case,monkeypatch):
    bus=case[0];row=prep(case);original=O._support_send_rpc
    def revoke(bus_arg,name,body):
        result=original(bus_arg,name,body)
        if name=='support_admission_acquire_lane':
            del bus.read_authenticated_client_closed_projection
        return result
    monkeypatch.setattr(O,'_support_send_rpc',revoke)
    calls,result=_run(bus,exact_message_id=row['id'])
    assert calls==[] and result['posted']==0
    assert bus.message(row['id'])['delivery_status']=='held'


def test_ticket_reopens_during_final_observation_stops_closed_claim(case,monkeypatch):
    bus,tid,_,_=case;row=prep(case)
    observe=bus.read_authenticated_client_closed_projection
    armed=False
    def observe_then_reopen(*args):
        result=observe(*args)
        if armed:
            bus.tickets[tid]['status']='new'
        return result
    bus.read_authenticated_client_closed_projection=observe_then_reopen
    original=O._support_send_rpc
    def acquire_then_arm(bus_arg,name,body):
        nonlocal armed
        result=original(bus_arg,name,body)
        if name=='support_admission_acquire_lane':
            armed=True
        return result
    monkeypatch.setattr(O,'_support_send_rpc',acquire_then_arm)
    calls,result=_run(bus,exact_message_id=row['id'])
    assert calls==[] and result['posted']==0
    assert bus.ticket(tid)['status']=='new'
