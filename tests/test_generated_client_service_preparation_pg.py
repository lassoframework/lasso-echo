"""Exact service preparation read against the unchanged full PG17 release fixture."""
from tests import test_generated_client_release_manifest_pg as release


def test_generated_client_service_preparation_pg17(monkeypatch):
    original = release.accepted_seed

    def seed_with_service_checks():
        # The accepted loader compiles a frozen fixture. Inject assertions at the
        # committed preparation boundary, before finalization changes authority.
        raw = release.SEED.read_text()
        marker = "   assert prepared['prepared'] and prepared['calendar_row_id']==new_id"
        assert raw.count(marker) == 1
        checks = '''
   args=['gym',new_id,version,issued['receipt_id'],prepared['manifest_sha256'],Jsonb(prepared['stage_plan'])]
   got=rpc(service,'generated_client_service_preparation_20261009',*args)
   assert got['stage_plan']==prepared['stage_plan'] and got['prepared']
   for offset,bad in [(0,'other-gym'),(1,uuid.uuid4()),(2,uuid.uuid4()),(3,uuid.uuid4()),(4,'0'*64),(5,Jsonb({}))]:
    forged=list(args);forged[offset]=bad
    denied(lambda:rpc(service,'generated_client_service_preparation_20261009',*forged),'forged exact preparation refuses')
   denied(lambda:rpc(owner,'generated_client_service_preparation_20261009',*args),'owner cannot use service preparation')
   owner.rollback()
   denied(lambda:sql('select * from generated_client_admission_20261009',con=service),'no broad preparation SELECT')
   sql('update generated_client_control_20261009 set enabled=false')
   denied(lambda:rpc(service,'generated_client_service_preparation_20261009',*args),'revoked preparation refuses')
   sql('update generated_client_control_20261009 set enabled=true')
   sql("update content_calendar set caption=caption||' changed' where id=%s",(rid,))
   denied(lambda:rpc(service,'generated_client_service_preparation_20261009',*args),'changed placeholder refuses')
   sql('update content_calendar set caption=%s where id=%s',(prepared['stage_plan']['old_snapshot']['caption'],rid))
   assert rpc(service,'generated_client_service_preparation_20261009',*args)['prepared']
   sql('create role preparation_authenticator login noinherit;grant service_role,authenticated to preparation_authenticator')
   rest=connect('preparation_authenticator')
   rest.execute('begin');rest.execute('set local role service_role')
   assert rpc(rest,'generated_client_service_preparation_20261009',*args)['prepared']
   rest.execute('rollback');rest.execute('begin');rest.execute('set local role authenticated')
   denied(lambda:rpc(rest,'generated_client_service_preparation_20261009',*args),'PostgREST authenticated ACL refuses')
   rest.execute('rollback');rest.close()
   # Deliberately corrupt only the disposable transaction: a sentinel alone
   # must never acquire persisted stage authority. Rollback restores all rows.
   db.execute('begin');db.execute('set local session_replication_role=replica')
   fake={**prepared['stage_plan']['planned_row'],'media_not_ready_reason':'forward_reservation_staged'}
   db.execute('insert into content_calendar select (jsonb_populate_record(null::content_calendar,%s)).*',(Jsonb(fake),))
   db.execute('set local session_replication_role=origin');db.execute('set local role service_role')
   denied(lambda:rpc(db,'generated_client_service_preparation_20261009',*args),'sentinel without matching stage batch refuses')
   db.execute('rollback')
   print('PASS exact service preparation, forged tuple/plan, cross tenant, revoked and changed snapshot; no table or owner grant',flush=True)
'''
        # Apply the same accepted fixture adapters using a temporary Path-like
        # proxy. Hash-pinned original files remain untouched.
        class SeedProxy:
            def read_text(self):
                stage_marker="   denied(lambda:sql(\"update content_calendar set status='approved' where id=%s\",(new_id,)),'approve before finalization')"
                staged_checks="""
   assert rpc(service,'generated_client_service_preparation_20261009',*args)['prepared']
   for corrupt in ["update forward_schedule_stage_batch_20261008 set tenant_id='foreign' where batch_id=%s",
                   "update forward_schedule_stage_batch_20261008 set request_payload='{}'::jsonb where batch_id=%s",
                   "update forward_schedule_stage_batch_20261008 set request_payload=jsonb_set(request_payload,'{tenant_id}','\\\"foreign\\\"'::jsonb) where batch_id=%s",
                   "update forward_schedule_stage_batch_20261008 set request_payload=request_payload||'{\\\"extra\\\":true}'::jsonb where batch_id=%s",
                   "update content_calendar set caption=caption||' changed' where id=%s"]:
    db.execute('begin');db.execute('set local session_replication_role=replica')
    db.execute(corrupt,(new_id if corrupt.startswith('update content_calendar') else batch,))
    db.execute('set local session_replication_role=origin');db.execute('set local role service_role')
    denied(lambda:rpc(db,'generated_client_service_preparation_20261009',*args),'foreign or changed staged authority refuses')
    db.execute('rollback')
   assert rpc(service,'generated_client_service_preparation_20261009',*args)['prepared']
   print('PASS service replay after exact staged sentinel and persisted batch membership',flush=True)
"""
                return raw.replace(marker, marker + checks).replace(stage_marker,staged_checks+stage_marker).replace(
                    "   print('PASS actual separate finalizer worker function + service SQL + exact terminal receipt retry',flush=True)",
                    "   denied(lambda:rpc(service,'generated_client_service_preparation_20261009',*args),'finalized preparation refuses')\n   print('PASS actual separate finalizer worker function + service SQL + exact terminal receipt retry',flush=True)")
            def __fspath__(self):
                return str(prior)
            def __str__(self):
                return str(prior)
        prior = release.SEED
        release.SEED = SeedProxy()
        try:
            return original()
        finally:
            release.SEED = prior

    monkeypatch.setattr(release, 'accepted_seed', seed_with_service_checks)
    release.test_generated_client_full_release_manifest_pg17(False, monkeypatch)
