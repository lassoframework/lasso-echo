import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SQL = (ROOT / "migrations" / "lasso_campaign_stage_20260923.sql").read_text()
SPEC = importlib.util.spec_from_file_location(
    "stage_lasso_campaign_rows", ROOT / "scripts" / "stage_lasso_campaign_rows.py"
)
stage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage)


def test_rpc_serializes_before_any_calendar_read_or_insert():
    lock = SQL.index("lock table public.content_calendar in share row exclusive mode")
    existing_read = SQL.index("select * into v_existing")
    slot_read = SQL.index("'occupied_logical_slot'")
    insert = SQL.index("insert into public.content_calendar")
    assert lock < existing_read < slot_read < insert
    assert "pg_advisory" not in SQL


def test_rpc_is_service_only_insert_only_and_campaign_bounded():
    assert "security definer" in SQL
    assert "set search_path = public" in SQL
    assert "revoke all on function public.stage_lasso_campaign_row" in SQL
    assert "from public, anon, authenticated" in SQL
    assert "grant execute on function public.stage_lasso_campaign_row" in SQL
    assert "to service_role" in SQL
    assert "date '2026-09-23'" in SQL and "date '2026-11-08'" in SQL
    assert "update public.content_calendar" not in SQL
    assert "delete from public.content_calendar" not in SQL
    assert "'pending'" in SQL and "'active'" in SQL


def test_rpc_checks_reviewed_artifact_and_rejects_schema_invention():
    for contract in (
        "a.evidence->>'grade_status' = 'PASS'",
        "a.evidence->>'image_sha256' = p_image_sha256",
        "a.evidence->>'policy_version' = p_policy_version",
        "a.source_identity->>'source_hash' = p_source_hash",
        "a.tenant = p_artifact_tenant",
        "a.image_url = v_image_url",
    ):
        assert contract in SQL
    assert "'category'" not in SQL
    assert "'draft_type'" not in SQL
    assert "unsupported_row_key" in SQL


def test_rpc_allows_same_day_ig_fb_pair_but_refuses_cross_day_reuse():
    cross_day = SQL[SQL.index("'cross_day_creative_reuse'") - 900:]
    assert "c.post_date = v_day" in cross_day
    assert "c.slot_index = v_slot" in cross_day
    assert "<> v_account" in cross_day
    assert "c.image_url = v_image_url or c.caption = v_caption" in cross_day
    assert "occupied_logical_slot" in SQL
    assert "id_reused_with_different_row" in SQL
    assert "'result', 'idempotent'" in SQL


def test_null_typed_values_and_artifact_hashes_fail_closed():
    assert "v_day is null" in SQL
    assert "v_slot is null" in SQL
    assert "p_source_hash is null" in SQL
    assert "p_image_sha256 is null" in SQL
    assert "p_artifact_tenant not in ('lasso', 'lasso_ig')" in SQL
    assert "p_row->>'gym_id' is distinct from 'lasso'" in SQL


def test_legacy_null_slot_blocks_automatic_staging():
    occupied = SQL[SQL.index("-- An active occupying row"):SQL.index("-- Instagram and Facebook")]
    assert "c.slot_index = v_slot or c.slot_index is null" in occupied
    assert "'occupied_logical_slot'" in occupied


def test_manifest_client_persists_backup_and_each_receipt(tmp_path):
    item = {
        "row": {"id": "00000000-0000-0000-0000-000000000001"},
        "artifact_tenant": "lasso_ig",
        "source_hash": "a" * 64,
        "policy_version": "policy-v1",
        "image_sha256": "b" * 64,
    }
    seen = []
    receipts = stage.apply_manifest(
        {"rows": [item]}, [item], object(), tmp_path,
        call_fn=lambda _store, sent: seen.append(sent) or {"result": "inserted", "id": sent["row"]["id"]},
    )
    assert seen == [item]
    assert receipts[0]["result"] == "inserted"
    assert (tmp_path / "lasso-campaign-input.json").exists()
    assert (tmp_path / "lasso-campaign-stage-receipts.json").exists()


def test_manifest_client_refuses_missing_provenance_without_calling_rpc(tmp_path):
    item = {"row": {"id": "00000000-0000-0000-0000-000000000001"}}
    receipts = stage.apply_manifest(
        {"rows": [item]}, [item], object(), tmp_path,
        call_fn=lambda *_: (_ for _ in ()).throw(AssertionError("must not call RPC")),
    )
    assert receipts == [{"id": item["row"]["id"], "result": "conflict", "reason": "invalid_manifest_item"}]


def test_manifest_client_refuses_to_overwrite_existing_evidence(tmp_path):
    existing = tmp_path / "lasso-campaign-stage-receipts.json"
    existing.write_text("preserve me\n")
    try:
        stage.apply_manifest({"rows": []}, [], object(), tmp_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing evidence must block a second run")
    assert existing.read_text() == "preserve me\n"
    assert not (tmp_path / "lasso-campaign-input.json").exists()


def test_manifest_client_persists_error_receipt_before_reraising(tmp_path):
    item = {
        "row": {"id": "00000000-0000-0000-0000-000000000001"},
        "artifact_tenant": "lasso_ig", "source_hash": "a" * 64,
        "policy_version": "policy-v1", "image_sha256": "b" * 64,
    }
    try:
        stage.apply_manifest({"rows": [item]}, [item], object(), tmp_path,
                             call_fn=lambda *_: (_ for _ in ()).throw(RuntimeError("wire failed")))
    except RuntimeError:
        pass
    else:
        raise AssertionError("RPC exception must propagate")
    receipts = json.loads((tmp_path / "lasso-campaign-stage-receipts.json").read_text())
    assert receipts == [{"id": item["row"]["id"], "result": "error", "reason": "RuntimeError"}]
