"""Disposable full release plus actual queue SQL, through restricted logins.

Reuses the hash-pinned accepted release seed and install order. Only the issuer
boundary is adapted to enqueue its exact frozen manifest and run the real
PostgreSQL-backed worker. Hosted bytes are the fixture's synthetic PNG reader;
this proves SQL/role composition, not production object-store delivery.
"""
import pytest

from tests import test_generated_client_release_manifest_pg as release


@pytest.mark.parametrize('ordinary', [False, True], ids=['generated-dispatch', 'ordinary-photo'])
def test_generated_client_full_dispatch_pg17(ordinary, monkeypatch):
    install = release.install
    loader = release.accepted_seed

    def install_queue(db):
        install(db)
        release.apply(db, release.ROOT / 'migrations/DRAFT_generated_issuer_dispatch_20261009.sql')
        release.apply(db, release.ROOT / 'migrations/DRAFT_generated_issuer_dispatch_20261009.verify.sql')

    def queued_seed():
        prior = release.SEED
        raw = prior.read_text()
        boundary = "   issued=issuer.issue(artifact_version_id=version,hosted_url=c['original_url'],expected_sha256=c['original_sha256'],manifest_bytes=frozen['manifest_bytes'])"
        assert raw.count(boundary) == 1
        queued = '''   from agent.generated_issuer_dispatch_store import GeneratedIssuerDispatchStore
   from agent.generated_hosted_byte_issuer_worker import IssuerDispatchRequest,HostedByteIssuerWorker,dispatch_key
   sql("create role dispatch_producer login;grant generated_issuer_dispatch_producer_20261009 to dispatch_producer;grant generated_issuer_dispatch_issuer_20261009 to issuer_a;insert into generated_issuer_dispatch_principals_20261009 values('dispatch_producer','gym',true,false),('issuer_a','gym',false,true)")
   producer=GeneratedIssuerDispatchStore(lambda:connect('dispatch_producer'))
   queue=GeneratedIssuerDispatchStore(lambda:connect('issuer_a'))
   request=IssuerDispatchRequest('gym',str(version),c['original_url'],c['original_sha256'],frozen['manifest_bytes'])
   submitted=producer.submit(request)
   assert submitted['status']=='submitted'
   assert producer.result('gym',str(version),request.binding_digest())['status']=='submitted'
   pending=queue.pending(frozenset({'gym'}),1)
   assert pending==[request] and pending[0].manifest_bytes==frozen['manifest_bytes']
   worker=HostedByteIssuerWorker(authority=issuer,ledger=queue)
   with patch.dict(os.environ,{'AGENT_GENERATED_HOSTED_BYTE_ISSUER':'true'}):
    issued=worker.dispatch(pending[0],authenticated_tenant='gym')
    replay=worker.dispatch(request,authenticated_tenant='gym')
   assert issued['status']=='issued' and replay['status']=='replayed'
   assert replay['receipt_id']==issued['receipt_id']
   result=producer.result('gym',str(version),request.binding_digest())
   assert result['receipt_id']==issued['receipt_id'] and result['status']=='issued'
   assert queue.pending(['gym'],1)==[]
   assert sql('select count(*) from generated_issuer_dispatch_ledger_20261009 where dispatch_key=%s',(dispatch_key('gym',str(version)),))[0][0]==1
   assert sql('select count(*) from generated_hosted_byte_receipts_20261009 where artifact_version_id=%s',(version,))[0][0]==1
   probe=connect('dispatch_producer')
   denied(lambda:rpc(probe,'generated_issuer_dispatch_claim_20261009',dispatch_key('gym',str(version)),request.binding_digest()),'producer cannot claim')
   denied(lambda:sql('select * from generated_issuer_dispatch_requests_20261009',con=probe),'producer has no table read')
   probe.close()
   print('PASS full manifest exact frozen queue, restricted issuer, durable result and replay',flush=True)
'''
        preparation = "   assert prepared['prepared'] and prepared['calendar_row_id']==new_id"
        assert raw.count(preparation) == 1
        service = '''
   exact=rpc(service,'generated_client_service_preparation_20261009','gym',new_id,version,issued['receipt_id'],prepared['manifest_sha256'],Jsonb(prepared['stage_plan']))
   assert exact['prepared'] and exact['stage_plan']==prepared['stage_plan']
   denied(lambda:rpc(service,'generated_client_service_preparation_20261009','foreign',new_id,version,issued['receipt_id'],prepared['manifest_sha256'],Jsonb(prepared['stage_plan'])),'service cross tenant refuses')
   print('PASS queued receipt reader admission and exact service preparation',flush=True)
'''

        class Proxy:
            def read_text(self):
                return raw.replace(boundary, queued).replace(preparation, preparation + service)

            def __fspath__(self):
                return str(prior)

            def __str__(self):
                return str(prior)

        release.SEED = Proxy()
        try:
            return loader()
        finally:
            release.SEED = prior

    monkeypatch.setattr(release, 'install', install_queue)
    monkeypatch.setattr(release, 'accepted_seed', queued_seed)
    release.test_generated_client_full_release_manifest_pg17(ordinary, monkeypatch)
