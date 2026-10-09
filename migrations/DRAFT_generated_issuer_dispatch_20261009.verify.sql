-- Read-only LOCAL installation checks. Not production permission/activation.
do $$
declare t text; role_name text;
begin
 foreach t in array array['generated_issuer_dispatch_principals_20261009',
  'generated_issuer_dispatch_requests_20261009','generated_issuer_dispatch_ledger_20261009',
  'generated_issuer_dispatch_quarantine_20261009'] loop
  if not (select relrowsecurity from pg_class where oid=('public.'||t)::regclass) then
   raise exception 'issuer dispatch RLS missing: %',t; end if;
  foreach role_name in array array['anon','authenticated','service_role',
   'generated_issuer_dispatch_producer_20261009','generated_issuer_dispatch_issuer_20261009',
   'generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
   if has_table_privilege(role_name,'public.'||t,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') then
    raise exception 'issuer dispatch table ACL leaked: %, %',t,role_name; end if;
  end loop;
 end loop;
 foreach t in array array['generated_issuer_dispatch_producer_20261009',
  'generated_issuer_dispatch_issuer_20261009'] loop
  if not exists(select 1 from pg_roles where rolname=t
   and not rolcanlogin and not rolsuper and not rolcreatedb and not rolcreaterole
   and not rolbypassrls and not rolinherit) then
   raise exception 'dedicated dispatch role must be unprivileged NOLOGIN: %',t; end if;
 end loop;
 if exists(select 1 from pg_roles p where p.rolname in ('anon','authenticated','service_role') and
  (pg_has_role(p.rolname,'generated_issuer_dispatch_producer_20261009','MEMBER')
   or pg_has_role(p.rolname,'generated_issuer_dispatch_issuer_20261009','MEMBER'))) then
  raise exception 'producer/service roles inherit dedicated dispatch roles'; end if;
 if not exists(select 1 from pg_trigger where tgrelid='public.generated_issuer_dispatch_requests_20261009'::regclass
   and tgname='generated_issuer_dispatch_request_guard' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.generated_issuer_dispatch_requests_20261009'::regclass
   and tgname='generated_issuer_dispatch_request_no_truncate' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.generated_issuer_dispatch_ledger_20261009'::regclass
   and tgname='generated_issuer_dispatch_ledger_guard' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.generated_issuer_dispatch_ledger_20261009'::regclass
   and tgname='generated_issuer_dispatch_ledger_no_truncate' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.generated_issuer_dispatch_quarantine_20261009'::regclass
   and tgname='generated_issuer_dispatch_quarantine_guard' and tgenabled='O')
  or not exists(select 1 from pg_trigger where tgrelid='public.generated_issuer_dispatch_quarantine_20261009'::regclass
   and tgname='generated_issuer_dispatch_quarantine_no_truncate' and tgenabled='O') then
  raise exception 'issuer dispatch immutability guard missing'; end if;
 -- Producer role: submit/read-own-result only, never issue/claim/commit/pending.
 if not has_function_privilege('generated_issuer_dispatch_producer_20261009',
   'public.generated_issuer_dispatch_submit_20261009(text,uuid,text,text,text,bytea,text,text)','EXECUTE')
  or not has_function_privilege('generated_issuer_dispatch_producer_20261009',
   'public.generated_issuer_dispatch_result_20261009(text,uuid,text)','EXECUTE') then
  raise exception 'producer RPC grant missing'; end if;
 if has_function_privilege('generated_issuer_dispatch_producer_20261009',
   'public.generated_issuer_dispatch_pending_20261009(text[],integer)','EXECUTE')
  or has_function_privilege('generated_issuer_dispatch_producer_20261009',
   'public.generated_issuer_dispatch_claim_20261009(text,text)','EXECUTE')
  or has_function_privilege('generated_issuer_dispatch_producer_20261009',
   'public.generated_issuer_dispatch_commit_20261009(text,text,uuid)','EXECUTE')
  or has_function_privilege('generated_issuer_dispatch_producer_20261009',
   'public.generated_issuer_dispatch_quarantine_20261009(text,text,text)','EXECUTE') then
  raise exception 'producer role must never issue, claim, commit or quarantine'; end if;
 -- Issuer role: pending/claim/commit/quarantine only, never submit or read results.
 if not has_function_privilege('generated_issuer_dispatch_issuer_20261009',
   'public.generated_issuer_dispatch_pending_20261009(text[],integer)','EXECUTE')
  or not has_function_privilege('generated_issuer_dispatch_issuer_20261009',
   'public.generated_issuer_dispatch_claim_20261009(text,text)','EXECUTE')
  or not has_function_privilege('generated_issuer_dispatch_issuer_20261009',
   'public.generated_issuer_dispatch_commit_20261009(text,text,uuid)','EXECUTE')
  or not has_function_privilege('generated_issuer_dispatch_issuer_20261009',
   'public.generated_issuer_dispatch_quarantine_20261009(text,text,text)','EXECUTE') then
  raise exception 'issuer RPC grant missing'; end if;
 if has_function_privilege('generated_issuer_dispatch_issuer_20261009',
   'public.generated_issuer_dispatch_submit_20261009(text,uuid,text,text,text,bytea,text,text)','EXECUTE')
  or has_function_privilege('generated_issuer_dispatch_issuer_20261009',
   'public.generated_issuer_dispatch_result_20261009(text,uuid,text)','EXECUTE') then
  raise exception 'issuer role must never submit or read results'; end if;
 foreach role_name in array array['anon','authenticated','service_role',
  'generated_hosted_byte_reader_20261009','generated_hosted_byte_issuer_20261009'] loop
  if has_function_privilege(role_name,
    'public.generated_issuer_dispatch_submit_20261009(text,uuid,text,text,text,bytea,text,text)','EXECUTE')
   or has_function_privilege(role_name,
    'public.generated_issuer_dispatch_result_20261009(text,uuid,text)','EXECUTE')
   or has_function_privilege(role_name,
    'public.generated_issuer_dispatch_pending_20261009(text[],integer)','EXECUTE')
   or has_function_privilege(role_name,
    'public.generated_issuer_dispatch_claim_20261009(text,text)','EXECUTE')
   or has_function_privilege(role_name,
    'public.generated_issuer_dispatch_commit_20261009(text,text,uuid)','EXECUTE')
   or has_function_privilege(role_name,
    'public.generated_issuer_dispatch_quarantine_20261009(text,text,text)','EXECUTE') then
   raise exception 'issuer dispatch RPC ACL leaked: %',role_name; end if;
 end loop;
 if exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname in
   ('generated_issuer_dispatch_authorized_20261009','generated_issuer_dispatch_submit_20261009',
    'generated_issuer_dispatch_result_20261009','generated_issuer_dispatch_pending_20261009',
    'generated_issuer_dispatch_claim_20261009','generated_issuer_dispatch_commit_20261009',
    'generated_issuer_dispatch_quarantine_20261009')
   and (not p.prosecdef
    or not (coalesce(p.proconfig,array[]::text[])
     && array['search_path=pg_catalog,public','search_path=pg_catalog, public']))) then
  raise exception 'issuer dispatch RPC must be security definer with pinned search_path'; end if;
end $$;
