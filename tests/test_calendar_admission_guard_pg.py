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


def platform_sibling(sql, seed, dsn, anchor):
    """Same logical slot/source with a real distinct controlled rendition.

    Each row has its own observation, signed certificate, manifest, lineage and
    visual attestation. Only the synthetic signing key is reused from the
    composed harness; production functions and constraints remain unchanged.
    """
    import copy
    import hashlib
    from dataclasses import asdict
    import psycopg
    from agent import forward_media_attester as attester, forward_media_prepare as media_prepare
    from agent.forward_media_photo_certificate import canonical, digest
    from agent.forward_media_still_certificate_v2 import IndependentStillPhotoAuditorV2

    closure = dict(zip(seed.__code__.co_freevars, (cell.cell_contents for cell in seed.__closure__)))
    recipe = attester.make_still_recipe('gbp_crop_4x3')
    delivered = attester.replay_still_recipe(anchor['bytes'], recipe)['image_bytes']
    assert delivered != anchor['bytes']
    image_md5 = hashlib.md5(delivered).hexdigest()
    image_sha = hashlib.sha256(delivered).hexdigest()
    assert image_sha != anchor['sha']
    rid, batch = str(uuid.uuid4()), str(uuid.uuid4())
    image_url = 'https://scratch.example/' + uuid.uuid4().hex + '.jpg'
    group = anchor['packet']['payload']['candidate']['group_key']
    row = {'id': rid, 'gym_id': anchor['tenant'], 'post_date': anchor['day'],
           'account': 'facebook', 'format': 'feed', 'status': 'pending',
           'logical_post_id': anchor['logical'], 'source_media_url': anchor['url'],
           'image_url': image_url, 'source_media_asset_id': anchor['asset'], 'visual_group_key': group}
    observation = {'schema_version': 1, 'provenance_status': 'unverified', 'tenant': anchor['tenant'],
                   'source_asset_id': anchor['asset'], 'source_exact_url': anchor['url'],
                   'delivered_exact_url': image_url, 'source_sha256': anchor['sha'],
                   'delivered_sha256': image_sha, 'source_byte_length': len(anchor['bytes']),
                   'delivered_byte_length': len(delivered), 'recipe': recipe, 'hold_reasons': []}
    raw = canonical(observation)
    obs = {'digest_input': raw, 'observation_json': canonical(dict(
        observation, observation_digest=hashlib.sha256(raw.encode()).hexdigest()))}
    request = canonical({'members': [{'row': row, 'observation': obs}], 'old_rows': []})
    sql('select stage_forward_schedule_batch_20261008(%s,%s,%s,%s)',
        (anchor['tenant'], batch, request, hashlib.sha256(request.encode()).hexdigest()), 'service_role')
    receipt = 'source-receipt:' + digest(rid)
    sql("""insert into fixer_forward_media_source_receipt_20261007
      (receipt_ref,calendar_row_id,row_revision,binding_revision,tenant_id,source_asset_id,
       source_id,folder_id,exact_source_url,source_fingerprint,source_sha256,source_length,evidence_json)
      select %s,r.id,md5(to_jsonb(r)::text),md5(jsonb_build_array(to_jsonb(a),to_jsonb(s))::text),
        r.gym_id,a.id,s.id,s.folder_id,r.source_media_url,%s,%s,%s,
        'SYNTHETIC independently verified same original sibling bytes'
      from content_calendar r join media_asset a on a.id=r.source_media_asset_id
      join media_source s on s.id=a.source_id where r.id=%s""",
        (receipt, 'md5:' + anchor['md5'], 'sha256:' + anchor['sha'], len(anchor['bytes']), rid))
    candidate = copy.deepcopy(anchor['packet']['payload']['candidate'])
    candidate.update(calendar_row_id=rid, image_url=image_url,
                     image_fingerprint='md5:' + image_md5, image_sha256='sha256:' + image_sha,
                     image_length=len(delivered), source_receipt_ref=receipt,
                     render_recipe_digest=digest(recipe), content_digest=sql(
                         "select 'sha256:'||encode(sha256(convert_to(fixer_forward_media_photo_content_20261007(%s)::text,'UTF8')),'hex')",
                         (rid,))[0][0])
    snapshot = sql('select fixer_still_photo_snapshot_v2_20261008()')[0][0]
    payload = copy.deepcopy(anchor['packet']['payload'])
    dispositions = [{'history_key': h['history_key'], 'disposition': 'reviewed_visual_nonmatch',
                     'inspected_sha256': h['visual_sha256'], 'published_binding_ref': h['published_binding_ref'],
                     'review_evidence_ref': 'SYNTHETIC complete corpus review with authorized same-slot sibling'}
                    for h in snapshot['rows']]
    payload.update(audit_id=str(uuid.uuid4()), candidate=candidate, baseline_id=snapshot['baseline_id'],
                   generation=snapshot['generation'], spine_digest=snapshot['spine_digest'],
                   dispositions=dispositions, disposition_digest=digest(dispositions))
    packet = {'payload': payload, 'signature_hex': closure['private'].sign(canonical(payload).encode()).hex()}
    with psycopg.connect(dsn) as conn:
        conn.execute('set role photo_auditor')
        cert = IndependentStillPhotoAuditorV2(conn, 'photo_auditor').submit(packet)
    original = media_prepare.OriginalRegistration(anchor['tenant'], anchor['asset'], anchor['url'],
        'md5:' + anchor['md5'], len(anchor['bytes']), receipt)
    manifest = media_prepare.build_render_manifest(original, image_url, delivered, 'render',
        cert.receipt_ref, render_recipe=recipe)
    sql('select fixer_prepare_owner_staged_still_v2_20261008(%s,%s::jsonb,%s::jsonb)',
        (payload['audit_id'], json.dumps(asdict(original)), json.dumps(asdict(manifest))), 'photo_owner')
    sql('select fixer_bind_forward_schedule_staged_manifest_20261008(%s)', (rid,), 'service_role')
    rev = sql("select fixer_forward_media_attestation_request_20261006(%s)->>'revision'", (rid,))[0][0]
    evidence = str(uuid.uuid4())
    sql("select fixer_attest_forward_media_20261006(%s,%s,%s,%s,%s,%s,%s,null,null,'render',%s)",
        (rid, rev, evidence, 'md5:' + anchor['md5'], len(anchor['bytes']), 'md5:' + image_md5,
         len(delivered), cert.receipt_ref), 'fixer_forward_media_attester_20261006')
    reads = sql('select source_read_receipt,image_read_receipt from fixer_forward_media_lineage_20261006 where evidence_id=%s', (evidence,))[0]
    ids = []
    for role, url, data, sha, md5, read in [
        ('original', anchor['url'], anchor['bytes'], anchor['sha'], anchor['md5'], reads[0]),
        ('delivered', image_url, delivered, image_sha, image_md5, reads[1]),
        ('thumbnail', image_url, delivered, image_sha, image_md5, reads[1])]:
        aid = str(uuid.uuid4()); ids.append(aid)
        sql('insert into forward_media_visual_attestation(attestation_id,tenant_key,media_url,role,source_sha256,source_md5,byte_length,phash_v1,row_revision,lineage_receipt_id,object_read_receipt_id) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (aid, anchor['tenant'], url, role, sha, md5, len(data), 0, int(rev[:15], 16), evidence, read),
            'fixer_forward_media_attester_20261006')
    return {'rid': rid, 'logical': anchor['logical'], 'batch': batch, 'tenant': anchor['tenant'],
            'rev': rev, 'ids': ids, 'packet': packet, 'image_sha': image_sha}


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

    # Same source/day/logical slot can activate a distinct platform rendition
    # without weakening the staged-preparation duplicate-rendition exclusion.
    sibling = platform_sibling(sql, seed, dsn, c)
    occupancy = sql('select admit_prospective_still_v2_20261008(%s,%s,%s,%s::uuid[],%s)',
        (sibling['rid'], sibling['logical'], sibling['rev'], sibling['ids'],
         sibling['packet']['payload']['audit_id']), 'photo_owner')[0][0]
    assert occupancy == sql('select occupancy_id from forward_prospective_photo_binding_20261008 where calendar_row_id=%s', (c['rid'],))[0][0]
    sibling_receipt = finalize(sibling)
    assert finalize(sibling) == sibling_receipt
    assert sql('select count(*) from forward_prospective_photo_occupancy_20261008')[0][0] == 1
    assert sql('select count(*) from forward_prospective_photo_binding_20261008 where occupancy_id=%s', (occupancy,))[0][0] == 2
    assert sql("select array_agg(account order by account) from content_calendar where logical_post_id=%s and variant_status='active'", (c['logical'],))[0][0] == ['facebook', 'instagram']
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
    print('PASS: real PG17 registered preparation/finalization; null logical insert; PATCH/backfill/clear/swap; definer/GUC/exact OID/config/default-ACL/grantor-cascade isolation; borrowed/stale authority; tenant/date/source/derivative conflicts; distinct-rendition same-slot platform siblings; concurrent refusal; nonmedia operations; exact persisted old-row archival')


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
