-- DRAFT / UNAPPLIED. Diagnostic-only generated owner read surface.
-- Requires owner photo clearance + its claim/source/photo dependencies.
-- No LOGIN/token/key, trusted job/copy/palette/inventory authority or generated
-- reservation is provisioned. All completion booleans remain FALSE. Counts and
-- revisions cover current DB rows, never claim full historical/source coverage.
begin;
create role fixer_generated_owner_reader_20261007 nologin;
grant usage on schema public to fixer_generated_owner_reader_20261007;

create function public.fixer_generated_owner_diagnostics_20261007(p_tenant text)
returns jsonb language sql stable security definer set search_path=pg_catalog,public as $$
 with calendar as (
  select coalesce(jsonb_agg(to_jsonb(r) order by r.id),'[]'::jsonb) rows,
   count(*) row_count,
   count(*) filter(where r.status='published' or r.published_at is not null or r.late_post_id is not null) published_count,
   count(*) filter(where r.post_date is null) undated_count
  from public.content_calendar r
 ), inventory as (
  select coalesce(jsonb_agg(to_jsonb(a) order by a.id),'[]'::jsonb) rows,count(*) asset_count
  from public.media_asset a where a.gym_id=p_tenant
 ), sources as (
  select coalesce(jsonb_agg(to_jsonb(s) order by s.id),'[]'::jsonb) rows,count(*) source_count
  from public.media_source s where s.gym_id=p_tenant
 ), claims as (
  select coalesce(jsonb_agg(to_jsonb(c) order by c.claim_token),'[]'::jsonb) rows,count(*) claim_count
  from public.fixer_forward_media_claim_receipt_20261006 c
 ), uses as (
  select coalesce(jsonb_agg(to_jsonb(u) order by u.fingerprint),'[]'::jsonb) rows
  from public.fixer_forward_media_use_20261006 u
 ), originals as (
  select coalesce(jsonb_agg(to_jsonb(h) order by h.calendar_row_id),'[]'::jsonb) rows,count(*) audited_count
  from public.fixer_forward_media_historical_original_20261007 h
 ), photo as (
  select public.fixer_forward_media_photo_snapshot_20261007() snapshot
 )
 select jsonb_build_object(
  'tenant_id',p_tenant,'diagnostic_only',true,
  'database_calendar_rows',calendar.row_count,'database_published_rows',calendar.published_count,
  'database_undated_rows',calendar.undated_count,'database_claims',claims.claim_count,
  'database_audited_originals',originals.audited_count,
  'database_tenant_assets',inventory.asset_count,'database_tenant_sources',sources.source_count,
  'photo_inventory_revision','sha256:'||encode(sha256(convert_to(jsonb_build_object(
    'tenant_id',p_tenant,'assets',inventory.rows,'sources',sources.rows)::text,'UTF8')),'hex'),
  'history_revision','sha256:'||encode(sha256(convert_to(jsonb_build_object(
    'calendar',calendar.rows,'claims',claims.rows,'uses',uses.rows,
    'audited_originals',originals.rows,'photo_snapshot',photo.snapshot)::text,'UTF8')),'hex'),
  'photo_inventory_complete',false,'history_complete',false,
  'eligible_photo_count',null,'reviewed_no_match',false,'brand_source_verified',false,
  'missing_authorities',jsonb_build_array('trusted_generated_job_binding',
    'versioned_approved_palette_and_copy','complete_authenticated_photo_inventory',
    'complete_exact_perceptual_undated_unresolved_history',
    'generated_owner_reservation_and_claim_fence'))
 from calendar,inventory,sources,claims,uses,originals,photo;
$$;
revoke all on function public.fixer_generated_owner_diagnostics_20261007(text)
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,
 fixer_forward_media_attester_20261006,fixer_forward_media_photo_auditor_20261007;
grant execute on function public.fixer_generated_owner_diagnostics_20261007(text)
 to fixer_generated_owner_reader_20261007;
-- Deliberately no raw table privileges, mutation RPC, reservation table or
-- positive clearance integration. No DB snapshot can mint a signed receipt.
commit;
