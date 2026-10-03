"""Disposable local PostgreSQL acceptance for phase 1 receipt preparation only."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


DSN = os.environ.get("VISUAL_GROUP_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
pytestmark = pytest.mark.skipif(not DSN, reason="isolated local VISUAL_GROUP_TEST_DSN unset")


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql(statement):
    done = subprocess.run([PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-d", DSN, "-c", statement], text=True,
                          capture_output=True, timeout=30)
    if done.returncode:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def database():
    if not DSN:
        pytest.skip("local DSN unset")
    assert PSQL
    assert re.fullmatch(
        r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_visual_ledger_test(?: user=[A-Za-z0-9_-]+)?",
        DSN), "only named disposable Unix-socket DB allowed"
    assert sql("select current_database()") == "echo_visual_ledger_test"
    sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if; "
        "if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; "
        "if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
    sql("drop schema public cascade; create schema public; grant usage on schema public to anon,authenticated,service_role;")
    sql("""create table public.content_calendar(
        id uuid primary key default gen_random_uuid(), gym_id text, post_date date,
        status text default 'pending', account text, format text,
        variant_status text default 'active', image_url text, source_media_url text,
        source_media_asset_id text, drive_file_id text, byte_hash text, r2_key text,
        media_not_ready_reason text, published_at timestamptz, late_post_id text,
        publish_reservation_day date, publish_claim_token uuid,
        created_at timestamptz default now());
        create table public.media_asset(id text primary key,gym_id text,content_hash text);
        grant select,insert,update,delete on public.content_calendar to service_role;""")
    for name in ("DRAFT_visual_group_schema_20261002.sql",
                 "DRAFT_visual_group_claim_trigger_20261002.sql",
                 "DRAFT_visual_group_backfill_20261002.sql",
                 "DRAFT_visual_global_history_20261002.sql"):
        sql((MIGRATIONS / name).read_text())


def tenant_and_group(url):
    key, tid = "g_" + uuid.uuid4().hex, str(uuid.uuid4())
    sql(f"select public.visual_group_tenant_register({q(key)},{q(tid)}::uuid)")
    group = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',{q(url)})")
    return tid, group


def read_receipt(tid, url, data, *, method="verified_object_read", asset_id=None):
    receipt = str(uuid.uuid4())
    fingerprint = "md5:" + hashlib.md5(data).hexdigest()
    sql("insert into public.visual_global_object_read_receipt "
        "(receipt_id,tenant_id,exact_url,fingerprint,byte_length,acquisition_method,asset_id,evidence_ref,observed_by) "
        f"values({q(receipt)}::uuid,{q(tid)},{q(url)},{q(fingerprint)},{len(data)},"
        f"{q(method)},{q(asset_id) if asset_id else 'null'},'object-read-test','fixture_owner')")
    return receipt, fingerprint


def render_receipt(tid, source, delivered, source_url, delivered_url, source_md5, delivered_md5):
    receipt = str(uuid.uuid4())
    sql("insert into public.visual_global_render_receipt "
        "(receipt_id,tenant_id,source_read_receipt,delivered_read_receipt,source_exact_url,"
        "delivered_exact_url,source_fingerprint,delivered_fingerprint,operation,evidence_ref,rendered_by) "
        f"values({q(receipt)}::uuid,{q(tid)},{q(source)}::uuid,{q(delivered)}::uuid,"
        f"{q(source_url)},{q(delivered_url)},{q(source_md5)},{q(delivered_md5)},"
        "'render','render-test','fixture_owner')")
    return receipt


def prepare(tid, group, source, delivered, render):
    render_arg = f"{q(render)}::uuid" if render else "null"
    return sql("set role service_role; select public.visual_global_prepare_source_rendition("
               f"{q(tid)},{q(group)},{q(source)}::uuid,{q(delivered)}::uuid,"
               f"{render_arg},'test_actor')")


def test_distinct_source_and_delivered_bytes_are_attested_without_usage():
    source_url = "https://test/source.jpg?version=1"
    delivered_url = "https://test/story.jpg?version=1"
    tid, group = tenant_and_group(source_url)
    source, source_md5 = read_receipt(tid, source_url, b"raw source")
    delivered, delivered_md5 = read_receipt(tid, delivered_url, b"burned story")
    render = render_receipt(tid, source, delivered, source_url, delivered_url, source_md5, delivered_md5)
    result = prepare(tid, group, source, delivered, render)
    assert source_md5 != delivered_md5
    assert '"usage_claimed": false' in result
    assert sql(f"select count(*) from public.visual_global_scene_object_member where tenant_id={q(tid)}") == "2"
    assert sql(f"select count(*) from public.visual_global_object_lineage where tenant_id={q(tid)}") == "1"
    assert sql("select count(*) from public.visual_global_usage") == "0"
    assert prepare(tid, group, source, delivered, render) == result
    assert sql("select has_table_privilege('service_role','public.visual_global_object_read_receipt','INSERT')") == "f"
    assert sql("select has_table_privilege('service_role','public.visual_global_render_receipt','INSERT')") == "f"
    assert sql("select has_function_privilege('service_role',"
               "'public.visual_global_prepare_source_rendition(text,text,uuid,uuid,uuid,text)','EXECUTE')") == "t"
    assert sql("select has_function_privilege('service_role',"
               "'public.visual_global_claim(text,text,date,uuid,text,boolean,boolean,text)','EXECUTE')") == "f"
    assert sql("select has_function_privilege('service_role',"
               "'public.visual_global_claim_fingerprint_set(text,text,date,uuid,text,boolean,boolean,text[])',"
               "'EXECUTE')") == "f"
    assert sql("select has_function_privilege('service_role',"
               "'public.visual_global_refresh_scene_history(text,text)','EXECUTE')") == "f"
    assert sql("select has_function_privilege('service_role',"
               "'public.visual_global_scene_link_claim_guard()','EXECUTE')") == "f"
    assert sql("select has_table_privilege('service_role','public.visual_global_usage','INSERT')") == "f"
    assert sql("select has_table_privilege('service_role','public.visual_global_usage_member','INSERT')") == "f"
    with pytest.raises(RuntimeError, match="READ COMMITTED isolation"):
        sql("begin isolation level repeatable read; set role service_role; "
            "select public.visual_global_prepare_source_rendition("
            f"{q(tid)},{q(group)},{q(source)}::uuid,{q(delivered)}::uuid,"
            f"{q(render)}::uuid,'test_actor'); rollback")


def test_foreign_tenant_missing_lineage_and_rebound_url_fail_atomically():
    source_url = "https://test/source-2.jpg?version=1"
    delivered_url = "https://test/story-2.jpg?version=1"
    tid, group = tenant_and_group(source_url)
    foreign, _ = tenant_and_group("https://test/foreign.jpg")
    source, source_md5 = read_receipt(tid, source_url, b"raw 2")
    delivered, delivered_md5 = read_receipt(tid, delivered_url, b"story 2")
    with pytest.raises(RuntimeError, match="actual render receipt"):
        prepare(tid, group, source, delivered, str(uuid.uuid4()))
    assert sql(f"select count(*) from public.visual_global_object_attestation where tenant_id={q(tid)}") == "0"
    bad_tenant_read, _ = read_receipt(foreign, delivered_url, b"story 2")
    with pytest.raises(RuntimeError, match="canonical tenant"):
        prepare(tid, group, source, bad_tenant_read, str(uuid.uuid4()))
    render = render_receipt(tid, source, delivered, source_url, delivered_url, source_md5, delivered_md5)
    prepare(tid, group, source, delivered, render)
    rebound, _ = read_receipt(tid, delivered_url, b"different bytes")
    rebound_render = render_receipt(tid, source, rebound, source_url, delivered_url,
                                    source_md5, "md5:" + hashlib.md5(b"different bytes").hexdigest())
    with pytest.raises(RuntimeError, match="already bound"):
        prepare(tid, group, source, rebound, rebound_render)
    assert sql(f"select count(*) from public.visual_global_object_attestation where tenant_id={q(tid)}") == "2"
    foreign_url = "https://test/foreign-owned.jpg?version=1"
    sql(f"select public.visual_group_register_alias({q(foreign)},'canonical_url',{q(foreign_url)})")
    foreign_aliased, foreign_md5 = read_receipt(tid, foreign_url, b"foreign alias")
    foreign_render = render_receipt(tid, source, foreign_aliased, source_url, foreign_url,
                                    source_md5, foreign_md5)
    with pytest.raises(RuntimeError, match="foreign tenant"):
        prepare(tid, group, source, foreign_aliased, foreign_render)
    with pytest.raises(RuntimeError, match="another tenant or scene"):
        sql(f"select public.visual_group_register_alias({q(foreign)},'canonical_url',{q(delivered_url)})")
    same_tenant_other_group = sql(f"select public.visual_group_register_alias({q(tid)},'drive_id',"
                                  f"{q('other-' + uuid.uuid4().hex)})")
    with pytest.raises(RuntimeError, match="another tenant or scene|another group"):
        sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',"
            f"{q(delivered_url)},{q(same_tenant_other_group)})")


def test_drive_asset_hash_mismatch_refuses_registration():
    source_url = "https://test/drive-source.jpg?version=1"
    tid, group = tenant_and_group(source_url)
    asset = "asset_" + uuid.uuid4().hex
    sql(f"insert into public.media_asset values({q(asset)},{q(tid)},{q('0' * 32)})")
    sql(f"select public.visual_group_register_alias({q(tid)},'source_asset',{q(asset)},{q(group)})")
    source, _ = read_receipt(tid, source_url, b"actual bytes", method="drive_asset", asset_id=asset)
    with pytest.raises(RuntimeError, match="Drive asset tenant or MD5"):
        prepare(tid, group, source, source, None)
    assert sql(f"select count(*) from public.visual_global_object_attestation where tenant_id={q(tid)}") == "0"


def test_drive_asset_must_belong_to_the_requested_scene():
    url = "https://test/drive-scene.jpg?version=1"
    tid, group = tenant_and_group(url)
    other = sql(f"select public.visual_group_register_alias({q(tid)},'drive_id',"
                f"{q('scene-' + uuid.uuid4().hex)})")
    asset = "asset_" + uuid.uuid4().hex
    data = b"actual asset bytes"
    sql(f"insert into public.media_asset values({q(asset)},{q(tid)},"
        f"{q(hashlib.md5(data).hexdigest())})")
    sql(f"select public.visual_group_register_alias({q(tid)},'source_asset',{q(asset)},{q(other)})")
    receipt, _ = read_receipt(tid, url, data, method="drive_asset", asset_id=asset)
    with pytest.raises(RuntimeError, match="not a member of the visual scene"):
        prepare(tid, group, receipt, receipt, None)


def prepared_calendar_row(prefix):
    source_url = f"https://test/{prefix}-source.jpg?version=1"
    delivered_url = f"https://test/{prefix}-story.jpg?version=1"
    tid, group = tenant_and_group(source_url)
    source, source_md5 = read_receipt(tid, source_url, (prefix + " raw").encode())
    delivered, delivered_md5 = read_receipt(tid, delivered_url, (prefix + " story").encode())
    render = render_receipt(tid, source, delivered, source_url, delivered_url,
                            source_md5, delivered_md5)
    prepare(tid, group, source, delivered, render)
    row_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,visual_group_key) "
        f"values({q(row_id)}::uuid,{q(tid)},'2026-10-03','held','instagram',"
        f"{q(delivered_url)},{q(source_url)},{q('derived:' + delivered_md5)},{q(group)})")
    return tid, group, row_id, source_md5, delivered_md5


def test_scene_claim_consumes_source_and_rendition_and_is_owner_only():
    tid, group, row_id, source_md5, delivered_md5 = prepared_calendar_row(
        "claim-" + uuid.uuid4().hex)
    claimed = sql("select public.visual_global_claim_scene(c,false,false) "
                  f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    assert source_md5 in claimed and delivered_md5 in claimed
    assert sql(f"select count(*) from public.visual_global_usage where fingerprint in "
               f"({q(source_md5)},{q(delivered_md5)})") == "2"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)}") == "2"
    assert sql("select has_function_privilege('service_role',"
               "'public.visual_global_claim_scene(public.content_calendar,boolean,boolean)',"
               "'EXECUTE')") == "f"
    with pytest.raises(RuntimeError, match="permission denied"):
        sql("set role service_role; select public.visual_global_claim_scene(c,false,false) "
            f"from public.content_calendar c where c.id={q(row_id)}::uuid")


def test_repeat_published_scene_claim_is_an_immutable_idempotent_noop():
    tid, group, row_id, source_md5, delivered_md5 = prepared_calendar_row(
        "repeat-published-" + uuid.uuid4().hex)
    reserved = sql("select public.visual_global_claim_scene(c,false,false) "
                   f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    first = sql("select public.visual_global_claim_scene(c,true,false) "
                f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    second = sql("select public.visual_global_claim_scene(c,true,false) "
                 f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    assert reserved == first == second
    assert sql("select count(*) from public.visual_global_usage "
               f"where fingerprint in ({q(source_md5)},{q(delivered_md5)}) "
               "and state='published'") == "2"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               "and state='published'") == "2"


def test_occupied_scene_rendition_refresh_keeps_writer_contract_and_published_members():
    tid, group, row_id, old_source, old_delivered = prepared_calendar_row(
        "occupied-refresh-" + uuid.uuid4().hex)
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,calendar_row_id,channel,state,published_at) "
        f"values({q(tid)},{q(group)},'2026-10-03',{q(row_id)}::uuid,"
        "'instagram','published',now())")
    sql("select public.visual_global_claim_scene(c,true,false) "
        f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")

    source_url = "https://test/occupied-new-source-" + uuid.uuid4().hex
    delivered_url = "https://test/occupied-new-delivered-" + uuid.uuid4().hex
    source, new_source = read_receipt(tid, source_url, b"occupied new source")
    delivered, new_delivered = read_receipt(tid, delivered_url, b"occupied new delivered")
    render = render_receipt(tid, source, delivered, source_url, delivered_url,
                            new_source, new_delivered)
    result = json.loads(prepare(tid, group, source, delivered, render))
    assert result["usage_claimed"] is False
    assert result["history_refreshed"] is True
    assert result["refreshed_local_groups"] == 1
    assert sql("select count(*) from public.visual_global_usage "
               f"where fingerprint in ({q(old_source)},{q(old_delivered)},"
               f"{q(new_source)},{q(new_delivered)}) and state='published'") == "4"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               "and state='published'") == "4"


def test_multi_fingerprint_collision_rolls_back_every_new_fingerprint():
    _, _, row_id, occupied, _ = prepared_calendar_row("occupied-" + uuid.uuid4().hex)
    # Occupy both prepared bytes first.
    sql("select public.visual_global_claim_scene(c,false,false) "
        f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    tid2, group2 = tenant_and_group("https://test/atomic-" + uuid.uuid4().hex)
    new_fingerprint = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()
    with pytest.raises(RuntimeError, match="another client or date"):
        sql("select public.visual_global_claim_fingerprint_set("
            f"{q(tid2)},{q(group2)},'2026-10-04',null,null,false,false,"
            f"array[{q(new_fingerprint)},{q(occupied)}])")
    assert sql(f"select count(*) from public.visual_global_usage where fingerprint={q(new_fingerprint)}") == "0"


def test_calendar_trigger_collision_rolls_back_row_local_ledger_and_rendition_claim():
    source_url = "https://test/trigger-source-" + uuid.uuid4().hex
    delivered_url = "https://test/trigger-delivered-" + uuid.uuid4().hex
    tid, group = tenant_and_group(source_url)
    source, source_md5 = read_receipt(tid, source_url, b"trigger shared source")
    delivered, delivered_md5 = read_receipt(tid, delivered_url, b"trigger unique rendition")
    render = render_receipt(tid, source, delivered, source_url, delivered_url,
                            source_md5, delivered_md5)
    prepare(tid, group, source, delivered, render)

    foreign, foreign_group = tenant_and_group(
        "https://test/trigger-foreign-" + uuid.uuid4().hex)
    sql("select public.visual_global_claim_fingerprint_set("
        f"{q(foreign)},{q(foreign_group)},'2026-10-12',null,null,false,false,"
        f"array[{q(source_md5)}])")
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")
    row_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="another client or date"):
        sql("set role service_role; insert into public.content_calendar "
            "(id,gym_id,post_date,status,account,image_url,source_media_url) "
            f"values({q(row_id)}::uuid,{q(tid)},'2026-10-13','pending','instagram',"
            f"{q(delivered_url)},{q(source_url)})")
    assert sql(f"select count(*) from public.content_calendar where id={q(row_id)}::uuid") == "0"
    assert sql("select count(*) from public.visual_group_usage_ledger "
               f"where gym_id={q(tid)} and group_key={q(group)}") == "0"
    assert sql("select count(*) from public.visual_group_usage_sibling "
               f"where gym_id={q(tid)} and group_key={q(group)}") == "0"
    assert sql(f"select count(*) from public.visual_global_usage where fingerprint={q(delivered_md5)}") == "0"


def test_opposite_input_order_converges_without_partial_concurrent_claims():
    tid1, group1 = tenant_and_group("https://test/concurrent-a-" + uuid.uuid4().hex)
    tid2, group2 = tenant_and_group("https://test/concurrent-b-" + uuid.uuid4().hex)
    shared = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()
    unique1 = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()
    unique2 = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()

    def claim(tid, group, fingerprints):
        try:
            sql("select public.visual_global_claim_fingerprint_set("
                f"{q(tid)},{q(group)},'2026-10-05',null,null,false,false,"
                f"array[{','.join(q(x) for x in fingerprints)}])")
            return "ok"
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: claim(*args), [
            (tid1, group1, [shared, unique1]),
            (tid2, group2, [unique2, shared]),
        ]))
    assert results.count("ok") == 1
    assert sum("another client or date" in result for result in results) == 1
    winning_tenant = sql(f"select tenant_id from public.visual_global_usage where fingerprint={q(shared)}")
    losing_unique = unique2 if winning_tenant == tid1 else unique1
    assert sql(f"select count(*) from public.visual_global_usage where fingerprint={q(losing_unique)}") == "0"


def test_orphan_released_history_imports_every_byte_as_still_consumed():
    tid, group, row_id, source_md5, delivered_md5 = prepared_calendar_row(
        "released-orphan-" + uuid.uuid4().hex)
    sql(f"delete from public.content_calendar where id={q(row_id)}::uuid")
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,calendar_row_id,channel,state,released_at) "
        f"values({q(tid)},{q(group)},'2026-09-30',{q(row_id)}::uuid,'instagram','released',now())")
    result = sql("select public.visual_global_import_history()")
    assert json.loads(result)["imported_local_groups"] == 1
    assert sql("select count(*) from public.visual_global_usage "
               f"where fingerprint in ({q(source_md5)},{q(delivered_md5)}) "
               f"and tenant_id={q(tid)} and used_date='2026-09-30' and state='reserved'") == "2"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} and state='reserved'") == "2"
    assert sql("select count(*) from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and group_key={q(group)} and issue='ready'") == "2"


def test_active_human_scene_link_claims_new_component_bytes_or_rolls_back_link():
    source_a = "https://test/link-a-source-" + uuid.uuid4().hex
    delivered_a = "https://test/link-a-delivered-" + uuid.uuid4().hex
    tid, group_a = tenant_and_group(source_a)
    group_b = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',"
                  f"{q('https://test/link-b-source-' + uuid.uuid4().hex)})")

    def prepare_group(group, source_url, delivered_url, raw, rendered):
        source, source_md5 = read_receipt(tid, source_url, raw)
        delivered, delivered_md5 = read_receipt(tid, delivered_url, rendered)
        render = render_receipt(tid, source, delivered, source_url, delivered_url,
                                source_md5, delivered_md5)
        prepare(tid, group, source, delivered, render)
        return source_md5, delivered_md5

    a_hashes = prepare_group(group_a, source_a, delivered_a, b"link a raw", b"link a render")
    source_b = sql("select alias_value from public.visual_group_alias "
                   f"where gym_id={q(tid)} and group_key={q(group_b)} and alias_kind='canonical_url'")
    delivered_b = "https://test/link-b-delivered-" + uuid.uuid4().hex
    b_hashes = prepare_group(group_b, source_b, delivered_b, b"link b raw", b"link b render")
    row_id = str(uuid.uuid4())
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,calendar_row_id,channel,state) "
        f"values({q(tid)},{q(group_a)},'2026-10-06',{q(row_id)}::uuid,'instagram','reserved')")
    sql("select public.visual_global_claim_fingerprint_set("
        f"{q(tid)},{q(group_a)},'2026-10-06',{q(row_id)}::uuid,'instagram',false,false,"
        f"array[{q(a_hashes[0])},{q(a_hashes[1])}])")
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")
    result = sql("set role service_role; select public.visual_group_link_scene("
                 f"{q(tid)},{q(group_a)},{q(group_b)},"
                 "'{\"review\":\"same scene\"}'::jsonb,'human reviewer')")
    assert '"linked": true' in result
    all_hashes = a_hashes + b_hashes
    assert sql("select count(*) from public.visual_global_usage where fingerprint in ("
               + ",".join(q(value) for value in all_hashes) + ")") == "4"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group_a)}") == "4"

    # A second occupied scene whose component bytes already belong to another
    # tenant/date must roll the human link itself back, not partially claim it.
    source_c = "https://test/link-c-source-" + uuid.uuid4().hex
    group_c = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',{q(source_c)})")
    delivered_c = "https://test/link-c-delivered-" + uuid.uuid4().hex
    c_hashes = prepare_group(group_c, source_c, delivered_c, b"link c raw", b"link c render")
    foreign, foreign_group = tenant_and_group("https://test/link-foreign-" + uuid.uuid4().hex)
    sql("select public.visual_global_claim_fingerprint_set("
        f"{q(foreign)},{q(foreign_group)},'2026-10-07',null,null,false,false,"
        f"array[{q(c_hashes[0])}])")
    with pytest.raises(RuntimeError, match="another client or date"):
        sql("set role service_role; select public.visual_group_link_scene("
            f"{q(tid)},{q(group_a)},{q(group_c)},"
            "'{\"review\":\"same scene collision\"}'::jsonb,'human reviewer')")
    assert sql("select count(*) from public.visual_group_scene_link "
               f"where gym_id={q(tid)} and group_key_b in ({q(group_c)},{q(group_a)}) "
               f"and group_key_a in ({q(group_c)},{q(group_a)})") == "0"


def test_history_coverage_reports_orphan_global_usage():
    tid, group = tenant_and_group("https://test/orphan-global-" + uuid.uuid4().hex)
    fingerprint = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()
    sql("insert into public.visual_global_usage(fingerprint,tenant_id,used_date,state) "
        f"values({q(fingerprint)},{q(tid)},'2026-10-08','reserved')")
    assert sql("select issue from public.visual_global_history_coverage() "
               f"where fingerprint={q(fingerprint)}") == "global_usage_without_member"


def test_activation_imports_complete_scene_bytes_for_every_tenant_under_barrier():
    histories = []
    for prefix, used_date in (("activate-a-" + uuid.uuid4().hex, "2026-10-09"),
                              ("activate-b-" + uuid.uuid4().hex, "2026-10-10")):
        tid, group, row_id, source_md5, delivered_md5 = prepared_calendar_row(prefix)
        sql(f"delete from public.content_calendar where id={q(row_id)}::uuid")
        sql("insert into public.visual_group_usage_ledger "
            "(gym_id,group_key,reserved_date,calendar_row_id,channel,state,released_at) "
            f"values({q(tid)},{q(group)},{q(used_date)},{q(row_id)}::uuid,"
            "'instagram','released',now())")
        histories.append((tid, group, source_md5, delivered_md5))

    sql((MIGRATIONS / "DRAFT_visual_group_activation_20261002.sql").read_text())
    target = histories[0][0]
    proof = sql("set role service_role; select public.visual_group_activate_guard("
                f"{q(target)},'activation test')")
    assert '"global_history_imported": true' in proof
    assert sql("select count(*) from public.visual_global_usage") == "4"
    for tid, group, source_md5, delivered_md5 in histories:
        assert sql("select count(*) from public.visual_global_usage_member "
                   f"where tenant_id={q(tid)} and group_key={q(group)} "
                   f"and fingerprint in ({q(source_md5)},{q(delivered_md5)})") == "2"
    assert sql("select enforce from public.gym_visual_guard_settings "
               f"where gym_id={q(target)}") == "t"
    assert sql("select count(*) from public.gym_visual_guard_settings where enforce") == "1"


def test_activation_rolls_back_when_another_tenant_has_orphan_global_history():
    target, _ = tenant_and_group("https://test/activation-target-" + uuid.uuid4().hex)
    foreign, _ = tenant_and_group("https://test/activation-foreign-" + uuid.uuid4().hex)
    fingerprint = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()
    sql("insert into public.visual_global_usage(fingerprint,tenant_id,used_date,state) "
        f"values({q(fingerprint)},{q(foreign)},'2026-10-11','reserved')")
    sql((MIGRATIONS / "DRAFT_visual_group_activation_20261002.sql").read_text())
    with pytest.raises(RuntimeError, match="post-import coverage incomplete"):
        sql("set role service_role; select public.visual_group_activate_guard("
            f"{q(target)},'activation rollback test')")
    assert sql("select count(*) from public.visual_group_activation") == "0"
    assert sql("select count(*) from public.gym_visual_guard_settings where enforce") == "0"


def test_legacy_single_identity_cannot_arm_historical_or_runtime_authority():
    url = "https://test/legacy-one-hash-" + uuid.uuid4().hex
    tid, group = tenant_and_group(url)
    fingerprint = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()
    sql("set role service_role; select public.visual_global_register_identity("
        f"{q(tid)},{q(group)},{q(fingerprint)},"
        f"'{{\"source\":\"legacy\",\"verified_bytes\":{json.dumps(fingerprint)}}}'::jsonb,"
        "'legacy repair',null)")
    row_id = str(uuid.uuid4())
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,calendar_row_id,channel,state,released_at) "
        f"values({q(tid)},{q(group)},'2026-10-14',{q(row_id)}::uuid,"
        "'instagram','released',now())")
    sql((MIGRATIONS / "DRAFT_visual_group_activation_20261002.sql").read_text())
    with pytest.raises(RuntimeError, match="incomplete byte evidence"):
        sql("set role service_role; select public.visual_group_activate_guard("
            f"{q(tid)},'legacy one hash refusal')")
    assert sql(f"select count(*) from public.visual_global_usage where fingerprint={q(fingerprint)}") == "0"
    assert sql("select count(*) from public.gym_visual_guard_settings where enforce") == "0"
