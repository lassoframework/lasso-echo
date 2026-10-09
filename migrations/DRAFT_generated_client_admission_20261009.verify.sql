-- Read-only LOCAL installation checks. Not production permission/activation.
do $$
declare t text; role_name text;
begin
 foreach t in array array['generated_client_control_20261009','generated_client_admission_20261009'] loop
  if not (select relrowsecurity from pg_class where oid=('public.'||t)::regclass) then
   raise exception 'client authority RLS missing: %',t; end if;
  foreach role_name in array array['anon','authenticated','service_role','fixer_forward_media_owner_20261006',
   'generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
   if has_table_privilege(role_name,'public.'||t,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') then
    raise exception 'client authority table ACL leaked: %, %',t,role_name; end if;
  end loop;
 end loop;
 if not exists(select 1 from pg_trigger where tgrelid='public.content_calendar'::regclass
   and tgname='001_generated_client_admission' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.content_calendar'::regclass
   and tgname='zzz_calendar_generated_approval_guard' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.generated_client_admission_20261009'::regclass
   and tgname='generated_client_immutable' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.generated_client_admission_20261009'::regclass
   and tgname='generated_client_no_truncate' and tgenabled='O') then
  raise exception 'client authority guard missing'; end if;
 foreach role_name in array array['anon','authenticated','service_role','generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
  if has_function_privilege(role_name,'public.generated_client_stage_20261009(uuid,jsonb,jsonb,jsonb,text,uuid,uuid,bytea)','EXECUTE')
   or has_function_privilege(role_name,'public.generated_client_reconcile_20261009(uuid,uuid,uuid,text)','EXECUTE') then
   raise exception 'client owner RPC ACL leaked: %',role_name; end if;
 end loop;
 if not has_function_privilege('fixer_forward_media_owner_20261006',
  'public.generated_client_stage_20261009(uuid,jsonb,jsonb,jsonb,text,uuid,uuid,bytea)','EXECUTE') then
  raise exception 'client owner RPC grant missing'; end if;
 if (select column_default from information_schema.columns where table_schema='public'
   and table_name='generated_client_control_20261009' and column_name='enabled') is distinct from 'false' then
  raise exception 'client authority does not default OFF'; end if;
end $$;
