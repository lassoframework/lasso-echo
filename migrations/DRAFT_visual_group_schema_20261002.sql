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

-- Private advisory acquisition for auxiliary RPCs. Normal callers serialize
-- before any calendar/group locks. If a caller already owns ANY calendar
-- relation lock stronger than AccessShareLock (including SELECT FOR UPDATE's
-- RowShareLock), NEVER wait for another transaction's advisory: it may be
-- waiting for our calendar tuple or barrier. Inspect locks, never a GUC.
create or replace function public.visual_group_auxiliary_lock(p_lock_key bigint)
returns void language plpgsql security definer set search_path = public as $$
begin
  if exists(select 1 from pg_locks where pid=pg_backend_pid() and granted
      and locktype='relation' and relation='public.content_calendar'::regclass
      and mode<>'AccessShareLock') then
    if not pg_try_advisory_xact_lock(p_lock_key) then
      raise exception 'auxiliary visual lock busy while holding calendar locks; retry transaction' using errcode='55P03';
    end if;
  else
    perform pg_advisory_xact_lock(p_lock_key);
  end if;
end;
$$;
revoke all on function public.visual_group_auxiliary_lock(bigint) from public,anon,authenticated,service_role;

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
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_tenant',p_tenant_id::text)::text,0));
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
    perform public.visual_group_auxiliary_lock(hashtextextended(
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
  alias_value text        not null check (
    alias_kind <> 'byte_hash' or alias_value ~
      '^(source|derived):(sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})$'),
  group_key   text        not null,
  created_at  timestamptz not null default now(),
  primary key (gym_id, alias_kind, alias_value),
  foreign key (gym_id, group_key)
    references public.visual_group (gym_id, group_key)
);

-- Existing draft databases may predate the inline constraint. Keep legacy
-- rows visible for activation audit, while rejecting every new unnamespaced
-- byte hash immediately; activation below refuses until legacy rows are fixed.
do $$ begin
  if not exists(select 1 from pg_constraint
      where conrelid='public.visual_group_alias'::regclass
        and conname='visual_group_alias_byte_hash_namespace') then
    alter table public.visual_group_alias add constraint visual_group_alias_byte_hash_namespace
      check (alias_kind <> 'byte_hash' or alias_value ~
        '^(source|derived):(sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})$') not valid;
  end if;
end $$;

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
    ('confirmed','rejected','auto_merged','review_hold','scene_linked')),
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
  'One row per (gym, group). Every staged date remains occupied after release; same-date siblings share it. Published usage fields are immutable forever; uncertainty metadata clears only with terminal evidence.';

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
-- A released reservation retains its original date and still blocks reuse on
-- any other date. Releases only change active membership state.

create or replace function public.visual_group_ledger_block_published_mutation()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if tg_op='DELETE' or
      new.gym_id<>old.gym_id or new.group_key<>old.group_key or
      new.reserved_date is distinct from old.reserved_date then
    raise exception 'staged visual identity and date are permanent' using errcode='23514';
  end if;
  if old.ambiguous and (tg_op='DELETE' or
      ((not new.ambiguous or new.state='released') and
        not public.visual_group_group_reconciled(old.gym_id,old.group_key))) then
    raise exception 'ambiguous usage requires evidence-based reconciliation' using errcode='23514';
  end if;
  if old.state = 'published' then
    -- Permanent usage fields never change. The only permitted metadata repair
    -- is clearing uncertainty after every original attempt has terminal proof.
    if tg_op='UPDATE' and old.ambiguous and not new.ambiguous
       and (to_jsonb(new)-'ambiguous') = (to_jsonb(old)-'ambiguous')
       and public.visual_group_group_reconciled(old.gym_id,old.group_key) then
      return new;
    end if;
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
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_tenant',p_gym_id)::text,0));
  if nullif(btrim(p_gym_id), '') is null or nullif(btrim(p_alias_value), '') is null
     or p_alias_kind is null or p_alias_kind not in
       ('source_asset','drive_id','byte_hash','canonical_url','r2_key','manual_scene')
     or (p_group_key is not null and nullif(btrim(p_group_key), '') is null) then
    raise exception 'invalid visual alias' using errcode = '22023';
  end if;
  if p_alias_kind = 'byte_hash' and lower(btrim(p_alias_value)) !~
      '^(source|derived):(sha256:[0-9a-f]{64}|md5:[0-9a-f]{32})$' then
    raise exception 'byte_hash requires an explicit source/derived and sha256/md5 namespace'
      using errcode = '22023';
  end if;
  if p_alias_kind = 'byte_hash' then
    p_alias_value := lower(btrim(p_alias_value));
  end if;
  perform public.visual_group_auxiliary_lock(hashtextextended(
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
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_tenant',p_gym_id)::text,0));
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
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_tenant',p_gym_id)::text,0));
  if nullif(btrim(p_reason),'') is null then raise exception 'reason required'; end if;
  insert into public.visual_group_member_event
    (gym_id,group_key,alias_kind,alias_value,action,actor,reason)
    values(p_gym_id,p_group_key,p_alias_kind,p_alias_value,'rejected',
      coalesce(nullif(btrim(p_actor),''),'system'),p_reason) returning id into v_id;
  return v_id;
end;
$$;


-- ---------------------------------------------------------------------------
-- 5. Manual same-scene union across distinct group keys (DRAFT, service-role)
-- ---------------------------------------------------------------------------
-- A human reviewer may confirm that two DIFFERENT stable group keys (distinct
-- exact files, e.g. the Swift River JCK_6328/JCK_6331 pair at pHash distance
-- 28) show one scene. The union is never inferred from pHash, distance or any
-- automatic heuristic: only this explicit service-role RPC creates links, and
-- it requires non-empty human evidence. Group keys, aliases and published
-- ledger/history stay exactly as they are; a link never reassigns or deletes
-- an alias/group and never rewrites history. The relation is transitive
-- (links form one equivalence component per tenant), same-tenant only,
-- cycle-safe (re-linking an already connected pair is an idempotent no-op),
-- persistent (append-only, immutable below) and idempotent.
create table if not exists public.visual_group_scene_link (
  gym_id      text        not null,
  group_key_a text        not null,
  group_key_b text        not null,
  evidence    jsonb       not null,
  created_by  text        not null,
  created_at  timestamptz not null default now(),
  primary key (gym_id, group_key_a, group_key_b),
  check (group_key_a < group_key_b),
  foreign key (gym_id, group_key_a)
    references public.visual_group (gym_id, group_key),
  foreign key (gym_id, group_key_b)
    references public.visual_group (gym_id, group_key)
);

comment on table public.visual_group_scene_link is
  'Append-only human-confirmed same-scene edges between distinct group keys of one canonical tenant. Normalized (a<b). Never updated or deleted; never created from pHash inference.';

create index if not exists visual_group_scene_link_b_idx
  on public.visual_group_scene_link (gym_id, group_key_b);

-- Transitive same-scene component of one group. Recursive walk with UNION
-- dedup, so cyclic edge sets terminate. Stable identity read; callers that
-- mutate must lock the component's group rows in key order first.
create or replace function public.visual_group_scene_members(p_gym_id text, p_group_key text)
returns setof text language sql stable security definer set search_path = public as $$
  with recursive walk(group_key) as (
    select p_group_key
    union
    select case when l.group_key_a = w.group_key then l.group_key_b else l.group_key_a end
      from public.visual_group_scene_link l
      join walk w on l.gym_id = p_gym_id and w.group_key in (l.group_key_a, l.group_key_b)
  ) select group_key from walk;
$$;

-- Lock complete components BEFORE endpoint/ledger locks. Each statement locks
-- one row, independent of planner order. A concurrent union may have changed
-- the closure while we waited: refuse/retry the entire transaction instead of
-- taking newly discovered (possibly lower) keys with old locks still held.
-- Private implementation helper; input identities are canonical internal keys.
create or replace function public.visual_group_lock_scene_components(p_targets jsonb)
returns void language plpgsql security definer set search_path = public as $$
declare v_members jsonb; v_recheck jsonb; v_item jsonb;
begin
  select coalesce(jsonb_agg(to_jsonb(component_group) order by component_group.gym_id,component_group.group_key),'[]'::jsonb)
    into v_members from (
      select distinct g.gym_id,g.group_key
      from jsonb_to_recordset(p_targets) t(gym_id text,group_key text)
      cross join lateral public.visual_group_scene_members(t.gym_id,t.group_key) m(group_key)
      join public.visual_group g on g.gym_id=t.gym_id and g.group_key=m.group_key
      where t.gym_id is not null and t.group_key is not null) component_group;
  for v_item in select value from jsonb_array_elements(v_members) loop
    perform 1 from public.visual_group
      where gym_id=v_item->>'gym_id' and group_key=v_item->>'group_key' for update;
  end loop;
  select coalesce(jsonb_agg(to_jsonb(component_group) order by component_group.gym_id,component_group.group_key),'[]'::jsonb)
    into v_recheck from (
      select distinct g.gym_id,g.group_key
      from jsonb_to_recordset(p_targets) t(gym_id text,group_key text)
      cross join lateral public.visual_group_scene_members(t.gym_id,t.group_key) m(group_key)
      join public.visual_group g on g.gym_id=t.gym_id and g.group_key=m.group_key
      where t.gym_id is not null and t.group_key is not null) component_group;
  if v_recheck is distinct from v_members then
    raise exception 'scene component changed while locking; retry transaction' using errcode='55P03';
  end if;
end;
$$;
revoke all on function public.visual_group_lock_scene_components(jsonb) from public,anon,authenticated,service_role;

-- Explicit human-confirmed union. Both groups must already belong to THIS
-- canonical tenant (cross-tenant union raises; unknown keys raise). Locks the
-- union of both endpoint components in deterministic key order -- the same
-- (gym_id, group_key) order the calendar guard and swap RPCs use -- so unions
-- serialize with reservations, publishes and concurrent reverse-order unions
-- without deadlock. Existing cross-date usage between the merged components
-- is refused while armed, or reported while OFF (blocking activation);
-- published ledger rows and history are never rewritten.
create or replace function public.visual_group_link_scene(
  p_gym_id text, p_group_a text, p_group_b text, p_evidence jsonb, p_actor text
) returns jsonb language plpgsql security definer set search_path = public as $$
declare v_tenant text; v_a text; v_b text; v_members text[];
  v_connected boolean; v_dates date[]; v_pub text[]; v_amb text[]; v_unknown_date boolean;
begin
  v_tenant := public.visual_group_tenant_strict(p_gym_id)::text;
  perform public.visual_group_auxiliary_lock(hashtextextended(
    jsonb_build_array('visual_tenant',v_tenant)::text,0));
  if nullif(btrim(p_actor), '') is null then
    raise exception 'a named human reviewer is required for a same-scene union' using errcode = '22023';
  end if;
  if p_evidence is null or jsonb_typeof(p_evidence) is distinct from 'object' or p_evidence = '{}'::jsonb then
    raise exception 'human confirmation evidence is required; a scene union is never inferred from pHash distance alone (JCK_6328/JCK_6331 distance 28 needs manual evidence)' using errcode = '22023';
  end if;
  if nullif(btrim(p_group_a), '') is null or nullif(btrim(p_group_b), '') is null
     or btrim(p_group_a) = btrim(p_group_b) then
    raise exception 'two distinct group keys are required' using errcode = '22023';
  end if;
  v_a := least(btrim(p_group_a), btrim(p_group_b));
  v_b := greatest(btrim(p_group_a), btrim(p_group_b));
  -- Same-tenant only: a key registered only under another tenant, or not at
  -- all, can never be linked here.
  if not exists(select 1 from public.visual_group where gym_id = v_tenant and group_key = v_a)
     or not exists(select 1 from public.visual_group where gym_id = v_tenant and group_key = v_b) then
    raise exception 'scene union groups must both be registered under this canonical tenant' using errcode = '23514';
  end if;
  perform public.visual_group_lock_scene_components(jsonb_build_array(
    jsonb_build_object('gym_id',v_tenant,'group_key',v_a),
    jsonb_build_object('gym_id',v_tenant,'group_key',v_b)));
  select array_agg(m order by m) into v_members from (
    select a2.m from public.visual_group_scene_members(v_tenant,v_a) a2(m)
    union select b2.m from public.visual_group_scene_members(v_tenant,v_b) b2(m)) u(m);
  -- Armed tenants must remain claim-safe immediately after a human union.
  -- OFF tenants may link conflicting historical scenes for review/reporting.
  select count(distinct reserved_date)>1, bool_or(reserved_date is null)
    into v_connected,v_unknown_date from public.visual_group_usage_ledger
    where gym_id=v_tenant and group_key=any(v_members);
  if exists(select 1 from public.gym_visual_guard_settings where gym_id=v_tenant and enforce)
     and (v_connected or coalesce(v_unknown_date,false)) then
    raise exception 'armed tenant scene union conflicts with occupied dates; disable and reconcile history first'
      using errcode='23514';
  end if;
  v_connected := exists(select 1 from public.visual_group_scene_members(v_tenant, v_a) m where m = v_b);
  if not v_connected then
    insert into public.visual_group_scene_link (gym_id, group_key_a, group_key_b, evidence, created_by)
      values (v_tenant, v_a, v_b, p_evidence, btrim(p_actor));
    -- Append-only audit: the partner key rides as a manual_scene alias value.
    insert into public.visual_group_member_event
      (gym_id, group_key, alias_kind, alias_value, action, actor, reason)
      values (v_tenant, v_a, 'manual_scene', v_b, 'scene_linked', btrim(p_actor), p_evidence::text);
  end if;
  -- Report, never rewrite: any existing cross-date usage inside the merged
  -- component is returned to the caller and surfaces in the conflict report.
  select array_agg(distinct l.reserved_date order by l.reserved_date),
         array_agg(distinct l.group_key order by l.group_key) filter (where l.state = 'published'),
         array_agg(distinct l.group_key order by l.group_key) filter (where l.ambiguous)
    into v_dates, v_pub, v_amb
    from public.visual_group_usage_ledger l
   where l.gym_id = v_tenant
     and l.group_key in (select m from public.visual_group_scene_members(v_tenant, v_a) m(m));
  return jsonb_build_object(
    'tenant', v_tenant,
    'linked', not v_connected,
    'idempotent', v_connected,
    'component', (select array_agg(m order by m) from public.visual_group_scene_members(v_tenant, v_a) m(m)),
    'cross_date_conflicts', case when coalesce(cardinality(v_dates), 0) > 1 then to_jsonb(v_dates) else '[]'::jsonb end,
    'published_group_keys', coalesce(to_jsonb(v_pub), '[]'::jsonb),
    'ambiguous_group_keys', coalesce(to_jsonb(v_amb), '[]'::jsonb),
    'activation_note', case when coalesce(cardinality(v_dates), 0) > 1
      then 'existing cross-date usage is reported and blocks activation; history is never rewritten' end);
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
  foreach t in array array['visual_group','visual_group_alias','visual_group_member_event','visual_group_reconciliation','tenant_alias','visual_group_scene_link'] loop
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

alter table public.visual_group_scene_link enable row level security;
drop policy if exists visual_group_scene_link_service_role on public.visual_group_scene_link;
create policy visual_group_scene_link_service_role on public.visual_group_scene_link
  for all to service_role using (true) with check (true);
revoke all on public.visual_group_scene_link from public,anon,authenticated,service_role;
-- Links are written only through visual_group_link_scene, which enforces
-- human evidence, same-tenant membership and deterministic component locks.
grant select on public.visual_group_scene_link to service_role;
revoke all on function public.visual_group_scene_members(text,text) from public,anon,authenticated;
grant execute on function public.visual_group_scene_members(text,text) to service_role;
revoke all on function public.visual_group_link_scene(text,text,text,jsonb,text) from public,anon,authenticated;
grant execute on function public.visual_group_link_scene(text,text,text,jsonb,text) to service_role;
