-- DRAFT / UNAPPLIED / DEFAULT OFF. Local scratch milestone only.
-- Apply after the group activation and scene ledger drafts, BEFORE the scene
-- calendar transaction draft. No activation RPC exists in this milestone.
-- TRUE is refused even to service_role/owner SQL: historical source lineage,
-- occupied backfill, coverage proofs and production activation are unresolved.
-- No setting is inserted or armed by applying this file. Missing means OFF.
-- Rollback: use the controlled disarm RPC first. Preserve settings/disarm audit,
-- occupancy, holds, receipts and calendar state. Restore the group-only trigger
-- plus provenance RPCs atomically; do not delete historical evidence or unhold
-- calendar rows. The scratch-only barrier may not be removed for deployment.
begin;
do $$
begin
  if current_database() <> 'echo_scene_ledger_test' or inet_server_addr() is not null then
    raise exception 'SCRATCH ONLY: scene historical backfill and activation remain unresolved';
  end if;
  if to_regprocedure('public.visual_group_enforcement_on(text)') is null
     or to_regprocedure('public.visual_scene_backfill_occupied()') is null then
    raise exception 'Apply group activation and scene ledger before scene authority';
  end if;
end;
$$;

create table public.gym_visual_scene_guard_settings (
  gym_id text primary key,
  enforce boolean not null default false,
  updated_at timestamptz not null default now()
);
comment on table public.gym_visual_scene_guard_settings is
  'DRAFT independent scene authority. Canonical tenant key, default/missing OFF. Direct arming always refused until verified historical occupancy activation exists.';

create table public.visual_scene_disarm_event (
  id uuid primary key default gen_random_uuid(),
  gym_id text not null,
  actor text not null check (nullif(btrim(actor),'') is not null),
  reason text not null check (nullif(btrim(reason),'') is not null),
  was_enforced boolean not null,
  transaction_id bigint not null default txid_current(),
  created_at timestamptz not null default now()
);
create trigger visual_scene_disarm_event_immutable
  before update or delete on public.visual_scene_disarm_event
  for each row execute function public.visual_scene_immutable();
create trigger visual_scene_disarm_event_no_truncate
  before truncate on public.visual_scene_disarm_event
  for each statement execute function public.visual_scene_immutable();

create or replace function public.visual_scene_settings_guard()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  if tg_op in ('DELETE','TRUNCATE') then
    raise exception 'scene settings are retained; use controlled disarm' using errcode='23514';
  end if;
  if public.visual_group_tenant_id(new.gym_id) is null
     or new.gym_id <> public.visual_group_tenant_id(new.gym_id)::text then
    raise exception 'scene setting requires canonical mapped tenant' using errcode='23514';
  end if;
  if new.enforce then
    -- Deliberately no GUC, empty-table exception, owner receipt or caller role
    -- can arm this incomplete milestone. A later verified activation package
    -- must replace this refusal after implementing actual occupied backfill.
    raise exception 'scene activation unavailable: verified historical occupied backfill is required'
      using errcode='0A000';
  end if;
  if tg_op='UPDATE' and old.enforce and not exists(
    select 1 from public.visual_scene_disarm_event e where e.gym_id=old.gym_id
      and e.was_enforced and e.transaction_id=txid_current()
  ) then
    raise exception 'scene disarming requires controlled disarm with audit reason' using errcode='23514';
  end if;
  if tg_op='UPDATE' and new.gym_id is distinct from old.gym_id then
    raise exception 'scene authority cannot change tenant' using errcode='23514';
  end if;
  return new;
end;
$$;
create trigger gym_visual_scene_guard_settings_guard
  before insert or update or delete on public.gym_visual_scene_guard_settings
  for each row execute function public.visual_scene_settings_guard();
create trigger gym_visual_scene_guard_settings_no_truncate
  before truncate on public.gym_visual_scene_guard_settings
  for each statement execute function public.visual_scene_settings_guard();

create or replace function public.visual_scene_enforcement_on(p_gym_id text)
returns boolean language plpgsql volatile security definer set search_path = public as $$
declare v_enforce boolean := false;
begin
  -- Keep this setting locked until the calendar transaction ends. Controlled
  -- disarm takes the calendar writer barrier FIRST, so it cannot race this read
  -- or wait for settings while blocking a calendar writer holding those locks.
  select s.enforce into v_enforce from public.gym_visual_scene_guard_settings s
    where s.gym_id=public.visual_group_tenant_id(p_gym_id)::text for share;
  if coalesce(v_enforce,false) and not public.visual_group_enforcement_on(p_gym_id) then
    raise exception 'scene enforcement requires exact-byte authority' using errcode='23514';
  end if;
  return coalesce(v_enforce,false);
end;
$$;

create or replace function public.visual_scene_disarm_guard(
  p_gym_id text, p_actor text, p_reason text
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_was_enforced boolean; v_event uuid;
begin
  if nullif(btrim(p_actor),'') is null or nullif(btrim(p_reason),'') is null then
    raise exception 'controlled scene disarm requires actor and reason' using errcode='23514';
  end if;
  if current_setting('transaction_isolation') <> 'read committed'
     or current_setting('transaction_read_only') <> 'off' then
    raise exception 'scene disarm requires fresh writable READ COMMITTED transaction' using errcode='25006';
  end if;
  if exists(select 1 from pg_locks where pid=pg_backend_pid() and granted and
      (locktype='advisory' or (locktype='relation'
        and relation='public.content_calendar'::regclass and mode<>'AccessShareLock'))) then
    raise exception 'scene disarm requires no prior advisory or calendar write/row locks' using errcode='25006';
  end if;
  -- Same barrier-first order as group activation. NOWAIT refuses contention
  -- instead of waiting for a queued writer that holds an auxiliary lock.
  lock table public.content_calendar in share row exclusive mode nowait;
  lock table public.gym_visual_scene_guard_settings, public.tenant_alias
    in share row exclusive mode nowait;
  v_tenant:=public.visual_group_tenant_strict(p_gym_id)::text;
  select enforce into v_was_enforced from public.gym_visual_scene_guard_settings
    where gym_id=v_tenant for update nowait;
  v_was_enforced:=coalesce(v_was_enforced,false);
  insert into public.visual_scene_disarm_event(gym_id,actor,reason,was_enforced)
    values(v_tenant,btrim(p_actor),btrim(p_reason),v_was_enforced) returning id into v_event;
  insert into public.gym_visual_scene_guard_settings(gym_id,enforce)
    values(v_tenant,false)
    on conflict(gym_id) do update set enforce=false,updated_at=now();
  return jsonb_build_object('tenant',v_tenant,'enforced',false,
                           'was_enforced',v_was_enforced,'disarm_event',v_event);
end;
$$;

alter table public.gym_visual_scene_guard_settings enable row level security;
alter table public.visual_scene_disarm_event enable row level security;
create policy gym_visual_scene_guard_settings_service_read
  on public.gym_visual_scene_guard_settings for select to service_role using(true);
create policy visual_scene_disarm_event_service_read
  on public.visual_scene_disarm_event for select to service_role using(true);
revoke all on public.gym_visual_scene_guard_settings, public.visual_scene_disarm_event
  from public,anon,authenticated,service_role;
grant select on public.gym_visual_scene_guard_settings, public.visual_scene_disarm_event to service_role;
revoke all on function public.visual_scene_settings_guard() from public,anon,authenticated,service_role;
revoke all on function public.visual_scene_enforcement_on(text) from public,anon,authenticated;
grant execute on function public.visual_scene_enforcement_on(text) to service_role;
revoke all on function public.visual_scene_disarm_guard(text,text,text) from public,anon,authenticated;
grant execute on function public.visual_scene_disarm_guard(text,text,text) to service_role;
commit;
