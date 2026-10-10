"""Disposable local PostgreSQL regression for global historical import fixes.

Historical published rows are immutable incidents, including distinct dates
inside a linked scene. Import is idempotent and does not manufacture a single
runtime owner for conflicting past uses. The keyed reservation case continues
to verify that an actual live owner remains unchanged while history is imported.

Requires psql and VISUAL_GROUP_TEST_DSN (libpq keyword DSN, absolute Unix
socket host, dbname=echo_visual_ledger_test), exactly as in
tests/test_visual_source_rendition_pg.py. Skipped without it; never point it
at a network or production database.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


DSN = os.environ.get("VISUAL_GROUP_TEST_DSN")
PSQL = shutil.which("psql")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
pytestmark = pytest.mark.skipif(not DSN, reason="isolated local VISUAL_GROUP_TEST_DSN unset")

D1 = "2026-09-01"
D2 = "2026-09-02"


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql(statement):
    done = subprocess.run([PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-d", DSN, "-c", statement], text=True,
                          capture_output=True, timeout=30)
    if done.returncode:
        raise RuntimeError(done.stderr)
    return done.stdout.strip()


def sql_error(statement):
    done = subprocess.run([PSQL, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-d", DSN, "-c", statement], text=True,
                          capture_output=True, timeout=30)
    assert done.returncode, f"expected failure, got: {done.stdout!r}"
    return done.stderr


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


def attest_same_object(label, tenant=None):
    """Register a tenant/group and attest one exact URL as BOTH source and
    delivered (same-object path: one receipt, no render lineage).

    Pass tenant=(key, tid) to attest a second object under an existing
    tenant, so two objects can be linked into one scene (scene links are
    tenant-scoped and cross-tenant links are rejected)."""
    if tenant is None:
        key, tid = "g_" + uuid.uuid4().hex, str(uuid.uuid4())
    else:
        key, tid = tenant
    # visual_group_tenant_register is advisory-locked and idempotent for the
    # same (key, tid), so the shared-tenant path registers on the first
    # attest and harmlessly confirms the existing mapping on the second.
    sql(f"select public.visual_group_tenant_register({q(key)},{q(tid)}::uuid)")
    url = f"https://test/{label}-{uuid.uuid4().hex}.jpg"
    data = f"exact bytes for {label} {uuid.uuid4().hex}".encode()
    fingerprint = "md5:" + hashlib.md5(data).hexdigest()
    group = sql(f"select public.visual_group_register_alias({q(tid)},'canonical_url',{q(url)})")
    receipt = str(uuid.uuid4())
    sql("insert into public.visual_global_object_read_receipt "
        "(receipt_id,tenant_id,exact_url,fingerprint,byte_length,acquisition_method,evidence_ref,observed_by) "
        f"values({q(receipt)}::uuid,{q(tid)},{q(url)},{q(fingerprint)},{len(data)},"
        "'verified_object_read','object-read-test','fixture_owner')")
    sql("set role service_role; select public.visual_global_prepare_source_rendition("
        f"{q(tid)},{q(group)},{q(receipt)}::uuid,{q(receipt)}::uuid,null,'test_actor')")
    assert sql(f"select public.visual_global_scene_complete({q(tid)},{q(group)})") == "t"
    return {"key": key, "tid": tid, "group": group, "url": url, "fingerprint": fingerprint}


def link_scene(tid, group_a, group_b):
    sql("select public.visual_group_link_scene("
        f"{q(tid)},{q(group_a)},{q(group_b)},"
        "'{\"review\":\"manual same-scene fixture\"}','human_reviewer')")


def insert_null_key_row(scene, date):
    row_id = str(uuid.uuid4())
    sql("insert into public.content_calendar"
        "(id,gym_id,post_date,status,account,image_url,source_media_url,published_at) values("
        f"{q(row_id)}::uuid,{q(scene['key'])},{q(date)},'published','ig',"
        f"{q(scene['url'])},{q(scene['url'])},now())")
    return row_id


def coverage_issues():
    out = sql("select coalesce(string_agg(issue,',' order by issue),'') "
              "from public.visual_global_coverage()")
    return out


def history_issues():
    out = sql("select coalesce(string_agg(issue,',' order by issue),'') "
              "from public.visual_global_history_coverage()")
    return out


def test_import_records_cross_date_scene_incidents_idempotently():
    tenant = ("g_" + uuid.uuid4().hex, str(uuid.uuid4()))
    a = attest_same_object("a", tenant)
    b = attest_same_object("b", tenant)
    assert a["tid"] == b["tid"] and a["fingerprint"] != b["fingerprint"]
    link_scene(a["tid"], a["group"], b["group"])
    insert_null_key_row(a, D1)
    insert_null_key_row(b, D2)

    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_published_rows"] == 2
    assert result["historical_incidents"] == 2
    # Past conflicts remain separate immutable evidence, not a fabricated
    # global owner/member or a refusal to import unrelated historical facts.
    assert sql("select count(*) from public.visual_global_usage") == "0"
    assert sql("select count(*) from public.visual_global_usage_member") == "0"
    assert sql("select count(*) from public.visual_global_published_attribution") == "0"
    assert coverage_issues() == "ready,ready"
    assert history_issues() == "ready,ready"
    second = json.loads(sql("select public.visual_global_import_history()"))
    assert second["historical_incidents"] == 2
    assert sql("select count(*) from public.visual_global_historical_incident") == "2"


def test_historical_claim_rpc_and_coverage_accept_cross_date_incidents():
    tenant = ("g_" + uuid.uuid4().hex, str(uuid.uuid4()))
    a = attest_same_object("a", tenant)
    b = attest_same_object("b", tenant)
    link_scene(a["tid"], a["group"], b["group"])
    row_a = insert_null_key_row(a, D1)
    row_b = insert_null_key_row(b, D2)

    # First row attributes fine on its own date.
    sql("select public.visual_global_claim_historical_row("
        f"{q(a['tid'])},{q(a['group'])},{q(D1)},{q(row_a)}::uuid,'ig',"
        f"array[{q(a['fingerprint'])}])")
    # A different-date sibling in the same linked scene is its own historical
    # incident and does not rewrite the first incident or create an owner.
    sql("select public.visual_global_claim_historical_row("
        f"{q(b['tid'])},{q(b['group'])},{q(D2)},{q(row_b)}::uuid,'ig',"
        f"array[{q(b['fingerprint'])}])")
    assert coverage_issues() == "ready,ready"
    assert history_issues() == "ready,ready"
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["historical_incidents"] == 2
    assert sql("select count(*) from public.visual_global_usage") == "0"


def test_keyed_same_date_reservation_keeps_state_and_gains_historical_incident():
    k = attest_same_object("k")
    keyed_row = str(uuid.uuid4())
    sql("insert into public.content_calendar"
        "(id,gym_id,post_date,status,account,visual_group_key,image_url,source_media_url) values("
        f"{q(keyed_row)}::uuid,{q(k['key'])},{q(D1)},'pending','ig',"
        f"{q(k['group'])},{q(k['url'])},{q(k['url'])})")
    sql("insert into public.visual_group_usage_ledger"
        "(gym_id,group_key,reserved_date,calendar_row_id,channel,state,ambiguous) values("
        f"{q(k['tid'])},{q(k['group'])},{q(D1)},{q(keyed_row)}::uuid,'ig','reserved',false)")
    null_key_row = insert_null_key_row(k, D1)

    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_local_groups"] == 1
    assert result["imported_null_key_rows"] == 1

    # The keyed reservation is never promoted or overwritten.
    owner = sql("select state from public.visual_global_usage "
                f"where fingerprint={q(k['fingerprint'])}")
    assert owner == "reserved"
    member = sql("select state||'|'||calendar_row_id::text from public.visual_global_usage_member "
                 f"where fingerprint={q(k['fingerprint'])}")
    assert member == f"reserved|{keyed_row}"
    # ...and both sources retain separate durable incident proofs.
    receipt = sql("select used_date::text||'|'||coalesce(channel,'') from "
                  "public.visual_global_historical_incident "
                  f"where source_kind='calendar' and source_key={q(null_key_row)} "
                  f"and tenant_id={q(k['tid'])} and group_key={q(k['group'])} "
                  f"and fingerprint={q(k['fingerprint'])} and calendar_row_id={q(null_key_row)}::uuid")
    assert receipt == f"{D1}|ig"
    assert coverage_issues() == "ready,ready"
    assert set(history_issues().split(",")) == {"ready"}

    # Historical incident evidence is immutable: no update, no delete.
    err = sql_error("delete from public.visual_global_historical_incident")
    assert "immutable" in err
    err = sql_error("update public.visual_global_historical_incident set used_date=used_date+1")
    assert "immutable" in err

    # Deleting the published calendar row and re-importing never erases or
    # duplicates the proof, and the keyed reservation is still intact.
    sql(f"delete from public.content_calendar where id={q(null_key_row)}::uuid")
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_local_groups"] == 1
    assert result["imported_null_key_rows"] == 0
    assert sql("select count(*) from public.visual_global_historical_incident "
               f"where source_key={q(null_key_row)}") == "1"
    assert sql("select state from public.visual_global_usage "
               f"where fingerprint={q(k['fingerprint'])}") == "reserved"
    member = sql("select state||'|'||calendar_row_id::text from public.visual_global_usage_member "
                 f"where fingerprint={q(k['fingerprint'])}")
    assert member == f"reserved|{keyed_row}"
    assert set(history_issues().split(",")) == {"ready"}
