"""Real service preparation replay for the real store's canonical payload shape."""
from tests import test_generated_client_release_manifest_pg as release
from tests import test_generated_client_service_preparation_pg as service_checks


def test_real_store_tenant_payload_service_replay_pg17(monkeypatch):
    original=release.SEED
    class CanonicalSeed:
        def read_text(self):
            source=original.read_text()
            old="payload=prep.canonical(dict(members=[dict(row=plan['planned_row'],observation=None)],old_rows=[plan['old_snapshot']]))"
            new="payload=prep.canonical(dict(tenant_id='gym',members=[dict(row=plan['planned_row'],observation=None)],old_rows=[plan['old_snapshot']]))"
            assert source.count(old) == 1
            marker="   # Exact old-placeholder change must rollback before rebind/reservation."
            checks='''
   for bad in [dict(tenant_id='foreign'),dict(extra=True)]:
    db.execute('begin');db.execute('set local session_replication_role=replica')
    db.execute('update forward_schedule_stage_batch_20261008 set request_payload=request_payload||%s where batch_id=%s',(Jsonb(bad),batch))
    db.execute('set local session_replication_role=origin');db.execute('set local role service_role')
    denied(lambda:rpc(db,'finalize_forward_schedule_staged_batch_20261008','gym',batch,Jsonb(candidates),Jsonb([plan['old_snapshot']])),'foreign tenant or extra canonical payload key refuses finalization')
    db.execute('rollback')
   assert sql('select count(*) from generated_client_finalizer_decision_20261009')[0][0]==0
'''
            assert source.count(marker) == 1
            return source.replace(old,new).replace(marker,checks+marker)
        def __str__(self):
            return str(original)
        def __getattr__(self,name):
            return getattr(original,name)
    monkeypatch.setattr(release,'SEED',CanonicalSeed())
    service_checks.test_generated_client_service_preparation_pg17(monkeypatch)
