"""Receipt-backed feed chains in the named disposable Unix-socket database."""
import os
import uuid

import pytest

from test_visual_source_rendition_pg import (
    database, prepare, q, read_receipt, render_receipt, sql, tenant_and_group,
)

pytestmark = pytest.mark.skipif(not os.environ.get("VISUAL_GROUP_TEST_DSN"),
                                reason="isolated local VISUAL_GROUP_TEST_DSN unset")


def chain(depth=2):
    prefix = "https://test/chain-" + uuid.uuid4().hex
    urls = [prefix + f"/{i}.jpg?v=1" for i in range(depth + 1)]
    tid, group = tenant_and_group(urls[0])
    reads = [read_receipt(tid, url, f"{prefix} bytes {i}".encode())
             for i, url in enumerate(urls)]
    for index in range(depth):
        edge(tid, group, urls[index], reads[index], urls[index + 1], reads[index + 1])
    return tid, group, urls, reads


def edge(tid, group, source_url, source, delivered_url, delivered):
    render = render_receipt(tid, source[0], delivered[0], source_url, delivered_url,
                            source[1], delivered[1])
    prepare(tid, group, source[0], delivered[0], render)
    return render


def verified(tid, group, source_url, source_hash, delivered_url, delivered_hash):
    return sql("select public.visual_global_lineage_verified("
               + ",".join(q(value) for value in (tid, group, source_url, source_hash,
                                                 delivered_url, delivered_hash)) + ")")


def calendar(tid, group, source_url, delivered_url, digest, *, status="held", asset_id=None):
    row_id = str(uuid.uuid4())
    sql("insert into public.content_calendar "
        "(id,gym_id,post_date,status,account,format,image_url,source_media_url,byte_hash,visual_group_key,"
        "source_media_asset_id,drive_file_id) "
        f"values({q(row_id)}::uuid,{q(tid)},'2026-10-03',{q(status)},'instagram','feed',"
        f"{q(delivered_url)},{q(source_url)},{q('derived:' + digest)},{q(group)},"
        f"{q(asset_id) if asset_id else 'null'},{q(asset_id) if asset_id else 'null'})")
    return row_id


def test_distinct_abc_chain_verifies_and_claims_all_bytes_without_fake_ac_receipt():
    tid, group, urls, reads = chain()
    assert len({item[1] for item in reads}) == 3
    assert verified(tid, group, urls[0], reads[0][1], urls[2], reads[2][1]) == "t"
    assert verified(tid, group, urls[0], "md5:" + "f" * 32, urls[2], reads[2][1]) == "f"
    assert verified(tid, group, urls[0], reads[0][1], urls[2], "md5:" + "f" * 32) == "f"
    assert sql("select count(*) from public.visual_global_object_lineage "
               f"where source_exact_url={q(urls[0])} and delivered_exact_url={q(urls[2])}") == "0"
    row_id = calendar(tid, group, urls[0], urls[2], reads[2][1])
    assert sql("select public.visual_global_row_bytes_verified(c) from public.content_calendar c "
               f"where c.id={q(row_id)}::uuid") == "t"
    claimed = sql("select public.visual_global_claim_scene(c,false,false) "
                  f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    assert all(digest in claimed for _, digest in reads)
    assert sql("select count(*) from public.visual_global_usage "
               f"where tenant_id={q(tid)} and used_date='2026-10-03'") == "3"
    assert sql("select has_function_privilege('service_role',"
               "'public.visual_global_lineage_verified(text,text,text,text,text,text)','EXECUTE')") == "f"
    for _, digest in reads:
        with pytest.raises(RuntimeError, match="another client or date"):
            sql("select public.visual_global_claim_fingerprint_set("
                f"{q(tid)},{q(group)},'2026-10-04',null,null,false,false,array[{q(digest)}])")
    foreign_tid, foreign_group = tenant_and_group("https://test/claim-foreign-" + uuid.uuid4().hex)
    for _, digest in reads:
        with pytest.raises(RuntimeError, match="another client or date"):
            sql("select public.visual_global_claim_fingerprint_set("
                f"{q(foreign_tid)},{q(foreign_group)},'2026-10-03',null,null,false,false,array[{q(digest)}])")


def test_guarded_feed_rendition_patch_keeps_raw_a_and_reserves_new_c_on_original_day():
    tid, group, urls, reads = chain(1)
    asset = "raw-asset-" + uuid.uuid4().hex
    sql("insert into public.media_asset(id,gym_id,content_hash) "
        f"values({q(asset)},{q(tid)},{q(reads[0][1].split(':', 1)[1])})")
    for kind in ("source_asset", "drive_id"):
        sql(f"select public.visual_group_register_alias({q(tid)},{q(kind)},{q(asset)},{q(group)})")
    sql("drop trigger visual_global_block_local_activation on public.gym_visual_guard_settings; "
        f"insert into public.gym_visual_guard_settings(gym_id,enforce) values({q(tid)},true)")
    row_id = calendar(tid, group, urls[0], urls[1], reads[1][1], status="approved", asset_id=asset)
    new_url = urls[1] + "&fit=1"
    new_read = read_receipt(tid, new_url, b"new fitted bytes")
    edge(tid, group, urls[1], reads[1], new_url, new_read)
    sql("set role service_role; update public.content_calendar "
        f"set image_url={q(new_url)},byte_hash={q('derived:' + new_read[1])} "
        f"where id={q(row_id)}::uuid and status='approved' and source_media_url={q(urls[0])}")
    assert sql(f"select source_media_url from public.content_calendar where id={q(row_id)}::uuid") == urls[0]
    assert sql("select source_media_asset_id||','||drive_file_id from public.content_calendar "
               f"where id={q(row_id)}::uuid") == asset + "," + asset
    assert sql("select count(*) from public.visual_global_usage "
               f"where tenant_id={q(tid)} and used_date='2026-10-03'") == "3"
    sql("set role service_role; insert into public.content_calendar "
        "(gym_id,post_date,status,account,format,image_url,source_media_url,byte_hash) "
        f"values({q(tid)},'2026-10-03','approved','facebook','feed',{q(new_url)},"
        f"{q(urls[0])},{q('derived:' + new_read[1])})")
    assert sql(f"select count(*) from public.content_calendar where gym_id={q(tid)}") == "2"
    with pytest.raises(RuntimeError, match="date"):
        sql("set role service_role; insert into public.content_calendar "
            "(gym_id,post_date,status,account,format,image_url,source_media_url) "
            f"values({q(tid)},'2026-10-04','approved','instagram','feed',{q(new_url)},{q(urls[0])})")
    assert sql(f"select count(*) from public.content_calendar where gym_id={q(tid)}") == "2"


def test_equal_digest_different_intermediate_url_is_not_continuous_lineage():
    tid, group, urls, reads = chain(1)
    other_b_url, c_url = urls[1] + "&different=1", urls[1] + "&final=1"
    # Equal intermediate fingerprints do not prove the actual render input URL.
    other_b = read_receipt(tid, other_b_url, (urls[0].rsplit("/", 1)[0] + " bytes 1").encode())
    assert other_b[1] == reads[1][1]
    c = read_receipt(tid, c_url, b"disconnected output")
    edge(tid, group, other_b_url, other_b, c_url, c)
    assert verified(tid, group, urls[0], reads[0][1], c_url, c[1]) == "f"
    row_id = calendar(tid, group, urls[0], c_url, c[1])
    with pytest.raises(RuntimeError, match="not fully attested"):
        sql("select public.visual_global_claim_scene(c,false,false) "
            f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    assert sql("select count(*) from public.visual_global_usage") == "0"


def test_foreign_scene_target_cannot_complete_the_chain():
    tid, group, urls, reads = chain(1)
    foreign_url = "https://test/foreign-" + uuid.uuid4().hex
    other_group = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',{q(foreign_url)})")
    foreign = read_receipt(tid, foreign_url, b"foreign scene bytes")
    prepare(tid, other_group, foreign[0], foreign[0], None)
    assert verified(tid, group, urls[0], reads[0][1], foreign_url, foreign[1]) == "f"
    row_id = calendar(tid, group, urls[0], foreign_url, foreign[1])
    assert sql("select public.visual_global_row_bytes_verified(c) from public.content_calendar c "
               f"where c.id={q(row_id)}::uuid") == "f"


def test_foreign_edge_cannot_be_filtered_out_of_an_otherwise_valid_chain():
    tid, group, urls, reads = chain()
    foreign_url = "https://test/foreign-edge-" + uuid.uuid4().hex
    foreign_tid, foreign_group = tenant_and_group(foreign_url)
    foreign_source = read_receipt(foreign_tid, urls[1], b"mis-scoped input receipt")
    foreign_output = read_receipt(foreign_tid, foreign_url, b"foreign output")
    prepare(foreign_tid, foreign_group, foreign_output[0], foreign_output[0], None)
    foreign_render = render_receipt(foreign_tid, foreign_source[0], foreign_output[0],
                                    urls[1], foreign_url, foreign_source[1], foreign_output[1])
    # Deliberately corrupt only this disposable fixture as its owner. Normal
    # FK enforcement rejects such a cross-tenant edge; the verifier must also
    # refuse corrupted history rather than silently discard the foreign edge.
    sql("begin; alter table public.visual_global_object_lineage disable trigger all; "
        "insert into public.visual_global_object_lineage "
        "(tenant_id,group_key,source_exact_url,delivered_exact_url,source_fingerprint,"
        "delivered_fingerprint,render_receipt) "
        f"values({q(foreign_tid)},{q(foreign_group)},{q(urls[1])},{q(foreign_url)},"
        f"{q(foreign_source[1])},{q(foreign_output[1])},{q(foreign_render)}::uuid); "
        "alter table public.visual_global_object_lineage enable trigger all; commit")
    assert verified(tid, group, urls[0], reads[0][1], urls[2], reads[2][1]) == "f"


def test_preparation_derived_alias_conflict_rolls_back_new_url_and_object_registration():
    tid, group, urls, reads = chain(1)
    c_url = urls[1] + "&conflict=1"
    c = read_receipt(tid, c_url, b"conflicting derived digest")
    other_group = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',"
                      f"{q('https://test/other-scene-' + uuid.uuid4().hex)})")
    sql(f"select public.visual_group_register_alias({q(tid)},'byte_hash',"
        f"{q('derived:' + c[1])},{q(other_group)})")
    render = render_receipt(tid, reads[1][0], c[0], urls[1], c_url, reads[1][1], c[1])
    with pytest.raises(RuntimeError, match="scene|group"):
        prepare(tid, group, reads[1][0], c[0], render)
    assert sql(f"select count(*) from public.visual_global_object_attestation where exact_url={q(c_url)}") == "0"
    assert sql(f"select count(*) from public.visual_group_alias where alias_value={q(c_url)}") == "0"
    assert sql(f"select count(*) from public.visual_global_object_lineage where delivered_exact_url={q(c_url)}") == "0"
    assert sql(f"select count(*) from public.visual_global_usage where tenant_id={q(tid)}") == "0"


def test_lineage_edge_must_match_its_actual_render_receipt():
    tid, group, urls, reads = chain(1)
    c_url = urls[1] + "&final=1"
    c = read_receipt(tid, c_url, b"output without matching receipt")
    prepare(tid, group, c[0], c[0], None)
    prepare(tid, group, reads[1][0], reads[1][0], None)
    wrong_receipt = render_receipt(tid, reads[0][0], reads[1][0], urls[0], urls[1],
                                   reads[0][1], reads[1][1])
    # Owner-seeded inconsistent evidence: existing FKs bind both objects and the
    # receipt ID, but only the verifier checks that the receipt describes B->C.
    sql("insert into public.visual_global_object_lineage "
        "(tenant_id,group_key,source_exact_url,delivered_exact_url,source_fingerprint,"
        "delivered_fingerprint,render_receipt) "
        f"values({q(tid)},{q(group)},{q(urls[1])},{q(c_url)},{q(reads[1][1])},"
        f"{q(c[1])},{q(wrong_receipt)}::uuid)")
    assert verified(tid, group, urls[0], reads[0][1], c_url, c[1]) == "f"


def test_reachable_cycle_refuses_proof_even_when_target_is_reachable():
    tid, group, urls, reads = chain()
    edge(tid, group, urls[1], reads[1], urls[0], reads[0])
    assert verified(tid, group, urls[0], reads[0][1], urls[2], reads[2][1]) == "f"
    row_id = calendar(tid, group, urls[0], urls[2], reads[2][1])
    with pytest.raises(RuntimeError, match="not fully attested"):
        sql("select public.visual_global_claim_scene(c,false,false) "
            f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    assert sql("select count(*) from public.visual_global_usage") == "0"


def test_depth_limit_accepts_32_edges_and_refuses_33_without_partial_claim():
    tid, group, urls, reads = chain(32)
    assert verified(tid, group, urls[0], reads[0][1], urls[-1], reads[-1][1]) == "t"
    new_url = urls[-1] + "&over-limit=1"
    new_read = read_receipt(tid, new_url, b"33rd rendition")
    edge(tid, group, urls[-1], reads[-1], new_url, new_read)
    assert verified(tid, group, urls[0], reads[0][1], new_url, new_read[1]) == "f"
    row_id = calendar(tid, group, urls[0], new_url, new_read[1])
    with pytest.raises(RuntimeError, match="not fully attested"):
        sql("select public.visual_global_claim_scene(c,false,false) "
            f"from public.content_calendar c where c.id={q(row_id)}::uuid")
    assert sql("select count(*) from public.visual_global_usage") == "0"
