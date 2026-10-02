-- DRAFT / UNAPPLIED. Stable per-gym visual groups, unique immutable aliases,
-- append-only review evidence, permanent usage ledger and default-OFF settings.
-- No production SQL or activation approval is granted by this file.
-- A published historical row with no knowable date uses NULL reserved_date:
-- it permanently blocks that scene on every dated claim. Active reservations
-- require a real date. Unknown identity reviews may have a NULL group_key;
-- other decision events must reference a known group.
-- Rollback: disable enforcement, drop calendar trigger and helper/RPCs first,
-- then remove added calendar column and tables in FK order. Preserve published
-- usage/history; DROP is not an acceptable data rollback after live activation.

create table if not exists public.visual_group (
  gym_id     text        not null,
  group_key  text        not null,
  created_at timestamptz not null default now(),
  primary key (gym_id, group_key)
);

comment on table public.visual_group is
  'Stable per-gym visual group. group_key (vg_<hex>) never changes when members are added.';

create table if not exists public.visual_group_alias (
  gym_id      text        not null,
  alias_kind  text        not null check (alias_kind in
    ('source_asset','drive_id','byte_hash','canonical_url','r2_key','manual_scene')),
  alias_value text        not null,
  group_key   text        not null,
  created_at  timestamptz not null default now(),
  primary key (gym_id, alias_kind, alias_value),
  foreign key (gym_id, group_key)
    references public.visual_group (gym_id, group_key)
);

comment on table public.visual_group_alias is
  'One alias maps to exactly one visual group per gym; duplicate registration to a different group fails.';

-- alias reverse-lookup helper: all aliases of a group (audit/backfill/reporting)
create index if not exists visual_group_alias_group_idx
  on public.visual_group_alias (gym_id, group_key);

create table if not exists public.visual_group_member_event (
  id          bigint      generated always as identity primary key,
  gym_id      text        not null,
  group_key   text,
  alias_kind  text,
  alias_value text,
  action      text        not null check (action in
    ('confirmed','rejected','auto_merged','review_hold')),
  actor       text        not null default 'system',
  check (group_key is not null or action = 'review_hold'),
  reason      text,
  created_at  timestamptz not null default now(),
  foreign key (gym_id, group_key)
    references public.visual_group (gym_id, group_key)
);

comment on table public.visual_group_member_event is
  'Persistent append-only audit of member confirmations/rejections/auto-merges/review holds. Never update or delete rows.';

create index if not exists visual_group_member_event_group_idx
  on public.visual_group_member_event (gym_id, group_key, created_at desc);

create index if not exists visual_group_member_event_alias_idx
  on public.visual_group_member_event(gym_id,alias_kind,alias_value,id desc);

create table if not exists public.visual_group_usage_ledger (
  gym_id          text        not null,
  group_key       text        not null,
  reserved_date   date,
  check (reserved_date is not null or state = 'published'),
  calendar_row_id uuid,
  channel         text,
  state           text        not null default 'reserved' check (state in
    ('reserved','published','released')),
  reserved_at     timestamptz not null default now(),
  published_at    timestamptz,
  released_at     timestamptz,
  ambiguous       boolean not null default false,
  primary key (gym_id, group_key),
  foreign key (gym_id, group_key)
    references public.visual_group (gym_id, group_key)
);

-- Sticky uncertainty is never cleared by ordinary calendar writes.
alter table public.visual_group_usage_ledger add column if not exists ambiguous boolean not null default false;

comment on table public.visual_group_usage_ledger is
  'One row per (gym, group). Same-date IG/FB/Story/GBP siblings share it; a different date while state<>''released'' is rejected; state=''published'' rows are immutable forever.';

-- backfill/coverage scans: which rows of the ledger sit on a given date/state
create index if not exists visual_group_usage_ledger_date_idx
  on public.visual_group_usage_ledger (gym_id, reserved_date, state);

create table if not exists public.gym_visual_guard_settings (
  gym_id     text        primary key,
  enforce    boolean     not null default false,
  updated_at timestamptz not null default now()
);

comment on table public.gym_visual_guard_settings is
  'Per-gym enforcement switch for the visual-group guard. Default OFF; a missing row means OFF.';

-- ---------------------------------------------------------------------------
-- 2. content_calendar.visual_group_key (nullable, no default, no backfill)
-- ---------------------------------------------------------------------------

alter table public.content_calendar
  add column if not exists visual_group_key text;

comment on column public.content_calendar.visual_group_key is
  'Resolved visual group for this row''s media. Nullable; populated by registration/backfill only. Read by the guard trigger only when gym_visual_guard_settings.enforce is true.';

create index if not exists content_calendar_visual_group_key_idx
  on public.content_calendar (gym_id, visual_group_key)
  where visual_group_key is not null;

-- ---------------------------------------------------------------------------
-- 3. Published ledger rows are permanent
-- ---------------------------------------------------------------------------
-- A confirmed provider publish makes the group permanently used, including
-- after the calendar row is deleted or edited. Block UPDATE and DELETE of any
-- ledger row whose state is 'published' -- no exceptions at the SQL layer.
-- Releases only ever apply to 'reserved' rows (state transition to 'released'),
-- which remains permitted.

create or replace function public.visual_group_ledger_block_published_mutation()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if old.ambiguous and (tg_op='DELETE' or not new.ambiguous or new.state='released') then
    raise exception 'ambiguous usage requires evidence-based reconciliation' using errcode='23514';
  end if;
  if old.state = 'published' then
    raise exception
      'visual_group_usage_ledger published row is immutable (gym_id=%, group_key=%)',
      old.gym_id, old.group_key
      using errcode = 'raise_exception';
  end if;
  if tg_op = 'UPDATE' and new.state = 'published' and old.state = 'published' then
    raise exception
      'visual_group_usage_ledger published row is immutable (gym_id=%, group_key=%)',
      old.gym_id, old.group_key
      using errcode = 'raise_exception';
  end if;
  return coalesce(new, old);
end;
$$;

drop trigger if exists visual_group_usage_ledger_published_immutable
  on public.visual_group_usage_ledger;
create trigger visual_group_usage_ledger_published_immutable
  before update or delete on public.visual_group_usage_ledger
  for each row execute function public.visual_group_ledger_block_published_mutation();

-- ---------------------------------------------------------------------------
-- 4. Row level security: service-role only on every new table
-- ---------------------------------------------------------------------------

alter table public.visual_group enable row level security;
alter table public.visual_group_alias enable row level security;
alter table public.visual_group_member_event enable row level security;
alter table public.visual_group_usage_ledger enable row level security;
alter table public.gym_visual_guard_settings enable row level security;

drop policy if exists visual_group_service_role on public.visual_group;
create policy visual_group_service_role on public.visual_group
  for all to service_role using (true) with check (true);

drop policy if exists visual_group_alias_service_role on public.visual_group_alias;
create policy visual_group_alias_service_role on public.visual_group_alias
  for all to service_role using (true) with check (true);

drop policy if exists visual_group_member_event_service_role on public.visual_group_member_event;
create policy visual_group_member_event_service_role on public.visual_group_member_event
  for all to service_role using (true) with check (true);

drop policy if exists visual_group_usage_ledger_service_role on public.visual_group_usage_ledger;
create policy visual_group_usage_ledger_service_role on public.visual_group_usage_ledger
  for all to service_role using (true) with check (true);

drop policy if exists gym_visual_guard_settings_service_role on public.gym_visual_guard_settings;
create policy gym_visual_guard_settings_service_role on public.gym_visual_guard_settings
  for all to service_role using (true) with check (true);

-- ---------------------------------------------------------------------------
-- 5. RPCs (service_role only)
-- ---------------------------------------------------------------------------

-- Alias registration serializes before creating a group. The advisory key
-- covers absent aliases; colliding hashes only reduce concurrency, never safety.
create or replace function public.visual_group_register_alias(
  p_gym_id text, p_alias_kind text, p_alias_value text,
  p_group_key text default null
) returns text language plpgsql security definer set search_path = public
as $$
declare v_group text;
begin
  if nullif(btrim(p_gym_id), '') is null or nullif(btrim(p_alias_value), '') is null
     or p_alias_kind is null or p_alias_kind not in
       ('source_asset','drive_id','byte_hash','canonical_url','r2_key','manual_scene')
     or (p_group_key is not null and nullif(btrim(p_group_key), '') is null) then
    raise exception 'invalid visual alias' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(
    jsonb_build_array('visual_alias',p_gym_id,p_alias_kind,p_alias_value)::text, 0));
  select group_key into v_group from public.visual_group_alias
    where gym_id=p_gym_id and alias_kind=p_alias_kind and alias_value=p_alias_value;
  if found then
    if p_group_key is not null and p_group_key <> v_group then
      raise exception 'visual alias already bound to another group' using errcode='23514';
    end if;
    return v_group;
  end if;
  v_group := coalesce(p_group_key, 'vg_' || replace(gen_random_uuid()::text,'-',''));
  insert into public.visual_group(gym_id,group_key) values(p_gym_id,v_group)
    on conflict do nothing;
  insert into public.visual_group_alias(gym_id,alias_kind,alias_value,group_key)
    values(p_gym_id,p_alias_kind,p_alias_value,v_group);
  return v_group;
end;
$$;

create or replace function public.visual_group_confirm(
  p_gym_id text, p_group_key text, p_alias_kind text, p_alias_value text, p_actor text
) returns bigint language plpgsql security definer set search_path = public
as $$
declare v_id bigint;
begin
  perform public.visual_group_register_alias(p_gym_id,p_alias_kind,p_alias_value,p_group_key);
  insert into public.visual_group_member_event
    (gym_id,group_key,alias_kind,alias_value,action,actor)
    values(p_gym_id,p_group_key,p_alias_kind,p_alias_value,'confirmed',
      coalesce(nullif(btrim(p_actor),''),'system')) returning id into v_id;
  return v_id;
end;
$$;

create or replace function public.visual_group_reject(
  p_gym_id text, p_group_key text, p_alias_kind text, p_alias_value text,
  p_actor text, p_reason text
) returns bigint language plpgsql security definer set search_path = public
as $$
declare v_id bigint;
begin
  if nullif(btrim(p_reason),'') is null then raise exception 'reason required'; end if;
  insert into public.visual_group_member_event
    (gym_id,group_key,alias_kind,alias_value,action,actor,reason)
    values(p_gym_id,p_group_key,p_alias_kind,p_alias_value,'rejected',
      coalesce(nullif(btrim(p_actor),''),'system'),p_reason) returning id into v_id;
  return v_id;
end;
$$;

-- Group and alias identity bindings, and decision history, are immutable.
-- Registration adds members; no UPDATE can silently retarget an existing alias.
create or replace function public.visual_group_block_identity_mutation()
returns trigger language plpgsql set search_path = public as $$
begin raise exception 'visual identity and decision history are immutable' using errcode='23514'; end;
$$;
do $$
declare t text;
begin
  foreach t in array array['visual_group','visual_group_alias','visual_group_member_event'] loop
    execute format('drop trigger if exists visual_group_identity_immutable on public.%I',t);
    execute format('create trigger visual_group_identity_immutable before update or delete on public.%I for each row execute function public.visual_group_block_identity_mutation()',t);
  end loop;
end;
$$;

revoke all on function public.visual_group_register_alias(text,text,text,text) from public,anon,authenticated;
grant execute on function public.visual_group_register_alias(text,text,text,text) to service_role;
revoke all on function public.visual_group_confirm(text,text,text,text,text) from public,anon,authenticated;
grant execute on function public.visual_group_confirm(text,text,text,text,text) to service_role;
revoke all on function public.visual_group_reject(text,text,text,text,text,text) from public,anon,authenticated;
grant execute on function public.visual_group_reject(text,text,text,text,text,text) to service_role;
revoke all on function public.visual_group_ledger_block_published_mutation() from public,anon,authenticated;
revoke all on function public.visual_group_block_identity_mutation() from public,anon,authenticated;
revoke all on public.visual_group, public.visual_group_alias, public.visual_group_member_event,
  public.visual_group_usage_ledger, public.gym_visual_guard_settings from public,anon,authenticated,service_role;
grant select,insert,update,delete on public.visual_group, public.visual_group_alias,
  public.visual_group_member_event, public.visual_group_usage_ledger,
  public.gym_visual_guard_settings to service_role;
revoke all on sequence public.visual_group_member_event_id_seq from public,anon,authenticated,service_role;
grant usage,select on sequence public.visual_group_member_event_id_seq to service_role;
