import copy
import hashlib
import uuid
import pytest
from agent.support_historical_closeout import prepare_historical_receipt, finalize_historical_receipt, HistoricalCloseoutError, PRECLOSE_BODY


class Fake:
    def __init__(self):
        self.t = dict(id=str(uuid.uuid4()), request_version=2, status='resolved',
            source='slack_conversation', product='echo', classification='ops_fix',
            client_id='gym', bot_identity='echo', slack_user_id='U',
            slack_channel_id='C', slack_thread_ts='1.2', resolved_at='2026-10-09T12:00:00Z',
            escalated=False, hold_tier=None, client_delivery_guard_required=True)
        self.rows=[]; self.calls=[]; self.refuse=False; self.uncertain=False; self.drift=None
    def ticket(self, tid): return copy.deepcopy(self.t)
    def messages(self, tid, limit): return copy.deepcopy(self.rows)
    def inbound_count(self, tid): return sum(r['direction']=='inbound' for r in self.rows)
    def message(self, mid): return None
    def reserve_historical_receipt(self,t,**kw):
        self.calls.append('reserve')
        if self.uncertain: raise TimeoutError()
        if self.refuse: return None
        result={**t, 'ticket_id':t['id'], 'status':'pending', 'request_key':kw['request_key'],
                'notice_message_id':kw['notice_id'], 'notice_body_sha256':kw['body_sha256'], 'evidence_ref':kw['evidence_ref']}
        if self.drift=='tenant': self.t['client_id']='other'
        if self.drift=='route': self.t['slack_channel_id']='other'
        if self.drift=='transcript': self.rows.append(dict(direction='inbound',author_type='client',id='correction',created_at='now',body='changed'))
        return result
    def record_outbound(self,**kw): self.calls.append('insert'); return kw


def prep(f):
    body=PRECLOSE_BODY.format(recipient=f.t['slack_user_id'],scope='the scheduling change')
    review=dict(ticket_id=f.t['id'],request_version=f.t['request_version'],
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        independently_verified_by='independent_verifier',
        evidence_ref='review:123',production_evidence_refs=['production:123'],
        verified_scope='the scheduling change')
    return prepare_historical_receipt(f, expected_ticket=copy.deepcopy(f.t),
        body=body, review=review, selected_notice_id=str(uuid.uuid4()))


@pytest.mark.parametrize('changed', ['missing','wrong_digest','wrong_ticket','no_production_proof','premature_close','scope_closed'])
def test_prepare_requires_exact_independent_review_and_no_premature_closure(changed):
    f=Fake(); body=PRECLOSE_BODY.format(recipient=f.t['slack_user_id'],scope='the scheduling change')
    review=dict(ticket_id=f.t['id'],request_version=2,body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        independently_verified_by='independent_verifier',evidence_ref='review:123',
        production_evidence_refs=['production:123'],verified_scope='the scheduling change')
    if changed=='missing': review=None
    elif changed=='wrong_digest': review['body_sha256']='0'*64
    elif changed=='wrong_ticket': review['ticket_id']=str(uuid.uuid4())
    elif changed=='no_production_proof': review['production_evidence_refs']=[]
    elif changed=='premature_close':
        body=f'<@{f.t["slack_user_id"]}> Your ticket is closed.'
        review['body_sha256']=hashlib.sha256(body.encode()).hexdigest()
    else:
        review['verified_scope']='we have completed and closed your ticket'
        body=PRECLOSE_BODY.format(recipient=f.t['slack_user_id'],
                                  scope=review['verified_scope'])
        review['body_sha256']=hashlib.sha256(body.encode()).hexdigest()
    with pytest.raises(HistoricalCloseoutError):
        prepare_historical_receipt(f,expected_ticket=copy.deepcopy(f.t),body=body,
            review=review,selected_notice_id=str(uuid.uuid4()))
    assert f.calls==[]


def test_prepare_one_exact_fenced_row_without_send():
    f=Fake(); row=prep(f)
    assert f.calls==['reserve','insert']
    assert row['delivery_status']=='ready'
    assert row['expected_request_version']==2
    assert row['meta']['delivery_expected_client_id']=='gym'
    assert row['meta']['identity']=='echo'
    assert row['meta']['historical_receipt_notice_id']==row['message_id']
    assert row['meta']['delivery_expected_source']=='slack_conversation'


@pytest.mark.parametrize('drift',['tenant','route','transcript'])
def test_post_reserve_drift_fails_closed(drift):
    f=Fake();f.drift=drift
    with pytest.raises(HistoricalCloseoutError): prep(f)
    assert f.calls==['reserve']


@pytest.mark.parametrize('uncertain',[False,True])
def test_reservation_refusal_or_uncertainty_never_retries(uncertain):
    f=Fake();f.refuse=not uncertain;f.uncertain=uncertain
    with pytest.raises((HistoricalCloseoutError,TimeoutError)): prep(f)
    assert f.calls==['reserve']


def test_ambiguous_existing_posted_receipt_blocks():
    f=Fake(); f.t['client_delivery_guard_required']=False
    f.rows=[dict(direction='outbound',delivery_status='posted',attachments={'kind':'status'})]
    with pytest.raises(HistoricalCloseoutError): prep(f)
    assert f.calls==[]


class CloseFake(Fake):
    def __init__(self):
        super().__init__()
        import hashlib
        from agent.slack_convo.outbox import _current_fixer_request_key
        key=_current_fixer_request_key(self,self.t)
        self.notice=str(uuid.uuid4());body='done';digest=hashlib.sha256(body.encode()).hexdigest()
        att={'kind':'status','fixer':True,'resolve_notice':True,'delivery_identity_fence':True,
             'request_version':2,'request_key':key,'delivery_readback_verified':True,
             'delivery_readback_channel':'C','delivery_readback_thread_ts':'1.2',
             'delivery_readback_ts':'2.3','delivery_readback_sender':'BOT',
             'delivery_readback_body_sha256':digest,'delivery_readback_request_version':2,
             'delivery_readback_request_key':key,'fixer_slack_delivery_intent':
             dict(channel='C',thread_ts='1.2',sender='BOT',body=body,request_version=2,request_key=key)}
        for field in ('source','product','classification','client_id','bot_identity','slack_user_id','slack_channel_id','slack_thread_ts'):
            att['delivery_expected_'+field]=self.t[field]
        att['delivery_expected_status']='resolved'
        self.rows=[dict(id=self.notice,ticket_id=self.t['id'],author_type='echo',direction='outbound',
            delivery_status='posted',delivery_request_version=2,slack_ts='2.3',body=body,attachments=att)]
        self.failure=None
    def message(self,mid): return copy.deepcopy(self.rows[0]) if mid==self.notice else None
    def rpc(self,name,args):
        self.calls.append((name,args))
        if self.failure==name: raise TimeoutError(name)
        if name=='support_admission_status_lane':
            return dict(lane='support-ticket-close',generation=7,paused=self.failure=='paused',unresolved=0)
        if name=='support_admission_acquire_lane':
            if self.failure=='transcript_after_acquire':
                self.rows.append(dict(direction='inbound',author_type='client',
                    id='same-version-correction',created_at='now',body='The request changed'))
            return dict(admitted=self.failure!='denied',lane=args['p_lane'],generation=7,invocation_id=args['p_invocation_id'])
        return dict(recorded=True,lane=args['p_lane'],invocation_id=args['p_invocation_id'])
    def finalize_historical_receipt(self,**kw):
        self.calls.append(('effect',kw))
        if self.failure=='effect': raise TimeoutError()
        if self.failure=='null': return None
        a=self.rows[0]['attachments']
        self.t['verification_after']={'fixer':{'historical_receipt_recovery':dict(
            notice_message_id=self.notice,request_version=2,request_key=a['request_key'],
            notice_body_sha256=a['delivery_readback_body_sha256'],evidence_ref='review:123',resolved_at=self.t['resolved_at'])}}
        if self.failure=='readback': self.rows[0]['attachments']['delivery_readback_verified']=False
        return copy.deepcopy(self.t)


def close(f,monkeypatch):
    monkeypatch.setenv('RAILWAY_DEPLOYMENT_ID','deployment')
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA','sha')
    return finalize_historical_receipt(f,expected_ticket=copy.deepcopy(f.t),
        selected_notice_id=f.notice,note='review:123',rpc=f.rpc)


def test_close_success_exact_admission_and_readback(monkeypatch):
    f=CloseFake();result=close(f,monkeypatch)
    assert result['status']=='resolved'
    assert [c[0] for c in f.calls]==['support_admission_status_lane','support_admission_acquire_lane','effect','support_admission_finish_lane']
    assert f.calls[-1][1]['p_outcome']=='completed'
    assert f.calls[2][1]['generation']==7


@pytest.mark.parametrize('failure',['paused','denied','support_admission_acquire_lane','transcript_after_acquire','effect','support_admission_finish_lane','null','readback'])
def test_close_failure_never_reposts_or_retries(failure,monkeypatch):
    f=CloseFake();f.failure=failure
    with pytest.raises((HistoricalCloseoutError,TimeoutError)): close(f,monkeypatch)
    assert len([c for c in f.calls if c[0]=='effect'])<=1
    assert 'insert' not in f.calls
    if failure in ('support_admission_acquire_lane','effect','support_admission_finish_lane'):
        count=len(f.calls)
        with pytest.raises(HistoricalCloseoutError,match='uncertain'): close(f,monkeypatch)
        assert len(f.calls)==count
    if failure=='null':
        assert f.calls[-1][1]['p_outcome']=='unknown'
        assert f._historical_close_uncertain is True
    if failure=='transcript_after_acquire':
        assert not any(c[0]=='effect' for c in f.calls)
        assert f.calls[-1][1]['p_outcome']=='unknown'
    if failure=='readback':
        count=len(f.calls)
        assert f._historical_close_uncertain is True
        with pytest.raises(HistoricalCloseoutError,match='uncertain'):
            close(f,monkeypatch)
        assert len(f.calls)==count


@pytest.mark.parametrize('field',['delivery_readback_verified','delivery_identity_fence'])
def test_close_missing_fence_blocks_before_admission(field,monkeypatch):
    f=CloseFake();del f.rows[0]['attachments'][field]
    with pytest.raises(HistoricalCloseoutError): close(f,monkeypatch)
    assert f.calls==[]
