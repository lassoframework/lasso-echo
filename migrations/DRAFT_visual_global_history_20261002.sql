-- DRAFT / UNAPPLIED. Global exact-byte visual authority and historical import.
-- Apply after schema, claim trigger and backfill drafts, BEFORE the activation
-- draft (which must be last). This does not arm any guard.
-- All writes below are additive. Rollback before activation: DROP these RPCs
-- and tables in reverse dependency order. After a confirmed publish, preserve
-- visual_global_usage and visual_global_usage_member as permanent history.
begin;
lock table public.gym_visual_guard_settings in share row exclusive mode;
do $$ begin
  if exists(select 1 from public.gym_visual_guard_settings where enforce) then
    raise exception 'global draft cannot be installed while a tenant-local visual guard is armed'
      using errcode='23514';
  end if;
end $$;

-- One canonical MD5 byte fingerprint per tenant-local group. Use MD5 for ALL
-- media, including generated assets, because Drive supplies MD5 natively;
-- mixing MD5 and SHA256 keys would let identical bytes evade one-use checks.
-- A URL, Drive ID, R2 key or pHash is not a byte fingerprint. Manual same-scene
-- links still require a global component integration before activation.
create table if not exists public.visual_global_identity (
  tenant_id text not null,
  group_key text not null,
  fingerprint text not null check (
    fingerprint ~ '^md5:[0-9a-f]{32}$'),
  evidence jsonb not null check (jsonb_typeof(evidence) = 'object' and evidence <> '{}'::jsonb),
  verified_by text not null check (btrim(verified_by) <> ''),
  verified_at timestamptz not null default now(),
  primary key (tenant_id, group_key),
  foreign key (tenant_id, group_key) references public.visual_group(gym_id, group_key)
);

-- One fingerprint has exactly one tenant/date owner globally. A published
-- owner is permanent, including when all calendar rows later disappear.
create table if not exists public.visual_global_usage (
  fingerprint text primary key,
  tenant_id text not null,
  used_date date,
  state text not null check (state in ('reserved','published','released')),
  ambiguous boolean not null default false,
  first_seen_at timestamptz not null default now(),
  published_at timestamptz,
  check (used_date is not null or state = 'published'),
  check (fingerprint ~ '^md5:[0-9a-f]{32}$')
);
create table if not exists public.visual_global_usage_member (
  tenant_id text not null,
  group_key text not null,
  fingerprint text not null references public.visual_global_usage(fingerprint),
  calendar_row_id uuid,
  channel text,
  used_date date,
  state text not null check (state in ('reserved','published','released')),
  ambiguous boolean not null default false,
  recorded_at timestamptz not null default now(),
  primary key (tenant_id, group_key),
  foreign key (tenant_id, group_key) references public.visual_global_identity(tenant_id, group_key)
);
create index if not exists visual_global_usage_member_fingerprint_idx
  on public.visual_global_usage_member(fingerprint);
-- Legacy release receipts remain inspectable. A staged reservation is never
-- reusable; new releases do not write this table.
create table if not exists public.visual_global_release_history (
  id bigint generated always as identity primary key,
  fingerprint text not null,
  tenant_id text not null,
  group_key text not null,
  used_date date not null,
  calendar_row_id uuid,
  released_at timestamptz not null default now()
);

-- These are owner-only writes. The service role can inspect receipts but must
-- use the validated SECURITY DEFINER functions to bind and import history.
alter table public.visual_global_identity enable row level security;
alter table public.visual_global_usage enable row level security;
alter table public.visual_global_usage_member enable row level security;
alter table public.visual_global_release_history enable row level security;
revoke all on public.visual_global_identity, public.visual_global_usage,
  public.visual_global_usage_member, public.visual_global_release_history from public, anon, authenticated, service_role;
grant select on public.visual_global_identity, public.visual_global_usage,
  public.visual_global_usage_member, public.visual_global_release_history to service_role;

create or replace function public.visual_global_immutable()
returns trigger language plpgsql set search_path = public as $$
begin
  if tg_op = 'DELETE' or to_jsonb(new) is distinct from to_jsonb(old) then
    raise exception 'global visual identity/history is immutable' using errcode = '23514';
  end if;
  return new;
end;
$$;
drop trigger if exists visual_global_identity_immutable on public.visual_global_identity;
create trigger visual_global_identity_immutable before update or delete
  on public.visual_global_identity for each row execute function public.visual_global_immutable();
drop trigger if exists visual_global_release_immutable on public.visual_global_release_history;
create trigger visual_global_release_immutable before update or delete
  on public.visual_global_release_history for each row execute function public.visual_global_immutable();
create or replace function public.visual_global_member_guard()
returns trigger language plpgsql set search_path = public as $$
begin
  if tg_op='DELETE' or old.state='published' then
    raise exception 'published global member is permanent' using errcode='23514';
  end if;
  if new.tenant_id<>old.tenant_id or new.group_key<>old.group_key
      or new.fingerprint<>old.fingerprint or
      (old.ambiguous and not new.ambiguous) or
      (old.state='reserved' and new.state not in ('reserved','published','released')) then
    raise exception 'global member identity or uncertainty cannot change' using errcode='23514';
  end if;
  return new;
end;
$$;
drop trigger if exists visual_global_member_guard on public.visual_global_usage_member;
create trigger visual_global_member_guard before update or delete
  on public.visual_global_usage_member for each row execute function public.visual_global_member_guard();
create or replace function public.visual_global_usage_guard()
returns trigger language plpgsql set search_path = public as $$
begin
  if tg_op='DELETE' or old.state='published' then
    raise exception 'published global usage is permanent' using errcode='23514';
  end if;
  if new.fingerprint<>old.fingerprint
      or new.tenant_id<>old.tenant_id or new.used_date is distinct from old.used_date
      or old.state='released' or new.state='released'
      or new.first_seen_at is distinct from old.first_seen_at
      or (old.ambiguous and not new.ambiguous)
      or (old.state='reserved' and new.state not in ('reserved','published')) then
    raise exception 'global claim owner, date and uncertainty are immutable' using errcode='23514';
  end if;
  return new;
end;
$$;
drop trigger if exists visual_global_usage_guard on public.visual_global_usage;
create trigger visual_global_usage_guard before update or delete
  on public.visual_global_usage for each row execute function public.visual_global_usage_guard();

-- Registration never guesses a fingerprint from URLs. A Drive asset may
-- attest its stored MD5 only when its raw key resolves to this exact tenant.
-- Other sources require explicit human evidence with an actor and source.
create or replace function public.visual_global_register_identity(
  p_gym_key text, p_group_key text, p_fingerprint text,
  p_evidence jsonb, p_actor text, p_asset_id text default null
) returns text language plpgsql security definer set search_path = public as $$
declare v_tenant uuid; v_existing text; v_asset_hash text;
begin
  v_tenant := public.visual_group_tenant_strict(p_gym_key);
  p_fingerprint := lower(btrim(p_fingerprint));
  if p_fingerprint !~ '^md5:[0-9a-f]{32}$'
     or nullif(btrim(p_group_key),'') is null
     or nullif(btrim(p_actor),'') is null
     or p_evidence is null or jsonb_typeof(p_evidence) <> 'object'
     or p_evidence = '{}'::jsonb then
    raise exception 'attested byte fingerprint, group, actor and evidence required' using errcode='22023';
  end if;
  if not exists(select 1 from public.visual_group
      where gym_id=v_tenant::text and group_key=p_group_key) then
    raise exception 'group does not belong to canonical tenant' using errcode='23503';
  end if;
  if nullif(btrim(p_asset_id),'') is not null then
    if not exists(select 1 from public.visual_group_alias a
        where a.gym_id=v_tenant::text and a.group_key=p_group_key
          and a.alias_kind='source_asset' and a.alias_value=btrim(p_asset_id)) then
      raise exception 'asset is not a member of the visual group' using errcode='23514';
    end if;
    select lower(btrim(a.content_hash)) into v_asset_hash
      from public.media_asset a where a.id=p_asset_id
        and public.visual_group_tenant_id(a.gym_id)=v_tenant;
    if v_asset_hash is null or p_fingerprint <> 'md5:' || v_asset_hash then
      raise exception 'asset hash or tenant does not match fingerprint' using errcode='23514';
    end if;
  elsif nullif(btrim(p_evidence->>'source'),'') is null
      or nullif(btrim(p_evidence->>'verified_bytes'),'') is null then
    raise exception 'non-Drive fingerprint requires source and verified_bytes evidence' using errcode='23514';
  end if;
  -- Serialize with calendar claims and historical imports through the group
  -- row. An existing group binding cannot be re-pointed to a different hash.
  perform 1 from public.visual_group where gym_id=v_tenant::text
    and group_key=p_group_key for update;
  select fingerprint into v_existing from public.visual_global_identity
    where tenant_id=v_tenant::text and group_key=p_group_key;
  if v_existing is not null then
    if v_existing <> p_fingerprint then
      raise exception 'group fingerprint is already bound' using errcode='23514';
    end if;
    return v_existing;
  end if;
  insert into public.visual_global_identity
    (tenant_id,group_key,fingerprint,evidence,verified_by)
    values(v_tenant::text,p_group_key,p_fingerprint,p_evidence,btrim(p_actor));
  return p_fingerprint;
end;
$$;

-- A group key is scene identity, not byte identity. A calendar claim needs a
-- digest for its selected media, and every known byte-bearing member of the
-- group must agree with the attested global fingerprint. Unknown or SHA-only
-- members cannot be proven equal to the canonical MD5 and fail closed.
create or replace function public.visual_global_row_fingerprint(p_row public.content_calendar)
returns text language plpgsql stable security definer set search_path = public as $$
declare v_tenant uuid; v_asset_hash text; v_row_hash text;
begin
  v_tenant := public.visual_group_tenant_id(p_row.gym_id);
  if v_tenant is null then return null; end if;
  if nullif(btrim(to_jsonb(p_row)->>'source_media_asset_id'),'') is not null then
    select 'md5:'||lower(btrim(a.content_hash)) into v_asset_hash
      from public.media_asset a where a.id=to_jsonb(p_row)->>'source_media_asset_id'
        and public.visual_group_tenant_id(a.gym_id)=v_tenant
        and lower(btrim(a.content_hash)) ~ '^[0-9a-f]{32}$';
    if v_asset_hash is null then return null; end if;
  end if;
  if nullif(btrim(to_jsonb(p_row)->>'byte_hash'),'') is not null then
    if lower(btrim(to_jsonb(p_row)->>'byte_hash')) !~ '^(source|derived):md5:[0-9a-f]{32}$' then
      return null;
    end if;
    if split_part(lower(btrim(to_jsonb(p_row)->>'byte_hash')),':',1)='source'
       and nullif(btrim(to_jsonb(p_row)->>'source_media_url'),'') is distinct from
           nullif(btrim(p_row.image_url),'') then
      return null;
    end if;
    v_row_hash := split_part(lower(btrim(to_jsonb(p_row)->>'byte_hash')),':',2)||':'||
      split_part(lower(btrim(to_jsonb(p_row)->>'byte_hash')),':',3);
  end if;
  if v_asset_hash is not null and v_row_hash is not null and v_asset_hash<>v_row_hash then
    return null;
  end if;
  -- A source asset digest is the delivered digest only when the calendar
  -- explicitly names the same source URL. Renders/renditions need their own
  -- derived byte digest; a lineage link alone cannot attest delivered bytes.
  if v_row_hash is null and (v_asset_hash is null or
      nullif(btrim(to_jsonb(p_row)->>'source_media_url'),'') is distinct from
      nullif(btrim(p_row.image_url),'')) then
    return null;
  end if;
  return coalesce(v_row_hash,v_asset_hash);
end;
$$;

create or replace function public.visual_global_group_bytes_verified(
  p_tenant text,p_group text,p_fingerprint text
) returns boolean language sql stable security definer set search_path = public as $$
  select p_fingerprint ~ '^md5:[0-9a-f]{32}$'
    -- Multiple delivered URLs can be byte-distinct even when the group is
    -- visually equivalent. Until each URL has a separate byte attestation,
    -- reject the whole group instead of letting one MD5 bless every alias.
    and (select count(*) from public.visual_group_alias a
      where a.gym_id=p_tenant and a.group_key=p_group
        and a.alias_kind='canonical_url') <= 1
    and not exists(select 1 from public.visual_group_alias a
      where a.gym_id=p_tenant and a.group_key=p_group and a.alias_kind='byte_hash'
        and a.alias_value not in ('source:'||p_fingerprint,'derived:'||p_fingerprint))
    and not exists(select 1 from public.visual_group_alias a
      left join public.media_asset m on m.id=a.alias_value
        and public.visual_group_tenant_id(m.gym_id)::text=p_tenant
      where a.gym_id=p_tenant and a.group_key=p_group and a.alias_kind='source_asset'
        and (m.id is null or lower(btrim(m.content_hash)) is distinct from
          split_part(p_fingerprint,':',2)));
$$;

-- Atomic cross-client claim. The fingerprint PK serializes first use, even
-- if two tenant-local groups are created concurrently. Same-day siblings are
-- permitted only for the SAME canonical tenant; another tenant always fails.
-- Unknown historical dates (NULL published date) block every future claim.
create or replace function public.visual_global_claim(
  p_gym_key text, p_group_key text, p_date date, p_row_id uuid,
  p_channel text, p_published boolean, p_ambiguous boolean default false,
  p_selected_fingerprint text default null
) returns text language plpgsql security definer set search_path = public as $$
declare v_tenant uuid; v_hash text; v_usage public.visual_global_usage%rowtype;
  v_member public.visual_global_usage_member%rowtype;
begin
  v_tenant := public.visual_group_tenant_strict(p_gym_key);
  if p_published is null or p_ambiguous is null or (p_date is null and not p_published) then
    raise exception 'claim needs a verified date or permanent unknown-date publication' using errcode='22023';
  end if;
  select fingerprint into v_hash from public.visual_global_identity
    where tenant_id=v_tenant::text and group_key=p_group_key;
  if v_hash is null then
    raise exception 'global fingerprint missing for visual group' using errcode='23514';
  end if;
  if not public.visual_global_group_bytes_verified(v_tenant::text,p_group_key,v_hash) then
    raise exception 'visual group has byte-distinct or unverifiable members' using errcode='23514';
  end if;
  if p_selected_fingerprint is not null and p_selected_fingerprint<>v_hash then
    raise exception 'selected calendar asset is not bound to claimed bytes' using errcode='23514';
  end if;
  -- The INSERT creates and locks the absent key; ON CONFLICT waits for the
  -- winner, then the row lock reads its committed owner/date.
  insert into public.visual_global_usage
    (fingerprint,tenant_id,used_date,state,ambiguous,published_at)
    values(v_hash,v_tenant::text,p_date,case when p_published then 'published' else 'reserved' end,
      p_ambiguous,case when p_published then now() end)
    on conflict (fingerprint) do nothing;
  select * into v_usage from public.visual_global_usage
    where fingerprint=v_hash for update;
  if v_usage.state='released' then
    raise exception 'legacy released global fingerprint requires historical repair' using errcode='23514';
  elsif v_usage.tenant_id <> v_tenant::text or v_usage.used_date is distinct from p_date then
    raise exception 'visual byte fingerprint already used by another client or date' using errcode='23514';
  end if;
  if p_published and v_usage.state='reserved' then
    update public.visual_global_usage set state='published',
      published_at=now(),ambiguous=ambiguous or p_ambiguous
      where fingerprint=v_hash;
  elsif p_ambiguous and not v_usage.ambiguous and v_usage.state='reserved' then
    update public.visual_global_usage set ambiguous=true where fingerprint=v_hash;
  end if;
  select * into v_member from public.visual_global_usage_member
    where tenant_id=v_tenant::text and group_key=p_group_key;
  if found and v_member.state='released' then
    raise exception 'legacy released global member requires historical repair' using errcode='23514';
  elsif found and (v_member.fingerprint <> v_hash or
      v_member.used_date is distinct from p_date) then
    raise exception 'visual group has conflicting historical membership' using errcode='23514';
  elsif found and p_published and v_member.state='reserved' then
    update public.visual_global_usage_member set state='published',
      ambiguous=ambiguous or p_ambiguous where tenant_id=v_tenant::text and group_key=p_group_key;
  elsif found and p_ambiguous and not v_member.ambiguous and v_member.state='reserved' then
    update public.visual_global_usage_member set ambiguous=true
      where tenant_id=v_tenant::text and group_key=p_group_key;
  end if;
  insert into public.visual_global_usage_member
    (tenant_id,group_key,fingerprint,calendar_row_id,channel,used_date,state,ambiguous)
    values(v_tenant::text,p_group_key,v_hash,p_row_id,p_channel,p_date,
      case when p_published then 'published' else 'reserved' end,p_ambiguous)
    on conflict (tenant_id,group_key) do nothing;
  return v_hash;
end;
$$;

-- Calendar row release changes active membership only. Once staged, the byte
-- claim remains occupied on its original tenant/date forever.
create or replace function public.visual_global_release(
  p_gym_key text,p_group_key text
) returns boolean language plpgsql security definer set search_path = public as $$
declare v_tenant uuid; v_hash text; v_usage public.visual_global_usage%rowtype;
  v_member public.visual_global_usage_member%rowtype;
begin
  v_tenant:=public.visual_group_tenant_strict(p_gym_key);
  select fingerprint into v_hash from public.visual_global_identity
    where tenant_id=v_tenant::text and group_key=p_group_key;
  if v_hash is null then raise exception 'global identity missing' using errcode='23514'; end if;
  select * into v_usage from public.visual_global_usage where fingerprint=v_hash for update;
  select * into v_member from public.visual_global_usage_member
    where tenant_id=v_tenant::text and group_key=p_group_key for update;
  if not found or v_usage.fingerprint is null or v_member.state='released' or v_usage.state='released' then
    raise exception 'global staged history is missing or legacy released' using errcode='23514';
  end if;
  if v_member.state='published' or v_member.ambiguous or v_usage.ambiguous
      or v_usage.tenant_id<>v_tenant::text
      or v_usage.used_date is distinct from v_member.used_date or not exists(
        select 1 from public.visual_group_usage_ledger l
        where l.gym_id=v_tenant::text and l.group_key=p_group_key
          and l.state='released' and not l.ambiguous)
      or exists(select 1 from public.visual_group_usage_sibling s
        where s.gym_id=v_tenant::text and s.group_key=p_group_key
          and s.state='active') then
    raise exception 'global reservation still occupied or uncertain' using errcode='23514';
  end if;
  -- Keep both owner and member occupied. This also preserves same-date reuse
  -- for a legitimate sibling while rejecting every cross-date/client claim.
  return true;
end;
$$;

-- Backfill from the tenant-local ledger, including orphaned published groups
-- whose calendar row was deleted. The caller should run in a transaction and
-- ROLLBACK for a dry run. A per-tenant migration/canary must use a global
-- content_calendar write barrier before this import and coverage re-read.
create or replace function public.visual_global_import_history()
returns jsonb language plpgsql security definer set search_path = public as $$
declare r record; v_count integer:=0; v_missing integer;
begin
  if current_setting('transaction_isolation') <> 'read committed' then
    raise exception 'global history import requires READ COMMITTED' using errcode='25006';
  end if;
  -- Direct owner invocation takes the same table-wide barrier as activation.
  -- Refuse a prior advisory/calendar writer lock that could invert lock order.
  if exists(select 1 from pg_locks where pid=pg_backend_pid() and granted and
      (locktype='advisory' or (locktype='relation'
        and relation='public.content_calendar'::regclass and mode<>'AccessShareLock')))
      and not exists(select 1 from pg_locks where pid=pg_backend_pid() and granted
        and locktype='relation' and relation='public.content_calendar'::regclass
        and mode='ShareRowExclusiveLock') then
    raise exception 'global history import requires calendar barrier before writer locks'
      using errcode='55P03';
  end if;
  lock table public.content_calendar in share row exclusive mode;
  lock table public.visual_group_usage_ledger, public.visual_group_alias,
    public.visual_global_identity, public.visual_group_scene_link,
    public.visual_group_usage_sibling, public.visual_group_member_event,
    public.visual_group_reconciliation, public.tenant_alias
    in share row exclusive mode nowait;
  if exists(select 1 from public.visual_global_coverage() c where c.issue<>'ready') then
    raise exception 'global history import refused: calendar coverage incomplete' using errcode='23514';
  end if;
  -- Every recorded publication must still have a permanent local ledger row.
  -- A deleted calendar row is not grounds for silently discarding this event.
  if exists(select 1 from public.visual_group_reconciliation e
      where e.outcome='confirmed_published'
        and not exists(select 1 from public.visual_group_usage_ledger l
          where l.gym_id=e.gym_id and l.group_key=e.delivered_group_key
            and l.state='published')) then
    raise exception 'global history import refused: published event lacks ledger owner'
      using errcode='23514';
  end if;
  select count(*) into v_missing from public.visual_group_usage_ledger l
    left join public.visual_global_identity i
      on i.tenant_id=l.gym_id and i.group_key=l.group_key
    where i.fingerprint is null;
  if v_missing>0 then
    raise exception 'global history import refused: % occupied groups lack verified byte fingerprints',v_missing
      using errcode='23514';
  end if;
  if exists(select 1 from public.visual_group_usage_ledger l
      where (
        select count(distinct i.fingerprint)
        from public.visual_group_scene_members(l.gym_id,l.group_key) sm(group_key)
        join public.visual_global_identity i
          on i.tenant_id=l.gym_id and i.group_key=sm.group_key)>1) then
    raise exception 'global history import refused: linked scene has distinct byte fingerprints without component authority'
      using errcode='23514';
  end if;
  -- Published first means an existing permanent owner wins over a competing
  -- reservation; either order still refuses a cross-client/date collision.
  for r in select l.* from public.visual_group_usage_ledger l
      order by (l.state='published') desc,l.gym_id,l.group_key loop
    perform public.visual_global_claim(r.gym_id,r.group_key,r.reserved_date,
      r.calendar_row_id,r.channel,r.state='published',r.ambiguous);
    v_count:=v_count+1;
  end loop;
  return jsonb_build_object('imported_local_groups',v_count,
    'global_fingerprints',(select count(*) from public.visual_global_usage));
end;
$$;

-- Read-only coverage before importing any history. Every published/active/
-- ambiguous calendar row must have a resolved tenant-local group and an
-- attested fingerprint. A published row with no post_date remains an explicit
-- unknown-date blocker rather than borrowing its UTC published_at date.
create or replace function public.visual_global_coverage()
returns table(calendar_row_id uuid, raw_gym_key text, tenant_id uuid,
  group_key text, fingerprint text, status text, post_date date, issue text)
language sql stable security definer set search_path = public as $$
  select c.id,c.gym_id,public.visual_group_tenant_id(c.gym_id),
    c.visual_group_key,i.fingerprint,c.status,c.post_date,
    case
      when public.visual_group_tenant_id(c.gym_id) is null then 'unmapped_tenant'
      when c.visual_group_key is null then 'unresolved_group'
      when i.fingerprint is null then 'missing_verified_fingerprint'
      when not public.visual_global_group_bytes_verified(
          public.visual_group_tenant_id(c.gym_id)::text,c.visual_group_key,i.fingerprint)
        then 'byte_distinct_or_unverified_group_members'
      when public.visual_global_row_fingerprint(c) is distinct from i.fingerprint
        then 'selected_media_bytes_unverified'
      when (c.status='published' or c.published_at is not null) and c.post_date is null
        then 'published_date_unverified'
      when not exists(select 1 from public.visual_group_usage_ledger l
          where l.gym_id=public.visual_group_tenant_id(c.gym_id)::text
            and l.group_key=c.visual_group_key and l.state<>'released'
            and l.reserved_date is not distinct from c.post_date)
        then 'missing_matching_local_ledger'
      when (c.status='published' or c.published_at is not null) and
          not exists(select 1 from public.visual_group_usage_ledger l
          where l.gym_id=public.visual_group_tenant_id(c.gym_id)::text
            and l.group_key=c.visual_group_key and l.state='published')
        then 'published_row_without_permanent_local_ledger'
      else 'ready' end
  from public.content_calendar c
  left join public.visual_global_identity i
    on i.tenant_id=public.visual_group_tenant_id(c.gym_id)::text
      and i.group_key=c.visual_group_key
  where c.status='published' or c.published_at is not null
    or public.visual_group_row_active(c) or public.visual_group_row_ambiguous(c);
$$;

-- The calendar alone cannot reveal a deleted published row. Report every
-- occupied local-ledger group too, including its potentially missing hash.
create or replace function public.visual_global_history_coverage()
returns table(tenant_id text, group_key text, fingerprint text,
  used_date date, local_state text, issue text)
language sql stable security definer set search_path = public as $$
  select l.gym_id,l.group_key,i.fingerprint,l.reserved_date,l.state,
    case when i.fingerprint is null then 'missing_verified_fingerprint'
      when not public.visual_global_group_bytes_verified(l.gym_id,l.group_key,i.fingerprint)
        then 'byte_distinct_or_unverified_group_members'
      when g.fingerprint is not null and
        (g.tenant_id<>l.gym_id or g.used_date is distinct from l.reserved_date)
        then 'global_owner_or_date_conflict'
      when (select count(distinct ci.fingerprint)
        from public.visual_group_scene_members(l.gym_id,l.group_key) sm(group_key)
        left join public.visual_global_identity ci
          on ci.tenant_id=l.gym_id and ci.group_key=sm.group_key)>1
        then 'scene_component_has_distinct_bytes_without_global_authority'
      when g.fingerprint is null then 'not_imported'
      when m.fingerprint is distinct from i.fingerprint
        or m.used_date is distinct from l.reserved_date
        or m.state is distinct from case when l.state='published' then 'published' else 'reserved' end
        then 'global_member_state_mismatch'
      else 'ready' end
  from public.visual_group_usage_ledger l
  left join public.visual_global_identity i
    on i.tenant_id=l.gym_id and i.group_key=l.group_key
  left join public.visual_global_usage g on g.fingerprint=i.fingerprint
  left join public.visual_global_usage_member m
    on m.tenant_id=l.gym_id and m.group_key=l.group_key
  ;
$$;

-- The separate DRAFT activation migration adds the all-tenant calendar-write
-- barrier, historical import, coverage re-read, and atomic arm. This file
-- keeps tenant-local activation blocked until that migration is applied.
create or replace function public.visual_global_block_local_activation()
returns trigger language plpgsql set search_path = public as $$
begin
  if new.enforce then
    raise exception 'global once-used activation is not integrated; tenant-local guard cannot arm'
      using errcode='23514';
  end if;
  return new;
end;
$$;
drop trigger if exists visual_global_block_local_activation
  on public.gym_visual_guard_settings;
create trigger visual_global_block_local_activation
  before insert or update on public.gym_visual_guard_settings
  for each row execute function public.visual_global_block_local_activation();

revoke all on function public.visual_global_register_identity(text,text,text,jsonb,text,text)
  from public,anon,authenticated;
revoke all on function public.visual_global_row_fingerprint(public.content_calendar)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_group_bytes_verified(text,text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_claim(text,text,date,uuid,text,boolean,boolean,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_release(text,text)
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_import_history()
  from public,anon,authenticated,service_role;
revoke all on function public.visual_global_coverage()
  from public,anon,authenticated;
revoke all on function public.visual_global_history_coverage()
  from public,anon,authenticated;
grant execute on function public.visual_global_register_identity(text,text,text,jsonb,text,text)
  to service_role;
grant execute on function public.visual_global_coverage() to service_role;
grant execute on function public.visual_global_history_coverage() to service_role;
commit;
