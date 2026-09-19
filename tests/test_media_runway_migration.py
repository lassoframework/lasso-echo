from pathlib import Path


SQL = (Path(__file__).parents[1] / "migrations" / "media_runway_state_20260918.sql").read_text()


def test_shared_runway_table_is_service_role_only_and_rls_enabled():
    normalized = " ".join(SQL.lower().split())
    assert "alter table public.media_runway_state enable row level security" in normalized
    assert "revoke all on table public.media_runway_state from public, anon, authenticated" in normalized
    assert "grant select, insert, update on table public.media_runway_state to service_role" in normalized
    assert "create policy" not in normalized


def test_shared_runway_schema_rejects_empty_tenants_and_non_object_state():
    normalized = " ".join(SQL.lower().split())
    assert "nullif(btrim(gym_id), '') is not null" in normalized
    assert "jsonb_typeof(fallback_episode) = 'object'" in normalized
    assert "jsonb_typeof(notice_state) = 'object'" in normalized
