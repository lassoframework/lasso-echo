"""Static release contracts for the unapplied global visual SQL drafts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def _sql(name: str) -> str:
    return (MIGRATIONS / name).read_text().lower()


def test_calendar_guard_claims_and_releases_global_authority_atomically():
    sql = _sql("DRAFT_visual_group_claim_trigger_20261002.sql")
    sync = sql[sql.index("create or replace function public.visual_group_sync_row") :]
    assert "perform public.visual_group_global_release" in sync
    assert "perform public.visual_group_global_claim" in sync
    assert "global visual claim authority is missing for armed tenant" in sql
    assert "before insert or update or delete" in sql
    assert "revoke all on function public.visual_group_global_claim" in sql
    assert "revoke all on function public.visual_group_global_release" in sql


def test_armed_calendar_refuses_statement_level_truncate():
    sql = _sql("DRAFT_visual_group_claim_trigger_20261002.sql")
    assert "create trigger content_calendar_visual_group_truncate_guard before truncate" in sql
    assert "where enforce" in sql


def test_activation_is_last_and_imports_under_existing_barrier_before_arm():
    sql = _sql("DRAFT_visual_group_activation_20261002.sql")
    assert "apply this activation draft last" in sql
    assert "apply global visual history before integrated activation" in sql
    assert "drop trigger if exists visual_global_block_local_activation" in sql
    barrier = sql.index("lock table public.content_calendar in share row exclusive mode")
    imported = sql.index("perform public.visual_global_import_history()", barrier)
    covered = sql.index("public.visual_global_history_coverage()", imported)
    receipt = sql.index("insert into public.visual_group_activation", covered)
    armed = sql.index("insert into public.gym_visual_guard_settings", receipt)
    assert barrier < imported < covered < receipt < armed
    assert "'global_history_imported', true" in sql
    assert "where c.issue<>'ready'" in sql
    assert "where h.issue<>'ready'" in sql
    assert "where c.tenant_id=v_tenant" not in sql
    assert "where h.tenant_id=v_tenant_text" not in sql


def test_byte_hash_alias_requires_explicit_source_or_derived_namespace():
    sql = _sql("DRAFT_visual_group_schema_20261002.sql")
    assert "alias_kind <> 'byte_hash'" in sql
    assert "^(source|derived):(sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})$" in sql
    assert "byte_hash requires an explicit source/derived and sha256/md5 namespace" in sql
    activation = _sql("DRAFT_visual_group_activation_20261002.sql")
    assert "legacy byte_hash lacks source/derived algorithm namespace" in activation


def test_activation_rollback_preserves_global_published_history():
    sql = _sql("DRAFT_visual_group_activation_20261002.sql")
    assert "preserve all global published rows during rollback" in sql
    assert "never remove calendar claim/release integration" in sql


def test_staged_history_keeps_its_original_date_after_release():
    schema = _sql("DRAFT_visual_group_schema_20261002.sql")
    claim = _sql("DRAFT_visual_group_claim_trigger_20261002.sql")
    global_sql = _sql("DRAFT_visual_global_history_20261002.sql")
    assert "staged visual identity and date are permanent" in schema
    assert "if found and l.reserved_date is distinct from p_new.post_date" in claim
    assert "staged visual group cannot be redated" in claim
    release = global_sql.split("create or replace function public.visual_global_release(", 1)[1].split("end;\n$$;", 1)[0]
    assert "update public.visual_global_usage set state='released'" not in release
    assert "update public.visual_global_usage_member set state='released'" not in release
    assert "legacy released global fingerprint requires historical repair" in global_sql
    assert "update public.visual_group_usage_ledger set reserved_date=p_new_date" not in claim
    assert "l.state<>'released' or l.ambiguous or l.reserved_date is distinct from p_new_date" in claim


def test_released_local_history_is_imported_and_covered():
    global_sql = _sql("DRAFT_visual_global_history_20261002.sql")
    activation = _sql("DRAFT_visual_group_activation_20261002.sql")
    importer = global_sql.split("create or replace function public.visual_global_import_history()", 1)[1].split("end;\n$$;", 1)[0]
    assert "for r in select l.* from public.visual_group_usage_ledger l\n      order by" in importer
    coverage = global_sql.split("create or replace function public.visual_global_history_coverage()", 1)[1].split("$$;", 1)[0]
    assert "where l.state<>'released'" not in coverage
    assert "when l.state='published' then 'published' else 'reserved'" in coverage
    assert "where l.gym_id=v_tenant_text and l.state<>'released'" not in activation
    backfill = _sql("DRAFT_visual_group_backfill_20261002.sql")
    assert "elsif found and l.reserved_date is distinct from day" in backfill
    assert "update public.visual_group_usage_ledger set state='reserved',reserved_date=day" not in backfill


def test_asset_registration_requires_membership_in_exact_group():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    registration = sql.split("create or replace function public.visual_global_register_identity(", 1)[1].split("end;\n$$;", 1)[0]
    assert "a.alias_kind='source_asset' and a.alias_value=btrim(p_asset_id)" in registration
    assert "a.group_key=p_group_key" in registration
    assert "asset is not a member of the visual group" in registration


def test_history_migration_order_names_activation_last():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    assert "before the activation" in sql.split("begin;", 1)[0]


def test_global_import_is_owner_only_and_takes_calendar_barrier():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    importer = sql.split("create or replace function public.visual_global_import_history()", 1)[1].split("end;\n$$;", 1)[0]
    assert "lock table public.content_calendar in share row exclusive mode" in importer
    assert "lock table public.visual_group_usage_ledger, public.visual_group_alias" in importer
    assert "from public.visual_group_usage_ledger l" in importer
    assert "for r in select l.* from public.visual_group_usage_ledger l\n      order by" in importer
    assert "from public,anon,authenticated,service_role" in sql.split(
        "revoke all on function public.visual_global_import_history()", 1)[1].split(";", 1)[0]
    assert "grant execute on function public.visual_global_import_history() to service_role" not in sql


def test_selected_bytes_and_group_members_are_bound_before_claim():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    guard = _sql("DRAFT_visual_group_claim_trigger_20261002.sql")
    assert "create or replace function public.visual_global_row_fingerprint" in sql
    assert "public.visual_global_row_fingerprint(c) is distinct from i.fingerprint" in sql
    assert "public.visual_global_group_bytes_verified(v_tenant::text,p_group_key,v_hash)" in sql
    assert "p_selected_fingerprint<>v_hash" in sql
    assert "public.visual_global_row_fingerprint(p_new)" in guard
    assert "selected calendar asset has no verified byte fingerprint" in guard
    assert "and a.alias_kind='canonical_url') <= 1" in sql


def test_release_rpc_is_not_publicly_executable():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    assert "revoke all on function public.visual_global_release(text,text)\n  from public,anon,authenticated,service_role" in sql
