from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from agent.slack_convo.bus import Bus

T='11111111-1111-1111-1111-111111111111'; G='22222222-2222-2222-2222-222222222222'; M='33333333-3333-3333-3333-333333333333'
@pytest.fixture
def case(monkeypatch):
 for k,v in {'SUPPORT_CLOSED_PROJECTION_ENABLED':'true','SUPPORT_CLOSED_PROJECTION_SECRET':'hidden-secret','SUPPORT_CLOSED_PROJECTION_URL':'https://ops.lassoframework.com/api/internal/support/closed-projection'}.items():monkeypatch.setenv(k,v)
 t=dict(id=T,client_id=G,request_version=1,source='slack_conversation',product='echo',classification='ops_fix',bot_identity='echo',slack_user_id='U1',slack_channel_id='C1',slack_thread_ts=None,resolved_at='2026-10-10T10:00:00Z',verification_after={'fixer':{'request_key':'a'*64,'request_key_version':1,'historical_receipt_recovery':{'notice_message_id':M,'request_version':1,'request_key':'a'*64}}})
 result={**t,'ticket_id':T,'authenticated_client_id':G,'completion_message_id':M,'request_key':'a'*64,'display_status':'done','client_delivery_confirmed':True,'authentication_kind':'machine_projection_parity','live_client_ui_canary_required':True,'observed_at':datetime.now(timezone.utc).isoformat(),'observation_ref':'portal-machine-projection:test','completion_slack_ts':'1.0'}
 calls=[]
 def post(*args,**kw):calls.append((args,kw));return SimpleNamespace(status_code=200,json=lambda:result)
 return Bus(url='unused',service_key='unused',http=SimpleNamespace(post=post)),t,result,calls

def test_machine_parity_contract(case):
 b,t,r,c=case;assert b.read_authenticated_client_closed_projection(t,M)==r
 assert len(c)==1;assert c[0][1]['allow_redirects'] is False
 assert 'hidden-secret' not in str(r)

@pytest.mark.parametrize('field,value',[('authenticated_client_id',M),('completion_message_id',G),('request_version',2),('display_status','working'),('client_delivery_confirmed',False),('authentication_kind','authenticated_client'),('observed_at','2020-01-01T00:00:00Z')])
def test_invalid_projection_returns_no_proof(case,field,value):
 b,t,r,c=case;r[field]=value;assert b.read_authenticated_client_closed_projection(t,M) is None;assert len(c)==1

def test_default_off_and_missing_secret(case,monkeypatch):
 b,t,r,c=case;monkeypatch.delenv('SUPPORT_CLOSED_PROJECTION_ENABLED');assert b.read_authenticated_client_closed_projection(t,M) is None
 monkeypatch.setenv('SUPPORT_CLOSED_PROJECTION_ENABLED','true');monkeypatch.delenv('SUPPORT_CLOSED_PROJECTION_SECRET');assert b.read_authenticated_client_closed_projection(t,M) is None;assert not c

def test_wrong_recovery_receipt(case):
 b,t,r,c=case;t['verification_after']['fixer']['historical_receipt_recovery']['notice_message_id']=G
 assert b.read_authenticated_client_closed_projection(t,M) is None

@pytest.mark.parametrize('url',[
 'https://untrusted.example/api/internal/support/closed-projection',
 'https://ops.lassoframework.com:8443/api/internal/support/closed-projection',
 'https://ops.lassoframework.com@untrusted.example/api/internal/support/closed-projection',
])
def test_projection_secret_never_sent_to_other_hosts(case,monkeypatch,url):
 b,t,r,c=case;monkeypatch.setenv('SUPPORT_CLOSED_PROJECTION_URL',url)
 assert b.read_authenticated_client_closed_projection(t,M) is None
 assert c==[]

def test_uncertainty_has_no_retry_or_secret_error(case):
 b,t,r,c=case
 def fail(*a,**kw):c.append(1);raise RuntimeError('hidden-secret')
 b._http.post=fail;assert b.read_authenticated_client_closed_projection(t,M) is None;assert len(c)==1
