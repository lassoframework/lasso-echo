from pathlib import Path


SQL = (Path(__file__).parents[1] / "migrations" / "media_runway_state_20260918.sql").read_text()
FIX_SQL = (Path(__file__).parents[1] / "migrations" / "media_runway_state_rpc_conflict_fix_20260919.sql").read_text()


def test_shared_runway_table_is_service_role_only_and_rls_enabled():
    normalized = " ".join(SQL.lower().split())
    assert "alter table public.media_runway_state enable row level security" in normalized
    assert "revoke all on table public.media_runway_state from public, anon, authenticated" in normalized
    assert "grant select on table public.media_runway_state to service_role" in normalized
    assert "revoke insert, update, delete on table public.media_runway_state from service_role" in normalized
    assert "create policy" not in normalized


def test_shared_runway_writes_use_a_hardened_compare_and_swap_rpc():
    normalized = " ".join(SQL.lower().split())
    assert "create or replace function public.upsert_media_runway_state" in normalized
    assert "security definer set search_path = pg_catalog, public" in normalized
    assert "p_expected_revision bigint" in normalized
    assert "current_state.revision = p_expected_revision" in normalized
    assert "on conflict on constraint media_runway_state_pkey do nothing" in normalized
    assert "p_revision <= p_expected_revision" in normalized
    assert "drop function if exists public.upsert_media_runway_state(text, bigint, jsonb, jsonb)" in normalized
    assert "p_gym_id <> btrim(p_gym_id)" in normalized
    assert "grant execute on function public.upsert_media_runway_state" in normalized
    assert "to service_role" in normalized


def test_shared_runway_production_fix_avoids_returns_table_name_collision():
    normalized = " ".join(FIX_SQL.lower().split())
    assert "create or replace function public.upsert_media_runway_state" in normalized
    assert "on conflict on constraint media_runway_state_pkey do nothing" in normalized
    assert "on conflict (gym_id)" not in normalized
    assert "security definer set search_path = pg_catalog, public" in normalized
    assert "grant execute on function public.upsert_media_runway_state" in normalized


def test_shared_runway_schema_rejects_empty_tenants_and_non_object_state():
    normalized = " ".join(SQL.lower().split())
    assert "nullif(btrim(gym_id), '') is not null" in normalized
    assert "revision bigint not null" in normalized
    assert "media_runway_state_revision_positive check (revision > 0)" in normalized
    assert "jsonb_typeof(fallback_episode) = 'object'" in normalized
    assert "jsonb_typeof(notice_state) = 'object'" in normalized
