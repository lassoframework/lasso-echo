"""Disposable local PostgreSQL regression for global historical import fixes.

Covers the two independent-review historical one-use ledger fixes on the
unapplied DRAFT_visual_global_history_20261002.sql:

P1 -- two attested published null-key rows with DISTINCT fingerprints in one
manually linked scene must hit a component-wide date barrier when their dates
differ (importer, historical-claim RPC and calendar coverage); same-date
siblings pass.

P2 -- when a keyed same-date reserved owner/member already exists, historical
attribution adds a durable append-only published-row receipt without promoting
or overwriting the keyed reservation; deleting the receipt is impossible and
calendar deletion plus re-import never erases the proof.

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


def test_import_rejects_component_wide_date_conflict_but_passes_same_date_siblings():
    tenant = ("g_" + uuid.uuid4().hex, str(uuid.uuid4()))
    a = attest_same_object("a", tenant)
    b = attest_same_object("b", tenant)
    assert a["tid"] == b["tid"] and a["fingerprint"] != b["fingerprint"]
    link_scene(a["tid"], a["group"], b["group"])
    insert_null_key_row(a, D1)
    insert_null_key_row(b, D2)

    err = sql_error("select public.visual_global_import_history()")
    assert "component-wide usage date" in err
    # The refused import is atomic: no partial owner, member or receipt remains.
    assert sql("select count(*) from public.visual_global_usage") == "0"
    assert sql("select count(*) from public.visual_global_usage_member") == "0"
    assert sql("select count(*) from public.visual_global_published_attribution") == "0"

    # Same-date siblings import cleanly.
    sql(f"update public.content_calendar set post_date={q(D1)} where image_url={q(b['url'])}")
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_null_key_rows"] == 2
    assert result["global_fingerprints"] == 2
    assert sql("select count(*) from public.visual_global_usage where state='published'") == "2"
    assert sql("select count(*) from public.visual_global_usage_member where state='published'") == "2"
    assert sql("select count(*) from public.visual_global_published_attribution") == "2"
    assert coverage_issues() == "ready,ready"
    assert history_issues() == "ready,ready"


def test_historical_claim_rpc_and_coverage_flag_component_conflict():
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
    # A different-date sibling in the same linked scene is barred, even though
    # its byte has no owner conflict of its own.
    err = sql_error("select public.visual_global_claim_historical_row("
                    f"{q(b['tid'])},{q(b['group'])},{q(D2)},{q(row_b)}::uuid,'ig',"
                    f"array[{q(b['fingerprint'])}])")
    assert "component-wide usage date" in err
    # Calendar coverage flags the conflict, so the importer barrier refuses
    # before any write; activation's coverage re-read sees it too.
    assert "component_usage_date_conflict" in coverage_issues()
    err = sql_error("select public.visual_global_import_history()")
    assert "calendar coverage incomplete" in err
    # The same-date sibling claim passes and coverage returns to ready.
    sql("select public.visual_global_claim_historical_row("
        f"{q(b['tid'])},{q(b['group'])},{q(D1)},{q(row_b)}::uuid,'ig',"
        f"array[{q(b['fingerprint'])}])")
    sql(f"update public.content_calendar set post_date={q(D1)} where id={q(row_b)}::uuid")
    assert coverage_issues() == "ready,ready"


def test_keyed_same_date_reservation_keeps_state_and_gains_durable_attribution():
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
    # ...but the published row leaves durable append-only proof.
    receipt = sql("select used_date::text||'|'||coalesce(channel,'') from "
                  "public.visual_global_published_attribution "
                  f"where tenant_id={q(k['tid'])} and group_key={q(k['group'])} "
                  f"and fingerprint={q(k['fingerprint'])} and calendar_row_id={q(null_key_row)}::uuid")
    assert receipt == f"{D1}|ig"
    assert coverage_issues() == "ready,ready"
    assert history_issues() == "ready"

    # The receipt is immutable: no update, no delete.
    err = sql_error("delete from public.visual_global_published_attribution")
    assert "immutable" in err
    err = sql_error("update public.visual_global_published_attribution set used_date=used_date+1")
    assert "immutable" in err

    # Deleting the published calendar row and re-importing never erases or
    # duplicates the proof, and the keyed reservation is still intact.
    sql(f"delete from public.content_calendar where id={q(null_key_row)}::uuid")
    result = json.loads(sql("select public.visual_global_import_history()"))
    assert result["imported_local_groups"] == 1
    assert result["imported_null_key_rows"] == 0
    assert sql("select count(*) from public.visual_global_published_attribution") == "1"
    assert sql("select state from public.visual_global_usage "
               f"where fingerprint={q(k['fingerprint'])}") == "reserved"
    member = sql("select state||'|'||calendar_row_id::text from public.visual_global_usage_member "
                 f"where fingerprint={q(k['fingerprint'])}")
    assert member == f"reserved|{keyed_row}"
    assert history_issues() == "ready"
