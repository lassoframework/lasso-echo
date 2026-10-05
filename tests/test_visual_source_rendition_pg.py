"""Disposable local PostgreSQL acceptance for phase 1 receipt preparation only."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
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
        variant_status text default 'active', image_url text, thumbnail_url text,
        source_media_url text,
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


def test_three_object_video_poster_chain_claims_every_exact_byte():
    source_url = "https://test/video-source-" + uuid.uuid4().hex
    video_url = "https://test/video-rendition-" + uuid.uuid4().hex
    poster_url = "https://test/video-poster-" + uuid.uuid4().hex
    tid, group = tenant_and_group(source_url)

    source, source_md5 = read_receipt(tid, source_url, b"raw video bytes")
    video, video_md5 = read_receipt(tid, video_url, b"selected video bytes")
    video_render = render_receipt(
        tid, source, video, source_url, video_url, source_md5, video_md5)
    prepare(tid, group, source, video, video_render)

    video_source, repeated_video_md5 = read_receipt(
        tid, video_url, b"selected video bytes")
    poster, poster_md5 = read_receipt(tid, poster_url, b"poster frame bytes")
    poster_render = render_receipt(
        tid, video_source, poster, video_url, poster_url,
        repeated_video_md5, poster_md5)
    prepare(tid, group, video_source, poster, poster_render)

    assert repeated_video_md5 == video_md5
    assert sql(f"select count(*) from public.visual_global_object_attestation "
               f"where tenant_id={q(tid)} and group_key={q(group)}") == "3"
    assert sql(f"select count(*) from public.visual_global_object_lineage "
               f"where tenant_id={q(tid)} and group_key={q(group)}") == "2"
    assert sql(f"select count(*) from public.visual_global_scene_object_member "
               f"where tenant_id={q(tid)} and group_key={q(group)}") == "4"

    sql("drop trigger visual_global_block_local_activation "
        "on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) "
        f"values({q(tid)},true)")
    row_id = str(uuid.uuid4())
    sql("set role service_role; insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,thumbnail_url,"
        "source_media_url,byte_hash,visual_group_key) "
        f"values({q(row_id)}::uuid,{q(tid)},'2026-10-14','pending','instagram',"
        f"{q(video_url)},{q(poster_url)},{q(source_url)},"
        f"{q('derived:' + video_md5)},{q(group)})")

    claimed = set(sql("select fingerprint from public.visual_global_usage "
                      f"where tenant_id={q(tid)} order by fingerprint").splitlines())
    assert claimed == {source_md5, video_md5, poster_md5}
    assert sql(f"select public.visual_global_row_bytes_verified(c) "
               f"from public.content_calendar c where id={q(row_id)}::uuid") == "t"


def test_concurrent_identical_poster_edge_converges_to_one_lineage():
    source_url = "https://test/concurrent-poster-source-" + uuid.uuid4().hex
    video_url = "https://test/concurrent-poster-video-" + uuid.uuid4().hex
    poster_url = "https://test/concurrent-poster-frame-" + uuid.uuid4().hex
    tid, group = tenant_and_group(source_url)
    source, source_md5 = read_receipt(tid, source_url, b"concurrent raw video")
    video, video_md5 = read_receipt(tid, video_url, b"concurrent video")
    video_render = render_receipt(
        tid, source, video, source_url, video_url, source_md5, video_md5)
    prepare(tid, group, source, video, video_render)

    video_source, repeated_video_md5 = read_receipt(
        tid, video_url, b"concurrent video")
    poster, poster_md5 = read_receipt(tid, poster_url, b"concurrent poster")
    poster_render = render_receipt(
        tid, video_source, poster, video_url, poster_url,
        repeated_video_md5, poster_md5)
    barrier = threading.Barrier(2)

    def prepare_same_edge(_):
        barrier.wait(timeout=5)
        return prepare(tid, group, video_source, poster, poster_render)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare_same_edge, range(2)))
    assert results[0] == results[1]
    assert sql(f"select count(*) from public.visual_global_object_attestation "
               f"where tenant_id={q(tid)} and group_key={q(group)}") == "3"
    assert sql(f"select count(*) from public.visual_global_object_lineage "
               f"where tenant_id={q(tid)} and group_key={q(group)}") == "2"
    assert sql(f"select count(*) from public.visual_global_object_lineage "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               f"and source_exact_url={q(video_url)} "
               f"and delivered_exact_url={q(poster_url)}") == "1"


def test_poster_must_descend_from_selected_video_object():
    source_url = "https://test/poster-source-" + uuid.uuid4().hex
    video_url = "https://test/poster-video-" + uuid.uuid4().hex
    poster_url = "https://test/poster-wrong-edge-" + uuid.uuid4().hex
    tid, group = tenant_and_group(source_url)
    source_data = b"poster test raw source"
    source, source_md5 = read_receipt(tid, source_url, source_data)
    video, video_md5 = read_receipt(tid, video_url, b"poster test video")
    video_render = render_receipt(
        tid, source, video, source_url, video_url, source_md5, video_md5)
    prepare(tid, group, source, video, video_render)

    # The poster is attested in the same scene, but the immutable render edge
    # starts at the raw source instead of the selected video. Alias agreement
    # alone must not authorize this row.
    repeated_source, repeated_source_md5 = read_receipt(
        tid, source_url, source_data)
    poster, poster_md5 = read_receipt(tid, poster_url, b"wrong edge poster")
    wrong_render = render_receipt(
        tid, repeated_source, poster, source_url, poster_url,
        repeated_source_md5, poster_md5)
    prepare(tid, group, repeated_source, poster, wrong_render)

    sql("drop trigger visual_global_block_local_activation "
        "on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) "
        f"values({q(tid)},true)")
    row_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="source and rendition bytes are not fully attested"):
        sql("set role service_role; insert into public.content_calendar "
            "(id,gym_id,post_date,status,account,image_url,thumbnail_url,"
            "source_media_url,byte_hash,visual_group_key) "
            f"values({q(row_id)}::uuid,{q(tid)},'2026-10-15','pending','instagram',"
            f"{q(video_url)},{q(poster_url)},{q(source_url)},"
            f"{q('derived:' + video_md5)},{q(group)})")
    assert sql(f"select count(*) from public.content_calendar "
               f"where id={q(row_id)}::uuid") == "0"
    assert sql("select count(*) from public.visual_global_usage") == "0"


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


def test_null_key_published_history_attributes_or_holds_scene_link():
    # Component B owns a historically published calendar row that never
    # recorded a visual_group_key, even though every exact alias on the row
    # resolves into component B with fully attested source/rendition bytes.
    source_b = "https://test/nullhist-b-source-" + uuid.uuid4().hex
    delivered_b = "https://test/nullhist-b-delivered-" + uuid.uuid4().hex
    tid, group_b = tenant_and_group(delivered_b)
    source, b_source_md5 = read_receipt(tid, source_b, b"nullhist b raw")
    delivered, b_delivered_md5 = read_receipt(tid, delivered_b, b"nullhist b render")
    render = render_receipt(tid, source, delivered, source_b, delivered_b,
                            b_source_md5, b_delivered_md5)
    prepare(tid, group_b, source, delivered, render)
    hist_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "published_at) "
        f"values({q(hist_id)}::uuid,{q(tid)},'2026-09-15','published','instagram',"
        f"{q(delivered_b)},{q(source_b)},{q('derived:' + b_delivered_md5)},now())")

    # A foreign tenant's unresolved null-key published row must never hold or
    # be attributed by this tenant's scene expansion.
    foreign, _ = tenant_and_group("https://test/nullhist-foreign-" + uuid.uuid4().hex)
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,published_at) "
        f"values({q(str(uuid.uuid4()))}::uuid,{q(foreign)},'2026-09-14','published',"
        f"'instagram',{q('https://test/nullhist-foreign-img-' + uuid.uuid4().hex)},now())")

    # Component A is prepared but unoccupied; its own bytes must not be
    # attributed to the historical row it never appeared in.
    source_a = "https://test/nullhist-a-source-" + uuid.uuid4().hex
    delivered_a = "https://test/nullhist-a-delivered-" + uuid.uuid4().hex
    group_a = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',"
                  f"{q(source_a)})")
    src_a, a_source_md5 = read_receipt(tid, source_a, b"nullhist a raw")
    del_a, a_delivered_md5 = read_receipt(tid, delivered_a, b"nullhist a render")
    render_a = render_receipt(tid, src_a, del_a, source_a, delivered_a,
                              a_source_md5, a_delivered_md5)
    prepare(tid, group_a, src_a, del_a, render_a)
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")

    # The human scene link refreshes history: the null-key published row is
    # attributed to component B with its original publication date, claiming
    # only the bytes this row used without touching the row.
    result = sql("set role service_role; select public.visual_group_link_scene("
                 f"{q(tid)},{q(group_a)},{q(group_b)},"
                 "'{\"review\":\"null-key history\"}'::jsonb,'human reviewer')")
    assert '"linked": true' in result
    assert sql("select count(*) from public.visual_global_usage where fingerprint in ("
               f"{q(b_source_md5)},{q(b_delivered_md5)}) and tenant_id={q(tid)} "
               "and used_date='2026-09-15' and state='published'") == "2"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group_b)} "
               f"and calendar_row_id={q(hist_id)}::uuid and state='published'") == "2"
    # Component A's own bytes were not consumed by the historical row.
    assert sql("select count(*) from public.visual_global_usage where fingerprint in ("
               f"{q(a_source_md5)},{q(a_delivered_md5)})") == "0"
    # The historical published row stays exactly as published: no key, date,
    # status or media rewrite was invented for it.
    assert sql("select visual_group_key is null and status='published' "
               f"and post_date='2026-09-15' and image_url={q(delivered_b)} "
               f"from public.content_calendar where id={q(hist_id)}::uuid") == "t"
    # The foreign tenant's unresolved row was not attributed anywhere.
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(foreign)}") == "0"
    # The attributed members have no local ledger row; the exact null-key
    # published anchor exempts them in history coverage without blanketing.
    assert sql("select count(*) from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and issue<>'ready'") == "0"
    assert sql("select count(*) from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and group_key={q(group_b)} "
               "and issue='ready'") == "2"

    # A later cross-date claim of the same bytes is denied by the attributed
    # history instead of repeating the old image.
    repeat_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "visual_group_key) "
        f"values({q(repeat_id)}::uuid,{q(tid)},'2026-10-09','held','instagram',"
        f"{q(delivered_b)},{q(source_b)},{q('derived:' + b_delivered_md5)},{q(group_b)})")
    with pytest.raises(RuntimeError, match="visual byte fingerprint already used"):
        sql("select public.visual_global_claim_scene(c,false,false) "
            f"from public.content_calendar c where c.id={q(repeat_id)}::uuid")
    assert sql("select count(*) from public.visual_global_usage "
               f"where fingerprint={q(b_delivered_md5)} and used_date='2026-10-09'") == "0"


def test_null_key_published_row_with_unattested_bytes_holds_scene_link():
    # Exact aliases resolve the historical null-key published row into
    # component D, but no source/rendition attestation backs those bytes. The
    # refresh must fail closed and roll the human scene link back rather than
    # silently skipping consumed history.
    source_d = "https://test/nullhold-d-source-" + uuid.uuid4().hex
    delivered_d = "https://test/nullhold-d-delivered-" + uuid.uuid4().hex
    tid, group_d = tenant_and_group(delivered_d)
    d_md5 = "md5:" + hashlib.md5(b"nullhold d render").hexdigest()
    sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',"
        f"{q(source_d)},{q(group_d)})")
    sql(f"select public.visual_group_register_alias({q(tid)},'byte_hash',"
        f"{q('derived:' + d_md5)},{q(group_d)})")
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "published_at) "
        f"values({q(str(uuid.uuid4()))}::uuid,{q(tid)},'2026-09-12','published',"
        f"'instagram',{q(delivered_d)},{q(source_d)},{q('derived:' + d_md5)},now())")

    source_c = "https://test/nullhold-c-source-" + uuid.uuid4().hex
    delivered_c = "https://test/nullhold-c-delivered-" + uuid.uuid4().hex
    group_c = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',"
                  f"{q(source_c)})")
    src_c, c_source_md5 = read_receipt(tid, source_c, b"nullhold c raw")
    del_c, c_delivered_md5 = read_receipt(tid, delivered_c, b"nullhold c render")
    render_c = render_receipt(tid, src_c, del_c, source_c, delivered_c,
                              c_source_md5, c_delivered_md5)
    prepare(tid, group_c, src_c, del_c, render_c)
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,channel,state) "
        f"values({q(tid)},{q(group_c)},'2026-10-06','instagram','reserved')")
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")

    with pytest.raises(RuntimeError,
                       match="null-key scene history has incomplete exact-byte evidence"):
        sql("set role service_role; select public.visual_group_link_scene("
            f"{q(tid)},{q(group_c)},{q(group_d)},"
            "'{\"review\":\"unattested null-key history\"}'::jsonb,'human reviewer')")
    assert sql("select count(*) from public.visual_group_scene_link "
               f"where gym_id={q(tid)} and group_key_b in ({q(group_c)},{q(group_d)}) "
               f"and group_key_a in ({q(group_c)},{q(group_d)})") == "0"
    assert sql("select count(*) from public.visual_global_usage "
               f"where tenant_id={q(tid)}") == "0"


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


def test_null_key_same_date_distinct_renditions_attribute_and_refresh_idempotent():
    # Two historically published null-key rows shared one attested source but
    # delivered distinct renditions on the same date. Each must attribute its
    # own verified byte subset; neither may reject the other's already
    # attributed bytes, and a repeated refresh stays idempotent even after
    # keyed full-set growth adds a new reserved member to the same scene.
    source_url = "https://test/nkpair-source-" + uuid.uuid4().hex
    delivered_1 = "https://test/nkpair-delivered-a-" + uuid.uuid4().hex
    delivered_2 = "https://test/nkpair-delivered-b-" + uuid.uuid4().hex
    tid, group = tenant_and_group(delivered_1)
    source, source_md5 = read_receipt(tid, source_url, b"nkpair raw")
    del1, delivered_1_md5 = read_receipt(tid, delivered_1, b"nkpair render a")
    render_1 = render_receipt(tid, source, del1, source_url, delivered_1,
                              source_md5, delivered_1_md5)
    prepare(tid, group, source, del1, render_1)
    del2, delivered_2_md5 = read_receipt(tid, delivered_2, b"nkpair render b")
    render_2 = render_receipt(tid, source, del2, source_url, delivered_2,
                              source_md5, delivered_2_md5)
    prepare(tid, group, source, del2, render_2)
    row_1, row_2 = str(uuid.uuid4()), str(uuid.uuid4())
    for row_id, delivered_url, delivered_md5 in (
            (row_1, delivered_1, delivered_1_md5),
            (row_2, delivered_2, delivered_2_md5)):
        sql("insert into public.content_calendar "
            "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
            "published_at) "
            f"values({q(row_id)}::uuid,{q(tid)},'2026-09-15','published','instagram',"
            f"{q(delivered_url)},{q(source_url)},{q('derived:' + delivered_md5)},now())")
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")

    assert sql("select public.visual_global_refresh_scene_history("
               f"{q(tid)},{q(group)})") == "2"
    assert sql("select count(*) from public.visual_global_usage where fingerprint in ("
               f"{q(source_md5)},{q(delivered_1_md5)},{q(delivered_2_md5)}) "
               f"and tenant_id={q(tid)} and used_date='2026-09-15' "
               "and state='published'") == "3"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               "and state='published'") == "3"
    # Both historical rows stay exactly as published: no key, date, status or
    # media rewrite was invented for either of them.
    assert sql("select count(*) from public.content_calendar "
               f"where id in ({q(row_1)}::uuid,{q(row_2)}::uuid) "
               "and visual_group_key is null and status='published' "
               "and post_date='2026-09-15'") == "2"

    # Keyed full-set growth on the same date: a new staged rendition joins
    # the occupied scene through the keyed claim path, which keeps the
    # full-set invariant and reserves only the new byte.
    delivered_3 = "https://test/nkpair-delivered-c-" + uuid.uuid4().hex
    del3, delivered_3_md5 = read_receipt(tid, delivered_3, b"nkpair render c")
    render_3 = render_receipt(tid, source, del3, source_url, delivered_3,
                              source_md5, delivered_3_md5)
    prepare(tid, group, source, del3, render_3)
    keyed_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "visual_group_key) "
        f"values({q(keyed_id)}::uuid,{q(tid)},'2026-09-15','held','instagram',"
        f"{q(delivered_3)},{q(source_url)},{q('derived:' + delivered_3_md5)},{q(group)})")
    sql("select public.visual_global_claim_scene(c,false,false) "
        f"from public.content_calendar c where c.id={q(keyed_id)}::uuid")
    assert sql("select state from public.visual_global_usage "
               f"where fingerprint={q(delivered_3_md5)}") == "reserved"

    # Repeated refresh after member growth must neither fail the keyed
    # full-set invariant nor rewrite any historical or staged state.
    assert sql("select public.visual_global_refresh_scene_history("
               f"{q(tid)},{q(group)})") == "2"
    assert sql("select count(*) from public.visual_global_usage where fingerprint in ("
               f"{q(source_md5)},{q(delivered_1_md5)},{q(delivered_2_md5)}) "
               "and state='published'") == "3"
    assert sql("select state from public.visual_global_usage "
               f"where fingerprint={q(delivered_3_md5)}") == "reserved"
    assert sql("select state from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               f"and fingerprint={q(delivered_3_md5)}") == "reserved"
    # No ledger rows exist in this fixture: the historically anchored
    # published members are exempted by their exact null-key anchor proof,
    # while the unanchored keyed member is never blanket-exempted.
    assert sql("select count(*) from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and fingerprint in "
               f"({q(source_md5)},{q(delivered_1_md5)},{q(delivered_2_md5)}) "
               "and issue='ready'") == "3"
    assert sql("select issue from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and fingerprint={q(delivered_3_md5)}"
               ) == "global_member_without_local_ledger"


def test_historical_attribution_never_promotes_keyed_reserved_and_holds_cross_date():
    # A keyed staged reservation owns its scene bytes on a date. A null-key
    # published row on the same date that verifiably consumed one of those
    # bytes plus its own distinct rendition must attribute only its own
    # subset: the keyed reserved usage/member is never promoted to published
    # by historical import. A second null-key row on a different date that
    # reuses the attributed rendition still collides and rolls back fully.
    source_url = "https://test/nkhold-source-" + uuid.uuid4().hex
    delivered_k = "https://test/nkhold-delivered-keyed-" + uuid.uuid4().hex
    delivered_h = "https://test/nkhold-delivered-hist-" + uuid.uuid4().hex
    tid, group = tenant_and_group(delivered_k)
    source, source_md5 = read_receipt(tid, source_url, b"nkhold raw")
    del_k, delivered_k_md5 = read_receipt(tid, delivered_k, b"nkhold keyed render")
    render_k = render_receipt(tid, source, del_k, source_url, delivered_k,
                              source_md5, delivered_k_md5)
    prepare(tid, group, source, del_k, render_k)
    keyed_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "visual_group_key) "
        f"values({q(keyed_id)}::uuid,{q(tid)},'2026-09-15','held','instagram',"
        f"{q(delivered_k)},{q(source_url)},{q('derived:' + delivered_k_md5)},{q(group)})")
    sql("select public.visual_global_claim_scene(c,false,false) "
        f"from public.content_calendar c where c.id={q(keyed_id)}::uuid")
    del_h, delivered_h_md5 = read_receipt(tid, delivered_h, b"nkhold hist render")
    render_h = render_receipt(tid, source, del_h, source_url, delivered_h,
                              source_md5, delivered_h_md5)
    prepare(tid, group, source, del_h, render_h)
    hist_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "published_at) "
        f"values({q(hist_id)}::uuid,{q(tid)},'2026-09-15','published','instagram',"
        f"{q(delivered_h)},{q(source_url)},{q('derived:' + delivered_h_md5)},now())")
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")

    assert sql("select public.visual_global_refresh_scene_history("
               f"{q(tid)},{q(group)})") == "1"
    # The historical row's own rendition is published history...
    assert sql("select state from public.visual_global_usage "
               f"where fingerprint={q(delivered_h_md5)}") == "published"
    assert sql("select state from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               f"and fingerprint={q(delivered_h_md5)} "
               f"and calendar_row_id={q(hist_id)}::uuid") == "published"
    # ...but the keyed staged reservation of the shared source and its own
    # rendition is left exactly reserved: history never promotes pending bytes.
    assert sql("select count(*) from public.visual_global_usage where fingerprint in ("
               f"{q(source_md5)},{q(delivered_k_md5)}) and state='reserved' "
               "and used_date='2026-09-15'") == "2"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               f"and fingerprint in ({q(source_md5)},{q(delivered_k_md5)}) "
               "and state='reserved'") == "2"
    # The historical member is anchored by its exact null-key published row;
    # the keyed reserved members have no ledger in this fixture and stay held.
    assert sql("select issue from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and fingerprint={q(delivered_h_md5)}"
               ) == "ready"
    assert sql("select count(*) from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and fingerprint in "
               f"({q(source_md5)},{q(delivered_k_md5)}) "
               "and issue='global_member_without_local_ledger'") == "2"

    # A different-date null-key published row reusing the attributed
    # rendition is rejected by the active calendar trigger before it lands.
    hist_2 = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="already used by another client or date"):
        sql("insert into public.content_calendar "
            "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
            "published_at) "
            f"values({q(hist_2)}::uuid,{q(tid)},'2026-09-16','published','instagram',"
            f"{q(delivered_h)},{q(source_url)},{q('derived:' + delivered_h_md5)},now())")
    assert sql("select count(*) from public.visual_global_usage "
               f"where tenant_id={q(tid)}") == "3"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)}") == "3"
    assert sql("select count(*) from public.visual_global_usage "
               "where used_date='2026-09-16'") == "0"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where calendar_row_id={q(hist_2)}::uuid") == "0"
    assert sql("select count(*) from public.content_calendar "
               f"where id={q(hist_2)}::uuid") == "0"


def impnk_scene(prefix, tid, group, source_url, renditions):
    source, source_md5 = read_receipt(tid, source_url, (prefix + " raw").encode())
    hashes = {}
    for name, url in renditions.items():
        receipt, md5 = read_receipt(tid, url, (prefix + " " + name).encode())
        render = render_receipt(tid, source, receipt, source_url, url, source_md5, md5)
        prepare(tid, group, source, receipt, render)
        hashes[name] = md5
    return source_md5, hashes


def test_import_history_claims_null_key_subset_after_keyed_ledgers():
    # A keyed reserved ledger owns the complete scene; a null-key published
    # row on the same date verifiably consumed the shared source plus its own
    # distinct rendition. Import claims the keyed complete-scene set first,
    # then the historical subset, and history never promotes reserved bytes.
    source_url = "https://test/impnk-source-" + uuid.uuid4().hex
    delivered_k = "https://test/impnk-delivered-keyed-" + uuid.uuid4().hex
    delivered_h = "https://test/impnk-delivered-hist-" + uuid.uuid4().hex
    delivered_u = "https://test/impnk-delivered-untouched-" + uuid.uuid4().hex
    tid, group = tenant_and_group(delivered_k)
    source_md5, hashes = impnk_scene("impnk", tid, group, source_url, {
        "keyed": delivered_k, "hist": delivered_h, "untouched": delivered_u})
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,channel,state) "
        f"values({q(tid)},{q(group)},'2026-09-15','instagram','reserved')")
    hist_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "published_at) "
        f"values({q(hist_id)}::uuid,{q(tid)},'2026-09-15','published','instagram',"
        f"{q(delivered_h)},{q(source_url)},{q('derived:' + hashes['hist'])},now())")
    # Before import the verified null-key published row reports not_imported
    # for exactly its consumed subset and never for the untouched rendition.
    assert sql("select count(*) from public.visual_global_coverage() "
               f"where calendar_row_id={q(hist_id)}::uuid "
               "and issue='not_imported'") == "2"
    assert sql("select count(*) from public.visual_global_coverage() "
               f"where calendar_row_id={q(hist_id)}::uuid "
               f"and fingerprint={q(hashes['untouched'])}") == "0"
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_local_groups"] == 1
    assert result["imported_null_key_rows"] == 1
    # Keyed full set imported reserved on its date; historical attribution
    # never promoted any owner or member to published.
    assert sql("select count(*) from public.visual_global_usage "
               f"where tenant_id={q(tid)} and used_date='2026-09-15' "
               "and state='reserved'") == "4"
    assert sql("select count(*) from public.visual_global_usage "
               f"where tenant_id={q(tid)} and state='published'") == "0"
    assert sql("select count(*) from public.visual_global_coverage() "
               "where issue<>'ready'") == "0"
    assert sql("select count(*) from public.visual_global_history_coverage() "
               "where issue<>'ready'") == "0"
    # The historical row was never mutated to carry a key, date or media swap.
    assert sql("select visual_group_key is null and status='published' "
               f"and post_date='2026-09-15' and image_url={q(delivered_h)} "
               f"from public.content_calendar where id={q(hist_id)}::uuid") == "t"


def test_import_history_holds_unresolved_null_key_history_and_rolls_back():
    source_url = "https://test/imphold-source-" + uuid.uuid4().hex
    delivered = "https://test/imphold-delivered-" + uuid.uuid4().hex
    tid, group = tenant_and_group(delivered)
    source_md5, hashes = impnk_scene("imphold", tid, group, source_url,
                                     {"keyed": delivered})
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,channel,state) "
        f"values({q(tid)},{q(group)},'2026-09-15','instagram','reserved')")
    unknown_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,published_at) "
        f"values({q(unknown_id)}::uuid,{q(tid)},'2026-09-14','published','instagram',"
        f"{q('https://test/imphold-unknown-img-' + uuid.uuid4().hex)},"
        f"{q('https://test/imphold-unknown-src-' + uuid.uuid4().hex)},now())")
    assert sql("select issue from public.visual_global_coverage() "
               f"where calendar_row_id={q(unknown_id)}::uuid") == "unresolved_group"
    # The unknown row blocks the whole import; the keyed ledger import rolls
    # back with it, leaving no global writes behind.
    with pytest.raises(RuntimeError, match="calendar coverage incomplete"):
        sql("select public.visual_global_import_history()")
    assert sql("select count(*) from public.visual_global_usage") == "0"
    assert sql("select count(*) from public.visual_global_usage_member") == "0"


def test_import_history_rolls_back_everything_on_cross_date_historical_conflict():
    source_url = "https://test/impxdate-source-" + uuid.uuid4().hex
    delivered_k = "https://test/impxdate-delivered-keyed-" + uuid.uuid4().hex
    delivered_h = "https://test/impxdate-delivered-hist-" + uuid.uuid4().hex
    tid, group = tenant_and_group(delivered_k)
    source_md5, hashes = impnk_scene("impxdate", tid, group, source_url, {
        "keyed": delivered_k, "hist": delivered_h})
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,channel,state) "
        f"values({q(tid)},{q(group)},'2026-09-15','instagram','reserved')")
    hist_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "published_at) "
        f"values({q(hist_id)}::uuid,{q(tid)},'2026-09-16','published','instagram',"
        f"{q(delivered_h)},{q(source_url)},{q('derived:' + hashes['hist'])},now())")
    # The historical row reuses the keyed source bytes on a different date:
    # the claim conflict aborts the statement, rolling the keyed import back.
    with pytest.raises(RuntimeError, match="already used by another client or date"):
        sql("select public.visual_global_import_history()")
    assert sql("select count(*) from public.visual_global_usage") == "0"
    assert sql("select count(*) from public.visual_global_usage_member") == "0"
    assert sql("select count(*) from public.visual_global_coverage() "
               f"where calendar_row_id={q(hist_id)}::uuid "
               "and issue='not_imported'") == "2"


def test_history_coverage_anchored_published_mix_ready_and_forged_mix_held():
    # Historical attribution lands first, then a keyed reserved ledger imports
    # over the same bytes: the published members stay published under a
    # reserved ledger and are accepted only through the exact anchor proof.
    source_url = "https://test/impmix-source-" + uuid.uuid4().hex
    delivered_h = "https://test/impmix-delivered-hist-" + uuid.uuid4().hex
    tid, group = tenant_and_group(delivered_h)
    source_md5, hashes = impnk_scene("impmix", tid, group, source_url,
                                     {"hist": delivered_h})
    hist_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "published_at) "
        f"values({q(hist_id)}::uuid,{q(tid)},'2026-09-15','published','instagram',"
        f"{q(delivered_h)},{q(source_url)},{q('derived:' + hashes['hist'])},now())")
    sql("select public.visual_global_claim_historical_row("
        f"{q(tid)},{q(group)},'2026-09-15',{q(hist_id)}::uuid,'instagram',"
        f"array[{q(source_md5)},{q(hashes['hist'])}])")
    sql("insert into public.visual_group_usage_ledger "
        "(gym_id,group_key,reserved_date,channel,state) "
        f"values({q(tid)},{q(group)},'2026-09-15','instagram','reserved')")
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_local_groups"] == 1
    assert result["imported_null_key_rows"] == 1
    # The keyed reserved claim never rewrote the anchored published members.
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               f"and state='published' and calendar_row_id={q(hist_id)}::uuid") == "2"
    assert sql("select count(*) from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} and issue<>'ready'") == "0"

    # A forged published member under the same reserved ledger without the
    # exact anchor proof stays held and blocks any further import. Published
    # members are immutable, so the forgery adds a new scene byte whose member
    # points at a nonexistent row instead of a real null-key published anchor.
    delivered_f = "https://test/impmix-delivered-forge-" + uuid.uuid4().hex
    receipt_f, forge_md5 = read_receipt(tid, delivered_f, b"impmix forged render")
    source_row = sql("select receipt_id from public.visual_global_object_read_receipt "
                     f"where tenant_id={q(tid)} and exact_url={q(source_url)}")
    render_f = render_receipt(tid, source_row, receipt_f, source_url, delivered_f,
                              source_md5, forge_md5)
    prepare(tid, group, source_row, receipt_f, render_f)
    sql("insert into public.visual_global_usage(fingerprint,tenant_id,used_date,state) "
        f"values({q(forge_md5)},{q(tid)},'2026-09-15','reserved')")
    sql("insert into public.visual_global_usage_member"
        "(tenant_id,group_key,fingerprint,calendar_row_id,channel,used_date,state) "
        f"values({q(tid)},{q(group)},{q(forge_md5)},{q(str(uuid.uuid4()))}::uuid,"
        "'instagram','2026-09-15','published')")
    assert sql("select issue from public.visual_global_history_coverage() "
               f"where tenant_id={q(tid)} "
               f"and fingerprint={q(forge_md5)}") == "global_member_state_mismatch"
    with pytest.raises(RuntimeError, match="post-import coverage incomplete"):
        sql("select public.visual_global_import_history()")


def test_import_history_anchors_orphan_members_and_keeps_orphan_usage_held():
    # A null-key published row imports with no local ledger at all: its
    # members are orphans covered only by the exact anchor proof. An orphan
    # global owner without any member remains an activation blocker.
    source_url = "https://test/imporph-source-" + uuid.uuid4().hex
    delivered_h = "https://test/imporph-delivered-hist-" + uuid.uuid4().hex
    tid, group = tenant_and_group(delivered_h)
    source_md5, hashes = impnk_scene("imporph", tid, group, source_url,
                                     {"hist": delivered_h})
    hist_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,image_url,source_media_url,byte_hash,"
        "published_at) "
        f"values({q(hist_id)}::uuid,{q(tid)},'2026-09-15','published','instagram',"
        f"{q(delivered_h)},{q(source_url)},{q('derived:' + hashes['hist'])},now())")
    assert sql("select count(*) from public.visual_global_coverage() "
               f"where calendar_row_id={q(hist_id)}::uuid "
               "and issue='not_imported'") == "2"
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_local_groups"] == 0
    assert result["imported_null_key_rows"] == 1
    assert sql("select count(*) from public.visual_global_usage "
               f"where tenant_id={q(tid)} and used_date='2026-09-15' "
               "and state='published'") == "2"
    assert sql("select count(*) from public.visual_global_usage_member "
               f"where tenant_id={q(tid)} and group_key={q(group)} "
               f"and state='published' and channel='instagram' "
               f"and calendar_row_id={q(hist_id)}::uuid") == "2"
    assert sql("select count(*) from public.visual_global_coverage() "
               "where issue<>'ready'") == "0"
    assert sql("select count(*) from public.visual_global_history_coverage() "
               "where issue<>'ready'") == "0"
    # Orphan usage without any member stays held and blocks re-import.
    fingerprint = "md5:" + hashlib.md5(uuid.uuid4().bytes).hexdigest()
    sql("insert into public.visual_global_usage(fingerprint,tenant_id,used_date,state) "
        f"values({q(fingerprint)},{q(tid)},'2026-10-08','published')")
    assert sql("select issue from public.visual_global_history_coverage() "
               f"where fingerprint={q(fingerprint)}") == "global_usage_without_member"
    with pytest.raises(RuntimeError, match="post-import coverage incomplete"):
        sql("select public.visual_global_import_history()")
