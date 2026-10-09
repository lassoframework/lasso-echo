-- LOCAL read-only verification, not activation or production permission.
-- Frozen source signatures/body SHA-256 and explicit copied-validation deltas:
-- fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)
-- 16aaf2a5aa367d35c8c546bbdd9c2be0c7a32c05b7ed0c4423a9bffa01cb22a9
-- Delta: use frozen planned candidate tuple; snapshot exact placeholder;
-- accept candidate eligibility, deny legacy replay; omit calendar UPDATE.
-- All existing visual/source/census/history/permanent reservation writes retained.
-- fixer_generated_runtime_check_20261007(uuid)
-- afe550a47ade18d757a47ee382a8a1a4aa8c8021afe5e2cb2bbebb4dad66e1ba
-- Delta: only before INSERT, exact admitted planned tuple replaces missing row,
-- while snapshot targets its frozen old placeholder. Full history loop retained.
-- finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)
-- 092e3b3823a26c4ad5b446455274b99e11b4efae5e60632006376654a9a4937b
-- Private original body byte-for-byte. Public wrapper alone adds generated
-- singleton exact preparation/receipt/batch/placeholder/gap CAS + transaction
-- decision + atomic gap rebind. Ordinary batches use original helper directly.
-- Public original signature/OID/owner/ACL/settings unchanged; only postgres
-- owner is supported (alternate owner refuses install before any writes).
do $$
declare pin public.generated_client_install_pin_20261009%rowtype; current_proc record;
 role_name text; table_name text; signature text;
begin
 if (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc
  where oid='public.fixer_reserve_generated_bundle_20261007(uuid,jsonb,jsonb,jsonb,text)'::regprocedure)
  is distinct from '16aaf2a5aa367d35c8c546bbdd9c2be0c7a32c05b7ed0c4423a9bffa01cb22a9'
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc
   where oid='public.fixer_generated_runtime_check_20261007(uuid)'::regprocedure)
   is distinct from 'afe550a47ade18d757a47ee382a8a1a4aa8c8021afe5e2cb2bbebb4dad66e1ba' then
  raise exception 'frozen generated owner/runtime source body drift'; end if;
 select * into pin from public.generated_client_install_pin_20261009;
 select * into current_proc from pg_proc where oid='public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)'::regprocedure;
 if row(current_proc.oid,current_proc.proowner,current_proc.proacl,current_proc.proconfig,current_proc.prosecdef)
   is distinct from row(pin.original_oid,pin.original_owner,pin.original_acl,pin.original_config,pin.original_definer)
  or pin.body_sha256 is distinct from '092e3b3823a26c4ad5b446455274b99e11b4efae5e60632006376654a9a4937b'
  or (select encode(sha256(convert_to(prosrc,'UTF8')),'hex') from pg_proc
   where oid='public.generated_client_original_finalizer_20261009(text,uuid,jsonb,jsonb)'::regprocedure)
   is distinct from pin.body_sha256 then raise exception 'finalizer identity or original body drift'; end if;
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
