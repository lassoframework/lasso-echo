-- DRAFT / UNAPPLIED / migration-last after the complete staged + still-v2 stack.
-- No activation. Independent guard defaults OFF, including on installations
-- whose older reservation gate is already armed. No caller-set GUC is authority.
begin;
do $$ begin
 if current_user <> 'postgres' then raise exception 'migration requires postgres'; end if;
 if to_regprocedure('public.fixer_prospective_conflict_fence_20261008(uuid,boolean)') is null
   or to_regprocedure('public.finalize_forward_schedule_staged_batch_20261008(text,uuid,jsonb,jsonb)') is null then
  raise exception 'complete staged and prospective still v2 stack required'; end if;
end $$;
create table public.fixer_calendar_admission_gate_20261009 (
 singleton boolean primary key default true check(singleton), enabled boolean not null default false
);
insert into public.fixer_calendar_admission_gate_20261009 values(true,false);
create table public.fixer_calendar_admission_capability_20261009 (
 capability_id uuid primary key, backend_pid integer not null, transaction_id xid8 not null,
 mode text not null check(mode in ('finalize','prepare')), row_ids uuid[] not null
);
revoke all on public.fixer_calendar_admission_gate_20261009,
 public.fixer_calendar_admission_capability_20261009 from public,anon,authenticated,service_role,
 fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006,
 fixer_forward_media_photo_auditor_20261007;
alter table public.fixer_calendar_admission_gate_20261009 enable row level security;
alter table public.fixer_calendar_admission_capability_20261009 enable row level security;
grant select on public.fixer_calendar_admission_gate_20261009 to service_role;
create policy service_read on public.fixer_calendar_admission_gate_20261009 for select to service_role using(true);

-- Keep original OIDs, signatures, config and exact ACLs on public entries. Private copied
-- bodies keep the entire prior proof stack. A capability lives only around its
-- exact trusted call, is bound to backend + xid + request row IDs, and is
-- removed before return; SQL exception rollback removes it as well. Nested
-- finalizers have independent capabilities. Old postgres-owned functions do
-- not receive one simply because they have the same owner.
do $wrap$
declare f record; acl record; definition text; args text; body text;
 internal_name text; scope text; mode text;
begin
 for f in select p.*, pg_get_functiondef(p.oid) as definition,
   pg_get_function_identity_arguments(p.oid) as identity_args,
   pg_get_function_arguments(p.oid) as declaration_args,
   p.prorettype::regtype::text as result_type
  from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname=any(array[
   'finalize_forward_schedule_batch_20261008','finalize_forward_schedule_staged_batch_20261008',
   'fixer_bind_forward_media_manifest_20261006','fixer_bind_forward_schedule_staged_manifest_20261008'])
 loop
  if not f.prosecdef or f.proowner <> 'postgres'::regrole or f.result_type not in ('jsonb','boolean')
    or f.prolang <> (select oid from pg_language where lanname='plpgsql')
    or exists(select 1 from aclexplode(coalesce(f.proacl,acldefault('f',f.proowner))) a where a.grantee=0) then
   raise exception 'trusted admission entry shape drift: %',f.proname; end if;
  internal_name:='fixer_admission_body_'||f.oid::text;
  select string_agg(quote_ident(a),',') into args from unnest(f.proargnames) a;
  mode:=case when f.proname like 'finalize%' then 'finalize' else 'prepare' end;
  scope:=case when mode='finalize' then
   'array(select (x->>''calendar_row_id'')::uuid from jsonb_array_elements(p_candidates) x) || array(select (x->>''id'')::uuid from jsonb_array_elements(p_expected_old_rows) x)'
   else 'array[p_calendar_row_id]' end;
  -- Clone the complete definition, retaining security/volatility/config and
  -- frozen body bytes. CREATE (not OR REPLACE) refuses an existing body name.
  definition:=replace(f.definition,
    format('CREATE OR REPLACE FUNCTION public.%I(',f.proname),
    format('CREATE FUNCTION public.%I(',internal_name));
  if definition=f.definition then raise exception 'trusted entry header drift: %',f.proname; end if;
  execute definition;
  -- Only the new private copy receives default creation grants. Remove every
  -- nonowner grantee before publishing the wrapper; original OID and ACL
  -- grantor chains are never renamed, revoked or reconstructed.
  execute format('revoke all on function public.%I(%s) from public',internal_name,f.identity_args);
  for acl in select a.* from pg_proc p join pg_namespace n on n.oid=p.pronamespace
   cross join lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
   where n.nspname='public' and p.proname=internal_name and p.proargtypes=f.proargtypes
  loop
   if acl.grantee<>0 and acl.grantee<>f.proowner then
    execute format('revoke all on function public.%I(%s) from %I',internal_name,f.identity_args,pg_get_userbyid(acl.grantee));
   end if;
  end loop;
  body:=format($b$declare cap uuid:=gen_random_uuid(); result %s;
   begin
    insert into public.fixer_calendar_admission_capability_20261009 values
      (cap,pg_backend_pid(),pg_current_xact_id(),%L,%s);
    result:=public.%I(%s);
    delete from public.fixer_calendar_admission_capability_20261009 where capability_id=cap;
    return result;
   end;$b$,f.result_type,mode,scope,internal_name,args);
  if f.proname='finalize_forward_schedule_batch_20261008' then
   body:=format($b$begin
    if exists(select 1 from public.fixer_calendar_admission_gate_20261009 where singleton and enabled) then
     return public.finalize_forward_schedule_staged_batch_20261008(p_tenant_id,
      (select batch_id from public.forward_schedule_stage_member_20261008
       where calendar_row_id=(p_candidates->0->>'calendar_row_id')::uuid),
      p_candidates,p_expected_old_rows);
    end if;
    return public.%I(%s);
   end;$b$,internal_name,args);
  end if;
  -- CREATE OR REPLACE changes only the body of the original function.
  -- Its OID, signature, properties, config, ACL grantors/options and existing
  -- dependent grants remain under PostgreSQL's original authority graph.
  execute replace(f.definition,f.prosrc,body);
 end loop;
 if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname like 'fixer_admission_body_%') <> 4 then
  raise exception 'all four exact trusted entries required'; end if;
end $wrap$;

create function public.fixer_calendar_admission_lock_20261009()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform pg_advisory_xact_lock_shared(hashtextextended('fixer_forward_graph_20261006',0));
 if exists(select 1 from public.fixer_calendar_admission_gate_20261009 where singleton and enabled) then
  if current_setting('transaction_isolation') <> 'read committed' then
   raise exception 'calendar admission requires read committed' using errcode='25000'; end if;
  perform pg_advisory_xact_lock(hashtextextended('fixer_forward_photo_census_20261007',0));
 end if;
 return null;
end $$;
create function public.fixer_calendar_admission_guard_20261009()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare o jsonb:=to_jsonb(old); n jsonb:=to_jsonb(new); media boolean; finalizing boolean; preparing boolean;
 bound text[]:=array['id','variant_of','caption','gym_id','post_date','logical_post_id','account','format','gbp_location_id','visual_group_key','source_media_asset_id','source_media_url','image_url','thumbnail_url','render_manifest_digest'];
 k text;
begin
 if not exists(select 1 from public.fixer_calendar_admission_gate_20261009 where singleton and enabled) then return coalesce(new,old); end if;
 media:=exists(select 1 from unnest(array['source_media_asset_id','source_media_url','image_url','thumbnail_url','render_manifest_digest']) a
  where nullif(btrim(coalesce(n->>a,'')),'') is not null
   or nullif(btrim(coalesce(o->>a,'')),'') is not null);
 if not media then return coalesce(new,old); end if;
 select coalesce(bool_or(mode='finalize'),false),coalesce(bool_or(mode='prepare'),false) into finalizing,preparing
 from public.fixer_calendar_admission_capability_20261009
 where backend_pid=pg_backend_pid() and transaction_id=pg_current_xact_id()
  and coalesce(new.id,old.id)=any(row_ids);
 if tg_op='INSERT' and new.variant_status='active' then
  raise exception 'active media INSERT requires registered staged finalization' using errcode='42501'; end if;
 if tg_op='DELETE' and old.variant_status='active' then
  raise exception 'active media DELETE requires retained staged replacement' using errcode='42501'; end if;
 if tg_op='UPDATE' then
  if old.variant_status='active' then
   foreach k in array bound loop
    if o->k is distinct from n->k and not (preparing and k='render_manifest_digest' and o->>k is null) then
     raise exception 'active media admission identity immutable: %',k using errcode='42501'; end if;
   end loop;
   if new.variant_status is distinct from 'active' and not (finalizing and new.variant_status='archived'
     and exists(select 1 from public.forward_schedule_stage_old_row_20261008 s
       join public.forward_schedule_stage_batch_20261008 b using(batch_id)
       where s.calendar_row_id=old.id and b.state='staged' and s.old_snapshot=o)) then
    raise exception 'active media retirement requires exact staged finalization' using errcode='42501'; end if;
  elsif new.variant_status='active' then
   if not finalizing or old.variant_status is distinct from 'candidate'
     or old.media_not_ready_reason is distinct from 'forward_reservation_staged'
     or not exists(select 1 from public.forward_schedule_stage_member_20261008 m
       join public.forward_schedule_stage_batch_20261008 b using(batch_id)
       where m.calendar_row_id=old.id and b.state='staged'
        and (m.staged_snapshot-'source_media_asset_id'-'render_manifest_digest')=(o-'source_media_asset_id'-'render_manifest_digest'))
     or (n-'variant_status'-'media_not_ready_reason') is distinct from (o-'variant_status'-'media_not_ready_reason') then
    raise exception 'media activation requires exact registered staged finalization' using errcode='42501'; end if;
   -- Existing finalizers reserve/check lineage, still occupancy, conflicts and
   -- exact revisions after activation in this same transaction. Failure rolls
   -- back activation. A borrowed reservation alone grants no capability.
  end if;
 end if;
 return coalesce(new,old);
end $$;
revoke all on function public.fixer_calendar_admission_lock_20261009(),public.fixer_calendar_admission_guard_20261009()
 from public,anon,authenticated,service_role,fixer_forward_media_owner_20261006,fixer_forward_media_attester_20261006;
create trigger fixer_calendar_admission_lock before insert or update or delete on public.content_calendar
 for each statement execute function public.fixer_calendar_admission_lock_20261009();
create trigger fixer_calendar_admission_guard before insert or update or delete on public.content_calendar
 for each row execute function public.fixer_calendar_admission_guard_20261009();
commit;
