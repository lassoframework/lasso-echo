-- DRAFT, UNAPPLIED. Additive to accepted inventory mutation protocol #362.
-- Default OFF. No selector/history/publish authority is replaced or relaxed.
-- Apply is one PG transaction: exact source + asset CAS and immutable receipt.
-- Local flock must be released before RPC. Unknown commits require readback.
-- Never-landed release is deliberately unavailable in this tranche.
begin;
do $$ declare t text; begin
 if to_regprocedure('public.fixer_inventory_protocol_lock_private_20261008()') is null
  or to_regprocedure('public.fixer_inventory_mutator_check_private_20261008()') is null
  or to_regclass('public.fixer_inventory_generation_20261008') is null then
  raise exception 'accepted inventory mutation protocol required' using errcode='55000'; end if;
 foreach t in array array['media_source','media_asset'] loop
  if not exists(select 1 from pg_trigger where tgrelid=('public.'||t)::regclass
   and tgname='generated_inventory_lock' and tgenabled in ('O','A') and tgtype=62
   and tgfoid=to_regprocedure('public.fixer_generated_inventory_lock_20261007()'))
   or not exists(select 1 from pg_trigger where tgrelid=('public.'||t)::regclass
   and tgname='inventory_generation_20261008' and tgenabled in ('O','A') and tgtype=29
   and tgfoid=to_regprocedure('public.fixer_inventory_database_write_20261008()')) then
   raise exception 'accepted inventory graph/census/generation guards required for %',t using errcode='55000'; end if;
 end loop;
end $$;
create sequence public.fixer_remote_drive_version_20261008;
alter table public.media_asset add column drive_use_version bigint not null default nextval('public.fixer_remote_drive_version_20261008');
alter table public.media_source add column drive_use_version bigint not null default nextval('public.fixer_remote_drive_version_20261008');
create function public.fixer_remote_drive_version_private_20261008()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 -- A global sequence prevents delete/reinsert ABA, including no-op updates.
 new.drive_use_version:=nextval('public.fixer_remote_drive_version_20261008');
 return new;
end $$;
create trigger remote_drive_version before insert or update on public.media_asset
 for each row execute function public.fixer_remote_drive_version_private_20261008();
create trigger remote_drive_version before insert or update on public.media_source
 for each row execute function public.fixer_remote_drive_version_private_20261008();
-- Existing rows receive sequence versions at installation. New-row defaults
-- must not require runtime sequence privileges before the definer trigger runs.
alter table public.media_asset alter column drive_use_version set default 0;
alter table public.media_source alter column drive_use_version set default 0;
create table public.fixer_remote_drive_use_control_20261008 (
 singleton boolean primary key default true check(singleton),
 enabled boolean not null default false,
 writers_verified_ref text,
 check(not enabled or length(btrim(writers_verified_ref)) between 16 and 1000),
 check(not enabled or writers_verified_ref is not null)
);
insert into public.fixer_remote_drive_use_control_20261008(singleton) values(true);
create trigger inventory_state_entry before insert or update or delete or truncate on public.fixer_remote_drive_use_control_20261008
 for each statement execute function public.fixer_inventory_state_write_lock_20261008();
create table public.fixer_remote_drive_use_20261008 (
 use_id uuid primary key,
 request jsonb not null,
 receipt jsonb not null,
 applied_at timestamptz not null default clock_timestamp()
);
-- Immutable even for the table owner through ordinary DML; no destructive
-- receipt operation is part of the runtime or operator reconciliation contract.
create function public.fixer_remote_drive_use_immutable_20261008()
returns trigger language plpgsql set search_path=pg_catalog,public as $$
begin
 raise exception 'remote drive receipts are immutable' using errcode='23514';
end $$;
create trigger remote_drive_use_immutable before update or delete or truncate on public.fixer_remote_drive_use_20261008
 for each statement execute function public.fixer_remote_drive_use_immutable_20261008();
-- No runtime direct DML. Receipts stay permanent across denial, hide and swap.
alter table public.fixer_remote_drive_use_20261008 enable row level security;
alter table public.fixer_remote_drive_use_control_20261008 enable row level security;
create function public.fixer_remote_drive_use_receipt_20261008(p_request jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.fixer_remote_drive_use_20261008%rowtype;
begin
 perform public.fixer_inventory_mutator_check_private_20261008();
 select * into r from public.fixer_remote_drive_use_20261008 where use_id=(p_request->>'use_id')::uuid;
 if not found then raise exception 'remote drive outcome unresolved' using errcode='55000'; end if;
 if r.request is distinct from p_request then
  raise exception 'immutable remote drive request conflict' using errcode='23514'; end if;
 return r.receipt;
end $$;
create function public.fixer_remote_drive_use_apply_20261008(p_request jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare a public.media_asset%rowtype; s public.media_source%rowtype;
 prior public.fixer_remote_drive_use_20261008%rowtype; result jsonb; epoch uuid; uid uuid;
begin
 perform public.fixer_inventory_mutator_check_private_20261008();
 perform public.fixer_inventory_protocol_lock_private_20261008();
 if jsonb_typeof(p_request) is distinct from 'object'
  or (select array_agg(k order by k) from jsonb_object_keys(p_request) k)
   is distinct from array['asset_before','asset_id','content_hash','epoch_id','gym_id','post_date','source_before','source_id','use_id']::text[] then
  raise exception 'exact remote drive request shape required' using errcode='23514'; end if;
 uid:=(p_request->>'use_id')::uuid;
 if uid is null or p_request->>'gym_id' is null or p_request->>'gym_id' !~ '^[a-z0-9][a-z0-9_-]{0,127}$'
  or p_request->>'gym_id' ~ '_(ig|fb|gbp)$'
  or coalesce(length(btrim(p_request->>'content_hash')),0)=0
  or (p_request->>'post_date')::date is null then
  raise exception 'exact remote drive binding required' using errcode='23514'; end if;
 -- Replay returns original receipt even after later row changes/disablement.
 select * into prior from public.fixer_remote_drive_use_20261008 where use_id=uid;
 if found then return public.fixer_remote_drive_use_receipt_20261008(p_request); end if;
 select epoch_id into epoch from public.fixer_still_cutover_20261007 where singleton and enabled;
 if epoch is null or epoch is distinct from (p_request->>'epoch_id')::uuid
  or not coalesce((select enabled from public.fixer_remote_drive_use_control_20261008 where singleton),false) then
  raise exception 'remote drive authority disabled or stale epoch' using errcode='55000'; end if;
 -- Graph/census entry precedes row locks. Lock order source then asset.
 select * into s from public.media_source where id=p_request->>'source_id' for update;
 if not found or s.gym_id is distinct from p_request->>'gym_id' or not coalesce(s.active,false)
  or s.kind is distinct from 'gym_drive' or to_jsonb(s) is distinct from p_request->'source_before' then
  raise exception 'remote drive source CAS conflict' using errcode='23514'; end if;
 select * into a from public.media_asset where id=p_request->>'asset_id' for update;
 if not found or a.gym_id is distinct from s.gym_id or a.source_id is distinct from s.id
  or a.content_hash is distinct from p_request->>'content_hash'
  or to_jsonb(a) is distinct from p_request->'asset_before'
  or a.used_count is distinct from 0 or a.last_used_at is not null
  or a.eligible is distinct from true or a.excluded_by_coach is distinct from false then
  raise exception 'remote drive asset CAS conflict' using errcode='23514'; end if;
 -- This is a consumption stamp, NEVER an eligibility/global-history certificate.
 update public.media_asset set used_count=1,last_used_at=clock_timestamp() where id=a.id returning * into a;
 result:=jsonb_build_object('state','applied','request',p_request,'asset_after',to_jsonb(a),'source_after',to_jsonb(s));
 insert into public.fixer_remote_drive_use_20261008(use_id,request,receipt) values(uid,p_request,result);
 return result;
end $$;
-- Default ACLs may grant mutator or arbitrarily inherited roles raw access.
-- Scrub every non-owner grantee on these new objects, not just known roles.
do $$ declare obj record; grantee record; target text; begin
 for obj in select oid,relname,relowner,relkind,relacl from pg_class
  where oid=any(array['public.fixer_remote_drive_use_20261008'::regclass,
   'public.fixer_remote_drive_use_control_20261008'::regclass,
   'public.fixer_remote_drive_version_20261008'::regclass]) loop
  target:=case when obj.relkind='S' then 'sequence' else 'table' end;
  for grantee in select distinct a.grantee from aclexplode(coalesce(obj.relacl,
   acldefault(case when obj.relkind='S' then 'S'::"char" else 'r'::"char" end,obj.relowner))) a
   where a.grantee<>obj.relowner loop
   execute format('revoke all on %s public.%I from %s cascade',target,obj.relname,
    case when grantee.grantee=0 then 'public' else quote_ident(pg_get_userbyid(grantee.grantee)) end);
  end loop;
 end loop;
 -- Functions can inherit default EXECUTE grants too, including private helpers.
 for obj in select oid,proowner,proacl from pg_proc where oid=any(array[
  'public.fixer_remote_drive_version_private_20261008()'::regprocedure,
  'public.fixer_remote_drive_use_immutable_20261008()'::regprocedure,
  'public.fixer_remote_drive_use_apply_20261008(jsonb)'::regprocedure,
  'public.fixer_remote_drive_use_receipt_20261008(jsonb)'::regprocedure]) loop
  for grantee in select distinct a.grantee from aclexplode(coalesce(obj.proacl,acldefault('f',obj.proowner))) a
   where a.grantee<>obj.proowner loop
   execute format('revoke all on function %s from %s cascade',obj.oid::regprocedure,
    case when grantee.grantee=0 then 'public' else quote_ident(pg_get_userbyid(grantee.grantee)) end);
  end loop;
 end loop;
end $$;
grant execute on function public.fixer_remote_drive_use_apply_20261008(jsonb),public.fixer_remote_drive_use_receipt_20261008(jsonb)
 to fixer_inventory_mutator_20261008;
-- Owner-role inheritance cannot be repaired by REVOKE. Refuse installation
-- if any non-superuser mutator member retains effective raw access.
do $$ declare principal record; target text; begin
 for principal in select oid,rolname from pg_roles where not rolsuper
  and pg_has_role(oid,'fixer_inventory_mutator_20261008','member') loop
  foreach target in array array['public.fixer_remote_drive_use_20261008',
   'public.fixer_remote_drive_use_control_20261008'] loop
   if has_table_privilege(principal.oid,target,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
    or has_any_column_privilege(principal.oid,target,'SELECT,INSERT,UPDATE,REFERENCES') then
    raise exception 'remote drive runtime retains raw table authority' using errcode='42501'; end if;
  end loop;
  if has_sequence_privilege(principal.oid,'public.fixer_remote_drive_version_20261008','USAGE,SELECT,UPDATE')
   or has_function_privilege(principal.oid,'public.fixer_remote_drive_version_private_20261008()','EXECUTE')
   or has_function_privilege(principal.oid,'public.fixer_remote_drive_use_immutable_20261008()','EXECUTE') then
   raise exception 'remote drive runtime retains private authority' using errcode='42501'; end if;
 end loop;
end $$;
commit;
