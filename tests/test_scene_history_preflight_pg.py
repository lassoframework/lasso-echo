"""Read-only preflight on the existing private PG17 fixture, never a supplied DSN."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("scene_txn_fixture", ROOT / "tests/test_scene_calendar_transaction_pg.py")
txn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(txn)
ledger = txn.ledger
DRAFT = ROOT / "migrations/DRAFT_visual_scene_history_preflight_20261006.sql"


@pytest.fixture(scope="module", autouse=True)
def cluster():
    fixture = txn.cluster.__wrapped__()
    next(fixture)
    try:
        ledger._sql(DRAFT.read_text())
        yield
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


@pytest.fixture(autouse=True)
def reset(cluster):
    txn.reset.__wrapped__(None)


def report():
    return json.loads(ledger._one("select public.visual_scene_history_preflight()"))


def reasons(result):
    return {item["reason"] for item in result["issues"]}


def occupy(tenant, group, fp, phash="0000000000000000", date="2026-10-10"):
    ledger._sql("insert into public.visual_scene_phash_occupied"
                "(phash,tenant_id,group_key,used_date,fingerprint) values "
                f"('{phash}','{tenant}','{group}','{date}','{fp}') on conflict do nothing")


def test_empty_fleet_never_authorizes_clearance_or_activation():
    result = report()
    assert not result["coverage_complete"]
    assert not result["activation_available"] and not result["clearance_authorized"]
    assert result["issues"] == []


def test_default_off_candidate_is_not_occupied_history():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    result = report()
    assert "displayed_scene_occupancy_missing" in reasons(result)
    assert "permanent_member_scene_coverage_missing" in reasons(result)
    assert not result["coverage_complete"]
    assert ledger._occupied_count() == 0
    assert ledger._one("select count(*) from public.gym_visual_scene_guard_settings") == "0"


def test_complete_bound_sibling_snapshot_still_cannot_activate():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    occupy(tenant, group, fp)
    result = report()
    assert result["issues"] == []
    assert result["coverage_complete"]
    assert not result["activation_available"] and not result["clearance_authorized"]
    denied = ledger._run(f"insert into public.gym_visual_scene_guard_settings(gym_id,enforce) values ('{tenant}',true)", check=False)
    assert denied.returncode and "activation unavailable" in denied.stderr


@pytest.mark.parametrize("status", ["published", "publishing"])
def test_unknown_source_archived_history_stays_review_blocked(status):
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    occupy(tenant, group, fp)
    ledger._sql("set session_replication_role=replica; "
                f"update public.content_calendar set source_media_url=null,status='{status}',variant_status='archived' where id='{row}'")
    result = report()
    assert "historical_source_review_required" in reasons(result)
    counts = next(item for item in result["tenant_counts"] if item["tenant"] == tenant)
    assert counts["unknown_source_rows"] == 1
    assert not result["coverage_complete"]
    assert txn.state(row)["source_media_url"] is None


def test_unknown_poster_owner_evidence_cannot_use_image_candidate():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    ledger._sql("set session_replication_role=replica; "
                f"update public.content_calendar set thumbnail_url='https://scratch.example/other.jpg' where id='{row}'")
    assert "displayed_scene_owner_evidence_missing" in reasons(report())


def test_source_photo_and_attested_distinct_poster_need_delivery_review():
    tenant, group, image_url, fp, candidate, row = txn.seed(scene_on=False)
    # Activate the second synthetic tenant before constructing the deliberately
    # incomplete mixed-object history; real group activation correctly refuses it.
    other, other_group = ledger._seed_tenant()
    _, other_fp, _ = ledger._seed_object(other, other_group, "0000000000000000")
    poster_url, poster_fp, poster_candidate = ledger._seed_object(tenant, group, "ffffffffffffffff")
    # Synthetic immutable owner evidence for the poster role on the disposable
    # fixture. This tests stored binding, never provider delivery precedence.
    ledger._sql("set session_replication_role=replica; "
                f"update public.visual_scene_candidate set object_role='poster' where candidate_id='{poster_candidate}'; "
                "update public.visual_scene_owner_phash_receipt set object_role='poster' "
                f"where receipt_id::text=(select evidence->>'owner_phash_receipt' from public.visual_scene_candidate where candidate_id='{poster_candidate}'); "
                f"update public.content_calendar set thumbnail_url='{poster_url}' where id='{row}'")
    occupy(tenant, group, fp)
    occupy(tenant, group, poster_fp, "ffffffffffffffff")
    occupy(other, other_group, other_fp, date="2026-10-11")
    result = report()
    assert "near_frame_history_conflict" in reasons(result)
    assert "delivery_precedence_review_required" in reasons(result)
    counts = next(item for item in result["tenant_counts"] if item["tenant"] == tenant)
    assert counts["missing_display_evidence_rows"] == 0
    assert not result["coverage_complete"] and not result["clearance_authorized"]


def test_temporary_relations_cannot_shadow_privileged_history():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    baseline = report()
    tables = ("content_calendar", "visual_scene_candidate", "visual_scene_owner_phash_receipt",
              "visual_scene_phash_occupied", "visual_scene_review_hold", "visual_global_usage",
              "visual_global_usage_member", "visual_global_object_attestation", "tenant_alias")
    setup = "; ".join(f"create temp table {table} (like public.{table}); grant select on pg_temp.{table} to service_role"
                      for table in tables)
    raw = ledger._one("begin; " + setup + "; set local role service_role; "
                      "select public.visual_scene_history_preflight(); rollback")
    attacked = json.loads(raw)
    for key in ("issues", "calendar_rows", "permanent_members", "tenant_counts", "coverage_complete"):
        assert attacked[key] == baseline[key]
    config = ledger._one("select array_to_string(proconfig,',') from pg_proc "
                         "where oid='public.visual_scene_history_preflight()'::regprocedure")
    assert "search_path=pg_catalog, public, pg_temp" in config


@pytest.mark.parametrize("state", ["reserved", "released", "published"])
def test_orphan_and_released_members_are_not_ignored(state):
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    ledger._sql("set session_replication_role=replica; "
                f"delete from public.content_calendar where id='{row}'; "
                f"update public.visual_global_usage_member set state='{state}'")
    result = report()
    assert "permanent_member_scene_coverage_missing" in reasons(result)
    counts = result["tenant_counts"][0]
    assert counts["orphan_members"] == 1
    assert counts["retained_released_members"] == int(state == "released")
    assert not result["coverage_complete"]


@pytest.mark.parametrize("same_tenant,bits,reason", [
    (False, 0, "near_frame_history_conflict"),
    (False, 7, "uncertain_history_review_required"),
    (True, 1, "near_frame_history_conflict"),
    (True, 30, "uncertain_history_review_required"),
])
def test_fleet_cross_date_conflict_bands(same_tenant, bits, reason):
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    occupy(tenant, group, fp)
    other, other_group = (tenant, group) if same_tenant else ledger._seed_tenant()
    _, other_fp, _ = ledger._seed_object(other, other_group, ledger._near("0000000000000000", bits))
    occupy(other, other_group, other_fp, ledger._near("0000000000000000", bits), "2026-10-11")
    assert reason in reasons(report())


def test_same_tenant_group_date_siblings_do_not_conflict():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    occupy(tenant, group, fp)
    occupy(tenant, group, fp, "0000000000000001")
    assert not {"near_frame_history_conflict", "uncertain_history_review_required"} & reasons(report())


def test_cross_client_same_date_is_still_conflicting():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    occupy(tenant, group, fp)
    other, other_group = ledger._seed_tenant()
    _, other_fp, _ = ledger._seed_object(other, other_group, "0000000000000000")
    occupy(other, other_group, other_fp)
    assert "near_frame_history_conflict" in reasons(report())
    assert len(report()["tenant_counts"]) == 2


def test_beyond_uncertain_band_is_not_reported_as_a_pair_conflict():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    occupy(tenant, group, fp)
    other, other_group = ledger._seed_tenant()
    phash = ledger._near("0000000000000000", 31)
    _, other_fp, _ = ledger._seed_object(other, other_group, phash)
    occupy(other, other_group, other_fp, phash)
    assert not {"near_frame_history_conflict", "uncertain_history_review_required"} & reasons(report())


def test_published_unknown_date_never_counts_as_complete():
    tenant, group, url, fp, candidate, row = txn.seed(scene_on=False)
    ledger._sql("set session_replication_role=replica; "
                f"update public.content_calendar set post_date=null,status='published' where id='{row}'")
    assert "scene_usage_date_unknown" in reasons(report())
    assert not report()["coverage_complete"]


def test_occupied_orphan_cannot_prove_history():
    tenant, group = ledger._seed_tenant()
    _, fp, _ = ledger._seed_object(tenant, group, "0000000000000000")
    occupy(tenant, group, fp)
    assert "occupied_scene_without_permanent_member" in reasons(report())


def test_read_only_execution_and_acl():
    txn.seed(scene_on=False)
    before = ledger._one("select md5(string_agg(row_to_json(c)::text,',' order by id)) from public.content_calendar c")
    ledger._sql("begin read only; set local role service_role; select public.visual_scene_history_preflight(); rollback")
    after = ledger._one("select md5(string_agg(row_to_json(c)::text,',' order by id)) from public.content_calendar c")
    assert before == after
    for role in ("anon", "authenticated"):
        assert ledger._one(f"select has_function_privilege('{role}','public.visual_scene_history_preflight()','EXECUTE')") == "f"
    assert ledger._one("select count(*) from public.visual_scene_disarm_event") == "0"
    assert ledger._one("select count(*) from public.visual_scene_review_hold") == "0"


def test_wrong_database_application_rolls_back():
    # Fail the named-database barrier under a rollback rehearsal without ever
    # removing that barrier or applying to an operator database.
    body = DRAFT.read_text().replace("echo_scene_ledger_test", "not_this_database")
    result = ledger._run(body, check=False)
    assert result.returncode and "SCRATCH ONLY" in result.stderr
