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
    assert "for lrow in select l.* from public.visual_group_usage_ledger l\n      order by" in importer
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
    assert "for lrow in select l.* from public.visual_group_usage_ledger l\n      order by" in importer
    assert "from public,anon,authenticated,service_role" in sql.split(
        "revoke all on function public.visual_global_import_history()", 1)[1].split(";", 1)[0]
    assert "grant execute on function public.visual_global_import_history() to service_role" not in sql


def test_selected_bytes_and_group_members_are_bound_before_claim():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    guard = _sql("DRAFT_visual_group_claim_trigger_20261002.sql")
    assert "create or replace function public.visual_global_row_fingerprint" in sql
    assert "create or replace function public.visual_global_row_bytes_verified" in sql
    assert "visual_global_object_lineage l" in sql
    assert "m.object_role='delivered'" in sql
    assert "m.object_role='source'" in sql
    assert "public.visual_global_claim_scene($1,$2,$3)" in guard
    assert "using p_row,p_published,p_ambiguous" in guard
    assert "public.visual_global_row_fingerprint(p_new)" not in guard
    scene = sql.split(
        "create or replace function public.visual_global_scene_fingerprints(", 1
    )[1].split("$$;", 1)[0]
    assert "visual_global_identity" not in scene
    complete = sql.split(
        "create or replace function public.visual_global_scene_complete(", 1
    )[1].split("end;\n$$;", 1)[0]
    assert "m.object_role='source'" in complete
    assert "m.object_role='delivered'" in complete
    row_verify = sql.split(
        "create or replace function public.visual_global_row_bytes_verified_for(", 1
    )[1].split("end;\n$$;", 1)[0]
    assert "public.visual_global_row_bytes_verified_for(p_row,p_row.visual_group_key)" in sql
    assert "visual_global_row_fingerprint(p_row)" not in row_verify
    assert "to_jsonb(p_row)->>'thumbnail_url'" in row_verify
    assert "is distinct from\n      (to_jsonb(p_row)->>'image_url')" in row_verify
    assert "poster so direct database writers cannot invent poster lineage" in row_verify


def test_atomic_claim_locks_and_claims_the_complete_sorted_fingerprint_set():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    claim = sql.split("create or replace function public.visual_global_claim_fingerprint_set(", 1)[1].split("end;\n$$;", 1)[0]
    assert "select array_agg(f order by f)" in claim
    assert "foreach v_hash in array v_fingerprints" in claim
    assert "jsonb_build_array('visual_global_fingerprint',v_hash)" in claim
    assert claim.index("preflight existing owners") < claim.index("insert into public.visual_global_usage")
    assert "visual byte fingerprint already used by another client or date" in claim
    assert "on conflict (tenant_id,group_key,fingerprint)" in claim
    assert "where public.visual_global_usage_member.state<>'published'" in claim
    assert "excluded.state='published'" in claim
    assert "un-enumerated historical fingerprint" not in claim
    assert "unenumerated historical fingerprint" in claim
    assert "primary key (tenant_id, group_key, fingerprint)" in sql


def test_import_and_activation_enumerate_phase1_objects_and_fail_closed():
    global_sql = _sql("DRAFT_visual_global_history_20261002.sql")
    activation = _sql("DRAFT_visual_group_activation_20261002.sql")
    importer = global_sql.split("create or replace function public.visual_global_import_history()", 1)[1].split("end;\n$$;", 1)[0]
    assert "visual_global_object_attestation" in importer
    assert "visual_global_scene_object_member" in importer
    assert "visual_global_object_lineage" in importer
    assert "occupied scene has incomplete byte evidence" in importer
    assert "staged history has no original date" in importer
    assert "lrow.state='published'" in importer
    assert "visual_global_claim_fingerprint_set" in importer
    assert "visual_global_claim_scene(public.content_calendar,boolean,boolean)" in activation
    assert "visual_global_object_attestation" in activation
    assert "visual_global_scene_object_member" in activation
    assert "visual_global_object_lineage" in activation
    assert "visual_global_usage, public.visual_global_usage_member" in activation
    assert "tgname='visual_global_scene_link_claim_guard'" in activation
    assert "global scene-link claim guard is missing" in activation


def test_active_scene_expansion_refreshes_all_component_bytes_atomically():
    global_sql = _sql("DRAFT_visual_global_history_20261002.sql")
    refresh = global_sql.split(
        "create or replace function public.visual_global_refresh_scene_history(", 1
    )[1].split("end;\n$$;", 1)[0]
    preparation = global_sql.split(
        "create or replace function public.visual_global_prepare_source_rendition(", 1
    )[1].split("end;\n$$;", 1)[0]
    assert "where enforce" in refresh
    assert "visual_global_scene_fingerprints" in refresh
    assert "visual_global_claim_fingerprint_set" in refresh
    assert "order by (l.state='published') desc,l.group_key" in refresh
    assert "visual_global_refresh_scene_history(v_tenant,p_group_key)" in preparation
    assert "visual_group_lock_scene_components" in preparation
    assert "create trigger visual_global_scene_link_claim_guard after insert" in global_sql


def test_history_coverage_reports_stale_members_and_orphan_global_owners():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    coverage = sql.split(
        "create or replace function public.visual_global_history_coverage()", 1
    )[1].split("$$;", 1)[0]
    assert "global_member_without_local_ledger" in coverage
    assert "global_member_not_in_current_scene" in coverage
    assert "global_usage_without_member" in coverage


def test_internal_global_claim_and_trigger_functions_are_owner_only():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    for signature in (
        "public.visual_global_immutable()",
        "public.visual_global_member_guard()",
        "public.visual_global_usage_guard()",
        "public.visual_global_refresh_scene_history(text,text)",
        "public.visual_global_scene_link_claim_guard()",
        "public.visual_global_block_local_activation()",
    ):
        assert f"revoke all on function {signature}\n  from public,anon,authenticated,service_role" in sql


def test_source_rendition_preparation_registers_both_exact_urls_atomically():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    rpc = sql.split("create or replace function public.visual_global_prepare_source_rendition(", 1)[1].split("end;\n$$;", 1)[0]
    assert "select distinct on (exact_url)" in rpc
    assert "visual_group_register_alias(\n      v_tenant,'canonical_url',v_item.exact_url,p_group_key)" in rpc
    assert "exact url registration returned another scene" in rpc


def test_release_rpc_is_not_publicly_executable():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    assert "revoke all on function public.visual_global_release(text,text)\n  from public,anon,authenticated,service_role" in sql


def test_writer_bundle_is_atomic_and_does_not_claim_usage():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    bundle = sql.split("create or replace function public.visual_global_prepare_bundle(", 1)[1].split("end;\n$$;", 1)[0]
    assert "p_tenant is distinct from v_tenant" in bundle
    assert "jsonb_build_array('visual_tenant',v_tenant)" in bundle
    assert "jsonb_build_array('visual_global_fingerprint',p_fingerprint)" in bundle
    assert "bundle aliases belong to conflicting groups" in bundle
    assert "group has another delivered url" in bundle
    assert bundle.index("group has another delivered url") < bundle.index("visual_group_register_alias(v_tenant")
    assert "visual_group_register_alias(v_tenant" in bundle
    assert "v_registered<>v_group" in bundle
    assert "visual_global_register_identity(v_tenant,v_group" in bundle
    assert "v_identity<>p_fingerprint" in bundle
    assert "visual_global_usage" not in bundle
    assert "grant execute on function public.visual_global_prepare_bundle(text,jsonb,text,jsonb,text,text)\n  to service_role" in sql


def test_source_rendition_preparation_has_immutable_exact_object_authority():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    for table in ("visual_global_object_read_receipt", "visual_global_render_receipt",
                  "visual_global_object_attestation", "visual_global_scene_object_member",
                  "visual_global_object_lineage"):
        assert f"create table if not exists public.{table}" in sql
        assert f"alter table public.{table} enable row level security" in sql
        assert f"on public.{table} for each row execute function public.visual_global_immutable()" in sql
    assert "exact_url text primary key" in sql
    assert "fingerprint ~ '^md5:[0-9a-f]{32}$'" in sql
    assert "object_role in ('source','delivered')" in sql
    assert "primary key (tenant_id, exact_url, object_role)" in sql
    assert "primary key (tenant_id,source_exact_url,delivered_exact_url)" in sql


def test_source_rendition_rpc_consumes_owner_receipts_without_claiming_usage():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    rpc = sql.split("create or replace function public.visual_global_prepare_source_rendition(", 1)[1].split("end;\n$$;", 1)[0]
    assert "p_tenant is distinct from v_tenant" in rpc
    assert "v_source.tenant_id<>v_tenant or v_delivered.tenant_id<>v_tenant" in rpc
    assert "v_render.source_read_receipt<>v_source.receipt_id" in rpc
    assert "v_render.delivered_read_receipt<>v_delivered.receipt_id" in rpc
    assert "v_render.source_exact_url<>v_source.exact_url" in rpc
    assert "v_render.delivered_exact_url<>v_delivered.exact_url" in rpc
    assert "v_render.source_fingerprint<>v_source.fingerprint" in rpc
    assert "v_render.delivered_fingerprint<>v_delivered.fingerprint" in rpc
    assert "v_item.fingerprint<>'md5:'||v_asset_hash" in rpc
    assert "a.alias_kind='source_asset' and a.alias_value=v_item.asset_id" in rpc
    assert "a.gym_id<>v_tenant and a.alias_kind='canonical_url'" in rpc
    assert "jsonb_build_array('visual_exact_url',v_item.exact_url)" in rpc
    assert "create trigger visual_global_exact_url_alias_guard before insert" in sql
    assert "o.tenant_id<>new.gym_id or o.group_key<>new.group_key" in sql
    assert sql.count("current_setting('transaction_isolation')<>'read committed'") >= 2
    assert "v_object.tenant_id<>v_tenant or v_object.group_key<>p_group_key" in rpc
    assert "visual_global_usage" not in rpc
    assert "'usage_claimed',false" in rpc
    assert "'history_refreshed',v_refreshed>0" in rpc
    assert "revoke all on public.visual_global_object_read_receipt, public.visual_global_render_receipt" in sql
    assert "grant execute on function public.visual_global_prepare_source_rendition(text,text,uuid,uuid,uuid,text)\n  to service_role" in sql


def test_import_orders_keyed_ledgers_before_null_key_historical_subsets():
    sql = _sql("DRAFT_visual_global_history_20261002.sql")
    importer = sql.split(
        "create or replace function public.visual_global_import_history()", 1
    )[1].split("end;\n$$;", 1)[0]
    keyed = importer.index(
        "for lrow in select l.* from public.visual_group_usage_ledger l")
    historical = importer.index("where c.visual_group_key is null")
    assert keyed < historical
    assert "visual_global_claim_historical_row" in importer
    assert "visual_global_row_verified_fingerprints(r,v_resolved)" in importer
    assert "visual_group_resolve_row" in importer
    assert "'imported_null_key_rows',v_historical" in importer
    assert "order by c.post_date,c.id" in importer
    coverage = sql.split(
        "create or replace function public.visual_global_coverage()", 1
    )[1].split("$$;", 1)[0]
    assert "public.visual_group_resolve_row(c)" in coverage
    assert "public.visual_global_row_verified_fingerprints(c,rg.resolved_group)" in coverage
    assert "'unresolved_group'" in coverage
    assert "'not_imported'" in coverage
    assert "c.visual_group_key is null and m.state not in ('reserved','published')" in coverage
    history = sql.split(
        "create or replace function public.visual_global_history_coverage()", 1
    )[1].split("$$;", 1)[0]
    assert "global_member_without_local_ledger" in history
    assert "global_member_not_in_current_scene" in history
    assert "global_usage_without_member" in history
    assert "c.id=m.calendar_row_id and c.visual_group_key is null" in history
    assert "public.visual_group_resolve_row(c)=m.group_key" in history
    assert "c.account is not distinct from m.channel" in history
    assert "no blanket" in history
