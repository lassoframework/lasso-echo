-- LOCAL read-only verification, not activation or production permission.
-- Frozen source signatures/body SHA-256 and explicit copied-validation deltas:
-- fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)
-- 65a0f94cc8da12d658e73abbe783ad1c1daab39680c91a92d851b3bb4b4ae724
-- Delta: use frozen planned candidate tuple; snapshot exact placeholder;
-- accept candidate eligibility, deny legacy replay; omit calendar UPDATE.
-- All existing visual/source/census/history/permanent reservation writes retained.
-- Source includes the inventory-generation/pending-mutation aware newest-census resolver.
-- fixer_generated_runtime_check_20261007(uuid)
-- 0181bf7326da07c6b46e801e2d04fa90e61bd49aa8c8be1fb443a58b5d8bf441
-- Delta: only before INSERT, exact admitted planned tuple replaces missing row,
-- while snapshot targets its frozen old placeholder. Full history loop retained.
-- Latest local-census selector/freshness/generation semantics are inherited verbatim.
-- finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)
-- normalized calendar capability wrapper c658ed1e8e4194eb961e32445890f734d26d156d79011b658149a77c06caf5bc
-- underlying historical/prospective body 7eaacf4f87922e0f4948fc56a40a9da44b6bdc77cb90b553c6e413d0a7827f29
-- Private current capability wrapper byte-for-byte. Public wrapper adds generated
-- singleton exact preparation/receipt/batch/placeholder/gap CAS + transaction
-- decision + atomic gap rebind. Ordinary batches retain the calendar capability wrapper and underlying guards.
-- Public original signature/OID/owner/ACL/settings unchanged; only postgres
-- owner is supported (alternate owner refuses install before any writes).
-- Generated-only entry takes G EXCLUSIVE before C/rows, matching the latest
-- inventory resolver. Ordinary delegation retains original locks and body chain.
do $$
declare pin public.generated_client_install_pin_20261009%rowtype; current_proc record;
 role_name text; table_name text; signature text;
begin
 if (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc
  where oid='public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)'::regprocedure)
  is distinct from '65a0f94cc8da12d658e73abbe783ad1c1daab39680c91a92d851b3bb4b4ae724'
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc
   where oid='public.fixer_generated_runtime_check_20261007(uuid)'::regprocedure)
   is distinct from '0181bf7326da07c6b46e801e2d04fa90e61bd49aa8c8be1fb443a58b5d8bf441' then
  raise exception 'frozen generated owner/runtime source body drift'; end if;
 select * into pin from public.generated_client_install_pin_20261009;
 select * into current_proc from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure;
 if row(current_proc.oid,current_proc.proowner,current_proc.proacl,current_proc.proconfig,current_proc.prosecdef)
   is distinct from row(pin.original_oid,pin.original_owner,pin.original_acl,pin.original_config,pin.original_definer)
  or pin.normalized_sha256 is distinct from 'c658ed1e8e4194eb961e32445890f734d26d156d79011b658149a77c06caf5bc'
  or (select encode(sha256(convert_to(replace(prosrc,'fixer_admission_body_'||pin.original_oid::text,'fixer_admission_body_VALIDATED'),'UTF8')),'hex') from pg_proc
   where oid='public.generated_client_original_finalizer_20261009(text,uuid,jsonb,jsonb)'::regprocedure) is distinct from pin.normalized_sha256
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc
   where oid='public.generated_client_original_finalizer_20261009(text,uuid,jsonb,jsonb)'::regprocedure)
   is distinct from pin.body_sha256 then raise exception 'finalizer identity or original body drift'; end if;
 select * into strict current_proc from pg_proc where oid=pin.admission_body_oid;
 if current_proc.oid is distinct from to_regprocedure('public.fixer_admission_body_'||pin.original_oid::text||'(text,uuid,jsonb,jsonb)')
  or current_proc.proowner<>'postgres'::regrole or not current_proc.prosecdef
  or current_proc.prorettype<>'jsonb'::regtype
  or current_proc.proargnames is distinct from array['p_tenant_id','p_batch_id','p_candidates','p_expected_old_rows']::text[]
  or current_proc.prolang<>(select oid from pg_language where lanname='plpgsql')
  or current_proc.proconfig is distinct from pin.original_config
  or exists(select 1 from aclexplode(coalesce(current_proc.proacl,acldefault('f',current_proc.proowner))) a
   where a.grantee<>current_proc.proowner)
  or encode(sha256(convert_to(current_proc.prosrc,'UTF8')),'hex') is distinct from pin.admission_body_sha256
  or pin.admission_body_sha256 is distinct from '7eaacf4f87922e0f4948fc56a40a9da44b6bdc77cb90b553c6e413d0a7827f29' then
  raise exception 'private admission underlying finalizer chain drift'; end if;
 foreach table_name in array array['generated_client_finalizer_decision_20261009','generated_client_install_pin_20261009'] loop
  if not (select relrowsecurity from pg_class where oid=('public.'||table_name)::regclass) then
   raise exception 'private staged table RLS missing'; end if;
  foreach role_name in array array['anon','authenticated','service_role','fixer_forward_media_owner_20261006',
   'generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
   if has_table_privilege(role_name,'public.'||table_name,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') then
    raise exception 'private staged table ACL leaked: %, %',table_name,role_name; end if;
  end loop;
 end loop;
 foreach signature in array array[
  'generated_client_reserve_detached_20261009(uuid,uuid,jsonb,jsonb,jsonb,jsonb,text)',
  'generated_client_original_finalizer_20261009(text,uuid,jsonb,jsonb)',
  'generated_client_runtime_check_20261009(uuid)',
  'generated_client_current_preparation_20261009(public.generated_client_admission_20261009)'] loop
  foreach role_name in array array['anon','authenticated','service_role','fixer_forward_media_owner_20261006',
   'generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
   if has_function_privilege(role_name,'public.'||signature,'EXECUTE') then
    raise exception 'private staged helper ACL leaked: %, %',signature,role_name; end if;
  end loop;
 end loop;
 foreach signature in array array['generated_client_plan_20261009(uuid,uuid,uuid,jsonb,jsonb)',
  'generated_client_prepare_staged_20261009(uuid,uuid,jsonb,jsonb,jsonb,text,uuid,uuid,bytea)'] loop
  if not has_function_privilege('fixer_forward_media_owner_20261006','public.'||signature,'EXECUTE') then
   raise exception 'isolated owner staged grant missing'; end if;
  foreach role_name in array array['anon','authenticated','service_role','generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
   if has_function_privilege(role_name,'public.'||signature,'EXECUTE') then
    raise exception 'staged owner grant leaked'; end if;
  end loop;
 end loop;
 if not has_function_privilege('service_role','public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)','EXECUTE')
  or has_function_privilege('fixer_forward_media_owner_20261006','public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)','EXECUTE') then
  raise exception 'separate finalizer role boundary missing'; end if;
end $$;

do $$ declare r text; p record; begin
 select * into strict p from pg_proc where oid='public.generated_client_service_preparation_20261009(text,uuid,uuid,uuid,text,jsonb)'::regprocedure;
 if p.proowner<>'postgres'::regrole or not p.prosecdef or p.proconfig is distinct from array['search_path=pg_catalog, public']::text[]
  or not has_function_privilege('service_role',p.oid,'EXECUTE') then raise exception 'service preparation boundary missing'; end if;
 foreach r in array array['anon','authenticated','fixer_forward_media_owner_20261006','generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
  if has_function_privilege(r,p.oid,'EXECUTE') then raise exception 'service preparation ACL leaked'; end if;
 end loop;
 if has_table_privilege('service_role','public.generated_client_admission_20261009','SELECT') then
  raise exception 'service preparation table SELECT leaked'; end if;
end $$;
