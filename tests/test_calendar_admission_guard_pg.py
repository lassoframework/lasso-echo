"""Real disposable PG17 admission boundary, composed with the full still-v2 stack.

All source bytes, roles and signed certificates synthetic. No production DSN,
network, installs or provider calls. Reuses the established genuine Ed25519
fixture and performs only focused acceptance for this migration-last seam.
"""
import json
from pathlib import Path
import sys
import uuid
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_forward_media_prospective_still_v2_pg import main as composed_pg
from tests.test_forward_media_owner_two_phase_pg import png

MIGRATION = 'DRAFT_fixer_calendar_admission_guard_20261009.sql'


def acceptance(*, sql, denied, seed, attest, dsn):
    import psycopg
    # Non-PUBLIC defaults must not widen an existing entry or its private copy.
    # Compare original OIDs and every explicit EXECUTE grantor, grantee and
    # grant option, including an onward custom-role grant.
    assert sql('select count(*) from synthetic_original_entry_acl')[0][0] >= 4
    assert sql("""select bool_and((to_jsonb(p)-'prosrc')=(before.catalog-'prosrc'))
      from synthetic_original_entry_catalog before join pg_proc p on p.oid=before.function_oid
    """)[0][0] is True, 'original entry catalog properties changed'
    assert sql("""select bool_and(body.prosrc=before.catalog->>'prosrc'
      and (to_jsonb(body)-array['prosrc','proacl','oid','proname'])=
          (before.catalog-array['prosrc','proacl','oid','proname']))
      from synthetic_original_entry_catalog before
      join pg_proc body on body.proname='fixer_admission_body_'||before.function_oid::text
    """)[0][0] is True, 'private copy body/config/security shape changed'

    assert sql("""with current_acl as (
      select p.oid as function_oid,p.proname,a.grantor,a.grantee,a.privilege_type,a.is_grantable
      from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      cross join lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
      where n.nspname='public' and p.proname in (select proname from synthetic_original_entry_acl)
    ), difference as (
      (select * from current_acl except select * from synthetic_original_entry_acl)
      union all
      (select * from synthetic_original_entry_acl except select * from current_acl)
    ) select count(*) from difference""")[0][0] == 0, 'original entry OID or exact grantor ACL drift'
    for role in ('anon','authenticated','synthetic_default_executor'):
        assert sql("select bool_and(not has_function_privilege(%s,p.oid,'execute')) from pg_proc p where p.proname in (select proname from synthetic_original_entry_acl)", (role,))[0][0] is True
    assert sql("select has_function_privilege('synthetic_original_executor','fixer_bind_forward_media_manifest_20261006(uuid)','execute with grant option')")[0][0] is True
    assert sql("select bool_and(not has_function_privilege('synthetic_original_executor',p.oid,'execute')) from pg_proc p where proname like 'fixer_admission_body_%'")[0][0] is True
    assert sql("select has_function_privilege('synthetic_downstream_executor','fixer_bind_forward_media_manifest_20261006(uuid)','execute')")[0][0] is True
    assert sql("select bool_and(not has_function_privilege('synthetic_downstream_executor',p.oid,'execute')) from pg_proc p where proname like 'fixer_admission_body_%'")[0][0] is True
    assert sql("""with current_acl as (
        select a.* from pg_proc p cross join lateral aclexplode(p.proacl) a
        where p.oid='public.synthetic_unrelated_acl()'::regprocedure
      ), difference as (
        (select * from current_acl except select * from synthetic_unrelated_acl_before)
        union all
        (select * from synthetic_unrelated_acl_before except select * from current_acl)
      ) select count(*) from difference""")[0][0] == 0, 'unrelated function grant chain changed'
    # PostgreSQL must still see the original grantor dependency, rather than
    # attributing the downstream grant to postgres during wrapper install.
    sql('revoke grant option for execute on function public.fixer_bind_forward_media_manifest_20261006(uuid) from synthetic_original_executor cascade')
    assert sql("select has_function_privilege('synthetic_downstream_executor','fixer_bind_forward_media_manifest_20261006(uuid)','execute')")[0][0] is False
    assert sql("select has_function_privilege('synthetic_original_executor','fixer_bind_forward_media_manifest_20261006(uuid)','execute')")[0][0] is True
    assert sql("select has_function_privilege('synthetic_original_executor','fixer_bind_forward_media_manifest_20261006(uuid)','execute with grant option')")[0][0] is False
    assert sql('select synthetic_unrelated_acl()',role='synthetic_downstream_executor')[0][0] == 1
    assert sql('select enabled from fixer_calendar_admission_gate_20261009')[0][0] is False
    denied('update fixer_calendar_admission_gate_20261009 set enabled=true', fragment='permission denied')
    for role in ('service_role', 'fixer_forward_media_owner_20261006', 'fixer_forward_media_attester_20261006'):
        denied('insert into fixer_calendar_admission_capability_20261009 values(gen_random_uuid(),pg_backend_pid(),pg_current_xact_id(),\'finalize\',array[]::uuid[])', role=role, fragment='permission denied')
        assert sql("select bool_and(not has_function_privilege(%s,p.oid,'execute')) from pg_proc p where proname like 'fixer_admission_body_%%'", (role,))[0][0] is True
    sql('update fixer_calendar_admission_gate_20261009 set enabled=true')
    sql('update forward_prospective_photo_gate_20261008 set enabled=true')
    c = seed()

    def prepare(candidate, phash=0, delivered_phash=None):
        sql('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',
            (candidate['packet']['payload']['audit_id'],json.dumps(candidate['original']),json.dumps(candidate['manifest'])), 'photo_owner')
        sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)', (candidate['rid'],), 'service_role')
        attest(candidate, phash=phash, delivered_phash=delivered_phash)

    def candidates(candidate, **changes):
        item = {'calendar_row_id':candidate['rid'],'logical_post_id':candidate['logical'],
                'expected_revision':candidate['rev'],'attestation_ids':candidate['ids'],
                'expected_reservation_id':None}
        item.update(changes)
        return json.dumps([item])

    def finalize(candidate, **changes):
        return sql('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,\'[]\'::jsonb)',
                   (candidate['tenant'],candidate['batch'],candidates(candidate,**changes)), 'service_role')[0][0]

    prepare(c, delivered_phash=(1<<32)-1)
    sql('update forward_prospective_photo_gate_20261008 set enabled=true')
    sql('update forward_media_visual_gate_20261008 set enabled=true')
    sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
        (c['rid'],c['logical'],c['rev'],c['ids'],c['packet']['payload']['audit_id']), 'photo_owner')
    receipt = finalize(c)
    assert finalize(c) == receipt
    assert sql('select variant_status from content_calendar where id=%s', (c['rid'],))[0][0] == 'active'
    assert sql('select count(*) from fixer_calendar_admission_capability_20261009')[0][0] == 0

    # Null logical identity, caller flags and a postgres-owned legacy definer
    # cannot authorize a direct active insertion or a media swap.
    denied("insert into content_calendar(id,gym_id,variant_status,image_url) values(gen_random_uuid(),'gym','active','https://scratch.example/unproved')", fragment='active media INSERT')
    denied("set local fixer.calendar_authorized='true'; insert into content_calendar(id,gym_id,variant_status,image_url) values(gen_random_uuid(),'gym','active','https://scratch.example/unproved')", fragment='active media INSERT')
    sql("create function synthetic_legacy_media_patch(p uuid) returns void language sql security definer as $$update content_calendar set image_url='https://scratch.example/legacy' where id=p$$;grant execute on function synthetic_legacy_media_patch(uuid) to service_role")
    denied('select synthetic_legacy_media_patch(%s)', (c['rid'],), fragment='identity immutable')
    for column, value in [('source_media_asset_id', 'forged'),('source_media_url','https://scratch.example/backfill'),
                          ('image_url','https://scratch.example/swap'),('thumbnail_url','https://scratch.example/thumb'),
                          ('gym_id','other-gym'),('post_date','2026-10-11'),('logical_post_id',str(uuid.uuid4())),
                          ('render_manifest_digest','forged'),('account','facebook'),('format','story'),
                          ('visual_group_key','forged'),('caption','changed signed content')]:
        denied(f'update content_calendar set {column}=%s where id=%s', (value,c['rid']), fragment='logical_post_id cannot' if column=='logical_post_id' else 'identity immutable')
    denied('update content_calendar set source_media_asset_id=null,source_media_url=null,image_url=null,thumbnail_url=null,render_manifest_digest=null where id=%s', (c['rid'],), fragment='identity immutable')
    denied('update content_calendar set variant_status=\'archived\' where id=%s', (c['rid'],), fragment='retirement requires')
    denied('delete from content_calendar where id=%s', (c['rid'],), fragment='active media DELETE')
    sql("update content_calendar set status='approved' where id=%s", (c['rid'],), 'service_role')
    # Nonmedia content remains writable, including active null-logical rows.
    text_id = str(uuid.uuid4())
    sql("insert into content_calendar(id,gym_id,status,variant_status,caption) values(%s,'gym','draft','active','text only')", (text_id,), 'service_role')
    sql("update content_calendar set caption='text edited',status='approved' where id=%s", (text_id,), 'service_role')
    sql('delete from content_calendar where id=%s', (text_id,), 'service_role')

    # Borrowed/stale bindings never mint finalizer authority for another row.
    pending = seed(data_bytes=png('green')); prepare(pending, phash=-1)
    for changes in ({'expected_revision':c['rev'],'attestation_ids':c['ids'],'expected_reservation_id':receipt['reservation_ids'][0]},
                    {'expected_revision':'stale'}):
        denied('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,\'[]\'::jsonb)',
               (pending['tenant'],pending['batch'],candidates(pending,**changes)), fragment=None)
    denied("update content_calendar set variant_status='active',media_not_ready_reason=null where id=%s", (pending['rid'],), fragment='registered staged finalization')
    assert sql('select variant_status from content_calendar where id=%s', (pending['rid'],))[0][0]=='candidate'
    assert sql('select count(*) from fixer_calendar_admission_capability_20261009')[0][0]==0

    # Source reuse across tenant/date and derivative-only near pHash conflict
    # remain enforced by the composed permanent occupancy fence.
    for tenant, day in [('other-gym','2026-10-10'),('gym','2026-10-11')]:
        competitor = seed(tenant=tenant, day=day, data_bytes=c['bytes'])
        denied('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',
               (competitor['packet']['payload']['audit_id'],json.dumps(competitor['original']),json.dumps(competitor['manifest'])),
               role='photo_owner',fragment='already used cleared or reserved')
        denied('select reserve_forward_slot_20261008(%s,%s,%s,%s::uuid[],null)',
               (competitor['rid'],competitor['logical'],c['rev'],c['ids']),fragment='permanent prospective still occupancy conflict')
    derivative = seed(data_bytes=png('red'));prepare(derivative,phash=(1<<32)-1)
    denied('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
           (derivative['rid'],derivative['logical'],derivative['rev'],derivative['ids'],derivative['packet']['payload']['audit_id']),
           role='photo_owner',fragment='permanent prospective still occupancy conflict')

    # Two real sessions racing direct null-logical insertion both fail closed;
    # no active conflict can escape the statement census lock.
    def race(_):
        with psycopg.connect(dsn) as conn:
            conn.execute('set role service_role')
            try:
                conn.execute("insert into content_calendar(id,gym_id,variant_status,image_url) values(gen_random_uuid(),'other-gym','active',%s)",(c['url'],))
            except psycopg.Error as exc:
                assert 'active media INSERT' in str(exc); return 'held'
            raise AssertionError('concurrent direct insertion escaped')
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(race,range(2)))==['held','held']
    assert sql('select count(*) from fixer_calendar_admission_capability_20261009')[0][0]==0

    # Freeze a genuine active old row into the new batch's stage request.
    # The fixture SQL closure is intercepted only to provide its supported
    # old_rows input; all stage/proof/finalizer code remains real PostgreSQL.
    sql("update content_calendar set status='pending' where id=%s", (c['rid'],), 'service_role')
    old = sql('select to_jsonb(r) from content_calendar r where id=%s', (c['rid'],))[0][0]
    import hashlib
    from agent.forward_media_photo_certificate import canonical
    cell = seed.__closure__[seed.__code__.co_freevars.index('sql')]
    original_sql = cell.cell_contents
    def stage_with_old(q,args=None,role=None):
        if q.startswith('select stage_forward_schedule_batch_20261008'):
            tenant,batch,request,_ = args
            payload=json.loads(request);payload['old_rows']=[old]
            request=canonical(payload)
            args=(tenant,batch,request,hashlib.sha256(request.encode()).hexdigest())
        return original_sql(q,args,role)
    cell.cell_contents=stage_with_old
    try:
        replacement=seed(data_bytes=png('purple'))
    finally:
        cell.cell_contents=original_sql
    prepare(replacement,phash=-(1<<32))
    sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
        (replacement['rid'],replacement['logical'],replacement['rev'],replacement['ids'],replacement['packet']['payload']['audit_id']), 'photo_owner')
    archived=sql('select finalize_forward_schedule_staged_batch_20261008(%s,%s,%s::jsonb,%s::jsonb)',
                 (replacement['tenant'],replacement['batch'],candidates(replacement),json.dumps([old])), 'service_role')[0][0]
    assert archived['archived_old_row_ids']==[c['rid']]
    assert sql('select variant_status from content_calendar where id=%s',(c['rid'],))[0][0]=='archived'
    assert sql('select count(*) from fixer_calendar_admission_capability_20261009')[0][0]==0
    print('PASS: real PG17 registered preparation/finalization; null logical insert; PATCH/backfill/clear/swap; definer/GUC/exact OID/config/default-ACL/grantor-cascade isolation; borrowed/stale authority; tenant/date/source/derivative conflicts; concurrent refusal; nonmedia operations; exact persisted old-row archival')


def main():
    original_read = Path.read_text
    def with_default_acl(path, *args, **kwargs):
        source = original_read(path, *args, **kwargs)
        if path.name == MIGRATION:
            # Inject only disposable pre-migration catalog setup. The DRAFT
            # itself is then installed unchanged, after the full prior stack.
            source = """
              create role synthetic_default_executor;
              create role synthetic_original_executor;
              create role synthetic_downstream_executor;
              create function public.synthetic_unrelated_acl() returns integer language sql as 'select 1';
              revoke all on function public.synthetic_unrelated_acl() from public;
              grant execute on function public.synthetic_unrelated_acl() to synthetic_original_executor with grant option;
              grant execute on function public.fixer_bind_forward_media_manifest_20261006(uuid)
                to synthetic_original_executor with grant option;
              set role synthetic_original_executor;
              grant execute on function public.fixer_bind_forward_media_manifest_20261006(uuid) to synthetic_downstream_executor;
              grant execute on function public.synthetic_unrelated_acl() to synthetic_downstream_executor;
              reset role;
              create table synthetic_unrelated_acl_before as
                select a.* from pg_proc p cross join lateral aclexplode(p.proacl) a
                where p.oid='public.synthetic_unrelated_acl()'::regprocedure;
              create table synthetic_original_entry_acl as
                select p.oid as function_oid,p.proname,a.grantor,a.grantee,a.privilege_type,a.is_grantable
                from pg_proc p join pg_namespace n on n.oid=p.pronamespace
                cross join lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                where n.nspname='public' and p.proname=any(array[
                  'finalize_forward_schedule_batch_20261008','finalize_forward_schedule_staged_batch_20261008',
                  'fixer_bind_forward_media_manifest_20261006','fixer_bind_forward_schedule_staged_manifest_20261008']);
              create table synthetic_original_entry_catalog as
                select p.oid as function_oid,p.proname,to_jsonb(p) as catalog
                from pg_proc p where p.oid in (select function_oid from synthetic_original_entry_acl);
              alter default privileges grant execute on functions to anon,authenticated,synthetic_default_executor;
            """ + source
        return source
    with patch.object(Path, 'read_text', with_default_acl):
        composed_pg(runtime_check=acceptance,extra_migrations=(MIGRATION,))


def test_calendar_admission_guard_pg():
    main()


if __name__=='__main__':
    main()
