"""Focused PostgreSQL regressions for immutable historical incidents."""

import uuid

import pytest

from test_visual_global_history_pg import (
    D1,
    D2,
    database,
    history_issues,
    insert_null_key_row,
    q,
    sql,
    sql_error,
    attest_same_object,
)


D3 = "2026-09-03"


@pytest.fixture(autouse=True)
def reset_database(database):
    # Reuse the guarded disposable-DB fixture from the companion module.
    pass


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
