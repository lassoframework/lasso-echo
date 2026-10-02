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

-- ---------------------------------------------------------------------------
-- 0. Canonical tenant alias identity (preflight: work/media-tenant-alias-preflight-20261002)
-- ---------------------------------------------------------------------------
-- Every internal visual-group table (groups, aliases, events, ledger,
-- siblings, settings, reconciliation) is keyed by the CANONICAL tenant UUID,
-- never by a raw content_calendar.gym_id alias key. A raw calendar key is
-- resolved exactly once at the trust boundary via public.visual_group_tenant_id.
-- Unmapped keys (e.g. retired key zz-retired-20260904-f574c06c, 42 rows)
-- resolve to NULL: they stay unarmed and can never mint groups, ledger rows
-- or events. One alias row per key; re-binding a key to a different tenant
-- raises. Old and current alias keys of one real tenant share one canonical
-- identity, so their date-conflict authority is shared and unrelated tenants
-- remain isolated. Bindings are immutable (identity trigger below).
create table if not exists public.tenant_alias (
  alias_key  text        primary key,
  tenant_id  uuid        not null,
  created_at timestamptz not null default now()
);

comment on table public.tenant_alias is
  'Service-role-only calendar alias key -> canonical tenant UUID registry. One row per alias key; several keys may share one tenant UUID. Immutable once written.';

-- Fail-closed resolver. Returns the canonical tenant UUID for a raw calendar
-- key, or NULL when the key is unmapped. Never raises for an unknown key;
-- callers must treat NULL as held/unarmed. Blank keys are invalid input.
create or replace function public.visual_group_tenant_id(p_gym_id text)
returns uuid language plpgsql stable security definer set search_path = public as $$
declare v_tenant uuid;
begin
  -- NULL/blank keys also fail closed (NULL): identity helpers may see a NULL
  -- canonical key after the trigger boundary maps an unmapped raw key.
  if nullif(btrim(p_gym_id), '') is null then
    return null;
  end if;
  select tenant_id into v_tenant from public.tenant_alias where alias_key = btrim(p_gym_id);
  return v_tenant;
end;
$$;

-- Strict resolver for mutation paths that must never silently no-op or mint
-- under a raw alias: unmapped keys raise instead of returning NULL.
create or replace function public.visual_group_tenant_strict(p_gym_id text)
returns uuid language plpgsql stable security definer set search_path = public as $$
declare v_tenant uuid;
begin
  v_tenant := public.visual_group_tenant_id(p_gym_id);
  if v_tenant is null then
    raise exception 'calendar key % has no canonical tenant mapping; refusing to mint or mutate visual ledger identity', p_gym_id
      using errcode = 'P0002';
  end if;
  return v_tenant;
end;
$$;

-- Service-role registration. Advisory-locked, idempotent for the same tenant,
-- raises on attempted re-bind to a different tenant (ambiguity fails closed).
-- Seeds the tenant UUID as its own alias key so canonical pass-through works.
create or replace function public.visual_group_tenant_register(p_alias_key text, p_tenant_id uuid)
returns uuid language plpgsql security definer set search_path = public as $$
declare v_existing uuid; v_uuid uuid; v_lock_key text;
begin
  if nullif(btrim(p_alias_key), '') is null or p_tenant_id is null then
    raise exception 'invalid tenant alias registration' using errcode = '22023';
  end if;
  -- A UUID-shaped alias names its own tenant. Never allow a second tenant to
  -- claim another tenant's canonical pass-through key, even before that
  -- tenant has registered any other alias.
  begin
    v_uuid := btrim(p_alias_key)::uuid;
    if v_uuid <> p_tenant_id then
      raise exception 'canonical tenant key % cannot belong to another tenant', p_alias_key
        using errcode = '23514';
    end if;
  exception when invalid_text_representation then
    null;
  end;
  -- Registration may insert two keys. Lock both in one stable order so
  -- concurrent registrations cannot deadlock or race the self-alias check.
  for v_lock_key in
    select distinct key from unnest(array[btrim(p_alias_key), p_tenant_id::text]) as keys(key)
      order by key
  loop
    perform pg_advisory_xact_lock(hashtextextended(
      jsonb_build_array('tenant_alias', v_lock_key)::text, 0));
  end loop;
  select tenant_id into v_existing from public.tenant_alias where alias_key = btrim(p_alias_key);
  if found then
    if v_existing <> p_tenant_id then
      raise exception 'tenant alias % is already bound to another canonical tenant', p_alias_key
        using errcode = '23514';
    end if;
  else
    insert into public.tenant_alias(alias_key, tenant_id) values (btrim(p_alias_key), p_tenant_id);
  end if;
  insert into public.tenant_alias(alias_key, tenant_id) values (p_tenant_id::text, p_tenant_id)
    on conflict do nothing;
  select tenant_id into v_existing from public.tenant_alias where alias_key = p_tenant_id::text;
  if v_existing is distinct from p_tenant_id then
    raise exception 'canonical tenant key % is bound to another tenant', p_tenant_id
      using errcode = '23514';
  end if;
  return p_tenant_id;
end;
$$;

-- An unmapped key can never be armed: enforcement rows require a known
-- canonical tenant. Unarmed (enforce=false) rows stay permitted so a key can
-- be explicitly recorded as off, but arming an unmapped/unknown key raises.
create or replace function public.visual_group_settings_arm_guard()
returns trigger language plpgsql set search_path = public as $$
begin
  -- Armed rows must be keyed by the canonical tenant UUID itself; a raw alias
  -- key or an unmapped key can never arm the guard.
  if new.enforce and (public.visual_group_tenant_id(new.gym_id) is null
      or new.gym_id <> public.visual_group_tenant_id(new.gym_id)::text) then
    raise exception 'unmapped calendar key cannot arm the visual guard' using errcode = '23514';
  end if;
  return new;
end;
$$;

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

-- Owner-written immutable reconciliation receipts. Service role may read,
-- but cannot INSERT/UPDATE/DELETE; only the validated RPC writes receipts.
create table if not exists public.visual_group_reconciliation (
  id uuid primary key default gen_random_uuid(), gym_id text not null,
  calendar_row_id uuid not null,
  outcome text not null check(outcome in ('confirmed_not_sent','confirmed_published')),
  delivered_group_key text, reserved_groups text[] not null,
  attempts jsonb not null, hold_event_ids bigint[] not null,
  original_claim jsonb not null, evidence jsonb not null, actor text not null,
  transaction_id bigint not null default txid_current(),
  created_at timestamptz not null default now(),
  unique(gym_id,calendar_row_id,evidence)
);
alter table public.visual_group_reconciliation enable row level security;
drop policy if exists visual_group_reconciliation_service_read on public.visual_group_reconciliation;
create policy visual_group_reconciliation_service_read on public.visual_group_reconciliation
  for select to service_role using(true);
revoke all on public.visual_group_reconciliation from public,anon,authenticated,service_role;
grant select on public.visual_group_reconciliation to service_role;

create table if not exists public.gym_visual_guard_settings (
  gym_id     text        primary key,
  enforce    boolean     not null default false,
  updated_at timestamptz not null default now()
);

comment on table public.gym_visual_guard_settings is
  'Per-gym enforcement switch for the visual-group guard. Default OFF; a missing row means OFF.';

-- Armed only for registered canonical tenants; unmapped keys stay unarmed.
drop trigger if exists gym_visual_guard_settings_arm_guard on public.gym_visual_guard_settings;
create trigger gym_visual_guard_settings_arm_guard before insert or update
  on public.gym_visual_guard_settings
  for each row execute function public.visual_group_settings_arm_guard();

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
  if old.ambiguous and tg_op='UPDATE' and
      (new.gym_id<>old.gym_id or new.group_key<>old.group_key or new.reserved_date is distinct from old.reserved_date) then
    raise exception 'ambiguous reservation identity/date is immutable' using errcode='23514';
  end if;
  if old.ambiguous and (tg_op='DELETE' or
      ((not new.ambiguous or new.state='released') and
        not public.visual_group_group_reconciled(old.gym_id,old.group_key))) then
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

alter table public.tenant_alias enable row level security;
drop policy if exists tenant_alias_service_role on public.tenant_alias;
create policy tenant_alias_service_role on public.tenant_alias
  for all to service_role using (true) with check (true);

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
  -- Canonical tenant identity: raw calendar keys never key internal tables.
  -- Unmapped keys raise here and can never mint a group or alias.
  p_gym_id := public.visual_group_tenant_strict(p_gym_id)::text;
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
  p_gym_id := public.visual_group_tenant_strict(p_gym_id)::text;
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
  p_gym_id := public.visual_group_tenant_strict(p_gym_id)::text;
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
  foreach t in array array['visual_group','visual_group_alias','visual_group_member_event','visual_group_reconciliation','tenant_alias'] loop
    execute format('drop trigger if exists visual_group_identity_immutable on public.%I',t);
    execute format('create trigger visual_group_identity_immutable before update or delete on public.%I for each row execute function public.visual_group_block_identity_mutation()',t);
  end loop;
end;
$$;

revoke all on function public.visual_group_tenant_id(text) from public,anon,authenticated;
grant execute on function public.visual_group_tenant_id(text) to service_role;
revoke all on function public.visual_group_tenant_strict(text) from public,anon,authenticated;
grant execute on function public.visual_group_tenant_strict(text) to service_role;
revoke all on function public.visual_group_tenant_register(text,uuid) from public,anon,authenticated;
grant execute on function public.visual_group_tenant_register(text,uuid) to service_role;
revoke all on function public.visual_group_settings_arm_guard() from public,anon,authenticated;
revoke all on public.tenant_alias from public,anon,authenticated,service_role;
-- All writes pass through visual_group_tenant_register, which verifies both
-- the raw key and the canonical UUID self-key under ordered locks.
grant select on public.tenant_alias to service_role;
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
-- Service-role callers use the SECURITY DEFINER registration, backfill,
-- reconciliation and (future) guarded activation RPCs. Direct table writes
-- could forge aliases, receipts or a published usage without provider proof;
-- direct settings writes could arm a tenant before coverage is complete.
grant select on public.visual_group, public.visual_group_alias,
  public.visual_group_member_event, public.visual_group_usage_ledger,
  public.gym_visual_guard_settings to service_role;
revoke all on sequence public.visual_group_member_event_id_seq from public,anon,authenticated,service_role;
grant usage,select on sequence public.visual_group_member_event_id_seq to service_role;
