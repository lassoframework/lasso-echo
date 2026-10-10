"""Focused PostgreSQL regressions for immutable historical incidents."""

import json
import os
import re
from pathlib import Path
import uuid

import pytest

from test_visual_global_history_pg import (
    D1,
    D2,
    history_issues,
    insert_null_key_row,
    link_scene,
    q,
    sql,
    sql_error,
    attest_same_object,
)


D3 = "2026-09-03"


@pytest.fixture(autouse=True)
def database():
    # This package exclusively owns a named local disposable DB, never the
    # shared echo_visual_ledger_test database used by other active sessions.
    dsn = os.environ.get("VISUAL_GROUP_TEST_DSN", "")
    if not dsn:
        pytest.skip("isolated local DSN unset")
    assert re.fullmatch(r"host=/[A-Za-z0-9_./-]+ port=\d+ dbname=echo_scene_review_[A-Za-z0-9_]+(?: user=[A-Za-z0-9_-]+)?", dsn)
    assert sql("select current_database()").startswith("echo_scene_review_")
    sql("do $$ begin if not exists(select from pg_roles where rolname='anon') then create role anon; end if; "
        "if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if; "
        "if not exists(select from pg_roles where rolname='service_role') then create role service_role; end if; end $$;")
    sql("drop schema public cascade; create schema public; grant usage on schema public to anon,authenticated,service_role;")
    sql("""create table public.content_calendar(
        id uuid primary key default gen_random_uuid(), gym_id text, post_date date,
        status text default 'pending', account text, format text,
        variant_status text default 'active', image_url text, thumbnail_url text,
        source_media_url text, source_media_asset_id text, drive_file_id text,
        byte_hash text, r2_key text, media_not_ready_reason text,
        published_at timestamptz, late_post_id text, publish_reservation_day date,
        publish_claim_token uuid, created_at timestamptz default now());
        create table public.media_asset(id text primary key,gym_id text,content_hash text);
        grant select,insert,update,delete on public.content_calendar to service_role;""")
    for name in ("logical_post_id_20261004.sql", "DRAFT_visual_group_schema_20261002.sql",
                 "DRAFT_visual_group_claim_trigger_20261002.sql", "DRAFT_visual_group_backfill_20261002.sql",
                 "DRAFT_visual_global_history_20261002.sql"):
        sql((Path(__file__).resolve().parents[1] / "migrations" / name).read_text())


def insert_platform_row(scene, date, logical, account="instagram", fmt="feed", published=True):
    row = str(uuid.uuid4())
    sql("insert into public.content_calendar(id,gym_id,post_date,status,account,format,"
        "logical_post_id,image_url,source_media_url,published_at) values("
        f"{q(row)}::uuid,{q(scene['key'])},{q(date)},{q('published' if published else 'pending')},"
        f"{q(account)},{q(fmt)}," + (f"{q(logical)}::uuid" if logical else "null") +
        f",{q(scene['url'])},{q(scene['url'])}," + ("now()" if published else "null") + ")")
    return row


def activate(scene):
    sql((Path(__file__).resolve().parents[1] / "migrations" / "DRAFT_visual_group_activation_20261002.sql").read_text())
    return sql("set role service_role; select public.visual_group_activate_guard("
               f"{q(scene['tid'])},'historical sibling cutover test')")


def platform_insert_error(scene, logical, account="instagram", fmt="feed", date=D1):
    # Preserve the real BEFORE INSERT calendar path, including local sibling writes.
    return sql_error("insert into public.content_calendar(gym_id,post_date,status,account,format,"
        "logical_post_id,image_url,source_media_url) values("
        f"{q(scene['key'])},{q(date)},'pending',{q(account)},{q(fmt)}," +
        (f"{q(logical)}::uuid" if logical else "null") + f",{q(scene['url'])},{q(scene['url'])})")


def test_later_live_claim_rejects_digest_with_past_duplicate_incidents():
    scene = attest_same_object("duplicate")
    row_a = insert_null_key_row(scene, D1)
    row_b = insert_null_key_row(scene, D2)

    imported = sql("select public.visual_global_import_history()")
    assert '"historical_incidents": 2' in imported
    assert sql("select count(*) from public.visual_global_historical_incident "
               f"where fingerprint={q(scene['fingerprint'])}") == "2"

    err = sql_error("select public.visual_global_claim_fingerprint_set("
                    f"{q(scene['tid'])},{q(scene['group'])},{q(D3)},"
                    f"{q(str(uuid.uuid4()))}::uuid,'ig',false,false,"
                    f"array[{q(scene['fingerprint'])}])")
    assert "permanently consumed by historical incident" in err
    assert sql("select count(*) from public.visual_global_usage") == "0"
    assert sql("select count(*) from public.visual_global_usage_member") == "0"
    assert row_a != row_b


def test_historical_incidents_survive_calendar_row_deletion():
    scene = attest_same_object("retained")
    row_a = insert_null_key_row(scene, D1)
    row_b = insert_null_key_row(scene, D2)
    sql("select public.visual_global_import_history()")

    sql("delete from public.content_calendar "
        f"where id in ({q(row_a)}::uuid,{q(row_b)}::uuid)")
    assert sql("select count(*) from public.content_calendar") == "0"
    assert sql("select count(*) from public.visual_global_historical_incident "
               f"where fingerprint={q(scene['fingerprint'])}") == "2"
    assert history_issues() == "ready,ready"


def test_service_role_cannot_insert_historical_incident_directly():
    scene = attest_same_object("no-forge")
    err = sql_error("set role service_role; insert into public.visual_global_historical_incident "
                    "(source_kind,source_key,source_state,tenant_id,group_key,fingerprint,"
                    "calendar_row_id,channel,used_date,source_evidence,byte_evidence) values("
                    f"'calendar',{q(str(uuid.uuid4()))},'published',{q(scene['tid'])},"
                    f"{q(scene['group'])},{q(scene['fingerprint'])},{q(str(uuid.uuid4()))}::uuid,"
                    f"'ig',{q(D1)},'{{\"forged\":true}}'::jsonb,'[{{\"forged\":true}}]'::jsonb)")
    assert "permission denied" in err
    assert sql("select count(*) from public.visual_global_historical_incident") == "0"


def test_activated_same_logical_platform_siblings_have_unique_slots():
    scene = attest_same_object("slots")
    logical = str(uuid.uuid4())
    row_a = insert_platform_row(scene, D1, logical)
    assert '"global_history_imported": true' in activate(scene)
    # Historical feed occupies IG. FB feed and paired IG Story may join.
    fb = insert_platform_row(scene, D1, logical, "facebook", published=False)
    sql(f"update public.content_calendar set status='published',published_at=now() where id={q(fb)}::uuid")
    story = insert_platform_row(scene, D1, logical, "instagram", "story", published=False)
    for account, fmt in (("ig", "feed"), ("fb", "feed"), ("ig", "story")):
        assert ("historical incident" in platform_insert_error(scene, logical, account, fmt)
                if account == "ig" and fmt == "feed" else
                "slot occupied" in platform_insert_error(scene, logical, account, fmt))
    assert "historical incident" in platform_insert_error(scene, str(uuid.uuid4()), "facebook")
    assert "historical incident" in platform_insert_error(scene, None, "facebook")
    assert "historical incident" in platform_insert_error(scene, logical, "instagram", "unknown")
    assert "another date" in platform_insert_error(scene, logical, "facebook", date=D2)
    # Retrying the original row remains idempotent and receipt updates are valid.
    sql(f"update public.content_calendar set late_post_id='backfilled' where id={q(row_a)}::uuid")
    assert sql("select count(*) from public.content_calendar") == "3"
    assert fb != story


def test_linked_scene_slots_use_shared_identity_and_preserve_date_tenant_blocks():
    tenant = ("g_" + uuid.uuid4().hex, str(uuid.uuid4()))
    a = attest_same_object("linked-a", tenant)
    b = attest_same_object("linked-b", tenant)
    link_scene(a['tid'], a['group'], b['group'])
    logical = str(uuid.uuid4())
    insert_platform_row(a, D1, logical)
    activate(a)
    insert_platform_row(b, D1, logical, "facebook", published=False)
    assert "slot occupied" in platform_insert_error(a, logical, "facebook")
    assert "historical incident" in platform_insert_error(b, str(uuid.uuid4()), "instagram", "story")
    assert "another date" in platform_insert_error(b, logical, "instagram", "story", D2)
    # Exact-byte reuse from another canonical tenant remains globally blocked.
    other = attest_same_object("outsider")
    err = sql_error("select public.visual_global_claim_fingerprint_set("
                    f"{q(other['tid'])},{q(other['group'])},{q(D1)},"
                    f"{q(str(uuid.uuid4()))}::uuid,'ig',false,false,array[{q(a['fingerprint'])}])")
    assert "historical incident" in err


def test_null_historical_identity_holds_new_platform_sibling():
    scene = attest_same_object("legacy-null")
    row = insert_platform_row(scene, D1, None)
    activate(scene)
    assert "historical incident" in platform_insert_error(scene, str(uuid.uuid4()), "facebook")
    sql(f"update public.content_calendar set late_post_id='legacy-receipt' where id={q(row)}::uuid")


def test_receipt_backfill_after_import_is_not_source_evidence_drift():
    """P1 B: incident source evidence binds tenant, row, group, date, channel
    and immutable media/byte proof only. Backfilling the mutable provider
    receipt (published_at, late_post_id) after a fleet import must re-import
    cleanly; changing immutable byte evidence must still raise drift."""
    scene = attest_same_object("receipt")
    row = insert_null_key_row(scene, D1)
    imported = json.loads(sql("select public.visual_global_import_history()"))
    assert imported["historical_incidents"] == 1

    # Legitimate receipt backfill on the same published row: not drift.
    sql("update public.content_calendar set "
        "published_at=published_at+interval '1 hour',"
        "late_post_id='late_backfill_receipt' "
        f"where id={q(row)}::uuid")
    second = json.loads(sql("select public.visual_global_import_history()"))
    assert second["historical_incidents"] == 1
    assert sql("select count(*) from public.visual_global_historical_incident") == "1"
    assert history_issues() == "ready"

    # Immutable byte-evidence mutation is refused by coverage or drift checks
    # and never laundered into the recorded incident.
    sql("update public.content_calendar set byte_hash='md5:changed-proof' "
        f"where id={q(row)}::uuid")
    err = sql_error("select public.visual_global_import_history()")
    assert ("source evidence drift" in err
            or "calendar coverage incomplete" in err)
    assert sql("select source_evidence->>'byte_hash' from "
               "public.visual_global_historical_incident "
               f"where source_key={q(row)}") != "md5:changed-proof"

    # The mutable receipt fields are not part of stable incident identity.
    evidence = json.loads(sql("select source_evidence from "
                              "public.visual_global_historical_incident "
                              f"where source_key={q(row)}"))
    assert "published_at" not in evidence
    assert "provider_post_id" not in evidence
    for key in ("id", "raw_gym_key", "post_date", "channel", "image_url",
                "source_media_url", "thumbnail_url", "source_media_asset_id",
                "drive_file_id", "byte_hash", "r2_key", "visual_group_key",
                "logical_post_id", "format"):
        assert key in evidence


def test_deleted_history_keeps_original_slot_and_logical_identity():
    scene = attest_same_object("deleted-slot")
    logical = str(uuid.uuid4())
    original = insert_platform_row(scene, D1, logical)
    sql("select public.visual_global_import_history()")
    sql(f"delete from public.content_calendar where id={q(original)}::uuid")
    # Immutable historical evidence still rejects repeat IG after row deletion.
    repeat = insert_platform_row(scene, D1, logical)
    err = sql_error("select public.visual_global_claim_fingerprint_set("
        f"{q(scene['tid'])},{q(scene['group'])},{q(D1)},{q(repeat)}::uuid,"
        f"'instagram',true,false,array[{q(scene['fingerprint'])}])")
    assert "historical incident" in err
    fb = insert_platform_row(scene, D1, logical, "facebook")
    assert scene['fingerprint'] in sql("select public.visual_global_claim_fingerprint_set("
        f"{q(scene['tid'])},{q(scene['group'])},{q(D1)},{q(fb)}::uuid,"
        f"'facebook',true,false,array[{q(scene['fingerprint'])}])")


def test_format_is_immutable_historical_evidence():
    scene = attest_same_object("format-drift")
    row = insert_platform_row(scene, D1, str(uuid.uuid4()))
    sql("select public.visual_global_import_history()")
    sql(f"update public.content_calendar set format='story' where id={q(row)}::uuid")
    assert "source evidence drift" in sql_error("select public.visual_global_import_history()")
    assert sql("select source_evidence->>'format' from public.visual_global_historical_incident "
               f"where calendar_row_id={q(row)}::uuid") == "feed"


def test_same_date_different_unlinked_scene_stays_blocked():
    tenant = ("g_" + uuid.uuid4().hex, str(uuid.uuid4()))
    a = attest_same_object("unlinked-a", tenant)
    b = attest_same_object("unlinked-b", tenant)
    logical = str(uuid.uuid4())
    insert_platform_row(a, D1, logical)
    sql("select public.visual_global_import_history()")
    row = insert_platform_row(b, D1, logical, "facebook")
    err = sql_error("select public.visual_global_claim_fingerprint_set("
        f"{q(b['tid'])},{q(b['group'])},{q(D1)},{q(row)}::uuid,"
        f"'facebook',true,false,array[{q(a['fingerprint'])}])")
    assert "historical incident" in err


def test_deleted_runtime_publication_does_not_release_platform_slot():
    scene = attest_same_object("runtime-delete")
    logical = str(uuid.uuid4())
    insert_platform_row(scene, D1, logical)
    activate(scene)
    fb = insert_platform_row(scene, D1, logical, "facebook", published=True)
    sql(f"delete from public.content_calendar where id={q(fb)}::uuid")
    assert sql("select state from public.visual_group_usage_sibling "
               f"where calendar_row_id={q(fb)}::uuid") == "released"
    assert "slot occupied" in platform_insert_error(scene, logical, "facebook")
    # A different authorized slot remains possible, even after FB deletion.
    insert_platform_row(scene, D1, logical, "instagram", "story", published=False)
    assert sql("select channel||'/'||format from public.visual_global_row_slot_receipt "
               f"where calendar_row_id={q(fb)}::uuid") == "facebook/feed"


@pytest.mark.parametrize("account,fmt", [("instagram", "story"), ("facebook", "story")])
def test_runtime_published_slot_mutation_is_refused(account, fmt):
    scene = attest_same_object("runtime-mutation")
    logical = str(uuid.uuid4())
    insert_platform_row(scene, D1, logical)
    activate(scene)
    fb = insert_platform_row(scene, D1, logical, "facebook", published=True)
    error = sql_error("update public.content_calendar set "
                      f"account={q(account)},format={q(fmt)} where id={q(fb)}::uuid")
    assert ("slot identity is immutable" in error or "historical incident" in error)
    assert sql("select account||'/'||format from public.content_calendar "
               f"where id={q(fb)}::uuid") == "facebook/feed"
    assert "slot occupied" in platform_insert_error(scene, logical, "facebook")
    sql(f"update public.content_calendar set late_post_id='runtime-receipt' where id={q(fb)}::uuid")


def test_runtime_slot_receipt_cannot_be_mutated_or_forged():
    scene = attest_same_object("slot-no-forge")
    logical = str(uuid.uuid4())
    row = insert_platform_row(scene, D1, logical)
    activate(scene)
    row = insert_platform_row(scene, D1, logical, "facebook", published=False)
    assert "immutable" in sql_error("update public.visual_global_row_slot_receipt "
        f"set format='story' where calendar_row_id={q(row)}::uuid")
    assert "permission denied" in sql_error("set role service_role; insert into public.visual_global_row_slot_receipt "
        "(tenant_id,group_key,calendar_row_id,used_date,logical_post_id,channel,format) values("
        f"{q(scene['tid'])},{q(scene['group'])},{q(str(uuid.uuid4()))}::uuid,"
        f"{q(D1)},{q(logical)},'facebook','feed')")


def test_historical_published_slot_mutation_is_refused_after_activation():
    scene = attest_same_object("historical-slot-mutation")
    logical = str(uuid.uuid4())
    row = insert_platform_row(scene, D1, logical)
    activate(scene)
    assert "slot identity is immutable" in sql_error("update public.content_calendar "
        f"set account='facebook',format='feed' where id={q(row)}::uuid")
    insert_platform_row(scene, D1, logical, "facebook", published=False)


def test_import_restores_only_preexisting_keyed_reservation_not_new_siblings():
    from test_visual_global_history_pg import (
        test_keyed_same_date_reservation_keeps_state_and_gains_historical_incident,
    )
    test_keyed_same_date_reservation_keeps_state_and_gains_historical_incident()
    # The importer-only restoration cannot authorize a fresh runtime row,
    # despite identical tenant, scene, date and platform.
    data = json.loads(sql("select json_build_object('tid',g.gym_id,'group',g.group_key,"
        "'key',c.gym_id,'url',c.image_url,'fingerprint',m.fingerprint) "
        "from public.visual_global_usage_member m join public.visual_group g "
        "on g.gym_id=m.tenant_id and g.group_key=m.group_key "
        "join public.content_calendar c on c.id=m.calendar_row_id limit 1"))
    row = insert_platform_row(data, D1, None)
    assert "historical incident" in sql_error("select public.visual_global_claim_fingerprint_set("
        f"{q(data['tid'])},{q(data['group'])},{q(D1)},{q(row)}::uuid,'ig',false,false,"
        f"array[{q(data['fingerprint'])}])")
