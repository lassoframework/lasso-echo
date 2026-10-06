"""Owner-only historical occupancy import on the guarded disposable PG database.

Uses the existing local-only stack fixture. No activation or production writes.
"""
import uuid
import pytest
from test_scene_ledger_claim_pg import (
    DSN, PSQL, _scratch_stack, _isolate_scenarios, _seed_tenant,
    _seed_object, _sql, _one, _run, _occupied_count,
)

pytestmark = pytest.mark.skipif(not DSN or not PSQL, reason="disposable local PG required")


def seed_history(state="released", date="2026-09-01", ambiguous=False):
    tenant, group = str(uuid.uuid4()), "vg_" + uuid.uuid4().hex[:12]
    _sql(f"insert into public.tenant_alias(alias_key,tenant_id) values('{tenant}','{tenant}'); "
         f"insert into public.visual_group(gym_id,group_key) values('{tenant}','{group}')")
    url, fp, _ = _seed_object(tenant, group, "0000000000000000")
    owner = _one(f"select receipt_id from public.visual_scene_owner_phash_receipt where exact_url='{url}'")
    day = "null" if date is None else f"'{date}'"
    _sql("insert into public.visual_global_usage(fingerprint,tenant_id,used_date,state,ambiguous) "
         f"values('{fp}','{tenant}',{day},'{state}',{str(ambiguous).lower()}); "
         "insert into public.visual_global_usage_member(tenant_id,group_key,fingerprint,used_date,state,ambiguous,calendar_row_id) "
         f"values('{tenant}','{group}','{fp}',{day},'{state}',{str(ambiguous).lower()},'{uuid.uuid4()}')")
    return tenant, group, fp, owner


def receipt(tenant, group, fp, source, delivered=None, day="2026-09-01"):
    _sql("insert into public.visual_scene_history_receipt(receipt_id,tenant_id,group_key,member_fingerprint,used_date,"
         "source_phash_receipt,delivered_phash_receipt,attested_by,evidence_ref) values "
         f"(gen_random_uuid(),'{tenant}','{group}','{fp}','{day}','{source}','{delivered or source}',"
         "'history-owner','verified-publication-export')")


def test_released_orphan_import_is_permanent_and_idempotent():
    t,g,f,p = seed_history()
    receipt(t,g,f,p)
    assert _one("select public.visual_scene_backfill_occupied()") == "1"
    assert _one("select public.visual_scene_backfill_occupied()") == "0"
    assert _occupied_count() == 1
    assert _one("select state from public.visual_global_usage_member") == "released"
    assert _run("delete from public.visual_scene_phash_occupied",False).returncode


def test_unknown_date_has_no_inferred_occupancy():
    t,g,f,p = seed_history("published", None, True)
    assert _one("select public.visual_scene_backfill_occupied()") == "0"
    receipt(t,g,f,p)
    assert _run("select public.visual_scene_backfill_occupied()",False).returncode
    assert _occupied_count() == 0


def test_staging_is_never_usage():
    t,g = _seed_tenant()
    _seed_object(t,g,"0000000000000000")
    assert _one("select public.visual_scene_backfill_occupied()") == "0"
    assert _occupied_count() == 0


def test_cross_tenant_receipt_fails_and_rolls_back_whole_import():
    t,g,f,p = seed_history()
    t2,g2,f2,p2 = seed_history()
    receipt(t,g,f,p)
    receipt(t2,g2,f2,p2,p)
    assert _run("select public.visual_scene_backfill_occupied()",False).returncode
    assert _occupied_count() == 0


def test_transformed_display_requires_lineage():
    t,g,f,p = seed_history()
    _,_,candidate = _seed_object(t,g,"ffffffffffffffff")
    display = _one(f"select evidence->>'owner_phash_receipt' from public.visual_scene_candidate where candidate_id='{candidate}'")
    receipt(t,g,f,p,display)
    assert _run("select public.visual_scene_backfill_occupied()",False).returncode
    assert _occupied_count() == 0


def test_receipts_and_import_are_owner_only():
    t,g,f,p = seed_history()
    receipt(t,g,f,p)
    assert _run("update public.visual_scene_history_receipt set evidence_ref='replacement'",False).returncode
    assert _run("truncate public.visual_scene_history_receipt",False).returncode
    assert _run("set role service_role; select public.visual_scene_backfill_occupied()",False).returncode
    assert _run("set role service_role; select * from public.visual_scene_history_receipt",False).returncode


def test_caller_rollback_and_conflicting_fingerprint():
    t,g,f,p = seed_history()
    receipt(t,g,f,p)
    _sql("begin; select public.visual_scene_backfill_occupied(); rollback")
    assert _occupied_count() == 0
    _sql("insert into public.visual_scene_phash_occupied(phash,tenant_id,group_key,used_date,fingerprint) "
         f"values('0000000000000000','{t}','{g}','2026-09-01','md5:11111111111111111111111111111111')")
    assert _run("select public.visual_scene_backfill_occupied()",False).returncode
    assert _occupied_count() == 1


def test_verified_transformed_source_and_display_both_import():
    t,g,f,p = seed_history()
    _,_,candidate = _seed_object(t,g,"ffffffffffffffff")
    d = _one(f"select evidence->>'owner_phash_receipt' from public.visual_scene_candidate where candidate_id='{candidate}'")
    _sql("insert into public.visual_global_render_receipt(tenant_id,source_read_receipt,delivered_read_receipt,"
         "source_exact_url,delivered_exact_url,source_fingerprint,delivered_fingerprint,operation,evidence_ref,rendered_by) "
         "select s.tenant_id,sa.read_receipt,da.read_receipt,s.exact_url,d.exact_url,s.fingerprint,d.fingerprint,"
         "'render','scratch-render','history-owner' from public.visual_scene_owner_phash_receipt s "
         "join public.visual_global_object_attestation sa on sa.exact_url=s.exact_url "
         "cross join public.visual_scene_owner_phash_receipt d "
         "join public.visual_global_object_attestation da on da.exact_url=d.exact_url "
         f"where s.receipt_id='{p}' and d.receipt_id='{d}'; "
         "insert into public.visual_global_object_lineage(tenant_id,group_key,source_exact_url,delivered_exact_url,"
         "source_fingerprint,delivered_fingerprint,render_receipt) "
         f"select tenant_id,'{g}',source_exact_url,delivered_exact_url,source_fingerprint,delivered_fingerprint,receipt_id "
         "from public.visual_global_render_receipt")
    receipt(t,g,f,p,d)
    assert _one("select public.visual_scene_backfill_occupied()") == "2"
    assert _one("select public.visual_scene_backfill_occupied()") == "0"
    assert _occupied_count() == 2


def test_disposable_server_is_pg17():
    assert 170000 <= int(_one("show server_version_num")) < 180000
