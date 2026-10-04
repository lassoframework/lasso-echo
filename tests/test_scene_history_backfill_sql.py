"""Static contracts for the additive scene-history backfill draft."""

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "migrations" / "DRAFT_visual_scene_claim_wave_20261003.sql"
BACKFILL = ROOT / "migrations" / "DRAFT_visual_scene_history_backfill_20261004.sql"
FROZEN_SHA256 = "8fae2bdc788ed874e34a13d9f46ae77262bd93ef928cc2f5a4d8d4de9a5ee570"


def _function(sql, signature):
    return sql.split(f"create or replace function public.{signature}", 1)[1].split(
        "$$;", 1
    )[0].lower()


def test_frozen_scene_wave_bytes_and_stub_are_unchanged():
    raw = FROZEN.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_SHA256
    sql = raw.decode().lower()
    stub = _function(sql, "visual_scene_backfill_occupied()")
    assert "errcode='0a000'" in stub
    assert "insert into public.visual_scene_phash_occupied" not in stub


def test_backfill_is_additive_and_ordered_after_the_frozen_wave():
    assert BACKFILL.exists()
    assert BACKFILL.name > FROZEN.name
    header = BACKFILL.read_text().lower().split("begin;", 1)[0]
    assert "apply strictly after draft_visual_scene_claim_wave_20261003.sql" in header
    assert "draft / unapplied / off" in header


def test_zero_inference_and_exact_proof_binding():
    sql = BACKFILL.read_text().lower()
    evaluate = _function(sql, "visual_scene_history_evaluate(")
    assert evaluate.index("o_reason := 'source_null'") < evaluate.index(
        "visual_global_row_bytes_verified"
    )
    for token in (
        "visual_group_resolve_row",
        "stale_group_key",
        "ambiguous_candidate",
        "visual_scene_row_delivered_object",
        "visual_global_row_bytes_verified",
        "source_unattested",
    ):
        assert token in evaluate
    assert "v_candidate_count <> 1" in evaluate


def test_locked_writer_reloads_live_row_and_checks_every_proof_field():
    sql = BACKFILL.read_text().lower()
    writer = _function(sql, "visual_scene_history_backfill_locked(")
    assert writer.index("lock table public.content_calendar") < writer.index(
        "for update"
    )
    assert writer.index("visual_scene_history_lock_components_nowait") < writer.index(
        "lock table public.visual_scene_candidate"
    )
    assert writer.index("lock table public.visual_scene_candidate") < writer.index(
        "pg_try_advisory_xact_lock"
    )
    assert "scene proof writer busy; retry transaction" in writer
    component_lock = _function(
        sql, "visual_scene_history_lock_components_nowait("
    )
    assert "for update nowait" in component_lock
    assert "scene component busy; retry transaction" in component_lock
    assert writer.count("for update") >= 2
    assert "select c.* into v_live" in writer
    assert "to_jsonb(v_live) is distinct from v_plan->'row'" in writer
    assert "to_jsonb(v_eval) is distinct from v_plan->'evaluation'" in writer
    for proof_key in (
        "'candidate_id'",
        "'phash'",
        "'fingerprint'",
        "'exact_url'",
        "'object_role'",
        "'source_url'",
        "'source_fingerprint'",
        "'post_date'",
        "'published_at'",
    ):
        assert proof_key in writer


def test_activation_receipt_blocks_unresolved_history_under_write_barrier():
    sql = BACKFILL.read_text().lower()
    hook = _function(sql, "visual_scene_history_activation_receipt()")
    assert "sharerowexclusivelock" in hook
    assert "visual_scene_history_backfill_locked(null)" in hook
    assert "->>'scope','') <> 'fleet'" in hook
    assert "->>'transaction_id')::bigint" in hook
    assert "(v_receipt->>'unresolved')::integer <> 0" in hook
    assert "unresolved scene history requires review" in hook
    assert "jsonb_build_object('scene_history',v_receipt)" in hook
    assert "before insert or update on public.visual_group_activation" in sql


def test_service_callers_cannot_run_the_history_writer():
    sql = BACKFILL.read_text().lower()
    for signature in (
        "visual_scene_history_evaluate(public.content_calendar)",
        "visual_scene_history_lock_components_nowait(jsonb)",
        "visual_scene_history_backfill_locked(text)",
        "visual_scene_backfill_occupied()",
        "visual_scene_history_activation_receipt()",
    ):
        assert f"revoke all on function public.{signature}" in sql
    assert "grant execute on function public.visual_scene_history_audit(text)" in sql
